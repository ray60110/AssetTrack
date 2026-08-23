from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd


SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_sector_consensus_yfinance.py"
SPEC = importlib.util.spec_from_file_location("sector_validation", SCRIPT)
assert SPEC and SPEC.loader
validation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validation
SPEC.loader.exec_module(validation)


def test_exact_forward_return_uses_future_trading_rows_not_calendar_days():
    index = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-09"])
    returns = pd.Series([1.0, 2.0, 3.0, 4.0], index=index)

    result = validation.exact_forward_return(returns, 2)

    assert round(result.iloc[0], 6) == round((1.02 * 1.03 - 1.0) * 100.0, 6)
    assert round(result.iloc[1], 6) == round((1.03 * 1.04 - 1.0) * 100.0, 6)
    assert pd.isna(result.iloc[2])


def test_group_panel_reproduces_three_of_five_persistence_rule():
    index = pd.bdate_range("2026-01-05", periods=7)
    members = ["A", "B", "C", "D"]
    closes = pd.DataFrame(index=index, columns=members, dtype=float)
    closes.iloc[0] = [100.0] * 4
    daily_moves = [
        [1.0, 1.0, 1.0, -0.1],
        [1.0, 1.0, 1.0, -0.1],
        [-1.0, -1.0, -1.0, 0.1],
        [1.0, 1.0, 1.0, -0.1],
        [-1.0, -1.0, -1.0, 0.1],
        [-1.0, -1.0, -1.0, 0.1],
    ]
    for row, moves in enumerate(daily_moves, start=1):
        closes.iloc[row] = [closes.iloc[row - 1, col] * (1 + move / 100) for col, move in enumerate(moves)]

    panel = validation.build_group_panel(
        closes,
        members,
        index,
        lookback=5,
        min_days=3,
        breadth_threshold=0.5,
        return_threshold_pct=0.1,
        min_coverage=0.70,
    )

    assert panel.iloc[4]["direction"] == "up"
    assert panel.iloc[5]["direction"] == "up"
    assert panel.iloc[6]["direction"] == "down"


def test_group_panel_abstains_when_constituent_coverage_is_too_low():
    index = pd.bdate_range("2026-01-05", periods=5)
    closes = pd.DataFrame(
        {
            "A": [100, 101, 102, 103, 104],
            "B": [100, 101, 102, 103, 104],
            "C": [None, None, None, None, None],
            "D": [None, None, None, None, None],
            "E": [None, None, None, None, None],
        },
        index=index,
    )

    panel = validation.build_group_panel(
        closes,
        list(closes.columns),
        index,
        lookback=5,
        min_days=3,
        breadth_threshold=0.5,
        return_threshold_pct=0.1,
        min_coverage=0.70,
    )

    assert set(panel["direction"]) == {"none"}
    assert panel["basket_return_pct"].isna().all()


def test_overlap_purge_keeps_non_overlapping_forward_windows():
    clusters = [
        {"session_position": position, "date": f"d{position}"}
        for position in (1, 2, 5, 6, 10)
    ]

    selected = validation._purge_overlapping(clusters, horizon=5)

    assert [row["session_position"] for row in selected] == [1, 6]


def test_poisson_binomial_tail_keeps_each_clusters_matched_baseline():
    # P(at least two hits) = P(exactly two) + P(three)
    # = .1*.2*.3 + .1*.2*.7 + .1*.8*.3 + .9*.2*.3 = .098
    result = validation._poisson_binomial_sf(2, [0.1, 0.2, 0.3])

    assert round(result, 6) == 0.098


def test_primary_episode_gate_requires_positive_return_and_momentum_improvement():
    spec = validation.ValidationSpec(
        start="2016-01-01",
        end_exclusive="2026-01-01",
        horizons=(1, 5, 10),
        benchmarks=("QQQ", "SPY"),
        lookback=5,
        min_days=3,
        breadth_threshold=0.5,
        return_threshold_pct=0.1,
        min_coverage=0.7,
        cost_bps=10.0,
        bootstrap_samples=500,
        seed=7,
        repair=False,
    )
    blocks = [
        {
            "date": f"2025-01-{(i % 28) + 1:02d}",
            "signed_return_net_pct": 0.5,
            "signal_vs_momentum_pct": 0.2,
            "up_n": 1,
            "down_n": 0,
        }
        for i in range(60)
    ]

    passed = validation._summarise_primary_blocks(blocks, spec)
    failed = validation._summarise_primary_blocks(
        [{**row, "signal_vs_momentum_pct": -0.2} for row in blocks], spec
    )

    assert passed["passed"] is True
    assert failed["passed"] is False
