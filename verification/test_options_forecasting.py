from __future__ import annotations

from assettrack.options_forecasting import (
    assess_option_forecast,
    build_probability_backtest,
    purge_overlapping_records,
)


def _record(entry: int, outcome: int, *, hit: bool = True, direction: str = "up") -> dict:
    return {
        "underlying": "TEST",
        "date": f"2026-01-{entry + 1:02d}",
        "outcome_date": f"2026-01-{outcome + 1:02d}",
        "entry_index": entry,
        "outcome_index": outcome,
        "h": 5,
        "dir": direction,
        "hit": hit,
        "forward_return": 0.01 if hit == (direction == "up") else -0.01,
    }


def _baseline(entry: int, outcome: int, *, up: bool = True) -> dict:
    return {
        "underlying": "TEST",
        "date": f"2026-01-{entry + 1:02d}",
        "outcome_date": f"2026-01-{outcome + 1:02d}",
        "entry_index": entry,
        "outcome_index": outcome,
        "h": 5,
        "outcome_up": up,
        "forward_return": 0.01 if up else -0.01,
    }


def test_purge_removes_overlapping_forward_windows():
    rows = [_record(entry, entry + 5) for entry in range(8)]

    purged = purge_overlapping_records(rows)

    assert [(row["entry_index"], row["outcome_index"]) for row in purged] == [(0, 5), (6, 11)]


def test_probability_backtest_reports_raw_and_purged_counts():
    signals = [_record(entry, entry + 5, hit=entry % 2 == 0) for entry in range(8)]
    baseline = [_baseline(entry, entry + 5, up=entry % 2 == 0) for entry in range(8)]

    report = build_probability_backtest(signals, baseline, min_samples=3)
    metric = report["by_horizon"][5]["up"]

    assert metric["raw_n"] == 8
    assert metric["n"] == 2
    assert metric["ready"] is False
    assert 0.0 < metric["next_probability"] < 1.0
    assert metric["brier_score"] is not None


def test_insufficient_backtest_tells_user_not_to_tune():
    report = {
        "probability_backtest": {
            "min_samples": 20,
            "by_horizon": {
                5: {
                    "up": {
                        "n": 3,
                        "raw_n": 18,
                        "next_probability": 0.70,
                        "baseline_probability": 0.50,
                        "hit_rate": 0.67,
                        "baseline_hit_rate": 0.50,
                        "brier_skill": 0.05,
                    }
                }
            },
        },
        "by_horizon": {5: {"significance": {"up": {"significant_adj": True}}}},
        "stability": {"consistent": True},
        "model_health": {"status": "healthy"},
    }

    assessment = assess_option_forecast("多", report)

    assert assessment.status == "collecting"
    assert assessment.actionable_direction is None
    assert "不要調參" in assessment.modification_guidance
    assert "17" in assessment.modification_guidance


def test_failed_proper_score_gives_bounded_candidate_change():
    report = {
        "probability_backtest": {
            "min_samples": 20,
            "by_horizon": {
                5: {
                    "down": {
                        "n": 24,
                        "raw_n": 100,
                        "next_probability": 0.72,
                        "baseline_probability": 0.48,
                        "hit_rate": 0.45,
                        "baseline_hit_rate": 0.52,
                        "brier_skill": -0.12,
                    }
                }
            },
        },
        "by_horizon": {5: {"significance": {"down": {"significant_adj": False}}}},
        "stability": {"consistent": False},
        "model_health": {"status": "warning"},
    }

    assessment = assess_option_forecast(
        "空",
        report,
        verdict_params={"bias_min_pct": 0.03},
    )

    assert assessment.status == "negative_skill"
    assert assessment.actionable_direction is None
    assert "0.03%" in assessment.modification_guidance
    assert "0.05%" in assessment.modification_guidance
    assert "Brier skill" in assessment.modification_guidance


def test_only_fully_validated_probability_becomes_actionable():
    report = {
        "probability_backtest": {
            "min_samples": 20,
            "by_horizon": {
                5: {
                    "up": {
                        "n": 30,
                        "raw_n": 120,
                        "next_probability": 0.68,
                        "baseline_probability": 0.51,
                        "hit_rate": 0.70,
                        "baseline_hit_rate": 0.51,
                        "brier_skill": 0.09,
                    }
                }
            },
        },
        "by_horizon": {5: {"significance": {"up": {"significant_adj": True}}}},
        "stability": {"consistent": True},
        "model_health": {"by_direction": {"up": {"status": "healthy"}}},
    }

    assessment = assess_option_forecast("多", report)

    assert assessment.validated
    assert assessment.actionable_direction == "多"
    assert "校準機率 68%" in assessment.summary
    assert "purged n=30" in assessment.summary


def test_missing_significance_or_stability_never_becomes_actionable():
    report = {
        "probability_backtest": {
            "min_samples": 20,
            "by_horizon": {
                5: {
                    "up": {
                        "n": 30,
                        "raw_n": 100,
                        "next_probability": 0.70,
                        "baseline_probability": 0.50,
                        "hit_rate": 0.70,
                        "baseline_hit_rate": 0.50,
                        "brier_skill": 0.10,
                    }
                }
            },
        },
        "by_horizon": {5: {"significance": {"up": None}}},
        "stability": {"consistent": None},
        "model_health": {"status": "healthy"},
    }

    assessment = assess_option_forecast("多", report)

    assert not assessment.validated
    assert "顯著性" in assessment.diagnosis
    assert "穩定性" in assessment.diagnosis
