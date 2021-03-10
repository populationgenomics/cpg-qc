import hailtop.batch as hb
from typing import Union, Optional, List


def main(
    unpadded_intervals_file: str,
    combined_gvcf: str,
    callset_name: str,
    ref_fasta: str,
    ref_fasta_index: str,
    ref_dict: str,
    dbsnp_vcf: str,
    dbsnp_vcf_index: str,
    small_disk: int,
    medium_disk: int,
    huge_disk: int,
    snp_recalibration_tranche_values: List[str],
    snp_recalibration_annotation_values: List[str],
    indel_recalibration_tranche_values: List[str],
    indel_recalibration_annotation_values: List[str],
    eval_interval_list: str,
    hapmap_resource_vcf: str,
    hapmap_resource_vcf_index: str,
    omni_resource_vcf: str,
    omni_resource_vcf_index: str,
    one_thousand_genomes_resource_vcf: str,
    one_thousand_genomes_resource_vcf_index: str,
    mills_resource_vcf: str,
    mills_resource_vcf_index: str,
    axiomPoly_resource_vcf: str,
    axiomPoly_resource_vcf_index: str,
    snp_filter_level: float,
    indel_filter_level: float,
    SNP_VQSR_downsampleFactor: int,
    indel_VQSR_downsampleFactor: int,
    vcf_count: int,
    do_tabix: bool,
    unpadded_intervals: Optional[List[str]],
    dbsnp_resource_vcf: Optional[str] = None,
    dbsnp_resource_vcf_index: Optional[str] = None,
    excess_het_threshold: Optional[float] = 54.69,
    use_allele_specific_annotations: Optional[bool] = True,
    unboundedScatterCount: Optional[int] = None,
    scatterCount: Optional[int] = None,
    SplitIntervalList_sample_names_unique_done: Optional[bool] = True,
    SNPsVariantRecalibratorScattered_machine_mem_gb: Optional[int] = 60,
    is_small_callset: Optional[bool] = False,
    ApplyRecalibration_use_allele_specific_annotations: Optional[bool] = True,
):
    b = hb.Batch("VariantCallingOFTHEFUTURE")
    dbsnp_resource_vcf = (
        dbsnp_resource_vcf if dbsnp_resource_vcf is not None else dbsnp_vcf
    )
    dbsnp_resource_vcf_index = (
        dbsnp_resource_vcf_index
        if dbsnp_resource_vcf_index is not None
        else dbsnp_vcf_index
    )
    unboundedScatterCount = (
        unboundedScatterCount if unboundedScatterCount is not None else vcf_count
    )
    scatterCount = (
        scatterCount
        if scatterCount is not None
        else (unboundedScatterCount if (unboundedScatterCount > 10) else 10)
    )
    combined_gvcf = b.read_input(combined_gvcf)
    ref_fasta = b.read_input(ref_fasta)
    ref_fasta_index = b.read_input(ref_fasta_index)
    ref_dict = b.read_input(ref_dict)
    dbsnp_vcf = b.read_input(dbsnp_vcf)
    dbsnp_vcf_index = b.read_input(dbsnp_vcf_index)
    eval_interval_list = b.read_input(eval_interval_list)
    hapmap_resource_vcf = b.read_input(hapmap_resource_vcf)
    hapmap_resource_vcf_index = b.read_input(hapmap_resource_vcf_index)
    omni_resource_vcf = b.read_input(omni_resource_vcf)
    omni_resource_vcf_index = b.read_input(omni_resource_vcf_index)
    one_thousand_genomes_resource_vcf = b.read_input(
        one_thousand_genomes_resource_vcf
    )
    one_thousand_genomes_resource_vcf_index = b.read_input(
        one_thousand_genomes_resource_vcf_index
    )
    mills_resource_vcf = b.read_input(mills_resource_vcf)
    mills_resource_vcf_index = b.read_input(mills_resource_vcf_index)
    axiomPoly_resource_vcf = b.read_input(axiomPoly_resource_vcf)
    axiomPoly_resource_vcf_index = b.read_input(axiomPoly_resource_vcf_index)
    dbsnp_resource_vcf = b.read_input(dbsnp_resource_vcf)
    dbsnp_resource_vcf_index = b.read_input(dbsnp_resource_vcf_index)
    unpadded_intervals = [
        b.read_input(inner_unpadded_intervals)
        for inner_unpadded_intervals in unpadded_intervals
    ]

    TabixBGzippedFile = None
    if do_tabix:
        TabixBGzippedFile = add_TabixBGzippedFile_step(
            b, zipped_vcf=combined_gvcf, zipped_vcf_path=combined_gvcf
        )

    SplitIntervalList = add_SplitIntervalList_step(
        b,
        interval_list=unpadded_intervals_file,
        scatter_count=scatterCount,
        ref_fasta=ref_fasta,
        ref_fasta_index=ref_fasta_index,
        ref_dict=ref_dict,
        disk_size=small_disk,
        sample_names_unique_done=SplitIntervalList_sample_names_unique_done,
    )

    GnarlyGenotyperOnVcf = []
    for idx in range(len(unpadded_intervals)):
        GnarlyGenotyperOnVcf.append(
            add_GnarlyGenotyperOnVcf_step(
                b,
                combined_gvcf=combined_gvcf,
                combined_gvcf_index=[
                    a
                    for a in [
                        "TabixBGzippedFile.bucket_tabix_output",
                        "(combined_gvcf + '.tbi')",
                    ]
                    if a is not None
                ],
                interval=unpadded_intervals[idx],
                output_vcf_filename=(((callset_name + ".") + idx) + ".vcf.gz"),
                ref_fasta=ref_fasta,
                ref_fasta_index=ref_fasta_index,
                ref_dict=ref_dict,
                dbsnp_vcf=dbsnp_vcf,
            )
        )

    HardFilterAndMakeSitesOnlyVcf = []
    for idx in range(len(unpadded_intervals)):
        HardFilterAndMakeSitesOnlyVcf.append(
            add_HardFilterAndMakeSitesOnlyVcf_step(
                b,
                vcf=GnarlyGenotyperOnVcf.output_vcf,
                vcf_index=GnarlyGenotyperOnVcf.output_vcf_index,
                excess_het_threshold=excess_het_threshold,
                variant_filtered_vcf_filename=(
                    ((callset_name + ".") + idx) + ".variant_filtered.vcf.gz"
                ),
                sites_only_vcf_filename=(
                    ((callset_name + ".") + idx)
                    + ".sites_only.variant_filtered.vcf.gz"
                ),
                disk_size=medium_disk,
            )
        )
    SitesOnlyGatherVcf = add_SitesOnlyGatherVcf_step(
        b,
        input_vcfs=HardFilterAndMakeSitesOnlyVcf.sites_only_vcf,
        output_vcf_name=(callset_name + ".sites_only.vcf.gz"),
        disk_size=medium_disk,
    )
    IndelsVariantRecalibrator = add_IndelsVariantRecalibrator_step(
        b,
        sites_only_variant_filtered_vcf=SitesOnlyGatherVcf.output_vcf,
        sites_only_variant_filtered_vcf_index=SitesOnlyGatherVcf.output_vcf_index,
        recalibration_filename=(callset_name + ".indels.recal"),
        tranches_filename=(callset_name + ".indels.tranches"),
        recalibration_tranche_values=indel_recalibration_tranche_values,
        recalibration_annotation_values=indel_recalibration_annotation_values,
        mills_resource_vcf=mills_resource_vcf,
        mills_resource_vcf_index=mills_resource_vcf_index,
        axiomPoly_resource_vcf=axiomPoly_resource_vcf,
        axiomPoly_resource_vcf_index=axiomPoly_resource_vcf_index,
        dbsnp_resource_vcf=dbsnp_resource_vcf,
        dbsnp_resource_vcf_index=dbsnp_resource_vcf_index,
        use_allele_specific_annotations=use_allele_specific_annotations,
        disk_size=small_disk,
    )
    SNPsVariantRecalibratorCreateModel = add_SNPsVariantRecalibratorCreateModel_step(
        b,
        sites_only_variant_filtered_vcf=SitesOnlyGatherVcf.output_vcf,
        sites_only_variant_filtered_vcf_index=SitesOnlyGatherVcf.output_vcf_index,
        recalibration_filename=(callset_name + ".snps.recal"),
        tranches_filename=(callset_name + ".snps.tranches"),
        recalibration_tranche_values=snp_recalibration_tranche_values,
        recalibration_annotation_values=snp_recalibration_annotation_values,
        downsampleFactor=SNP_VQSR_downsampleFactor,
        model_report_filename=(callset_name + ".snps.model.report"),
        hapmap_resource_vcf=hapmap_resource_vcf,
        hapmap_resource_vcf_index=hapmap_resource_vcf_index,
        omni_resource_vcf=omni_resource_vcf,
        omni_resource_vcf_index=omni_resource_vcf_index,
        one_thousand_genomes_resource_vcf=one_thousand_genomes_resource_vcf,
        one_thousand_genomes_resource_vcf_index=one_thousand_genomes_resource_vcf_index,
        dbsnp_resource_vcf=dbsnp_resource_vcf,
        dbsnp_resource_vcf_index=dbsnp_resource_vcf_index,
        disk_size=small_disk,
        use_allele_specific_annotations=use_allele_specific_annotations,
    )

    SNPsVariantRecalibratorScattered = []
    for idx in range(len(HardFilterAndMakeSitesOnlyVcf.sites_only_vcf)):
        SNPsVariantRecalibratorScattered.append(
            add_SNPsVariantRecalibratorScattered_step(
                b,
                sites_only_variant_filtered_vcf=HardFilterAndMakeSitesOnlyVcf.sites_only_vcf[
                    idx
                ],
                sites_only_variant_filtered_vcf_index=HardFilterAndMakeSitesOnlyVcf.sites_only_vcf_index[
                    idx
                ],
                recalibration_filename=(
                    ((callset_name + ".snps.") + idx) + ".recal"
                ),
                tranches_filename=(
                    ((callset_name + ".snps.") + idx) + ".tranches"
                ),
                recalibration_tranche_values=snp_recalibration_tranche_values,
                recalibration_annotation_values=snp_recalibration_annotation_values,
                model_report=SNPsVariantRecalibratorCreateModel.model_report,
                hapmap_resource_vcf=hapmap_resource_vcf,
                hapmap_resource_vcf_index=hapmap_resource_vcf_index,
                omni_resource_vcf=omni_resource_vcf,
                omni_resource_vcf_index=omni_resource_vcf_index,
                one_thousand_genomes_resource_vcf=one_thousand_genomes_resource_vcf,
                one_thousand_genomes_resource_vcf_index=one_thousand_genomes_resource_vcf_index,
                dbsnp_resource_vcf=dbsnp_resource_vcf,
                dbsnp_resource_vcf_index=dbsnp_resource_vcf_index,
                disk_size=small_disk,
                machine_mem_gb=SNPsVariantRecalibratorScattered_machine_mem_gb,
                use_allele_specific_annotations=use_allele_specific_annotations,
            )
        )
    SNPGatherTranches = add_SNPGatherTranches_step(
        b,
        tranches=SNPsVariantRecalibratorScattered.tranches,
        output_filename=(callset_name + ".snps.gathered.tranches"),
        disk_size=small_disk,
    )

    ApplyRecalibration = []
    for idx in range(len(HardFilterAndMakeSitesOnlyVcf.variant_filtered_vcf)):
        ApplyRecalibration.append(
            add_ApplyRecalibration_step(
                b,
                recalibrated_vcf_filename=(
                    ((callset_name + ".filtered.") + idx) + ".vcf.gz"
                ),
                input_vcf=HardFilterAndMakeSitesOnlyVcf.variant_filtered_vcf[idx],
                input_vcf_index=HardFilterAndMakeSitesOnlyVcf.variant_filtered_vcf_index[
                    idx
                ],
                indels_recalibration=IndelsVariantRecalibrator.recalibration,
                indels_recalibration_index=IndelsVariantRecalibrator.recalibration_index,
                indels_tranches=IndelsVariantRecalibrator.tranches,
                snps_recalibration=SNPsVariantRecalibratorScattered.recalibration[
                    idx
                ],
                snps_recalibration_index=SNPsVariantRecalibratorScattered.recalibration_index[
                    idx
                ],
                snps_tranches=SNPGatherTranches.out_tranches,
                indel_filter_level=indel_filter_level,
                snp_filter_level=snp_filter_level,
                disk_size=medium_disk,
                use_allele_specific_annotations=ApplyRecalibration_use_allele_specific_annotations,
            )
        )
    CollectMetricsSharded = None
    if not is_small_callset:
        CollectMetricsSharded = add_CollectMetricsSharded_step(
            b,
            input_vcf=ApplyRecalibration.recalibrated_vcf,
            input_vcf_index=ApplyRecalibration.recalibrated_vcf_index,
            metrics_filename_prefix=((callset_name + ".") + idx),
            dbsnp_vcf=dbsnp_vcf,
            dbsnp_vcf_index=dbsnp_vcf_index,
            interval_list=eval_interval_list,
            ref_dict=ref_dict,
            disk_size=small_disk,
        )

    FinalGatherVcf = None
    if is_small_callset:
        FinalGatherVcf = add_FinalGatherVcf_step(
            b,
            input_vcfs=ApplyRecalibration.recalibrated_vcf,
            output_vcf_name=(callset_name + ".vcf.gz"),
            disk_size=huge_disk,
        )

    CollectMetricsOnFullVcf = None
    if is_small_callset:
        CollectMetricsOnFullVcf = add_CollectMetricsOnFullVcf_step(
            b,
            input_vcf=FinalGatherVcf.output_vcf,
            input_vcf_index=FinalGatherVcf.output_vcf_index,
            metrics_filename_prefix=callset_name,
            dbsnp_vcf=dbsnp_vcf,
            dbsnp_vcf_index=dbsnp_vcf_index,
            interval_list=eval_interval_list,
            ref_dict=ref_dict,
            disk_size=huge_disk,
        )

    return b


def add_TabixBGzippedFile_step(
    b,
    zipped_vcf,
    zipped_vcf_path,
    localized_tabix_path=None,
    bucket_tabix_path=None,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("TabixBGzippedFile")
    localized_tabix_path = (
        localized_tabix_path
        if localized_tabix_path is not None
        else (zipped_vcf + ".tbi")
    )
    bucket_tabix_path = (
        bucket_tabix_path
        if bucket_tabix_path is not None
        else (zipped_vcf_path + ".tbi")
    )
    j.image(container)
    j.command(
        f"""gatk IndexFeatureFile -F {zipped_vcf}
    gsutil cp {zipped_vcf}'.tbi' {bucket_tabix_path}
"""
    )

    j.command('ln "{bucket_tabix_path}" {j.bucket_tabix_output}')
    j.memory("4Gb")

    return j


def add_SplitIntervalList_step(
    b,
    interval_list,
    scatter_count,
    ref_fasta,
    ref_fasta_index,
    ref_dict,
    sample_names_unique_done,
    disk_size,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("SplitIntervalList")

    j.image(container)
    j.command(
        f"""gatk --java-options -Xms3g SplitIntervals \n      -L {interval_list} -O  scatterDir -scatter {scatter_count} -R {ref_fasta} \n      -mode BALANCING_WITHOUT_INTERVAL_SUBDIVISION_WITH_OVERFLOW
"""
    )

    j.command('ln "scatterDir/*")')
    j.memory("4Gb")

    return j


def add_GnarlyGenotyperOnVcf_step(
    b,
    combined_gvcf,
    combined_gvcf_index,
    interval,
    output_vcf_filename,
    ref_fasta,
    ref_fasta_index,
    ref_dict,
    dbsnp_vcf,
    gatk_docker="gcr.io/broad-dsde-methods/gnarly_genotyper:hail_ukbb_300K",
    container="gcr.io/broad-dsde-methods/gnarly_genotyper:hail_ukbb_300K",
):
    j = b.new_job("GnarlyGenotyperOnVcf")

    j.image(container)
    j.command(
        f"""set -e

    gatk --java-options -Xms8g \n      GnarlyGenotyper \n      -R {ref_fasta} \n      -O {output_vcf_filename} \n      -D {dbsnp_vcf} \n      --only-output-calls-starting-in-intervals \n      --keep-all-sites \n      -V {combined_gvcf} \n      -L {interval}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{output_vcf_filename}", dest=j.output_vcf
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{output_vcf_filename}.tbi", dest=j.output_vcf_index
        )
    )
    j.memory("4Gb")

    return j


def add_HardFilterAndMakeSitesOnlyVcf_step(
    b,
    vcf,
    vcf_index,
    excess_het_threshold,
    variant_filtered_vcf_filename,
    sites_only_vcf_filename,
    disk_size,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("HardFilterAndMakeSitesOnlyVcf")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    gatk --java-options -Xms3g \n      VariantFiltration \n      --filter-expression 'ExcessHet > {excess_het_threshold}' \n      --filter-name ExcessHet \n      -O {variant_filtered_vcf_filename} \n      -V {vcf}

    gatk --java-options -Xms3g \n      MakeSitesOnlyVcf \n      -I {variant_filtered_vcf_filename} \n      -O {sites_only_vcf_filename}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{variant_filtered_vcf_filename}", dest=j.variant_filtered_vcf
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{variant_filtered_vcf_filename}.tbi",
            dest=j.variant_filtered_vcf_index,
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{sites_only_vcf_filename}", dest=j.sites_only_vcf
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{sites_only_vcf_filename}.tbi", dest=j.sites_only_vcf_index
        )
    )
    j.memory("4Gb")

    return j


def add_SitesOnlyGatherVcf_step(
    b,
    input_vcfs,
    output_vcf_name,
    disk_size,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("SitesOnlyGatherVcf")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    # --ignore-safety-checks makes a big performance difference so we include it in our invocation.
    # This argument disables expensive checks that the file headers contain the same set of
    # genotyped samples and that files are in order by position of first record.
    gatk --java-options -Xms6g \n      GatherVcfsCloud \n      --ignore-safety-checks \n      --gather-type BLOCK \n      --input {input_vcfs} \n      --output {output_vcf_name}

    tabix {output_vcf_name}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{output_vcf_name}", dest=j.output_vcf
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{output_vcf_name}.tbi", dest=j.output_vcf_index
        )
    )
    j.memory("4Gb")

    return j


def add_IndelsVariantRecalibrator_step(
    b,
    recalibration_filename,
    tranches_filename,
    model_report,
    recalibration_tranche_values,
    recalibration_annotation_values,
    sites_only_variant_filtered_vcf,
    sites_only_variant_filtered_vcf_index,
    mills_resource_vcf,
    axiomPoly_resource_vcf,
    dbsnp_resource_vcf,
    mills_resource_vcf_index,
    axiomPoly_resource_vcf_index,
    dbsnp_resource_vcf_index,
    use_allele_specific_annotations,
    disk_size,
    max_gaussians=4,
    container="ubuntu:latest",
):
    j = b.new_job("IndelsVariantRecalibrator")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    MODEL_REPORT={model_report}

    gatk --java-options -Xms100g \n      VariantRecalibrator \n      -V {sites_only_variant_filtered_vcf} \n      -O {recalibration_filename} \n      --tranches-file {tranches_filename} \n      --trust-all-polymorphic \n      -tranche {recalibration_tranche_values} \n      -an {recalibration_annotation_values} \n      -mode INDEL \n      {use_allele_specific_annotations} \n      {model_report_arg} \n      --max-gaussians {max_gaussians} \n      -resource:mills,known=false,training=true,truth=true,prior=12 {mills_resource_vcf} \n      -resource:axiomPoly,known=false,training=true,truth=false,prior=10 {axiomPoly_resource_vcf} \n      -resource:dbsnp,known=true,training=false,truth=false,prior=2 {dbsnp_resource_vcf}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{recalibration_filename}", dest=j.recalibration
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{recalibration_filename}.idx", dest=j.recalibration_index
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{tranches_filename}", dest=j.tranches
        )
    )
    j.memory("4Gb")

    return j


def add_SNPsVariantRecalibratorCreateModel_step(
    b,
    recalibration_filename,
    tranches_filename,
    downsampleFactor,
    model_report_filename,
    recalibration_tranche_values,
    recalibration_annotation_values,
    sites_only_variant_filtered_vcf,
    sites_only_variant_filtered_vcf_index,
    hapmap_resource_vcf,
    omni_resource_vcf,
    one_thousand_genomes_resource_vcf,
    dbsnp_resource_vcf,
    hapmap_resource_vcf_index,
    omni_resource_vcf_index,
    one_thousand_genomes_resource_vcf_index,
    dbsnp_resource_vcf_index,
    disk_size,
    use_allele_specific_annotations,
    max_gaussians=6,
    java_mem=100,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.4.1",
    container="us.gcr.io/broad-gatk/gatk:4.1.4.1",
):
    j = b.new_job("SNPsVariantRecalibratorCreateModel")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    gatk --java-options -Xms{java_mem}g \n      VariantRecalibrator \n      -V {sites_only_variant_filtered_vcf} \n      -O {recalibration_filename} \n      --tranches-file {tranches_filename} \n      --trust-all-polymorphic \n      -tranche {recalibration_tranche_values} \n      -an {recalibration_annotation_values} \n      -mode SNP \n      {use_allele_specific_annotations} \n      --sample-every-Nth-variant {downsampleFactor} \n      --output-model {model_report_filename} \n      --max-gaussians {max_gaussians} \n      -resource:hapmap,known=false,training=true,truth=true,prior=15 {hapmap_resource_vcf} \n      -resource:omni,known=false,training=true,truth=true,prior=12 {omni_resource_vcf} \n      -resource:1000G,known=false,training=true,truth=false,prior=10 {one_thousand_genomes_resource_vcf} \n      -resource:dbsnp,known=true,training=false,truth=false,prior=7 {dbsnp_resource_vcf}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{model_report_filename}", dest=j.model_report
        )
    )
    j.memory("4Gb")

    return j


def add_SNPsVariantRecalibratorScattered_step(
    b,
    recalibration_filename,
    tranches_filename,
    model_report,
    recalibration_tranche_values,
    recalibration_annotation_values,
    sites_only_variant_filtered_vcf,
    sites_only_variant_filtered_vcf_index,
    hapmap_resource_vcf,
    omni_resource_vcf,
    one_thousand_genomes_resource_vcf,
    dbsnp_resource_vcf,
    hapmap_resource_vcf_index,
    omni_resource_vcf_index,
    one_thousand_genomes_resource_vcf_index,
    dbsnp_resource_vcf_index,
    disk_size,
    machine_mem_gb,
    use_allele_specific_annotations,
    max_gaussians=6,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("SNPsVariantRecalibratorScattered")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    MODEL_REPORT={model_report}

    gatk --java-options -Xms{{(machine_mem - 1)}}g \n      VariantRecalibrator \n      -V {sites_only_variant_filtered_vcf} \n      -O {recalibration_filename} \n      --tranches-file {tranches_filename} \n      --trust-all-polymorphic \n      -tranche {recalibration_tranche_values} \n      -an {recalibration_annotation_values} \n      -mode SNP \n      {use_allele_specific_annotations} \n       {model_report_arg} \n      --max-gaussians {max_gaussians} \n      -resource:hapmap,known=false,training=true,truth=true,prior=15 {hapmap_resource_vcf} \n      -resource:omni,known=false,training=true,truth=true,prior=12 {omni_resource_vcf} \n      -resource:1000G,known=false,training=true,truth=false,prior=10 {one_thousand_genomes_resource_vcf} \n      -resource:dbsnp,known=true,training=false,truth=false,prior=7 {dbsnp_resource_vcf}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{recalibration_filename}", dest=j.recalibration
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{recalibration_filename}.idx", dest=j.recalibration_index
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{tranches_filename}", dest=j.tranches
        )
    )
    j.memory("4Gb")

    return j


def add_SNPGatherTranches_step(
    b,
    tranches,
    output_filename,
    disk_size,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("SNPGatherTranches")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    tranches_fofn={JANIS: write_lines([inputs.tranches])}

    # Jose says:
    # Cromwell will fall over if we have it try to localize tens of thousands of files,
    # so we manually localize files using gsutil.
    # Using gsutil also lets us parallelize the localization, which (as far as we can tell)
    # PAPI doesn't do.

    # This is here to deal with the JES bug where commands may be run twice
    rm -rf tranches
    mkdir tranches
    RETRY_LIMIT=5

    count=0
    until cat $tranches_fofn | /usr/bin/gsutil -m cp -L cp.log -c -I tranches/; do
        sleep 1
        ((count++)) && ((count >= $RETRY_LIMIT)) && break
    done
    if [ '$count' -ge '$RETRY_LIMIT' ]; then
        echo 'Could not copy all the tranches from the cloud' && exit 1
    fi

    cat $tranches_fofn | rev | cut -d '/' -f 1 | rev | awk '{{print 'tranches/' $1}}' > inputs.list

    gatk --java-options -Xms6g \n      GatherTranches \n      --input inputs.list \n      --output {output_filename}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{output_filename}", dest=j.out_tranches
        )
    )
    j.memory("4Gb")

    return j


def add_ApplyRecalibration_step(
    b,
    recalibrated_vcf_filename,
    input_vcf,
    input_vcf_index,
    indels_recalibration,
    indels_recalibration_index,
    indels_tranches,
    snps_recalibration,
    snps_recalibration_index,
    snps_tranches,
    indel_filter_level,
    snp_filter_level,
    use_allele_specific_annotations,
    disk_size,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("ApplyRecalibration")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    gatk --java-options -Xms5g \n      ApplyVQSR \n      -O tmp.indel.recalibrated.vcf \n      -V {input_vcf} \n      --recal-file {indels_recalibration} \n      --tranches-file {indels_tranches} \n      --truth-sensitivity-filter-level {indel_filter_level} \n      --create-output-variant-index true \n      -mode INDEL {use_allele_specific_annotations} \n

    gatk --java-options -Xms5g \n      ApplyVQSR \n      -O {recalibrated_vcf_filename} \n      -V tmp.indel.recalibrated.vcf \n      --recal-file {snps_recalibration} \n      --tranches-file {snps_tranches} \n      --truth-sensitivity-filter-level {snp_filter_level} \n      --create-output-variant-index true \n      -mode SNP {use_allele_specific_annotations} \n
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{recalibrated_vcf_filename}", dest=j.recalibrated_vcf
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{recalibrated_vcf_filename}.tbi",
            dest=j.recalibrated_vcf_index,
        )
    )
    j.memory("4Gb")

    return j


def add_CollectMetricsSharded_step(
    b,
    input_vcf,
    input_vcf_index,
    metrics_filename_prefix,
    dbsnp_vcf,
    dbsnp_vcf_index,
    interval_list,
    ref_dict,
    disk_size,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("CollectMetricsSharded")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    gatk --java-options -Xms6g \n      CollectVariantCallingMetrics \n      --INPUT {input_vcf} \n      --DBSNP {dbsnp_vcf} \n      --SEQUENCE_DICTIONARY {ref_dict} \n      --OUTPUT {metrics_filename_prefix} \n      --THREAD_COUNT 8 \n      --TARGET_INTERVALS {interval_list}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{metrics_filename_prefix}.variant_calling_detail_metrics",
            dest=j.detail_metrics_file,
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{metrics_filename_prefix}.variant_calling_summary_metrics",
            dest=j.summary_metrics_file,
        )
    )
    j.memory("4Gb")

    return j


def add_FinalGatherVcf_step(
    b,
    input_vcfs,
    output_vcf_name,
    disk_size,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("FinalGatherVcf")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    # --ignore-safety-checks makes a big performance difference so we include it in our invocation.
    # This argument disables expensive checks that the file headers contain the same set of
    # genotyped samples and that files are in order by position of first record.
    gatk --java-options -Xms6g \n      GatherVcfsCloud \n      --ignore-safety-checks \n      --gather-type BLOCK \n      --input {input_vcfs} \n      --output {output_vcf_name}

    tabix {output_vcf_name}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{output_vcf_name}", dest=j.output_vcf
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{output_vcf_name}.tbi", dest=j.output_vcf_index
        )
    )
    j.memory("4Gb")

    return j


def add_CollectMetricsOnFullVcf_step(
    b,
    input_vcf,
    input_vcf_index,
    metrics_filename_prefix,
    dbsnp_vcf,
    dbsnp_vcf_index,
    interval_list,
    ref_dict,
    disk_size,
    gatk_docker="us.gcr.io/broad-gatk/gatk:4.1.1.0",
    container="us.gcr.io/broad-gatk/gatk:4.1.1.0",
):
    j = b.new_job("CollectMetricsOnFullVcf")

    j.image(container)
    j.command(
        f"""set -euo pipefail

    gatk --java-options -Xms6g \n      CollectVariantCallingMetrics \n      --INPUT {input_vcf} \n      --DBSNP {dbsnp_vcf} \n      --SEQUENCE_DICTIONARY {ref_dict} \n      --OUTPUT {metrics_filename_prefix} \n      --THREAD_COUNT 8 \n      --TARGET_INTERVALS {interval_list}
"""
    )

    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{metrics_filename_prefix}.variant_calling_detail_metrics",
            dest=j.detail_metrics_file,
        )
    )
    j.command(
        'ln "{value}" {dest}'.format(
            value=f"{metrics_filename_prefix}.variant_calling_summary_metrics",
            dest=j.summary_metrics_file,
        )
    )
    j.memory("4Gb")

    return j


if __name__ == "__main__":
    main(
        unpadded_intervals_file=None,
        combined_gvcf=None,
        callset_name=None,
        ref_fasta=None,
        ref_fasta_index=None,
        ref_dict=None,
        dbsnp_vcf=None,
        dbsnp_vcf_index=None,
        small_disk=None,
        medium_disk=None,
        huge_disk=None,
        snp_recalibration_tranche_values=None,
        snp_recalibration_annotation_values=None,
        indel_recalibration_tranche_values=None,
        indel_recalibration_annotation_values=None,
        eval_interval_list=None,
        hapmap_resource_vcf=None,
        hapmap_resource_vcf_index=None,
        omni_resource_vcf=None,
        omni_resource_vcf_index=None,
        one_thousand_genomes_resource_vcf=None,
        one_thousand_genomes_resource_vcf_index=None,
        mills_resource_vcf=None,
        mills_resource_vcf_index=None,
        axiomPoly_resource_vcf=None,
        axiomPoly_resource_vcf_index=None,
        snp_filter_level=None,
        indel_filter_level=None,
        SNP_VQSR_downsampleFactor=None,
        indel_VQSR_downsampleFactor=None,
        vcf_count=None,
        do_tabix=None,
        unpadded_intervals=None,
    )
