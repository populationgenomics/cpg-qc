"""
Hail Batch workflow to perform joint calling, sample QC, and variant QC with VQSR and 
random forest methods on a WGS germline callset.

1. For the sample QC and the random forest variant QC, mostly re-implementation of 
https://github.com/broadinstitute/gnomad_qc
2. For VQSR, compilation from the following 2 WDL workflows:
 a. hail-ukbb-200k-callset/GenotypeAndFilter.AS.wdl
 b. The Broad VQSR workflow:
    https://github.com/broadinstitute/warp/blob/develop/pipelines/broad/dna_seq/germline/joint_genotyping/JointGenotyping.wdl
    documented here:
    https://gatk.broadinstitute.org/hc/en-us/articles/360035531112--How-to-Filter-variants-either-with-VQSR-or-by-hard-filtering
Translated from WDL with a help of Janis:
https://github.com/PMCC-BioinformaticsCore/janis

Output
 * The output of sample QC is a CSV file:
 `<output_bucket>/meta.tsv`
 * The output of VQSR is a VCF file <output_bucket>/<callset>-recalibrated.vcf.gz,
   as well as a QC file <output_bucket>/<callset>-eval.txt
   and R scripts to plot VQSR models: <output_bucket>/plot-snps-recal.Rscript
   and <output_bucket>/plot-indels-recal.Rscript

The workflow is parametrised by the access level, the dataset name, 
batch names and the output version.

It must be only run with the CPG analysis-runner:
https://github.com/populationgenomics/analysis-runner (see helper script `joint_calling.sh` for analysis-runner submissions)
"""

import os
from os.path import join, dirname, abspath
from typing import List, Optional
import logging
import click
import pandas as pd
import hailtop.batch as hb
from hailtop.batch.job import Job
from analysis_runner import dataproc

from joint_calling import utils
from joint_calling.vqsr import make_vqsr_jobs
from joint_calling.rf import make_rf_jobs

logger = logging.getLogger('joint-calling')
logger.setLevel('INFO')


@click.command()
@click.option('--callset', 'callset_name', type=str, required=True)
@click.option('--version', 'callset_version', type=str, required=True)
@click.option('--batch', 'callset_batches', type=str, multiple=True)
@click.option(
    '--access-level', 'access_level', type=click.Choice(['test', 'standard', 'full'])
)
@click.option(
    '--from',
    'input_bucket_suffix',
    type=click.Choice(['main', 'test']),
    help='The bucket type to read from (default: "test" for "test" access level, '
    '"main" for "standard" and "full")',
)
@click.option(
    '--mt-to',
    'mt_output_bucket_suffix',
    type=click.Choice(['main', 'temporary']),
    help='The bucket type to write matrix tables to (default: "temporary" for "test" '
    'and "standard" access levels, "main" for "full")',
)
@click.option(
    '--analysis-to',
    'analysis_output_bucket_suffix',
    type=click.Choice(['analysis', 'temporary']),
    help='The bucket type to write analysis files to (default: "temporary" for "test" '
    'and "standard" access levels, "analysis" for "full")',
)
@click.option(
    '--ped-file', 'ped_file', help='PED file with family information', type=str
)
@click.option('--skip-input-meta', 'skip_input_meta', is_flag=True)
@click.option('--keep-scratch', 'keep_scratch', is_flag=True)
@click.option(
    '--reuse-scratch-run-id',
    'reuse_scratch_run_id',
    help='Run ID to reuse scratch from',
)
@click.option(
    '--overwrite/--reuse',
    'overwrite',
    is_flag=True,
    help='if an intermediate or a final file exists, skip running the code '
    'that generates it.',
)
@click.option('--dry-run', 'dry_run', is_flag=True)
@click.option('--run-vqsr/--skip-vqsr', 'run_vqsr', is_flag=True, default=True)
@click.option('--run-rf/--skip-rf', 'run_rf', is_flag=True, default=True)
def main(  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements
    callset_name: str,
    callset_version: str,
    callset_batches: List[str],
    access_level: str,
    input_bucket_suffix: str,
    mt_output_bucket_suffix: str,
    analysis_output_bucket_suffix: str,
    ped_file: str,
    skip_input_meta: bool,
    keep_scratch: bool,
    reuse_scratch_run_id: str,
    dry_run: bool,
    overwrite: bool,
    run_vqsr: bool,
    run_rf: bool,
):
    """
    Drive a Hail Batch workflow that creates and submits jobs. A job usually runs
    either a Hail Query script from the scripts folder in this repo using a Dataproc
    cluster; or a GATK command using the GATK or Gnarly image.
    """
    logger.info(f'Enable random forest: {run_rf}')
    logger.info(f'Enable VQSR: {run_vqsr}')

    billing_project = os.getenv('HAIL_BILLING_PROJECT') or callset_name

    if not mt_output_bucket_suffix:
        if access_level == 'full':
            mt_output_bucket_suffix = 'main'
        else:
            mt_output_bucket_suffix = 'temporary'

    if not analysis_output_bucket_suffix:
        if access_level == 'full':
            analysis_output_bucket_suffix = 'analysis'
        else:
            analysis_output_bucket_suffix = 'temporary'

    if not input_bucket_suffix:
        if access_level in ['standard', 'full']:
            input_bucket_suffix = 'main'
        else:
            input_bucket_suffix = 'test'

    if not callset_batches:
        if access_level == 'test':
            callset_batches = ['0']
        else:
            raise click.BadParameter(
                'Please, specify batch numbers with --batch '
                '(can put multiple times, e.g. --batch 0 --batch 1)'
            )

    mt_output_bucket = f'gs://cpg-{callset_name}-{mt_output_bucket_suffix}/joint_vcf'
    raw_combined_mt_path = join(mt_output_bucket, 'raw', 'genomes.mt')
    # pylint: disable=unused-variable
    filtered_combined_mt_path = join(mt_output_bucket, 'genomes.mt')

    base_bucket = f'gs://cpg-{callset_name}-{analysis_output_bucket_suffix}/joint_vcf'
    analysis_bucket = join(base_bucket, callset_version)
    hail_bucket = os.environ.get('HAIL_BUCKET') or join(analysis_bucket, 'hail')
    combiner_bucket = join(analysis_bucket, 'combiner')

    backend = hb.ServiceBackend(
        billing_project=billing_project,
        bucket=hail_bucket.replace('gs://', ''),
    )
    b = hb.Batch('Joint Calling', backend=backend)
    scripts_dir = abspath(join(dirname(__file__), '..', 'scripts'))

    # TODO: merge with existing data

    input_samples_path = join(analysis_bucket, 'samples.csv')
    combiner_ready_samples_path = join(combiner_bucket, 'samples.csv')
    subset_gvcf_jobs = []
    if not overwrite and utils.file_exists(combiner_ready_samples_path):
        samples_df = pd.read_csv(combiner_ready_samples_path, sep='\t').set_index(
            's', drop=False
        )
        samples_df = samples_df[pd.notnull(samples_df.s)]
    else:
        if not overwrite and utils.file_exists(input_samples_path):
            samples_df = pd.read_csv(input_samples_path, sep='\t').set_index(
                's', drop=False
            )
        else:
            input_buckets = []
            for cb in callset_batches:
                cb = f'batch{cb}' if not cb.startswith('batch') else cb
                input_buckets.append(
                    f'gs://cpg-{callset_name}-{input_bucket_suffix}/gvcf/{cb}'
                )
            samples_df = utils.find_inputs(input_buckets, skip_qc=skip_input_meta)
        samples_df = samples_df[pd.notnull(samples_df.s)]
        gvcfs = [
            b.read_input_group(**{'g.vcf.gz': gvcf, 'g.vcf.gz.tbi': gvcf + '.tbi'})
            for gvcf in list(samples_df.gvcf)
        ]
        subset_gvcf_jobs = add_prep_gvcfs_for_combiner_steps(
            b=b,
            gvcfs=gvcfs,
            hail_bucket=hail_bucket,
            reuse_scratch_run_id=reuse_scratch_run_id,
            samples_df=samples_df,
            output_sample_csv_path=combiner_ready_samples_path,
            combiner_bucket=combiner_bucket,
            overwrite=overwrite,
        )

    if not utils.file_exists(raw_combined_mt_path):
        combiner_job = dataproc.hail_dataproc_job(
            b,
            f'{scripts_dir}/combine_gvcfs.py '
            f'--meta-csv {combiner_ready_samples_path} '
            f'--out-mt {raw_combined_mt_path} '
            f'--bucket {combiner_bucket}/work '
            f'--hail-billing {billing_project} ',
            max_age='8h',
            packages=utils.DATAPROC_PACKAGES,
            num_secondary_workers=10,
            depends_on=subset_gvcf_jobs,
            job_name='Combine GVCFs',
        )
    else:
        combiner_job = b.new_job('Combine GVCFs')

    sample_qc_bucket = join(analysis_bucket, 'sample_qc')

    info_ht_path = join(sample_qc_bucket, 'info.ht')
    info_split_ht_path = join(sample_qc_bucket, 'info-split.ht')
    info_vcf_path = join(sample_qc_bucket, 'info.vcf')
    if not any(
        utils.file_exists(fp)
        for fp in [info_ht_path, info_split_ht_path, info_vcf_path]
    ):
        generate_info_job = dataproc.hail_dataproc_job(
            b,
            f'{scripts_dir}/generate_info_ht.py --overwrite '
            f'--mt {raw_combined_mt_path} '
            f'--out-info-ht {info_ht_path} '
            f'--out-split-info-ht {info_split_ht_path} '
            f'--out-info-vcf {info_vcf_path}',
            max_age='8h',
            packages=utils.DATAPROC_PACKAGES,
            num_secondary_workers=10,
            depends_on=[combiner_job],
            job_name='Sample QC',
        )
    else:
        generate_info_job = b.new_job('Generate info')

    hard_filtered_samples_ht_path = join(sample_qc_bucket, 'hard_filters.ht')
    meta_ht_path = join(combiner_bucket, 'meta.ht')
    if not any(
        utils.file_exists(fp) for fp in [hard_filtered_samples_ht_path, meta_ht_path]
    ):
        age_csv = join(base_bucket, 'age.csv')
        if utils.file_exists(age_csv):
            age_csv_param = f'--age-csv {age_csv} '
        else:
            age_csv_param = ''
        sample_qc_job = dataproc.hail_dataproc_job(
            b,
            f'{scripts_dir}/sample_qc.py --overwrite '
            f'--mt {raw_combined_mt_path} '
            f'--info-ht {info_ht_path} '
            f'{age_csv_param}'
            f'--meta-csv {combiner_ready_samples_path} '
            f'--bucket {combiner_bucket} '
            f'--out-hardfiltered-samples-ht {hard_filtered_samples_ht_path} '
            f'--out-meta-ht {meta_ht_path} '
            f'--hail-billing {billing_project} ',
            max_age='8h',
            packages=utils.DATAPROC_PACKAGES,
            num_secondary_workers=10,
            depends_on=[combiner_job, generate_info_job],
            job_name='Sample QC',
        )
    else:
        sample_qc_job = b.new_job('Sample QC')

    if run_rf:
        make_rf_jobs(
            b,
            combined_mt_path=raw_combined_mt_path,
            info_split_ht_path=info_split_ht_path,
            hard_filtered_samples_ht_path=hard_filtered_samples_ht_path,
            meta_ht_path=meta_ht_path,
            work_bucket=join(analysis_bucket, 'variant_qc'),
            depends_on=[sample_qc_job],
            scripts_dir=scripts_dir,
            ped_file=ped_file,
            overwrite=overwrite,
        )

    if run_vqsr:
        make_vqsr_jobs(
            b,
            combined_mt_path=raw_combined_mt_path,
            work_bucket=join(analysis_bucket, 'vqsr'),
            gvcf_count=len(samples_df),
            depends_on=[combiner_job],
            scripts_dir=scripts_dir,
        )

    b.run(dry_run=dry_run, delete_scratch_on_exit=not keep_scratch)


def add_prep_gvcfs_for_combiner_steps(
    b,
    gvcfs,
    hail_bucket: str,
    reuse_scratch_run_id: Optional[str],
    samples_df: pd.DataFrame,
    output_sample_csv_path: str,
    combiner_bucket: str,
    overwrite: bool,
) -> List[Job]:
    """
    Add steps required to prepare GVCFs from combining
    :param b:
    :param gvcfs:
    :param hail_bucket:
    :param reuse_scratch_run_id:
    :param samples_df:
    :param output_sample_csv_path:
    :param combiner_bucket:
    :param overwrite:
    :return:
    """
    found_reblocked_gvcf_paths = [
        join(
            hail_bucket,
            'batch',
            reuse_scratch_run_id,
            str(job_num),
            'output_gvcf.g.vcf.gz',
        )
        if reuse_scratch_run_id
        else None
        for job_num in range(1, 1 + len(gvcfs))
    ]
    reblocked_gvcfs = [
        b.read_input_group(
            **{
                'vcf.gz': found_gvcf_path,
                'vcf.gz.tbi': found_gvcf_path + '.tbi',
            }
        )
        if (not overwrite and found_gvcf_path and utils.file_exists(found_gvcf_path))
        else add_reblock_gvcfs_step(b, input_gvcf).output_gvcf
        for found_gvcf_path, input_gvcf in zip(found_reblocked_gvcf_paths, gvcfs)
    ]

    noalt_regions = b.read_input('gs://cpg-reference/hg38/v0/noalt.bed')
    combiner_gvcf_bucket = os.path.join(combiner_bucket, 'gvcfs')
    subset_gvcf_jobs = [
        add_subset_noalt_step(
            b,
            input_gvcf=input_gvcf,
            output_gvcf_path=output_gvcf_path,
            noalt_regions=noalt_regions,
        )
        if not utils.file_exists(output_gvcf_path)
        else b.new_job('SubsetToNoalt')
        for input_gvcf, output_gvcf_path in [
            (gvcf, join(combiner_gvcf_bucket, f'{sample}.g.vcf.gz'))
            for sample, gvcf in zip(list(samples_df.s), reblocked_gvcfs)
        ]
    ]
    for sn in samples_df.s:
        samples_df.loc[sn, ['gvcf']] = join(combiner_gvcf_bucket, sn + '.g.vcf.gz')
    samples_df.to_csv(output_sample_csv_path, index=False, sep='\t', na_rep='NA')
    logger.info(f'Saved metadata with updated GVCFs to {output_sample_csv_path}')
    return subset_gvcf_jobs


def add_reblock_gvcfs_step(
    b: hb.Batch,
    input_gvcf: hb.ResourceGroup,
) -> Job:
    """
    Runs ReblockGVCF to annotate with allele-specific VCF INFO fields
    required for recalibration
    """
    j = b.new_job('ReblockGVCF')
    j.image(utils.GATK_DOCKER)
    mem_gb = 8
    j.memory(f'{mem_gb}G')
    j.storage(f'30G')
    j.declare_resource_group(
        output_gvcf={
            'g.vcf.gz': '{root}.g.vcf.gz',
            'g.vcf.gz.tbi': '{root}.g.vcf.gz.tbi',
        }
    )

    j.command(
        f"""
    gatk --java-options "-Xms{mem_gb - 1}g" \\
        ReblockGVCF \\
        -V {input_gvcf['g.vcf.gz']} \\
        --drop-low-quals \\
        -do-qual-approx \\
        -O {j.output_gvcf['g.vcf.gz']} \\
        --create-output-variant-index true"""
    )
    return j


def add_subset_noalt_step(
    b: hb.Batch,
    input_gvcf: hb.ResourceGroup,
    output_gvcf_path: str,
    noalt_regions: str,
) -> Job:
    """
    1. Subset GVCF to main chromosomes to avoid downstream errors
    2. Removes the DS INFO field that is added to some HGDP GVCFs to avoid errors
       from Hail about mismatched INFO annotations
    """
    j = b.new_job('SubsetToNoalt')
    j.image('quay.io/biocontainers/bcftools:1.10.2--h4f4756c_2')
    mem_gb = 8
    j.memory(f'{mem_gb}G')
    j.storage(f'30G')
    j.declare_resource_group(
        output_gvcf={
            'g.vcf.gz': '{root}.g.vcf.gz',
            'g.vcf.gz.tbi': '{root}.g.vcf.gz.tbi',
        }
    )
    j.command(
        f"""set -e

    bcftools view \\
        {input_gvcf['g.vcf.gz']} \\
        -T {noalt_regions} \\
        | bcftools annotate -x INFO/DS \\
        -o {j.output_gvcf['g.vcf.gz']} \\
        -Oz

    bcftools index --tbi {j.output_gvcf['g.vcf.gz']}
        """
    )
    b.write_output(j.output_gvcf, output_gvcf_path.replace('.g.vcf.gz', ''))
    return j


if __name__ == '__main__':
    main()  # pylint: disable=E1120
