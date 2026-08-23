from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pandas as pd


SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_sector_algorithm_samples_yfinance.py"
SPEC = importlib.util.spec_from_file_location("sector_samples", SCRIPT)
assert SPEC and SPEC.loader
sector_samples = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sector_samples
SPEC.loader.exec_module(sector_samples)


def _context(daily_values: list[list[float]]) -> dict:
    sessions = pd.date_range("2025-01-02", periods=len(daily_values), freq="B")
    symbols = ["A", "B", "C", "D"]
    returns = pd.DataFrame(daily_values, index=sessions, columns=symbols)
    prices = (1.0 + returns).cumprod() * 100.0
    prices["QQQ"] = 100.0
    return {
        "groups": {"G": symbols},
        "sessions": sessions,
        "prices": prices,
        "daily_returns": returns.assign(QQQ=0.0),
    }


def test_current_consensus_matches_three_of_five_rule() -> None:
    context = _context([
        [0.02, 0.01, 0.01, -0.001],
        [0.02, 0.01, 0.01, -0.001],
        [-0.01, 0.01, 0.00, 0.00],
        [0.02, 0.01, 0.01, -0.001],
        [-0.01, 0.01, 0.00, 0.00],
        [0.02, 0.01, 0.01, -0.001],
    ])

    assert sector_samples.current_consensus_signal("G", 5, context) == 1


def test_current_consensus_abstains_when_only_two_days_agree() -> None:
    context = _context([
        [0.02, 0.01, 0.01, -0.001],
        [0.02, 0.01, 0.01, -0.001],
        [-0.01, 0.01, 0.00, 0.00],
        [-0.01, 0.01, 0.00, 0.00],
        [-0.01, 0.01, 0.00, 0.00],
        [0.00, 0.00, 0.00, 0.00],
    ])

    assert sector_samples.current_consensus_signal("G", 5, context) is None


def test_fixed_anchors_never_overlap_across_year_boundary() -> None:
    sessions = pd.date_range("2024-07-01", "2025-10-01", freq="B")
    anchors = (
        sector_samples._fixed_anchors(sessions, "2024-08-15", "2025-08-15")
        + sector_samples._fixed_anchors(sessions, "2025-08-15", "2026-08-15")
    )

    assert all(b - a >= sector_samples.HORIZON for a, b in zip(anchors, anchors[1:]))


def test_cross_sector_relative_momentum_selects_top_and_bottom_two() -> None:
    sessions = pd.date_range("2024-01-02", periods=300, freq="B")
    groups = {f"G{i}": [f"{i}{j}" for j in range(4)] for i in range(6)}
    prices = pd.DataFrame(index=sessions)
    for group_i, members in enumerate(groups.values()):
        daily = 1.0005 + group_i * 0.0002
        for symbol in members:
            prices[symbol] = [100.0 * (daily ** i) for i in range(len(sessions))]
    prices["QQQ"] = 100.0
    context = {
        "groups": groups,
        "sessions": sessions,
        "prices": prices,
        "daily_returns": prices.pct_change(fill_method=None),
    }

    assert sector_samples.cross_sector_relative_momentum_signal("G5", 299, context) == 1
    assert sector_samples.cross_sector_relative_momentum_signal("G0", 299, context) == -1
    assert sector_samples.cross_sector_relative_momentum_signal("G3", 299, context) is None


def test_agreement_signal_requires_both_methods_and_same_direction(monkeypatch) -> None:
    monkeypatch.setattr(sector_samples, "current_consensus_signal", lambda *_: 1)
    monkeypatch.setattr(sector_samples, "relative_momentum_breadth_signal", lambda *_: 1)
    assert sector_samples.agreement_signal("G", 300, {}) == 1

    monkeypatch.setattr(sector_samples, "relative_momentum_breadth_signal", lambda *_: -1)
    assert sector_samples.agreement_signal("G", 300, {}) is None

    monkeypatch.setattr(sector_samples, "relative_momentum_breadth_signal", lambda *_: None)
    assert sector_samples.agreement_signal("G", 300, {}) is None


def test_two_of_three_bullish_signal_requires_two_up_and_zero_down(monkeypatch) -> None:
    monkeypatch.setattr(sector_samples, "current_consensus_signal", lambda *_: 1)
    monkeypatch.setattr(
        sector_samples, "relative_momentum_breadth_signal", lambda *_: None
    )
    monkeypatch.setattr(sector_samples, "sma_5_150_signal", lambda *_: 1)
    assert sector_samples.two_of_three_bullish_signal("G", 300, {}) == 1

    monkeypatch.setattr(
        sector_samples, "relative_momentum_breadth_signal", lambda *_: -1
    )
    assert sector_samples.two_of_three_bullish_signal("G", 300, {}) is None

    monkeypatch.setattr(sector_samples, "current_consensus_signal", lambda *_: -1)
    monkeypatch.setattr(sector_samples, "sma_5_150_signal", lambda *_: -1)
    assert sector_samples.two_of_three_bullish_signal("G", 300, {}) is None
