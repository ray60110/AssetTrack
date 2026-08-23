from __future__ import annotations

from assettrack.sector_analysis import (
    generate_sector_conclusions,
    generate_sector_recommendations,
    generate_sector_risk_warnings,
)


def _flow(direction: str = "up") -> dict:
    return {
        "ready": True,
        "direction": direction,
        "up_days": 3 if direction == "up" else 0,
        "down_days": 3 if direction == "down" else 0,
        "days_evaluated": 5,
        "latest_capw": 1.25 if direction == "up" else -1.25,
    }


def _confirmation(
    direction: str = "up",
    trend_direction: str = "up",
    fast_trend_direction: str = "none",
) -> dict:
    sma5 = 125.0
    sma20 = 120.0 if fast_trend_direction == "up" else 130.0 if fast_trend_direction == "down" else 125.0
    sma150 = 110.0 if trend_direction == "up" else 140.0 if trend_direction == "down" else 125.0
    return {
        "ready": True,
        "direction": direction,
        "rank_direction": direction,
        "pct_above_50ma": 0.75 if direction == "up" else 0.25,
        "trend_ready": trend_direction in ("up", "down"),
        "trend_direction": trend_direction,
        "fast_trend_ready": fast_trend_direction in ("up", "down"),
        "fast_trend_direction": fast_trend_direction,
        "sma5": sma5,
        "sma20": sma20,
        "sma150": sma150,
        "as_of": "2026-08-14",
    }


def test_two_of_three_up_votes_forecast_a_two_week_rise() -> None:
    recs = generate_sector_recommendations(
        {"CPU": _flow("up")},
        confirmations={"CPU": _confirmation("none", "up")},
    )

    assert len(recs) == 1
    assert recs[0].verdict == "📈 【預測】CPU：未來 10 個交易日（約兩週）上漲"
    assert recs[0].direction == "多"
    assert "2-of-3" in recs[0].basis
    lines = generate_sector_conclusions(
        {"CPU": _flow("up")},
        confirmations={"CPU": _confirmation("none", "up")},
    )
    assert any("未來 10 個交易日（約兩週）上漲" in line for line in lines)


def test_two_up_votes_with_one_down_conflict_abstain() -> None:
    recs = generate_sector_recommendations(
        {"CPU": _flow("up")},
        confirmations={"CPU": _confirmation("up", "down")},
    )

    assert recs == []


def test_fewer_than_two_up_votes_abstain() -> None:
    flow = {"CPU": _flow("up")}

    assert generate_sector_recommendations(flow, confirmations={}) == []
    assert generate_sector_recommendations(
        flow, confirmations={"CPU": _confirmation("none", "none")}
    ) == []


def test_lagging_down_votes_without_fast_trend_do_not_warn() -> None:
    recs = generate_sector_recommendations(
        {"Memory": _flow("down")},
        confirmations={"Memory": _confirmation("down", "down")},
    )
    warnings = generate_sector_risk_warnings(
        {"Memory": _flow("down")},
        confirmations={"Memory": _confirmation("down", "down")},
    )

    assert recs == []
    assert warnings == []


def test_breadth_down_and_sma5_below_sma20_forecasts_a_two_week_fall() -> None:
    recs = generate_sector_recommendations(
        {"Memory": _flow("down")},
        confirmations={"Memory": _confirmation("none", "none", "down")},
    )
    warnings = generate_sector_risk_warnings(
        {"Memory": _flow("down")},
        confirmations={"Memory": _confirmation("none", "none", "down")},
    )
    lines = generate_sector_conclusions(
        {"Memory": _flow("down")},
        confirmations={"Memory": _confirmation("none", "none", "down")},
    )

    assert len(recs) == 1
    assert recs[0].direction == "空"
    assert recs[0].verdict == "📉 【預測】Memory：未來 10 個交易日（約兩週）下跌"
    assert "放空" in recs[0].basis
    assert warnings == [recs[0].verdict]
    assert any("未來 10 個交易日（約兩週）下跌" in line for line in lines)
