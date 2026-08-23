from __future__ import annotations

from datetime import date

import pytest

from assettrack.direction_forecast_validation import (
    ForecastRecord,
    ValidationInputError,
    ValidationSpec,
    validate,
)
from assettrack.market_sessions import NYSESessionCalendar


CAL = NYSESessionCalendar()

# 2026-01-02 is Friday; the next sessions are 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 20
# (19 Jan is MLK).
S = [
    date(2026, 1, 2),
    date(2026, 1, 5),
    date(2026, 1, 6),
    date(2026, 1, 7),
    date(2026, 1, 8),
    date(2026, 1, 9),
    date(2026, 1, 12),
    date(2026, 1, 13),
    date(2026, 1, 14),
    date(2026, 1, 15),
    date(2026, 1, 16),
    date(2026, 1, 20),
]


def _panel(symbol_closes: dict[str, list[float]]) -> dict[tuple[str, date], float]:
    prices: dict[tuple[str, date], float] = {}
    for symbol, closes in symbol_closes.items():
        assert len(closes) == len(S)
        for session, close in zip(S, closes):
            prices[(symbol, session)] = close
    return prices


def _claim(
    session: date,
    *,
    symbol: str = "AAA",
    horizon: int = 1,
    direction: str = "up",
    policy: str = "p1",
    probability_up: float | None = None,
) -> ForecastRecord:
    return ForecastRecord(
        policy_version_id=policy,
        outcome_target=symbol,
        entry_session=session,
        horizon_sessions=horizon,
        direction=direction,
        probability_up=probability_up,
    )


def _spec(**overrides) -> ValidationSpec:
    base = dict(
        horizons=(1, 5),
        primary_horizon=1,
        benchmarks=("QQQ",),
        cost_bps=0.0,
        min_independent_blocks=1,
        min_coverage=0.0,
        bootstrap_samples=200,
        seed=20260817,
        confidence=0.95,
    )
    base.update(overrides)
    return ValidationSpec(**base)


def test_exact_one_session_up_claim_hits_when_close_rises():
    prices = _panel({"AAA": [100] * 12, "QQQ": [100] * 12})
    prices[("AAA", S[1])] = 101.0
    prices[("QQQ", S[1])] = 100.0

    report = validate([_claim(S[0])], prices, _spec())

    matured = report.matured[0]
    assert matured.hit is True
    assert matured.asset_return == pytest.approx(0.01)
    assert matured.void_reason is None
    assert report.verdict == "PASS"


def test_missing_exact_settlement_session_is_void_not_next_bar():
    prices = _panel({"AAA": [100] * 12, "QQQ": [100] * 12})
    del prices[("AAA", S[1])]
    prices[("AAA", S[2])] = 120.0

    report = validate([_claim(S[0])], prices, _spec())

    assert report.matured[0].hit is None
    assert report.matured[0].void_reason == "missing_settlement_price"
    assert report.verdict == "UNDERPOWERED"


def test_overlap_purge_keeps_non_overlapping_five_session_windows():
    prices = _panel({
        "AAA": [100 + i for i in range(12)],
        "QQQ": [100] * 12,
    })
    claims = [
        _claim(S[0], horizon=5),
        _claim(S[1], horizon=5),
        _claim(S[6], horizon=5),
    ]

    report = validate(claims, prices, _spec(horizons=(5,), primary_horizon=5))

    kept = [row.forecast.entry_session for row in report.matured if row.hit is not None]
    assert kept == [S[0], S[6]]
    assert report.overall.independent_blocks == 2


def test_too_few_independent_blocks_is_underpowered_even_when_every_claim_hits():
    prices = _panel({"AAA": [100] * 12, "QQQ": [100] * 12})
    prices[("AAA", S[1])] = 110.0

    report = validate(
        [_claim(S[0])],
        prices,
        _spec(min_independent_blocks=30),
    )

    assert report.verdict == "UNDERPOWERED"
    assert report.overall.hit_rate == pytest.approx(1.0)
    assert "underpowered" in report.gate.failures


def test_weekend_entry_session_is_rejected():
    prices = {("AAA", S[0]): 100.0, ("QQQ", S[0]): 100.0}

    with pytest.raises(ValidationInputError) as err:
        validate([_claim(date(2026, 1, 3))], prices, _spec())

    assert err.value.code == "invalid_entry_session"


def test_mixed_policy_versions_are_rejected():
    prices = _panel({"AAA": [100] * 12, "QQQ": [100] * 12})

    with pytest.raises(ValidationInputError) as err:
        validate(
            [_claim(S[0], policy="a"), _claim(S[1], policy="b")],
            prices,
            _spec(),
        )

    assert err.value.code == "mixed_policy_version"


def test_direction_only_fail_when_signed_excess_is_below_floor():
    prices = _panel({"AAA": [100] * 12, "QQQ": [100] * 12})
    claims = []
    for i in range(0, 10, 2):
        prices[("AAA", S[i + 1])] = 90.0
        prices[("QQQ", S[i + 1])] = 100.0
        claims.append(_claim(S[i]))

    report = validate(
        claims,
        prices,
        _spec(min_independent_blocks=3, cost_bps=10.0),
    )

    assert report.scoring_mode == "direction_only"
    assert report.verdict == "FAIL"
    assert report.overall.mean_cost_adjusted_excess < 0
