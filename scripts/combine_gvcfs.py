#!/usr/bin/env python

"""
Combine a set of gVCFs and output a MatrixTable and a HailTable with metadata
"""

import os
from typing import List, Sequence
import logging
import click
import hail as hl
from hail.experimental.vcf_combiner import vcf_combiner

from cpg_qc.utils import get_validation_callback, file_exists
from cpg_qc import utils
from cpg_qc import _version

logger = logging.getLogger('vcf_combiner')
logger.setLevel('INFO')

DEFAULT_REF = 'GRCh38'
MAX_MULTI_WRITE_NUMBER = 50
MAX_COMBINE_NUMBER = 100
# The target number of rows per partition during each round of merging
TARGET_RECORDS = 25_000


@click.command()
@click.version_option(_version.__version__)
@click.option(
    '--sample-map',
    'sample_map_csv_path',
    required=True,
    callback=get_validation_callback(ext='csv', must_exist=True),
    help='path to a per-sample data in a CSV file with '
    'a first line as a header. The only 2 required columns are `sample` '
    'and `gvcf`, in any order, possibly mixed with other columns.',
)
@click.option(
    '--out-mt',
    'out_mt_path',
    required=True,
    callback=get_validation_callback(ext='mt'),
    help='path to write the MatrixTable. Must have an .mt extention. '
    'Can be a Google Storage URL (i.e. start with `gs://`). '
    'An accompanying file with a `.metadata.ht` suffix will ne written '
    'at the same folder or bucket location, containing the same columns '
    'as the input sample map. This file is needed for further incremental '
    'extending of the matrix table using new GVCFs.',
)
@click.option(
    '--existing-mt',
    'existing_mt_path',
    callback=get_validation_callback(ext='mt', must_exist=True),
    help='optional path to an existing MatrixTable. Must have an .mt '
    'extention. Can be a Google Storage URL (i.e. start with `gs://`). '
    'If provided, will be read and used as a base to get extended with the '
    'samples in the input sample map. Can be read-only, as it will not '
    'be overwritten, instead the result will be written to the new location '
    'provided with --out-mt. An accompanying `.metadata.ht` file is expected '
    'to be present at the same folder or bucket location, containing the '
    'same set of samples, and the same columns as the input sample map',
)
@click.option(
    '--bucket',
    'work_bucket',
    required=True,
    help='path to folder for intermediate output. '
    'Can be a Google Storage URL (i.e. start with `gs://`).',
)
@click.option(
    '--local-tmp-dir',
    'local_tmp_dir',
    required=True,
    help='local directory for temporary files and Hail logs (must be local)',
)
@click.option(
    '--reuse',
    'reuse',
    is_flag=True,
    help='if an intermediate or a final file exists, reuse it instead of '
    'rerunning the code that generates it',
)
def main(
    sample_map_csv_path: str,
    out_mt_path: str,
    existing_mt_path: str,
    work_bucket: str,
    local_tmp_dir: str,
    reuse: bool,
):
    """
    Runs the Hail
    [vcf_combiner](https://hail.is/docs/0.2/experimental/vcf_combiner.html)
    using the GVCFs files specified in a `gvcf` column in the `sample_map_csv`
    CSV file as input, and generates a multi-sample Matrix Table in a sparse
    format, saved as `out_mt_path`. It also generates an accompanying table
    in an HT format with a `.metadata.ht` suffix, with the contents of the
    sample map, which can be used for incremental adding of new samples,
    as well as for running the QC.

    If `existing_mt_path` is provided, uses that matrix table as a base to
    extend with new samples. However, it will not overwrite `existing_mt_path`,
    and instead write the new table to `out_mt_path`. It would also combine
    the accompanying metadata HT tables and write the result with a
    `.metadata.ht` suffix.
    """
    utils.init_hail('combine_gvcfs', local_tmp_dir)

    logger.info(f'Combining new samples')
    new_metadata_ht = hl.import_table(sample_map_csv_path, delimiter=',', key='sample')
    new_mt_path = (
        os.path.join(work_bucket, 'new.mt') if existing_mt_path else out_mt_path
    )
    if reuse and file_exists(new_mt_path):
        logger.info(f'MatrixTable with new samples exists, reusing: {new_mt_path}')
    else:
        combine_gvcfs_standard(
            gvcf_paths=new_metadata_ht.gvcf.collect(),
            out_mt_path=new_mt_path,
            work_bucket=work_bucket,
            overwrite=True,
        )
        logger.info(
            f'Written {new_metadata_ht.count()} new '
            f'samples into a MatrixTable {out_mt_path}'
        )

    if existing_mt_path:
        _combine_with_the_existing_mt(
            existing_mt=hl.read_matrix_table(existing_mt_path),
            new_mt_path=new_mt_path,
            out_mt_path=out_mt_path,
        )
        existing_meta_ht_path = os.path.splitext(existing_mt_path)[0] + '.metadata.ht'
        existing_meta_ht = hl.read_table(existing_meta_ht_path)
        metadata_ht = existing_meta_ht.union(new_metadata_ht)
    else:
        metadata_ht = new_metadata_ht

    metadata_ht_path = os.path.splitext(out_mt_path)[0] + '.metadata.ht'
    if reuse and file_exists(metadata_ht_path):
        logger.info(f'Metadata table exists, reusing: {metadata_ht_path}')
    else:
        metadata_ht.write(metadata_ht_path, overwrite=True)
        logger.info(f'Written metadata table to {metadata_ht_path}')


def _combine_with_the_existing_mt(
    existing_mt: hl.MatrixTable,
    new_mt_path: str,  # passing as a path because we are going
    # to re-read it with different intervals
    out_mt_path: str,
):
    existing_mt = existing_mt.drop('gvcf_info')
    logger.info(
        f'Combining with the existing matrix table '
        f'({existing_mt.count_cols()} samples)'
    )
    intervals = vcf_combiner.calculate_new_intervals(
        hl.read_matrix_table(new_mt_path).rows(),
        n=TARGET_RECORDS,
        reference_genome=DEFAULT_REF,
    )
    new_mt = hl.read_matrix_table(new_mt_path, _intervals=intervals)
    new_mt = new_mt.drop('gvcf_info')
    out_mt = vcf_combiner.combine_gvcfs([existing_mt, new_mt])
    out_mt.write(out_mt_path, overwrite=True)


def chunks(seq, size):
    """
    iterate through a list size elements at a time
    """
    return (seq[pos : pos + size] for pos in range(0, len(seq), size))


def _combine_multiwrite_chunk(
    paths: Sequence[str],
    tmp_dirpath: str,
    intervals: List[hl.utils.Interval],
    chunk_i: int,
    overwrite: bool = False,
):
    """
    Inner part of stage-one combining, including transformation GVCFs
    to sparse MatrixTables and combining them in chunks, limited by
    `MAX_COMBINE_NUMBER`. The function assumes that the input data is
    already a chunk of a size reasonable for `hl.experimental.write_matrix_tables`
    to be called once, i.e. of size MAX_MULTI_WRITE_NUMBER * MAX_COMBINE_NUMBER
    """
    inputs_path_chunks = list(chunks(paths, MAX_COMBINE_NUMBER))

    tmp_i_prefix = os.path.join(tmp_dirpath, f'{chunk_i}') + '/'
    pad = len(str(len(inputs_path_chunks)))
    out_combined_mt_paths = [
        os.path.join(tmp_i_prefix, str(j).zfill(pad) + '.mt')
        for j in range(len(inputs_path_chunks))
    ]
    if not overwrite and all(utils.file_exists(path) for path in out_combined_mt_paths):
        return out_combined_mt_paths

    # Create a MatrixTable for each single GVCF
    single_mts: List[hl.MatrixTable] = hl.import_gvcfs(
        paths,
        intervals,
        array_elements_required=False,
    )
    # Tranform each MatrixTable into the sparse format
    sparse_single_mts = [vcf_combiner.transform_one(mt) for mt in single_mts]
    # Combine into milti-sample MTs of a `MAX_COMBINE_NUMBER` chunk size
    combined_mts = [
        vcf_combiner.combine_gvcfs(chunk_of_single_mts)
        for chunk_of_single_mts in chunks(sparse_single_mts, MAX_COMBINE_NUMBER)
    ]
    # Write all per-chunk MTs
    hl.experimental.write_matrix_tables(combined_mts, tmp_i_prefix, overwrite=True)
    return out_combined_mt_paths


def _combine_in_chunks(
    gvcf_paths: Sequence[str],
    tmp_dirpath: str,
    intervals: List[hl.utils.Interval],
    overwrite: bool = True,
) -> List[str]:
    """
    Stage one of the combiner, responsible for importing GVCFs, transforming
    them into sparse MatrixTables, chunking and combining the chunks into
    multi-sample MatrixTables, and writing out those intermediate tables
    to combine them further.

    Note that we chunk the input data twice. Larger chunks are bound by the
    number of matrix tables we write with a single command (_combine_multiwrite_chunk`
    calls `hl.experimental.write_matrix_tables` once) - and this number is
    `multiwrite_chunk_size = MAX_MULTI_WRITE_NUMBER * MAX_COMBINE_NUMBER`)
    Inside `_combine_multiwrite_chunk` though, the chunk is further
    split into smaller sub-chunks (of size `MAX_COMBINE_NUMBER`), for which
    `vcf_combiner.combine_gvcfs` is called once.
    """
    out_mt_paths = []

    multiwrite_chunk_size = MAX_MULTI_WRITE_NUMBER * MAX_COMBINE_NUMBER
    multiwrite_chunk_i = 0
    for pos in range(0, len(gvcf_paths), multiwrite_chunk_size):
        combined_mt_paths = _combine_multiwrite_chunk(
            gvcf_paths[pos : pos + multiwrite_chunk_size],
            tmp_dirpath,
            intervals,
            multiwrite_chunk_i,
            overwrite=overwrite,
        )
        out_mt_paths.extend(combined_mt_paths)
        multiwrite_chunk_i += 1
    return out_mt_paths


def combine_gvcfs_standard(
    gvcf_paths: List[str], out_mt_path: str, work_bucket: str, overwrite: bool = True
):
    """
    Combine a set of GVCFs in one go
    """
    hl.experimental.run_combiner(
        gvcf_paths,
        out_file=out_mt_path,
        reference_genome=utils.DEFAULT_REF,
        use_genome_default_intervals=True,
        tmp_path=os.path.join(work_bucket, 'tmp'),
        key_by_locus_and_alleles=True,
        overwrite=overwrite,
    )


def combine_gvcfs_in_chunks(
    gvcf_paths: List[str], out_mt_path: str, work_bucket: str, overwrite: bool = True
):
    """
    First round of combination: chunking the entire set of new GVCFs
    and merging chunks into small multi-sample MTs. For all chunks,
    the initial set of intervals is used - default genome intervals
    """
    intervals: List[
        hl.utils.Interval
    ] = vcf_combiner.calculate_even_genome_partitioning(
        reference_genome=DEFAULT_REF,
        interval_size=vcf_combiner.CombinerConfig.default_genome_interval_size,
    )
    tmp_bucket = os.path.join(work_bucket, 'combiner-tmp')
    logger.info('Combining chunks of GVCFs')
    small_chunks_mt_paths = _combine_in_chunks(
        gvcf_paths=gvcf_paths,
        tmp_dirpath=tmp_bucket,
        intervals=intervals,
        overwrite=overwrite,
    )

    logger.info(
        'Recalculating the intervals based on one of the '
        'smaller-chunk newly combined MTs'
    )
    intervals = vcf_combiner.calculate_new_intervals(
        hl.read_matrix_table(small_chunks_mt_paths[0]).rows(),
        n=TARGET_RECORDS,
        reference_genome=DEFAULT_REF,
    )
    # Reading smaller-chunk MTs
    small_chunks_mts = [
        hl.read_matrix_table(path, _intervals=intervals)
        for path in small_chunks_mt_paths
    ]

    logger.info('Next round of combining: merging into larger chunks')
    larger_chunk_mts = [
        vcf_combiner.combine_gvcfs(chunk_of_mts)
        for chunk_of_mts in chunks(small_chunks_mts, MAX_COMBINE_NUMBER)
    ]
    i = 0
    # If this round wasn't enough, start recursively applying this procedure,
    # moving to larger chunks:
    while len(larger_chunk_mts) > 1:
        logger.info(
            f'Now the number of chunks is {larger_chunk_mts}, '
            f'moving the next level of mering'
        )
        tmp_i_bucket = os.path.join(tmp_bucket, f'{i}')
        pad = len(str(len(larger_chunk_mts)))
        larger_chunk_mt_paths = [
            os.path.join(tmp_i_bucket, str(j).zfill(pad) + '.mt')
            for j in range(len(larger_chunk_mts))
        ]
        for (mt_path, mt) in zip(larger_chunk_mt_paths, larger_chunk_mts):
            if overwrite or not utils.file_exists(mt_path):
                mt.write(mt_path, overwrite=True)

        intervals = vcf_combiner.calculate_new_intervals(
            hl.read_matrix_table(larger_chunk_mt_paths[0]).rows(),
            n=TARGET_RECORDS,
            reference_genome=DEFAULT_REF,
        )
        # Reading with recalculated intervals
        with_recalculated_intervals_mts = [
            hl.read_matrix_table(path, _intervals=intervals)
            for path in larger_chunk_mt_paths
        ]
        even_larger_chunk_mts = [
            vcf_combiner.combine_gvcfs(chunk_of_mts)
            for chunk_of_mts in chunks(
                with_recalculated_intervals_mts, MAX_COMBINE_NUMBER
            )
        ]
        larger_chunk_mts = even_larger_chunk_mts
        i += 1
    larger_chunk_mts[0].write(out_mt_path, overwrite=True)
    return larger_chunk_mts[0]


if __name__ == '__main__':
    main()  # pylint: disable=E1120
