from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pandas as pd

from assettrack.sector_analysis import assess_sector_composite


SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_sector_two_week_market_samples.py"
SPEC = importlib.util.spec_from_file_location("two_week_samples", SCRIPT)
assert SPEC and SPEC.loader
two_week = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = two_week
SPEC.loader.exec_module(two_week)


def test_composite_matches_live_two_up_zero_down_rule(monkeypatch) -> None:
    monkeypatch.setattr(two_week.samples, "current_consensus_signal", lambda *_: 1)
    monkeypatch.setattr(
        two_week.samples, "relative_momentum_breadth_signal", lambda *_: None
    )
    monkeypatch.setattr(two_week.samples, "sma_5_150_signal", lambda *_: 1)
    monkeypatch.setattr(two_week, "sma_5_20_signal", lambda *_: None)

    row = two_week.composite_stance("CPU", 300, {})
    assert row["status"] == "bullish_candidate"
    assert row["prediction"] == 1
    assert row["up_votes"] == 2
    assert row["down_votes"] == 0


def test_two_down_votes_are_risk_alert_not_a_short(monkeypatch) -> None:
    monkeypatch.setattr(two_week.samples, "current_consensus_signal", lambda *_: -1)
    monkeypatch.setattr(
        two_week.samples, "relative_momentum_breadth_signal", lambda *_: -1
    )
    monkeypatch.setattr(two_week.samples, "sma_5_150_signal", lambda *_: None)
    monkeypatch.setattr(two_week, "sma_5_20_signal", lambda *_: -1)

    row = two_week.composite_stance("Memory", 300, {})
    assert row["status"] == "risk_alert"
    assert row["prediction"] == -1
    live = assess_sector_composite(
        {"Memory": {"ready": True, "direction": "down"}},
        {"Memory": {
            "ready": True, "direction": "down",
            "trend_ready": False, "trend_direction": "none",
            "fast_trend_ready": True, "fast_trend_direction": "down",
        }},
    )
    assert live["Memory"]["status"] == "risk_alert"


def test_two_up_with_one_down_still_abstains(monkeypatch) -> None:
    monkeypatch.setattr(two_week.samples, "current_consensus_signal", lambda *_: 1)
    monkeypatch.setattr(
        two_week.samples, "relative_momentum_breadth_signal", lambda *_: 1
    )
    monkeypatch.setattr(two_week.samples, "sma_5_150_signal", lambda *_: -1)
    monkeypatch.setattr(two_week, "sma_5_20_signal", lambda *_: None)

    row = two_week.composite_stance("CPU", 300, {})
    assert row["status"] == "abstain"
    assert row["prediction"] is None


def test_sample_windows_are_non_overlapping_and_prefer_large_moves() -> None:
    windows = [
        {
            "index": 1000 + offset * 10,
            "abs_qqq_return": magnitude,
            "qqq_return": magnitude,
        }
        for offset, magnitude in enumerate(
            [0.01, 0.08, 0.02, 0.09, 0.015, 0.07, 0.03, 0.11, 0.012, 0.06,
             0.04, 0.10, 0.013, 0.05, 0.014, 0.12]
        )
    ]
    sampled = two_week.sample_windows(windows, n=8, seed=20260817)
    indexes = [row["index"] for row in sampled]
    eligible = two_week.eligible_move_windows(windows)
    assert len(eligible) == 8
    assert len(sampled) == 8
    assert indexes == sorted(indexes)
    assert all(b - a >= 10 for a, b in zip(indexes, indexes[1:]))
    assert {row["abs_qqq_return"] for row in sampled} <= {
        row["abs_qqq_return"] for row in eligible
    }

    filled = two_week.sample_windows(windows, n=10, seed=20260817)
    assert len(filled) == 10
    extra = sorted(
        row["abs_qqq_return"]
        for row in filled
        if row["abs_qqq_return"] not in {item["abs_qqq_return"] for item in eligible}
    )
    assert extra == [0.03, 0.04]


def test_candidate_windows_walk_back_from_last_complete_horizon() -> None:
    sessions = pd.date_range("2024-01-02", periods=400, freq="B")
    context = {"sessions": sessions}
    indexes = two_week.candidate_signal_indexes(context, "2025-01-01")
    assert indexes
    assert indexes[-1] == 400 - 1 - 10
    assert all(b - a == 10 for a, b in zip(indexes, indexes[1:]))
    assert all(sessions[i] >= pd.Timestamp("2025-01-01") for i in indexes)
