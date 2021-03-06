"""Functions to infer ancestry and perform ancestry-stratfied sample QC"""

import logging
import pickle
from os.path import join
from typing import Optional, List
import hail as hl
import pandas as pd

from gnomad.sample_qc.ancestry import run_pca_with_relateds, assign_population_pcs
from gnomad.sample_qc.relatedness import compute_related_samples_to_drop
from gnomad.sample_qc.filtering import (
    compute_qc_metrics_residuals,
    compute_stratified_metrics_filter,
)
from gnomad.sample_qc.pipeline import get_qc_mt

from gnomad.utils.file_utils import file_exists
from gnomad.utils.annotations import get_adj_expr

logger = logging.getLogger('sample_qc_pca')


def make_mt_for_pca(
    mt: hl.MatrixTable, work_bucket: str, overwrite: bool
) -> hl.MatrixTable:
    """
    Create a new MatrixTable suitable for PCA analysis of relatedness
    and ancestry.
    * Strips unnesessary entry-level fields.
    * Keeps only bi-allelic SNPs.
    * Calls gnomad_methods's get_qc_mt() to filter rows further down based on:
      - the presence in problematic regions
      - callrate thresholds
      - MAF thresholds
      - inbreeding coefficient
      - allelic frequency thresholds
      - genotypes ADJ critetria (GQ>=20, DP>=10, AB>0.2 for hets)

    :param mt: input matrix table
    :param work_bucket: path to write checkpoints
    :param overwrite: overwrite checkpoints if they exist
    :return: matrix table with variants selected for PCA analysis
    """
    logger.info('Making MatrixTable for PCA analysis')
    mt = mt.select_entries(
        'END', 'LGT', GT=mt.LGT, adj=get_adj_expr(mt.LGT, mt.GQ, mt.DP, mt.LAD)
    )
    mt = mt.filter_rows(
        (hl.len(mt.alleles) == 2) & hl.is_snp(mt.alleles[0], mt.alleles[1])
    )
    mt = mt.naive_coalesce(5000)

    return get_qc_mt(
        mt,
        adj_only=False,
        min_af=0.0,
        min_inbreeding_coeff_threshold=-0.025,
        min_hardy_weinberg_threshold=None,
        ld_r2=None,
        filter_lcr=False,
        filter_decoy=False,
        filter_segdup=False,
    ).checkpoint(
        join(work_bucket, 'for_pca.mt'),
        overwrite=overwrite,
        _read_if_exists=not overwrite,
    )


def compute_relatedness(
    for_pca_mt: hl.MatrixTable,
    work_bucket: str,
    overwrite: bool = False,
) -> hl.Table:
    """
    :param for_pca_mt: variants selected for PCA analysis
    :param work_bucket: path to write checkpoints
    :param overwrite: overwrite checkpoints if they exist
    :return: table with the following structure:
    Row fields:
        'i': str
        'j': str
        'kin': float64
        'ibd0': float64
        'ibd1': float64
        'ibd2': float64
    ----------------------------------------
    Key: ['i', 'j']
    """
    logger.info('Running relatedness check')
    out_ht_path = join(work_bucket, 'relatedness.ht')
    if not overwrite and file_exists(out_ht_path):
        return hl.read_table(out_ht_path)

    sample_num = for_pca_mt.cols().count()

    _, scores, _ = hl.hwe_normalized_pca(
        for_pca_mt.GT, k=max(1, min(sample_num // 3, 10)), compute_loadings=False
    )
    scores = scores.checkpoint(
        join(work_bucket, 'relatedness_pca_scores.ht'),
        overwrite=overwrite,
        _read_if_exists=not overwrite,
    )
    relatedness_ht = hl.pc_relate(
        for_pca_mt.GT,
        min_individual_maf=0.01,
        scores_expr=scores[for_pca_mt.col_key].scores,
        block_size=4096,
        min_kinship=0.05,
        statistics='all',
    )

    # Converting keys for type struct{str} to str to align
    # with the rank_ht `s` key:
    relatedness_ht = relatedness_ht.key_by(i=relatedness_ht.i.s, j=relatedness_ht.j.s)
    relatedness_ht.write(out_ht_path, overwrite=True)
    return relatedness_ht


def run_pca_ancestry_analysis(
    for_pca_mt: hl.MatrixTable,
    sample_to_drop_ht: hl.Table,
    work_bucket: str,
    n_pcs: int,
    overwrite: bool = False,
) -> hl.Table:
    """
    :param for_pca_mt: variants usable for PCA analysis
        of the same type, identifying the pair of samples for each row
    :param work_bucket: bucket path to write checkpoints
    :param sample_to_drop_ht: table with samples to drop based on
        previous relatedness analysis. With a `rank` row field
    :param n_pcs: maximum number of principal components
    :param overwrite: overwrite checkpoints if they exist
    :return: a Hail table `scores_ht` with a row field:
        'scores': array<float64>
    """
    logger.info('Running PCA ancestry analysis')
    scores_ht_path = join(work_bucket, 'pop_pca_scores.ht')
    if not overwrite and file_exists(scores_ht_path):
        return hl.read_table(scores_ht_path)

    # Adjusting the number of principal components not to exceed the
    # number of samples
    n_pcs = min(n_pcs, for_pca_mt.cols().count() - sample_to_drop_ht.count())
    _, scores_ht, _ = run_pca_with_relateds(for_pca_mt, sample_to_drop_ht, n_pcs=n_pcs)
    scores_ht.write(scores_ht_path, overwrite=True)
    return scores_ht


def assign_pops(
    pop_pca_scores_ht: hl.Table,
    metadata_ht: pd.DataFrame,
    work_bucket: str,
    min_prob: float,
    max_mislabeled_training_samples: int = 50,
    overwrite: bool = False,
) -> hl.Table:
    """
    Take population PCA results and training data, and run random forest
    to assign global population labels.

    :param pop_pca_scores_ht: output table of `_run_pca_ancestry_analysis()`
        with a row field 'scores': array<float64>
    :param metadata_ht: table with a `population` field. Samples for which
        the latter is defined will be used to train the random forest
    :param work_bucket: bucket to write checkpoints and intermediate files
    :param min_prob: min probability of belonging to a given population
        for the population to be set (otherwise set to `None`)
    :param max_mislabeled_training_samples: keep rerunning until the number
        of mislabeled samples is below this number
    :param overwrite: overwrite checkpoints if they exist
    :return: a table with the following row fields, including `prob_<POP>`
        probabily fields for each population label:
        'training_pop': str
        'pca_scores': array<float64>
        'pop': str
        'prob_CEU': float64
        'prob_YRI': float64
        ... (prob_*: float64 for each population label)
    """
    logger.info('Assigning global population labels')

    samples_with_pop_ht = metadata_ht.filter(metadata_ht.population != '')
    pop_pca_scores_ht = pop_pca_scores_ht.annotate(
        training_pop=samples_with_pop_ht[pop_pca_scores_ht.key].population
    )

    def _run_assign_population_pcs(pop_pca_scores_ht, min_prob):
        examples_num = pop_pca_scores_ht.aggregate(
            hl.agg.count_where(hl.is_defined(pop_pca_scores_ht.training_pop))
        )
        logger.info(f'Running RF using {examples_num} training examples')
        pop_ht, pops_rf_model = assign_population_pcs(
            pop_pca_scores_ht,
            pc_cols=pop_pca_scores_ht.scores,
            known_col='training_pop',
            min_prob=min_prob,
        )
        n_mislabeled_samples = pop_ht.aggregate(
            hl.agg.count_where(pop_ht.training_pop != pop_ht.pop)
        )
        return pop_ht, pops_rf_model, n_mislabeled_samples

    pop_ht, pops_rf_model, n_mislabeled_samples = _run_assign_population_pcs(
        pop_pca_scores_ht, min_prob
    )
    while n_mislabeled_samples > max_mislabeled_training_samples:
        logger.info(
            f'Found {n_mislabeled_samples} samples '
            f'labeled differently from their known pop. '
            f'Re-running without them.'
        )

        pop_ht = pop_ht[pop_pca_scores_ht.key]
        pop_pca_scores_ht = pop_pca_scores_ht.annotate(
            training_pop=hl.or_missing(
                (pop_ht.training_pop == pop_ht.pop), pop_pca_scores_ht.training_pop
            )
        ).persist()

        pop_ht, pops_rf_model, n_mislabeled_samples = _run_assign_population_pcs(
            pop_pca_scores_ht, min_prob
        )

    pop_ht = pop_ht.checkpoint(
        join(work_bucket, 'pop.ht'), overwrite=overwrite, _read_if_exists=not overwrite
    )

    # Writing a tab delimited file indicating inferred sample populations
    pop_tsv_file = join(work_bucket, 'RF_pop_assignments.txt.gz')
    if overwrite or not file_exists(pop_tsv_file):
        pc_cnt = min(hl.min(10, hl.len(pop_ht.pca_scores)).collect())
        pop_ht.transmute(
            **{f'PC{i + 1}': pop_ht.pca_scores[i] for i in range(pc_cnt)}
        ).export(pop_tsv_file)

    # Writing the RF model used for inferring sample populations
    pop_rf_file = join(work_bucket, 'pop.RF_fit.pickle')
    if overwrite or not file_exists(pop_rf_file):
        with hl.hadoop_open(pop_rf_file, 'wb') as out:
            pickle.dump(pops_rf_model, out)

    return pop_ht


def compute_stratified_qc(
    sample_qc_ht: hl.Table,
    pop_ht: hl.Table,
    work_bucket: str,
    filtering_qc_metrics: List[str],
    overwrite: bool = False,
) -> hl.Table:
    """
    Computes median, MAD, and upper and lower thresholds for each metric
    in `filtering_qc_metrics`, groupped by `pop` field in `pop_ht`

    :param sample_qc_ht: table with a row field
        `sample_qc` = struct { n_snp: int64, n_singleton: int64, ... }
    :param pop_ht: table with a `pop` row field
    :param work_bucket: bucket to write checkpoints
    :param filtering_qc_metrics: metrics to annotate with
    :param overwrite: overwrite checkpoints if they exist
    :return: table with the following structure:
        Global fields:
            'qc_metrics_stats': dict<tuple (
                str
                ), struct {
                    n_snp: struct {
                        median: int64,
                        mad: float64,
                        lower: float64,
                        upper: float64
                    },
                    n_singleton: struct {
                        ...
                    },
                    ...
            }
        Row fields:
            'fail_n_snp': bool
            'fail_n_singleton': bool
            ...
            'qc_metrics_filters': set<str>
    """
    logger.info(
        f'Computing stratified QC metrics filters using '
        f'metrics: {", ".join(filtering_qc_metrics)}'
    )

    sample_qc_ht = sample_qc_ht.annotate(qc_pop=pop_ht[sample_qc_ht.key].pop)
    stratified_metrics_ht = compute_stratified_metrics_filter(
        sample_qc_ht,
        qc_metrics={
            metric: sample_qc_ht.sample_qc[metric] for metric in filtering_qc_metrics
        },
        strata={'qc_pop': sample_qc_ht.qc_pop},
        metric_threshold={'n_singleton': (4.0, 8.0)},
    )
    return stratified_metrics_ht.checkpoint(
        join(work_bucket, 'stratified_metrics.ht'),
        overwrite=overwrite,
        _read_if_exists=not overwrite,
    )


def flag_related_samples(
    hard_filtered_samples_ht: hl.Table,
    sex_ht: hl.Table,
    relatedness_ht: hl.Table,
    regressed_metrics_ht: Optional[hl.Table],
    work_bucket: str,
    kin_threshold: float,
    overwrite: bool = False,
) -> hl.Table:
    """
    Flag samples to drop based on relatedness, so the final set
    has only unrelated samples, best quality one per family

    :param hard_filtered_samples_ht: table with failed samples
        and a `hard_filters` row field
    :param sex_ht: table with a `chr20_mean_dp` row field
    :param relatedness_ht: table keyed by exactly two fields (i and j)
        of the same type, identifying the pair of samples for each row
    :param regressed_metrics_ht: optional table with a `qc_metrics_filters`
        field calculated with _apply_regressed_filters() from PCA scores
    :param work_bucket: bucket to write checkpoints
    :param kin_threshold: kinship threshold to call two samples as related
    :param overwrite: overwrite checkpoints if they exist
    :return: a table of the samples to drop along with their rank
        row field: 'rank': int64
    """
    label = 'final' if regressed_metrics_ht is not None else 'intermediate'
    logger.info(f'Flagging related samples to drop, {label}')
    out_ht_path = join(work_bucket, f'{label}_related_samples_to_drop.ht')
    if not overwrite and file_exists(out_ht_path):
        return hl.read_table(out_ht_path)

    rank_ht = _compute_sample_rankings(
        hard_filtered_samples_ht,
        sex_ht,
        use_qc_metrics_filters=regressed_metrics_ht is not None,
        regressed_metrics_ht=regressed_metrics_ht,
    ).checkpoint(
        join(work_bucket, f'{label}_samples_rankings.ht'),
        overwrite=overwrite,
        _read_if_exists=not overwrite,
    )
    filtered_samples = hl.literal(
        rank_ht.aggregate(
            hl.agg.filter(rank_ht.filtered, hl.agg.collect_as_set(rank_ht.s))
        )
    )
    samples_to_drop_ht = compute_related_samples_to_drop(
        relatedness_ht,
        rank_ht,
        kin_threshold=kin_threshold,
        filtered_samples=filtered_samples,
    )
    samples_to_drop_ht.write(out_ht_path, overwrite=True)
    return samples_to_drop_ht


def _compute_sample_rankings(
    hard_filtered_samples_ht: hl.Table,
    sex_ht: hl.Table,
    use_qc_metrics_filters: bool = False,
    regressed_metrics_ht: Optional[hl.Table] = None,
) -> hl.Table:
    """
    Orders samples by hard filters and coverage and adds rank,
    which is the lower the better.

    :param hard_filtered_samples_ht: table with failed samples
        and a `hard_filters` row field
    :param sex_ht: table with a `chr20_mean_dp` row field
    :param use_qc_metrics_filters: apply population-stratified QC filters
    :param regressed_metrics_ht: table with a `qc_metrics_filters` field.
        Used only if `use_qc_metrics_filters` is True.
    :return: table ordered by rank, with the following row fields:
        `rank`, `filtered`
    """
    ht = sex_ht.drop(*list(sex_ht.globals.dtype.keys()))
    ht = ht.select(
        'chr20_mean_dp',
        filtered=hl.or_else(
            hl.len(hard_filtered_samples_ht[ht.key].hard_filters) > 0, False
        ),
    )
    if use_qc_metrics_filters and regressed_metrics_ht is not None:
        ht = ht.annotate(
            filtered=hl.cond(
                ht.filtered,
                True,
                hl.or_else(
                    hl.len(regressed_metrics_ht[ht.key].qc_metrics_filters) > 0, False
                ),
            )
        )

    ht = ht.order_by(ht.filtered, hl.desc(ht.chr20_mean_dp)).add_index(name='rank')
    return ht.key_by('s').select('filtered', 'rank')


def apply_regressed_filters(
    sample_qc_ht: hl.Table,
    pop_pca_scores_ht: hl.Table,
    work_bucket: str,
    overwrite: bool = False,
) -> hl.Table:
    """
    Re-compute QC metrics (with hl.sample_qc() - like n_snp, r_het_hom)
    per population, and adding "fail_*" row fields when a metric is below
    the the lower MAD threshold or higher the upper MAD threshold
    (see `compute_stratified_metrics_filter` for defaults)

    :param sample_qc_ht: table with a row field
       `bi_allelic_sample_qc` =
          struct { n_snp: int64, n_singleton: int64, ... }
    :param pop_pca_scores_ht: table with a `scores` row field
    :param work_bucket: bucket to write checkpoints
    :param filtering_qc_metrics: metrics to annotate with
    :param overwrite: overwrite checkpoints if they exist
    :return: a table with the folliwing structure:
        Global fields:
            'lms': struct {
                n_snp: struct {
                    beta: array<float64>,
                    standard_error: array<float64>,
                    t_stat: array<float64>,
                    p_value: array<float64>,
                    multiple_standard_error: float64,
                    multiple_r_squared: float64,
                    adjusted_r_squared: float64,
                    f_stat: float64,
                    multiple_p_value: float64,
                    n: int32
                },
                n_singleton: struct {
                    ...
                },
                ...
            }
            'qc_metrics_stats': struct {
                n_snp_residual: struct {
                    median: float64,
                    mad: float64,
                    lower: float64,
                    upper: float64
                },
                n_singleton_residual: struct {
                    ...
                },
                ...
            }
        Row fields:
            's': str
            'n_snp_residual': float64
            'n_singleton_residual': float64
            ...
            'fail_n_snp_residual': bool
            'fail_n_singleton_residual': bool
            ...
            'qc_metrics_filters': set<str>
    """
    logger.info('Compute QC metrics adjusted for popopulation')

    sample_qc_ht = sample_qc_ht.select(
        **sample_qc_ht.bi_allelic_sample_qc,
        **pop_pca_scores_ht[sample_qc_ht.key],
        releasable=hl.bool(True),
    )

    filtering_qc_metrics = [
        'n_snp',
        'n_singleton',
        'r_ti_tv',
        'r_insertion_deletion',
        'n_insertion',
        'n_deletion',
        'r_het_hom_var',
        'n_het',
        'n_hom_var',
        'n_transition',
        'n_transversion',
    ]
    residuals_ht = compute_qc_metrics_residuals(
        ht=sample_qc_ht,
        pc_scores=sample_qc_ht.scores,
        qc_metrics={metric: sample_qc_ht[metric] for metric in filtering_qc_metrics},
        regression_sample_inclusion_expr=sample_qc_ht.releasable,
    )

    stratified_metrics_ht = compute_stratified_metrics_filter(
        ht=residuals_ht,
        qc_metrics=dict(residuals_ht.row_value),
        metric_threshold={'n_singleton_residual': (4.0, 8.0)},
    )

    residuals_ht = residuals_ht.annotate(**stratified_metrics_ht[residuals_ht.key])
    residuals_ht = residuals_ht.annotate_globals(
        **stratified_metrics_ht.index_globals()
    )

    return residuals_ht.checkpoint(
        join(work_bucket, 'regressed_metrics.ht'),
        overwrite=overwrite,
        _read_if_exists=not overwrite,
    )
