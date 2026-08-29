"""Calendar screen: compact rows, EPS reaction, +3 trading-session move."""
from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from assettrack.quotes import fetch_earnings_reaction
from assettrack.shared import Recommendation, render_detail_recs
from assettrack.tui import (
    _CalEvent,
    _format_cpi_event_actuals,
    _format_earnings_reaction,
    _format_fed_event_actuals,
    _format_nfp_event_actuals,
    _grid_day_markup,
    _month_event_list,
    _month_heading,
    _render_monthly_calendar,
)


def _after_hours_dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 16, 0, tzinfo=ZoneInfo("America/New_York"))


def _pre_market_dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 8, 0, tzinfo=ZoneInfo("America/New_York"))


def _render(year: int, month: int, events: list[_CalEvent], today: date) -> str:
    from rich.console import Console

    console = Console(record=True, width=120, color_system=None)
    console.print(_render_monthly_calendar(year, month, events, today))
    return console.export_text()


def test_format_earnings_reaction_beat_with_surprise_and_end_date() -> None:
    text = _format_earnings_reaction(
        {
            "verdict": "beat",
            "surprise_pct": 20.0,
            "price_change_pct": 4.2,
            "price_end_date": date(2026, 7, 28),
        }
    )
    assert text == "EPS 擊敗 +20.0%；+4.2% →07-28"


def test_format_earnings_reaction_miss_without_surprise() -> None:
    text = _format_earnings_reaction(
        {
            "verdict": "miss",
            "price_change_pct": -2.1,
            "price_end_date": date(2026, 8, 12),
        }
    )
    assert text == "EPS 不如；-2.1% →08-12"


def test_format_earnings_reaction_omits_missing_parts() -> None:
    assert _format_earnings_reaction(None) == ""
    assert _format_earnings_reaction({"verdict": None, "price_change_pct": None}) == ""
    assert _format_earnings_reaction({"verdict": "meet"}) == "EPS 符合"
    assert (
        _format_earnings_reaction({"price_change_pct": 1.5, "price_end_date": date(2026, 8, 4)})
        == "+1.5% →08-04"
    )


def test_completed_events_group_by_date_on_one_visual_row() -> None:
    today = date(2026, 8, 27)
    events = [
        _CalEvent(
            date=date(2026, 8, 1),
            title="INTC",
            badge="SOX",
            when="盤後 16:00",
            completed=True,
            summary="EPS 擊敗 +4.2% →08-06",
            event_type="SOX",
        ),
        _CalEvent(
            date=date(2026, 8, 1),
            title="CPI",
            badge="",
            when="20:30",
            completed=True,
            summary="總指數 CPI 7月 YoY 2.85%（+0.15pp） MoM 0.20%（+0.10pp）",
            event_type="MACRO",
        ),
        _CalEvent(
            date=date(2026, 8, 29),
            title="FED",
            badge="",
            when="02:00",
            completed=False,
            summary="",
            event_type="MACRO",
        ),
    ]
    body = _render(2026, 8, events, today)
    assert "一 二 三 四 五 六 日" in body
    assert "月曆" in body
    assert "行事曆" in body
    assert body.count("08-01") == 1
    assert "INTC" in body
    assert "CPI" in body
    assert "EPS 擊敗" in body
    assert "總指數 CPI" in body
    assert "UTC+" not in body
    assert "待發生" not in body
    assert "↳ 更新" not in body
    assert "Revenue" not in body
    assert "○" in body
    assert "FED" in body
    week_line = next(
        line for line in body.splitlines() if "一 二 三 四 五 六 日" in line
    )
    assert "08-01" in week_line


def test_other_months_keep_the_left_calendar_when_expanded() -> None:
    today = date(2026, 8, 27)
    events = [
        _CalEvent(
            date=date(2026, 7, 23),
            title="INTC",
            badge="SOX",
            when="盤後 16:00",
            completed=True,
            summary="EPS 擊敗",
            event_type="SOX",
        ),
    ]
    body = _render(2026, 7, events, today)
    assert "一 二 三 四 五 六 日" in body
    assert "月曆" in body
    assert "行事曆" in body
    assert "INTC" in body
    week_line = next(
        line for line in body.splitlines() if "一 二 三 四 五 六 日" in line
    )
    assert "07-23" in week_line


def test_fetch_earnings_reaction_uses_third_trading_session_after_wednesday() -> None:
    # Wednesday after-hours: baseline Wed close, +3 sessions = Tuesday 7/28.
    earnings = pd.DataFrame(
        {
            "EPS Estimate": [1.00],
            "Reported EPS": [1.20],
            "Surprise(%)": [20.0],
        },
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-07-23 16:00:00", tz="America/New_York")],
            name="Earnings Date",
        ),
    )
    ticker = MagicMock()
    ticker.get_earnings_dates.return_value = earnings
    bars = [
        (date(2026, 7, 22), 98.0),
        (date(2026, 7, 23), 100.0),
        (date(2026, 7, 24), 104.0),
        (date(2026, 7, 27), 105.0),
        (date(2026, 7, 28), 106.0),
    ]
    with patch("assettrack.quotes.yf.Ticker", return_value=ticker), patch(
        "assettrack.quotes.fetch_benchmark_history", return_value=bars
    ):
        result = fetch_earnings_reaction(
            "NVDA",
            date(2026, 7, 24),
            event_dt=_after_hours_dt(2026, 7, 23),
            period="盤後",
        )
    assert result is not None
    assert result["verdict"] == "beat"
    assert result["surprise_pct"] == 20.0
    assert result["price_end_date"] == date(2026, 7, 28)
    assert round(result["price_change_pct"], 1) == 6.0


def test_fetch_earnings_reaction_friday_after_hours_does_not_stop_at_monday() -> None:
    # Friday after-hours +3 calendar days is Monday; +3 sessions is Wednesday.
    earnings = pd.DataFrame(
        {
            "EPS Estimate": [2.00],
            "Reported EPS": [1.50],
            "Surprise(%)": [-25.0],
        },
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-08-07 16:00:00", tz="America/New_York")],
            name="Earnings Date",
        ),
    )
    ticker = MagicMock()
    ticker.get_earnings_dates.return_value = earnings
    bars = [
        (date(2026, 8, 7), 50.0),
        (date(2026, 8, 10), 48.0),
        (date(2026, 8, 11), 47.0),
        (date(2026, 8, 12), 45.0),
    ]
    with patch("assettrack.quotes.yf.Ticker", return_value=ticker), patch(
        "assettrack.quotes.fetch_benchmark_history", return_value=bars
    ):
        result = fetch_earnings_reaction(
            "INTC",
            date(2026, 8, 8),
            event_dt=_after_hours_dt(2026, 8, 7),
            period="盤後",
        )
    assert result is not None
    assert result["verdict"] == "miss"
    assert result["price_end_date"] == date(2026, 8, 12)
    assert round(result["price_change_pct"], 1) == -10.0


def test_fetch_earnings_reaction_pre_market_starts_from_prior_session() -> None:
    earnings = pd.DataFrame(
        {
            "EPS Estimate": [1.00],
            "Reported EPS": [1.00],
            "Surprise(%)": [0.0],
        },
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-07-20 08:00:00", tz="America/New_York")],
            name="Earnings Date",
        ),
    )
    ticker = MagicMock()
    ticker.get_earnings_dates.return_value = earnings
    bars = [
        (date(2026, 7, 17), 100.0),
        (date(2026, 7, 20), 101.0),
        (date(2026, 7, 21), 104.0),
        (date(2026, 7, 22), 108.0),
    ]
    with patch("assettrack.quotes.yf.Ticker", return_value=ticker), patch(
        "assettrack.quotes.fetch_benchmark_history", return_value=bars
    ):
        result = fetch_earnings_reaction(
            "AMD",
            date(2026, 7, 20),
            event_dt=_pre_market_dt(2026, 7, 20),
            period="盤前",
        )
    assert result is not None
    assert result["verdict"] == "meet"
    assert result["price_end_date"] == date(2026, 7, 22)
    assert round(result["price_change_pct"], 1) == 8.0


def test_fetch_earnings_reactions_batch_caps_workers() -> None:
    from assettrack import quotes

    items = [(f"S{i}", date(2026, 7, 1), None, "盤後") for i in range(12)]
    with patch("concurrent.futures.ThreadPoolExecutor") as pool:
        pool.return_value.__enter__.return_value.submit = MagicMock()
        with patch("concurrent.futures.as_completed", return_value=[]):
            quotes.fetch_earnings_reactions_batch(items)
    assert pool.call_args.kwargs["max_workers"] == 4


def test_cpi_and_fed_actuals_name_their_series() -> None:
    from rich.text import Text

    cpi = _format_cpi_event_actuals(
        {
            "as_of": date(2026, 7, 1),
            "yoy_pct": 2.85,
            "mom_pct": 0.20,
            "prev_yoy_pct": 2.70,
            "prev_mom_pct": 0.10,
        }
    )
    assert cpi.startswith("總指數 CPI")
    assert "YoY 2.85%" in cpi
    assert "MoM 0.20%" in cpi
    assert Text.from_markup(cpi).cell_len <= 64
    nfp = _format_nfp_event_actuals(
        {
            "as_of": date(2026, 7, 1),
            "change": 175_000,
            "prev_change": 147_000,
        },
        {
            "rate_pct": 4.2,
            "prev_pct": 4.1,
            "change_pp": 0.1,
        },
    )
    assert "NFP" in nfp
    assert "4.2%" in nfp
    assert Text.from_markup(nfp).cell_len <= 64
    fed = _format_fed_event_actuals(
        {
            "range_before": (4.50, 4.75),
            "range_after": (4.25, 4.50),
            "delta_bps": -25,
        }
    )
    assert fed.startswith("目標區間")
    assert "4.25" in fed
    assert Text.from_markup(fed).cell_len <= 64


def test_completed_grid_day_keeps_event_type_color() -> None:
    markup = _grid_day_markup(12, ["SOX"], all_completed=True)
    assert "yellow reverse" in markup
    assert "d1d5db" not in markup
    holdings = _grid_day_markup(3, ["PORTFOLIO"], all_completed=True)
    assert "green reverse" in holdings
    assert "d1d5db" not in holdings


def test_month_heading_is_one_line_count() -> None:
    assert _month_heading(2026, 9, 8) == "2026年9月 · 8 件事"


def test_nfp_summary_fits_one_list_row_at_120_cols() -> None:
    today = date(2026, 8, 27)
    summary = _format_nfp_event_actuals(
        {
            "as_of": date(2026, 7, 1),
            "change": 175_000,
            "prev_change": 147_000,
        },
        {
            "rate_pct": 4.2,
            "prev_pct": 4.1,
            "change_pp": 0.1,
        },
    )
    from rich.console import Console

    console = Console(record=True, width=80, color_system=None)
    console.print(
        _month_event_list(
            [
                _CalEvent(
                    date=date(2026, 8, 7),
                    title="NFP",
                    when="20:30",
                    completed=True,
                    summary=summary,
                    event_type="MACRO",
                ),
            ],
            today,
        )
    )
    body = console.export_text()
    nfp_lines = [line for line in body.splitlines() if "NFP" in line and "+175K" in line]
    assert len(nfp_lines) == 1


class CalendarMonthCollapsibleTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_months_start_collapsed_and_header_omits_fred(self) -> None:
        from assettrack.models import Position
        from assettrack.tui import AssetTrackApp, UpcomingEventsScreen

        positions = [
            Position(
                broker="manual",
                symbol="AAPL",
                instrument_type="stock",
                quantity=1.0,
                avg_cost=1.0,
                market_price=1.0,
                market_value=1.0,
                prev_close=1.0,
                currency="USD",
                sector="科技",
                source="manual",
                last_updated=datetime(2026, 8, 27),
            )
        ]
        app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
        with patch.object(UpcomingEventsScreen, "run_calendar_fetch"), patch.object(
            UpcomingEventsScreen, "run_macro_readings_fetch"
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("4")
                await pilot.pause(0.1)
                screen = pilot.app.screen
                self.assertIsInstance(screen, UpcomingEventsScreen)
                screen._on_fetch_complete(
                    [
                        _CalEvent(
                            date=date(2026, 7, 23),
                            title="INTC",
                            badge="SOX",
                            completed=True,
                            event_type="SOX",
                        ),
                        _CalEvent(
                            date=date(2026, 8, 1),
                            title="CPI",
                            when="20:30",
                            completed=True,
                            summary=(
                                "總指數 CPI 7月 YoY 2.85%（+0.15pp） "
                                "MoM 0.20%（+0.10pp）"
                            ),
                            event_type="MACRO",
                        ),
                    ],
                    date(2026, 8, 27),
                )
                await pilot.pause(0.4)
                collapsibles = list(screen.query("Collapsible"))
                self.assertEqual(len(collapsibles), 2)
                by_title = {item.title: item for item in collapsibles}
                self.assertTrue(by_title["2026年7月 · 1 件事"].collapsed)
                self.assertFalse(by_title["2026年8月 · 1 件事"].collapsed)
                self.assertEqual(len(list(screen.query("Horizontal.month-split"))), 2)
                self.assertTrue(list(screen.query(".month-cal")))
                self.assertTrue(list(screen.query(".month-detail")))
                detail = by_title["2026年8月 · 1 件事"].query_one(".month-detail")
                self.assertGreaterEqual(detail.size.width, 70)
                from rich.console import Console

                detail_console = Console(
                    record=True, width=detail.size.width, color_system=None
                )
                detail_console.print(detail.content)
                detail_text = detail_console.export_text()
                cpi_lines = [
                    line for line in detail_text.splitlines() if "總指數 CPI" in line
                ]
                self.assertEqual(len(cpi_lines), 1)
                self.assertIn("MoM", cpi_lines[0])
                header_console = Console(record=True, width=120, color_system=None)
                header_console.print(screen.query_one("#events-header").content)
                header = header_console.export_text()
                self.assertNotIn("核心CPI", header)
                self.assertNotIn("聯邦資金利率", header)
                self.assertNotIn("核心 CPI", header)

    async def test_empty_calendar_shows_placeholder(self) -> None:
        from assettrack.models import Position
        from assettrack.tui import AssetTrackApp, UpcomingEventsScreen

        positions = [
            Position(
                broker="manual",
                symbol="AAPL",
                instrument_type="stock",
                quantity=1.0,
                avg_cost=1.0,
                market_price=1.0,
                market_value=1.0,
                prev_close=1.0,
                currency="USD",
                sector="科技",
                source="manual",
                last_updated=datetime(2026, 8, 27),
            )
        ]
        app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
        with patch.object(UpcomingEventsScreen, "run_calendar_fetch"), patch.object(
            UpcomingEventsScreen, "run_macro_readings_fetch"
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("4")
                await pilot.pause(0.1)
                screen = pilot.app.screen
                screen._on_fetch_complete([], date(2026, 8, 27))
                await pilot.pause(0.4)
                self.assertEqual(len(list(screen.query("Collapsible"))), 0)
                months = screen.query_one("#events-months")
                self.assertIn("無重大事件", str(months.children[0].content))


def test_compact_macro_recs_omit_basis_from_screen() -> None:
    rec = Recommendation(
        rec_id="event:core_cpi",
        category="event",
        direction=None,
        verdict="📌 核心 CPI 月增 0.24%",
        basis="這段依據不該出現在畫面上",
        detail_sections=[],
    )
    body, mapping = render_detail_recs([rec], compact=True)
    assert "核心 CPI" in body
    assert "公式細節" in body
    assert "依據" not in body
    assert "這段依據不該出現在畫面上" not in body
    assert "r0" in mapping
