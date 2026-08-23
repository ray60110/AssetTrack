"""Option richness is vol/premium vs realized vol — not stock direction."""
from __future__ import annotations

import math
from datetime import date, timedelta

from assettrack.greeks import bs_price
from assettrack.options_valuation import (
    EARNINGS_NOTE_MAX_DAYS,
    FAIR_VOL_BAND,
    RICHNESS_HISTORY_DAYS,
    RV_WINDOW,
    annualized_realized_vol,
    assess_contract_richness,
    assess_option_richness,
    days_to_earnings,
    earnings_remaining_note,
    format_richness_history,
    invert_contract_iv_series,
    richness_series,
    rolling_realized_vol,
)


def _atm_snapshot(
    *,
    call_mid: float,
    put_mid: float,
    iv: float,
    spot: float = 100.0,
    as_of: str = "2026-08-17",
    earnings_date: str | None = None,
) -> dict:
    expiry = (date.fromisoformat(as_of) + timedelta(days=30)).isoformat()
    record = {
        "date": as_of,
        "spot_price": spot,
        "contracts": [
            {
                "type": "call",
                "strike": 100.0,
                "expiry": expiry,
                "bid": call_mid - 0.05,
                "ask": call_mid + 0.05,
                "impliedVolatility": iv,
            },
            {
                "type": "put",
                "strike": 100.0,
                "expiry": expiry,
                "bid": put_mid - 0.05,
                "ask": put_mid + 0.05,
                "impliedVolatility": iv,
            },
        ],
    }
    if earnings_date:
        record["earnings_date"] = earnings_date
    return record


def _alternating_closes(end: date, n_returns: int = 10) -> list[tuple[str, float]]:
    prices = [100.0]
    sign = 1.0
    for _ in range(n_returns):
        prices.append(prices[-1] * math.exp(sign * 0.02))
        sign *= -1.0
    dates = [end - timedelta(days=n_returns - i) for i in range(n_returns + 1)]
    return [(d.isoformat(), px) for d, px in zip(dates, prices)]


def test_realized_vol_from_alternating_returns() -> None:
    # 10 log-returns of ±2%: sample std = 0.02, annualized = 0.02 * sqrt(252).
    closes = [100.0]
    sign = 1.0
    for _ in range(10):
        closes.append(closes[-1] * math.exp(sign * 0.02))
        sign *= -1.0
    rv = annualized_realized_vol(closes, min_returns=10)
    assert rv is not None
    # Sample std uses n-1; ten ±2% returns → 0.02 * sqrt(10/9).
    expected = 0.02 * math.sqrt(10 / 9) * math.sqrt(252)
    assert abs(rv - expected) < 1e-9


def test_realized_vol_not_ready_with_short_series() -> None:
    assert annualized_realized_vol([100.0, 101.0, 99.0], min_returns=10) is None


def test_high_iv_versus_low_rv_marks_atm_options_expensive() -> None:
    rv = 0.20
    snapshot = _atm_snapshot(call_mid=8.0, put_mid=8.0, iv=0.40)
    report = assess_option_richness(snapshot, realized_vol=rv, r=0.0)

    assert report["ready"] is True
    assert report["richness"] == "expensive"
    assert report["vol_spread"] == report["atm_iv"] - rv
    assert report["vol_spread"] > FAIR_VOL_BAND
    assert report["call"]["edge"] > 0
    assert report["put"]["edge"] > 0
    assert report["straddle_edge"] > 0
    assert "up" not in report["richness"]
    assert "down" not in report["richness"]


def test_low_iv_versus_high_rv_marks_atm_options_cheap() -> None:
    rv = 0.40
    snapshot = _atm_snapshot(call_mid=2.5, put_mid=2.5, iv=0.20)
    report = assess_option_richness(snapshot, realized_vol=rv, r=0.0)

    assert report["ready"] is True
    assert report["richness"] == "cheap"
    assert report["call"]["edge"] < 0
    assert report["put"]["edge"] < 0
    assert report["straddle_edge"] < 0


def test_call_can_be_expensive_while_put_is_cheap() -> None:
    rv = 0.25
    model = bs_price(100.0, 100.0, 30, rv, "call", r=0.0)
    assert model is not None
    snapshot = _atm_snapshot(call_mid=model + 1.50, put_mid=max(0.15, model - 0.80), iv=0.25)
    report = assess_option_richness(snapshot, realized_vol=rv, r=0.0)

    assert report["ready"] is True
    assert report["call"]["label"] == "expensive"
    assert report["put"]["label"] == "cheap"


def test_missing_realized_vol_is_not_ready() -> None:
    snapshot = _atm_snapshot(call_mid=5.0, put_mid=5.0, iv=0.30)
    report = assess_option_richness(snapshot, realized_vol=None, r=0.0)
    assert report["ready"] is False
    assert report["richness"] == "unknown"
    assert report["reason"] == "realized_vol_unavailable"


def test_held_contract_edge_uses_greeks_vol_gap() -> None:
    rv = 0.20
    market = 6.0
    out = assess_contract_richness(
        spot=100.0,
        strike=100.0,
        dte_days=30,
        option_type="call",
        market_price=market,
        realized_vol=rv,
        r=0.0,
    )
    assert out["ready"] is True
    assert out["edge"] > 0
    assert out["label"] == "expensive"
    assert out["vega"] is not None


def test_earnings_note_only_when_fewer_than_ten_days_remain() -> None:
    assert EARNINGS_NOTE_MAX_DAYS == 10
    assert days_to_earnings("2026-08-10", "2026-08-15") == 5
    assert days_to_earnings("2026-08-10", "2026-08-10") == 0
    assert days_to_earnings("2026-08-10", "2026-08-09") == -1
    assert days_to_earnings("2026-08-10", None) is None

    assert earnings_remaining_note(9) == "財報剩9天"
    assert earnings_remaining_note(1) == "財報剩1天"
    assert earnings_remaining_note(0) == "財報今日"
    assert earnings_remaining_note(10) is None
    assert earnings_remaining_note(-1) is None
    assert earnings_remaining_note(None) is None


def test_rolling_rv_uses_only_the_prior_twenty_sessions() -> None:
    assert RV_WINDOW == 20
    start = date(2026, 5, 1)
    quiet = [0.01 if i % 2 == 0 else -0.01 for i in range(20)]
    wild = [0.04 if i % 2 == 0 else -0.04 for i in range(20)]
    px = 100.0
    dated = [(start.isoformat(), px)]
    day = start
    for move in quiet + wild:
        day += timedelta(days=1)
        px *= math.exp(move)
        dated.append((day.isoformat(), px))
    end_quiet = start + timedelta(days=20)
    end_wild = start + timedelta(days=40)
    quiet_rv = rolling_realized_vol(dated, end_quiet.isoformat())
    wild_rv = rolling_realized_vol(dated, end_wild.isoformat())
    only_quiet = annualized_realized_vol([p for _, p in dated[:21]], min_returns=20, window=20)
    only_wild = annualized_realized_vol([p for _, p in dated[-21:]], min_returns=20, window=20)
    assert quiet_rv is not None and wild_rv is not None
    assert abs(quiet_rv - only_quiet) < 1e-9
    assert abs(wild_rv - only_wild) < 1e-9
    assert abs(wild_rv - quiet_rv) > 1e-6
    assert rolling_realized_vol(dated[:20], end_quiet.isoformat()) is None


def test_richness_series_does_not_use_future_closes_for_rv() -> None:
    as_of = date(2026, 8, 10)
    dated = _alternating_closes(as_of, n_returns=20)
    calm_prices = [px for _, px in dated]
    calm_rv = annualized_realized_vol(calm_prices, min_returns=20, window=20)
    assert calm_rv is not None

    spiked = dated + [((as_of + timedelta(days=1)).isoformat(), calm_prices[-1] * 1.50)]
    spiked_rv = annualized_realized_vol([px for _, px in spiked], min_returns=20, window=20)
    assert spiked_rv is not None
    assert abs(spiked_rv - calm_rv) > 1e-6

    points = richness_series(
        [_atm_snapshot(call_mid=8.0, put_mid=8.0, iv=0.50, as_of=as_of.isoformat())],
        dated_closes=spiked,
        r=0.0,
        as_of=as_of.isoformat(),
        window_days=0,
    )
    assert any(point["date"] == "2026-08-10" for point in points)
    point = next(item for item in points if item["date"] == "2026-08-10")
    assert abs(point["realized_vol"] - calm_rv) < 1e-9
    assert point["richness"] == "expensive"
    assert point["vol_spread"] == point["atm_iv"] - point["realized_vol"]


def test_richness_series_annotates_earnings_inside_ten_day_window() -> None:
    as_of = date(2026, 8, 10)
    dated = _alternating_closes(as_of, n_returns=20)
    near = _atm_snapshot(
        call_mid=8.0,
        put_mid=8.0,
        iv=0.50,
        as_of="2026-08-10",
        earnings_date="2026-08-15",
    )
    far = _atm_snapshot(
        call_mid=8.0,
        put_mid=8.0,
        iv=0.50,
        as_of="2026-08-10",
        earnings_date="2026-08-20",
    )
    near_points = richness_series([near], dated_closes=dated, r=0.0)
    far_points = richness_series([far], dated_closes=dated, r=0.0)
    near_row = next(item for item in near_points if item["date"] == "2026-08-10")
    far_row = next(item for item in far_points if item["date"] == "2026-08-10")
    assert near_row["days_to_earnings"] == 5
    assert near_row["earnings_note"] == "財報剩5天"
    assert far_row["days_to_earnings"] == 10
    assert far_row["earnings_note"] is None


def test_richness_history_view_lists_daily_iv_rv_and_earnings_note() -> None:
    points = [
        {
            "date": "2026-08-03",
            "atm_iv": 0.28,
            "realized_vol": 0.22,
            "vol_spread": 0.06,
            "richness": "expensive",
            "days_to_earnings": 4,
            "earnings_note": "財報剩4天",
        },
        {
            "date": "2026-08-10",
            "atm_iv": 0.18,
            "realized_vol": 0.24,
            "vol_spread": -0.06,
            "richness": "cheap",
            "days_to_earnings": None,
            "earnings_note": None,
        },
    ]
    text = format_richness_history("NVDA", points)
    assert "NVDA" in text
    assert "2026-08-03" in text
    assert "2026-08-10" in text
    assert "28%" in text
    assert "22%" in text
    assert "+6pp" in text
    assert "偏貴" in text
    assert "偏便宜" in text
    assert "財報剩4天" in text
    assert "看多" not in text
    assert "看空" not in text


def test_richness_series_drops_points_older_than_ninety_days() -> None:
    assert RICHNESS_HISTORY_DAYS == 90
    end = date(2026, 8, 23)
    too_old = end - timedelta(days=91)
    dated = _alternating_closes(too_old, n_returns=20) + _alternating_closes(end, n_returns=20)
    points = richness_series(
        [
            _atm_snapshot(call_mid=8.0, put_mid=8.0, iv=0.50, as_of=too_old.isoformat()),
            _atm_snapshot(call_mid=8.0, put_mid=8.0, iv=0.50, as_of=end.isoformat()),
        ],
        dated_closes=dated,
        r=0.0,
        as_of=end.isoformat(),
    )
    dates = [point["date"] for point in points]
    assert too_old.isoformat() not in dates
    assert end.isoformat() in dates


def test_richness_history_view_says_ninety_day_window() -> None:
    text = format_richness_history(
        "NVDA",
        [
            {
                "date": "2026-08-10",
                "atm_iv": 0.28,
                "realized_vol": 0.22,
                "vol_spread": 0.06,
                "richness": "expensive",
                "days_to_earnings": None,
                "earnings_note": None,
            }
        ],
    )
    assert "90天" in text


def test_prune_deletes_options_snapshots_older_than_ninety_days(tmp_path, monkeypatch) -> None:
    import json

    from assettrack import storage

    monkeypatch.setattr(storage, "get_data_dir", lambda: tmp_path)
    today = storage.taiwan_now().date()
    old = (today - timedelta(days=91)).isoformat()
    recent = today.isoformat()
    path = storage.get_options_history_dir() / "NVDA.jsonl"
    path.write_text(
        json.dumps({"date": old, "spot_price": 100, "contracts": []}) + "\n"
        + json.dumps({"date": recent, "spot_price": 101, "contracts": []}) + "\n"
    )
    storage.prune_options_history("NVDA", max_age_days=RICHNESS_HISTORY_DAYS)
    dates = [row["date"] for row in storage.load_options_daily_snapshots("NVDA")]
    assert old not in dates
    assert recent in dates


def test_richness_series_fills_ninety_calendar_days_from_closes() -> None:
    end = date(2026, 8, 21)
    start = end - timedelta(days=130)
    dated = []
    px = 100.0
    day = start
    sign = 1.0
    while day <= end:
        dated.append((day.isoformat(), px))
        px *= math.exp(sign * 0.01)
        sign *= -1.0
        day += timedelta(days=1)
    points = richness_series(
        [_atm_snapshot(call_mid=8.0, put_mid=8.0, iv=0.40, as_of=end.isoformat())],
        dated_closes=dated,
        r=0.0,
        as_of=end.isoformat(),
    )
    cutoff = (end - timedelta(days=90)).isoformat()
    assert len(points) >= 60
    assert points[0]["date"] >= cutoff
    assert points[-1]["date"] == end.isoformat()
    assert all(item["realized_vol"] is not None for item in points)
    assert any(item["atm_iv"] is not None for item in points)


def test_contract_history_inverts_to_the_input_vol() -> None:
    as_of = date(2026, 8, 10)
    expiry = "2026-09-09"
    strike = 100.0
    iv = 0.30
    dte = 30
    premium = bs_price(100.0, strike, dte, iv, "call", r=0.0)
    assert premium is not None
    spots = _alternating_closes(as_of, n_returns=1)
    # overwrite last spot to 100 so inversion matches
    spots[-1] = (as_of.isoformat(), 100.0)
    found = invert_contract_iv_series(
        spots,
        strike=strike,
        expiry=expiry,
        call_closes=[(as_of.isoformat(), premium)],
        put_closes=[],
        r=0.0,
    )
    assert as_of.isoformat() in found
    assert abs(found[as_of.isoformat()] - iv) < 0.01
