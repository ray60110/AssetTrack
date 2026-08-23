from __future__ import annotations

import inspect

from assettrack import storage


def test_research_and_truth_history_use_approved_two_year_retention():
    assert storage.ANALYSIS_CACHE_RETENTION_DAYS == 730
    assert storage.BENCHMARK_TRUTH_RETENTION_DAYS == 730


def test_all_snapshot_pruners_default_to_the_shared_retention_policy():
    for pruner in (
        storage.cleanup_old_etf_caches,
        storage.prune_etf_history,
        storage.prune_options_history,
        storage.prune_sector_history,
    ):
        default = inspect.signature(pruner).parameters["max_age_days"].default
        assert default == storage.ANALYSIS_CACHE_RETENTION_DAYS
