#!/usr/bin/env python

"""
Run sample QC on a MatrixTable, hard filter samples, add soft filter labels,
and output a sample-level Hail Table
"""

from os.path import join, splitext
from typing import Optional
import logging
import click
import hail as hl
import pandas as pd

from gnomad.resources.grch38 import telomeres_and_centromeres
from gnomad.sample_qc.filtering import compute_stratified_sample_qc
from gnomad.sample_qc.pipeline import annotate_sex
from gnomad.utils.annotations import bi_allelic_expr
from gnomad.utils.filtering import filter_to_autosomes
from gnomad.utils.filtering import add_filters_expr

from joint_calling.utils import file_exists, get_validation_callback
from joint_calling import hard_filtering, pop_strat_qc, utils, _version

logger = logging.getLogger('joint-calling')
logger.setLevel('INFO')


@click.command()
@click.version_option(_version.__version__)
@click.option(
    '--mt',
    'mt_path',
    required=True,
    callback=get_validation_callback(ext='mt', must_exist=True),
    help='path to the input Matrix Table. '
    'To generate it, run the `combine_gvcfs` script',
)
@click.option(
    '--meta-csv',
    'meta_csv_path',
    required=True,
    help='path to a CSV with QC and population metadata for the samples '
    'in the input Matrix Table. The following columns are expected: '
    's,population,gvcf,freemix,pct_chimeras,'
    'duplication,median_insert_size,mean_coverage. '
    'Must be keyed by "s". Samples with non-empty entries in '
    'the "population" column will be used to train the random forest '
    'for population inference of remaining samples. Other colums are '
    'used to apply QC hard filters to samples.',
)
@click.option(
    '--out-hardfiltered-samples-ht',
    'out_hardfiltered_samples_ht_path',
    required=True,
)
@click.option(
    '--out-meta-ht',
    'out_meta_ht_path',
    required=True,
)
@click.option(
    '--bucket',
    'work_bucket',
    required=True,
    help='path to write intermediate output and checkpoints. '
    'Can be a Google Storage URL (i.e. start with `gs://`).',
)
@click.option(
    '--local-tmp-dir',
    'local_tmp_dir',
    help='local directory for temporary files and Hail logs (must be local).',
)
@click.option(
    '--overwrite',
    'overwrite',
    is_flag=True,
    help='if an intermediate or a final file exists, skip running the code '
    'that generates it.',
)
@click.option(
    '--min-cov',
    'min_cov',
    default=18,
    help='minimum coverage for inclusion when computing hard filters.',
)
@click.option(
    '--kin-threshold',
    'kin_threshold',
    default=0.1,
    help='maximum kin threshold to be considered unrelated.',
)
@click.option(
    '--n-pcs', 'n_pcs', default=30, help='number of PCs to compute for ancestry PCA.'
)
@click.option(
    '--min-pop-prob',
    'min_pop_prob',
    default=0.9,
    help='minimum Random Forest probability for population assignment.',
)
@click.option(
    '--target-bed',
    'target_bed',
    help='for exomes, target regions in a BED file format.',
)
@click.option(
    '--hail-billing',
    'hail_billing',
    required=True,
    help='Hail billing account ID.',
)
def main(
    mt_path: str,
    meta_csv_path: str,
    out_hardfiltered_samples_ht_path: str,
    out_meta_ht_path: str,
    work_bucket: str,
    local_tmp_dir: str,
    overwrite: bool,
    min_cov: int,
    kin_threshold: float,
    n_pcs: int,
    min_pop_prob: float,
    target_bed: str,
    hail_billing: str,  # pylint: disable=unused-argument
):
    """
    Run sample QC on a MatrixTable, hard filter samples and add soft filter labels,
    output a sample-level Hail Table
    """
    utils.init_hail('sample_qc', local_tmp_dir)

    mt = hl.read_matrix_table(mt_path).key_rows_by('locus', 'alleles')
    df = pd.read_table(meta_csv_path)
    input_meta_ht = hl.Table.from_pandas(df)

    mt = _filter_callrate(mt, work_bucket, overwrite)

    # `hail_sample_qc_ht` row fields: sample_qc, bi_allelic_sample_qc
    hail_sample_qc_ht = _compute_hail_sample_qc(mt, work_bucket, overwrite)

    # `sex_ht` row fields: is_female, chr20_mean_dp, sex_karyotype
    sex_ht = _infer_sex(
        mt,
        work_bucket,
        overwrite,
        target_regions=hl.import_bed(target_bed) if target_bed else None,
    )

    # `hard_filtered_samples_ht` row fields: hard_filters;
    # also includes only failed samples
    hard_filtered_samples_ht = hard_filtering.compute_hard_filters(
        mt,
        input_meta_ht,
        sex_ht,
        hail_sample_qc_ht,
        out_ht_path=out_hardfiltered_samples_ht_path,
        cov_threshold=min_cov,
        overwrite=overwrite,
    )

    # Subset the matrix table to the variants suitable for PCA
    # (for both relateness and population analysis)
    for_pca_mt = pop_strat_qc.make_mt_for_pca(mt, work_bucket, overwrite)

    relatedness_ht = pop_strat_qc.compute_relatedness(
        for_pca_mt, work_bucket, overwrite
    )

    # We don't want to include related samples into the
    # ancestry PCA analysis
    pca_related_samples_to_drop_ht = pop_strat_qc.flag_related_samples(
        hard_filtered_samples_ht=hard_filtered_samples_ht,
        sex_ht=sex_ht,
        relatedness_ht=relatedness_ht,
        regressed_metrics_ht=None,
        work_bucket=work_bucket,
        kin_threshold=kin_threshold,
        overwrite=overwrite,
    )

    pop_pca_scores_ht = pop_strat_qc.run_pca_ancestry_analysis(
        for_pca_mt=for_pca_mt,
        sample_to_drop_ht=pca_related_samples_to_drop_ht,
        work_bucket=work_bucket,
        n_pcs=n_pcs,
        overwrite=overwrite,
    )

    # Using calculated PCA scores as well as training samples with known
    # `population` tag, to assign population tags to remaining samples
    pop_ht = pop_strat_qc.assign_pops(
        pop_pca_scores_ht,
        input_meta_ht,
        work_bucket=work_bucket,
        min_prob=min_pop_prob,
        overwrite=overwrite,
    )

    # Re-computing QC metrics per population and annotating failing samples
    regressed_metrics_ht = pop_strat_qc.apply_regressed_filters(
        hail_sample_qc_ht,
        pop_pca_scores_ht,
        work_bucket=work_bucket,
        overwrite=overwrite,
    )

    # Re-calculating the maximum set of unrelated samples now that
    # we have metrics adjusted for population, so newly QC-failed samples
    # are excluded
    final_related_samples_to_drop_ht = pop_strat_qc.flag_related_samples(  # pylint
        hard_filtered_samples_ht,
        sex_ht,
        relatedness_ht,
        regressed_metrics_ht,
        work_bucket=work_bucket,
        kin_threshold=kin_threshold,
        overwrite=overwrite,
    )

    # Combine all intermediate tables together
    _generate_metadata(
        sample_qc_ht=hail_sample_qc_ht,
        sex_ht=sex_ht,
        hard_filtered_samples_ht=hard_filtered_samples_ht,
        regressed_metrics_ht=regressed_metrics_ht,
        pop_ht=pop_ht,
        all_related_samples_to_drop_ht=pca_related_samples_to_drop_ht,
        final_related_samples_to_drop_ht=final_related_samples_to_drop_ht,
        out_ht_path=out_meta_ht_path,
        overwrite=overwrite,
    )


def _filter_callrate(
    mt: hl.MatrixTable,
    work_bucket: str,
    overwrite: bool = False,
) -> hl.MatrixTable:
    """
    Filter non-common variants, and annotate the sample callrate
    :param mt: input matrix table
    :param work_bucket: bucket path to write checkpoints
    :param overwrite: overwrite checkpoints if they exist
    :return: a new MatrixTable with a callrate column field
    """
    logger.info('Sample QC')
    out_mt_path = join(work_bucket, 'high_callrate_common_biallelic_snps.mt')
    if not overwrite and file_exists(out_mt_path):
        return hl.read_matrix_table(out_mt_path)

    logger.info('Filtering to bi-allelic, high-callrate, common SNPs for sample QC...')
    mt = mt.filter_rows(
        (hl.len(mt.alleles) == 2)
        & hl.is_snp(mt.alleles[0], mt.alleles[1])
        & (hl.agg.mean(mt.LGT.n_alt_alleles()) / 2 > 0.001)
        & (hl.agg.fraction(hl.is_defined(mt.LGT)) > 0.99)
    )
    mt = mt.annotate_cols(
        callrate=hl.agg.fraction(hl.is_defined(mt.LGT))
    ).naive_coalesce(5000)
    mt.write(out_mt_path, overwrite=overwrite)
    return mt


def _compute_hail_sample_qc(
    mt: hl.MatrixTable,
    work_bucket: str,
    overwrite: bool = False,
) -> hl.Table:
    """
    Runs Hail hl.sample_qc() to generate a Hail Table with
    per-sample QC metrics.
    :param mt: input MatrixTable
    :param work_bucket: bucket path to write checkpoints
    :param overwrite: overwrite checkpoints if they exist
    :return: a Hail Table with the following row fields:
        'sample_qc': {
            <output of hl.sample_qc() (n_filtered, n_hom_ref, etc)>
        }
        'bi_allelic_sample_qc': {
            <output of hl.sample_qc() for bi-allelic variants only>
        }
        'multi_allelic_sample_qc': {
            <output of hl.sample_qc() for multi-allelic variants only>
        }
    """
    logger.info('Sample QC')
    out_ht_path = join(work_bucket, 'hail_sample_qc.ht')
    if not overwrite and file_exists(out_ht_path):
        logger.info(f'{out_ht_path} exists, reusing')
        return hl.read_table(out_ht_path)

    mt = filter_to_autosomes(mt)
    mt = mt.filter_rows(
        ~hl.is_defined(telomeres_and_centromeres.ht()[mt.locus])
        & (hl.len(mt.alleles) > 1)
    )
    mt = mt.select_entries(GT=mt.LGT)

    regular_sample_qc_ht = hl.sample_qc(mt).cols()
    regular_sample_qc_ht.write(out_ht_path.replace('.ht', '.regular.ht'))

    sample_qc_ht = compute_stratified_sample_qc(
        mt,
        strata={
            'bi_allelic': bi_allelic_expr(mt),
            'multi_allelic': ~bi_allelic_expr(mt),
        },
        tmp_ht_prefix=None,
    )

    # Remove annotations that cannot be computed from the sparse format
    sample_qc_ht = sample_qc_ht.annotate(
        **{
            x: sample_qc_ht[x].drop('n_called', 'n_not_called', 'call_rate')
            for x in sample_qc_ht.row_value
        }
    )
    sample_qc_ht = sample_qc_ht.repartition(100)
    sample_qc_ht.write(out_ht_path, overwrite=True)
    return sample_qc_ht


def _infer_sex(
    mt: hl.MatrixTable,
    work_bucket: str,
    overwrite: bool = False,
    target_regions: Optional[hl.Table] = None,
) -> hl.Table:
    """
    Runs gnomad.sample_qc.annotate_sex() to infer sex
    :param mt: input MatrixTable
    :param work_bucket: bucket to write checkpoints
    :param overwrite: overwrite checkpoints if they exist
    :param target_regions: if exomes, it should correspond to the regions in the panel
    :return: a Hail Table with the following row fields:
        'is_female': bool
        'f_stat': float64
        'n_called': int64
        'expected_homs': float64
        'observed_homs': int64
        'chr20_mean_dp': float32
        'chrX_mean_dp': float32
        'chrY_mean_dp': float32
        'chrX_ploidy': float32
        'chrY_ploidy': float32
        'X_karyotype': str
        'Y_karyotype': str
        'sex_karyotype': str
    """
    logger.info('Inferring sex')
    out_ht_path = join(work_bucket, 'sex.ht')
    if not overwrite and file_exists(out_ht_path):
        return hl.read_table(out_ht_path)

    ht = annotate_sex(
        mt,
        excluded_intervals=telomeres_and_centromeres.ht(),
        included_intervals=target_regions,
        gt_expr='LGT',
    )

    ht.write(out_ht_path, overwrite=True)
    return ht


def _generate_metadata(
    sample_qc_ht: hl.Table,
    sex_ht: hl.Table,
    hard_filtered_samples_ht: hl.Table,
    regressed_metrics_ht: hl.Table,
    pop_ht: hl.Table,
    all_related_samples_to_drop_ht: hl.Table,
    final_related_samples_to_drop_ht: hl.Table,
    out_ht_path: str,
    overwrite: bool = False,
) -> hl.Table:
    """
    Combine all intermediate tables into a single metadata table

    :param sample_qc_ht: table with a `bi_allelic_sample_qc` row field
    :param sex_ht: table with the follwing row fields:
        `f_stat`, `n_called`, `expected_homs`, `observed_homs`
    :param hard_filtered_samples_ht: table with a `hard_filters` field
    :param regressed_metrics_ht: table with global fields
        `lms`, `qc_metrics_stats`;
        and metric row fields `n_snp_residual`, `r_ti_tv_residual`, etc;
        as well as corresponding `fail_n_snp_residual`, etc,
        and a `qc_metrics_filters` row field.
    :param pop_ht: table with the following row fields:
        `pop`, `prob_CEU`, `pca_scores`, `training_pop`.
    :param all_related_samples_to_drop_ht:
        table with related samples
    :param final_related_samples_to_drop_ht:
        table with related samples after re-ranking them based
        on population-stratified QC
    :param work_bucket: bucket to write checkpoints
    :param overwrite: overwrite checkpoints if they exist
    :return: table with relevant fields from input tables,
        annotated with the following row fields:
            'release_related': bool = in `final_related_samples_to_drop_ht`
            'all_samples_related': bool =
                in `intermediate_related_samples_to_drop_ht`
            'high_quality': bool =
                not `hard_filters_ht.hard_filters` &
                not `regressed_metrics_ht.qc_metrics_filters`
            'release': bool = high_quality & not `release_related`
    """
    logger.info('Generate the metadata HT and TSV files')

    if not overwrite and file_exists(out_ht_path):
        meta_ht = hl.read_table(out_ht_path)
    else:
        meta_ht = sex_ht.transmute(
            impute_sex_stats=hl.struct(
                f_stat=sex_ht.f_stat,
                n_called=sex_ht.n_called,
                expected_homs=sex_ht.expected_homs,
                observed_homs=sex_ht.observed_homs,
            )
        )

        meta_ht = meta_ht.annotate_globals(**regressed_metrics_ht.index_globals())

        meta_ht = meta_ht.annotate(
            sample_qc=sample_qc_ht[meta_ht.key].bi_allelic_sample_qc,
            **hard_filtered_samples_ht[meta_ht.key],
            **regressed_metrics_ht[meta_ht.key],
            **pop_ht[meta_ht.key],
            release_related=hl.is_defined(
                final_related_samples_to_drop_ht[meta_ht.key]
            ),
            all_samples_related=hl.is_defined(
                all_related_samples_to_drop_ht[meta_ht.key]
            ),
        )

        meta_ht = meta_ht.annotate(
            hard_filters=hl.or_else(meta_ht.hard_filters, hl.empty_set(hl.tstr)),
            sample_filters=add_filters_expr(
                filters={'related': meta_ht.release_related},
                current_filters=meta_ht.hard_filters.union(meta_ht.qc_metrics_filters),
            ),
        )

        meta_ht = meta_ht.annotate(
            high_quality=(hl.len(meta_ht.hard_filters) == 0)
            & (hl.len(meta_ht.qc_metrics_filters) == 0),
            release=hl.len(meta_ht.sample_filters) == 0,
        )
        meta_ht.write(out_ht_path, overwrite=True)

    if overwrite or not file_exists(splitext(out_ht_path)[0] + '.tsv'):
        n_pcs = meta_ht.aggregate(hl.agg.min(hl.len(meta_ht.pca_scores)))
        meta_ht = meta_ht.transmute(
            **{f'PC{i + 1}': meta_ht.pca_scores[i] for i in range(n_pcs)},
            hard_filters=hl.or_missing(
                hl.len(meta_ht.hard_filters) > 0, hl.delimit(meta_ht.hard_filters)
            ),
            qc_metrics_filters=hl.or_missing(
                hl.len(meta_ht.qc_metrics_filters) > 0,
                hl.delimit(meta_ht.qc_metrics_filters),
            ),
        )
        meta_ht.flatten().export(splitext(out_ht_path)[0] + '.tsv')

    return meta_ht


if __name__ == '__main__':
    main()  # pylint: disable=E1120
