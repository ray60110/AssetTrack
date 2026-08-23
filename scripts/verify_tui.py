#!/usr/bin/env python3
"""Automated verification for bug#00017 Textual TUI (方案三)."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from assettrack.models import Position
from assettrack.tui import (
    AssetTrackApp,
    DashboardScreen,
    LogoutConfirmModal,
    LoginScreen,
    OnboardingModal,
    SECIdentityModal,
    AddPositionModal,
    _build_holdings_table,
    _build_metrics_panel,
    _calc_weights,
    run_tui_dashboard,
)


def _sample_positions() -> list[Position]:
    return [
        Position(
            broker="manual",
            symbol="AAPL",
            instrument_type="stock",
            quantity=50.0,
            avg_cost=185.0,
            market_price=210.0,
            market_value=10500.0,
            prev_close=208.0,
            currency="USD",
            sector="科技",
            source="manual",
            last_updated=datetime.utcnow(),
        ),
        Position(
            broker="manual",
            symbol="TSLA",
            instrument_type="stock",
            quantity=10.0,
            avg_cost=240.0,
            market_price=250.0,
            market_value=2500.0,
            prev_close=245.0,
            currency="USD",
            sector="科技",
            source="manual",
            last_updated=datetime.utcnow(),
        ),
    ]


def verify_imports() -> None:
    # bug#00056: cli.py has been removed entirely — tui.py's main() is now the sole
    # command-line entry point (see pyproject.toml [project.scripts] / entrypoint.py).
    from assettrack.tui import main  # noqa: F401
    # shared.py houses the migrated pure-logic functions
    from assettrack.shared import (  # noqa: F401
        MACRO_EVENT_NAMES, get_upcoming_macro_events, draw_history_chart
    )
    # TUI should expose AddPositionModal
    from assettrack.tui import AddPositionModal  # noqa: F401
    assert callable(run_tui_dashboard)


def verify_render_builders() -> None:
    positions = _sample_positions()
    rate = 32.5
    weights = _calc_weights(positions, rate)
    metrics = _build_metrics_panel(positions, rate)
    holdings = _build_holdings_table(positions, rate, weights)
    assert metrics is not None
    assert holdings is not None
    assert len(weights) == 2


def verify_environment_loading() -> None:
    """The real main() startup path must load a deterministic .env location."""
    import os
    import sys
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from assettrack.tui import _load_environment, main

    with tempfile.TemporaryDirectory() as temp_dir:
        env_path = Path(temp_dir) / ".env"
        env_path.write_text("FRED_API_KEY=test-only-key\n")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FRED_API_KEY", None)
            assert _load_environment(env_path) == env_path.resolve()
            assert os.environ["FRED_API_KEY"] == "test-only-key"

        with patch.dict(os.environ, {"FRED_API_KEY": "already-exported"}, clear=False):
            _load_environment(env_path)
            assert os.environ["FRED_API_KEY"] == "already-exported"

    with patch("assettrack.tui._load_environment") as load_env, patch(
        "assettrack.tui.run_tui_dashboard"
    ) as run_dashboard, patch.object(sys, "argv", ["assettrack", "--user", "testuser"]):
        main()
        load_env.assert_called_once_with()
        run_dashboard.assert_called_once_with("testuser")


def verify_event_actuals_and_timezones() -> None:
    """Upcoming events retain comparable actuals and convert source times."""
    from datetime import datetime as dt
    from pathlib import Path
    import os
    import tempfile
    from unittest.mock import MagicMock, PropertyMock, patch

    import pandas as pd

    from assettrack.quotes import (
        compute_cpi_conclusion,
        fetch_earnings_actuals,
        fetch_fred_series,
        fred_failure_reason,
    )
    from assettrack.shared import get_upcoming_macro_events
    from assettrack.storage import load_user_preferences, save_user_preferences
    from assettrack.tui import (
        _event_card,
        _event_history_start,
        _format_cpi_event_actuals,
        _format_earnings_actuals,
        _format_nfp_event_actuals,
        _retain_event_history,
    )

    columns = pd.to_datetime([
        "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"
    ])
    income = pd.DataFrame(
        [
            [110.0, 0, 0, 0, 100.0],
            [22.0, 0, 0, 0, 20.0],
            [55.0, 0, 0, 0, 50.0],
            [11.0, 0, 0, 0, 10.0],
        ],
        index=["Total Revenue", "EBIT", "Gross Profit", "Net Income"],
        columns=columns,
    )
    cashflow = pd.DataFrame(
        [[-12.0, 0, 0, 0, -10.0], [18.0, 0, 0, 0, 15.0]],
        index=["Capital Expenditure", "Free Cash Flow"],
        columns=columns,
    )
    ticker = MagicMock()
    type(ticker).quarterly_income_stmt = PropertyMock(return_value=income)
    type(ticker).quarterly_cashflow = PropertyMock(return_value=cashflow)
    with patch("assettrack.quotes.yf.Ticker", return_value=ticker):
        actuals = fetch_earnings_actuals("INTC")

    assert actuals is not None
    assert round(actuals["metrics"]["revenue"]["yoy_pct"], 1) == 10.0
    assert actuals["metrics"]["capex"]["value"] == 12.0
    assert round(actuals["metrics"]["fcf"]["yoy_pct"], 1) == 20.0
    rendered = _format_earnings_actuals(actuals)
    for metric in ("Revenue", "CAPEX", "EBIT", "FCF", "去年同期"):
        assert metric in rendered

    completed_card = _event_card(
        dt(2026, 7, 23).date(),
        "💻 INTC[bold red](已發生)[/bold red] 財報公佈",
        dt(2026, 7, 24).date(),
        "SOX",
    )
    completed_panel = completed_card.renderable
    assert completed_panel.style == "black on #d1d5db"
    assert completed_panel.border_style == "#9ca3af"
    assert [column.width for column in completed_panel.renderable.columns[:2]] == [15, 10]

    upcoming_card = _event_card(
        dt(2026, 7, 30).date(),
        "▼ FED 利率決議 (02:00 UTC+08:00)",
        dt(2026, 7, 24).date(),
        "MACRO",
    )
    assert upcoming_card.renderable.style == "white on #161b22"
    assert upcoming_card.renderable.border_style == "#58a6ff"

    assert _event_history_start(dt(2026, 7, 24).date()) == dt(2026, 6, 1).date()
    assert _event_history_start(dt(2026, 1, 5).date()) == dt(2025, 12, 1).date()
    assert _retain_event_history(dt(2026, 6, 1).date(), dt(2026, 7, 24).date())
    assert not _retain_event_history(dt(2026, 5, 31).date(), dt(2026, 7, 24).date())

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FRED_API_KEY", None)
        assert fetch_fred_series("CPIAUCSL", limit=3) is None
        assert fetch_fred_series("CPIAUCNS", limit=14) is None
        assert fetch_fred_series("PAYEMS", limit=4) is None
        assert fetch_fred_series("UNRATE", limit=4) is None
        assert fred_failure_reason("CPIAUCSL", "CPIAUCNS") == "missing_key"
        assert "未載入 FRED_API_KEY" in _format_cpi_event_actuals(None)
        assert "未載入 FRED_API_KEY" in _format_nfp_event_actuals(None, None)

    sa_rows = [
        (dt(2026, 6, 1).date(), 320.0),
        (dt(2026, 5, 1).date(), 319.0),
        (dt(2026, 4, 1).date(), 318.0),
    ]
    nsa_rows = [
        (dt(2026, 6, 1).date(), 320.0 - i)
        for i in range(14)
    ]
    with patch(
        "assettrack.quotes.fetch_fred_series",
        side_effect=lambda series_id, limit: (
            sa_rows if series_id == "CPIAUCSL" else nsa_rows
        ),
    ) as fetch_series:
        cpi_result = compute_cpi_conclusion()
        assert cpi_result is not None
        assert cpi_result["prev_yoy_pct"] is not None
        fetch_series.assert_any_call("CPIAUCNS", limit=15)

    reference_date = dt(2026, 7, 24).date()
    taipei = get_upcoming_macro_events(
        30, timezone_name="Asia/Taipei", reference_date=reference_date
    )
    new_york = get_upcoming_macro_events(
        30, timezone_name="America/New_York", reference_date=reference_date
    )
    taipei_fed = next(item for item in taipei if item[1] == "▼FED")
    new_york_fed = next(item for item in new_york if item[1] == "▼FED")
    assert taipei_fed[0] == dt(2026, 7, 30).date() and taipei_fed[2] == "02:00"
    assert new_york_fed[0] == dt(2026, 7, 29).date() and new_york_fed[2] == "14:00"

    with tempfile.TemporaryDirectory() as temp_dir, patch(
        "assettrack.storage.get_data_dir", return_value=Path(temp_dir)
    ):
        save_user_preferences({"event_timezone": "America/New_York"}, "testuser")
        assert load_user_preferences("testuser")["event_timezone"] == "America/New_York"


async def verify_dashboard_mounts() -> None:
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        assert isinstance(screen, DashboardScreen)
        assert screen.query_one("#metrics-row")
        assert screen.query_one("#holdings-scroll")
        assert screen.query_one("#recommendations-scroll")
        assert screen.query_one("#status-bar")


async def verify_dashboard_scroll_layout() -> None:
    """Dashboard keeps ten holdings rows visible and scrolls recommendations separately."""
    template = _sample_positions()[0]
    positions = [
        template.model_copy(update={"symbol": f"TEST{index:02d}"})
        for index in range(12)
    ]
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        holdings_scroll = screen.query_one("#holdings-scroll")
        holdings_table = screen.query_one("#holdings-table")
        recommendations_scroll = screen.query_one("#recommendations-scroll")

        # One header row plus ten holding rows must remain visible at the default size.
        assert (
            holdings_table.scrollable_content_region.height
            == holdings_table.header_height + 10
        )
        assert holdings_table.row_count > 10
        assert holdings_table.max_scroll_y > 0
        assert holdings_table.show_vertical_scrollbar

        # The recommendation cards stay on-screen below holdings and own their scrollbar.
        assert recommendations_scroll.content_region.height > 0
        assert recommendations_scroll.max_scroll_y > 0
        assert recommendations_scroll.show_vertical_scrollbar
        assert recommendations_scroll.region.y >= holdings_scroll.region.bottom

        # Moving either scrollbar must not move the other viewport.
        recommendations_scroll.scroll_to(
            y=recommendations_scroll.max_scroll_y,
            animate=False,
        )
        await pilot.pause()
        assert recommendations_scroll.scroll_y == recommendations_scroll.max_scroll_y
        assert holdings_table.scroll_y == 0

        holdings_table.scroll_to(y=holdings_table.max_scroll_y, animate=False)
        await pilot.pause()
        assert holdings_table.scroll_y == holdings_table.max_scroll_y
        assert recommendations_scroll.scroll_y == recommendations_scroll.max_scroll_y


async def verify_bindings() -> None:
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        binding_keys = {b.key for b in pilot.app.screen.BINDINGS}
        for key in ("1", "2", "3", "4", "5", "6", "r", "q"):
            assert key in binding_keys, f"missing binding: {key}"


async def verify_logout_modal() -> None:
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("q")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, LogoutConfirmModal)
        
        # Verify initial focus and arrow keys
        modal = pilot.app.screen
        assert modal.focused == modal.query_one("#cancel")
        
        await pilot.press("left")
        await pilot.pause(0.1)
        assert modal.focused == modal.query_one("#confirm")
        
        await pilot.press("right")
        await pilot.pause(0.1)
        assert modal.focused == modal.query_one("#cancel")
        
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)


async def verify_empty_positions_onboarding_path() -> None:
    """Empty portfolio mounts after mocked TUI login and onboarding selection.

    Hermetic: the storage data dir is redirected to a fresh temp dir so the test
    (a) starts from a guaranteed-empty state without deleting anything in the real
    ``data/`` dir, and (b) doesn't depend on being able to unlink a real file
    (which fails under sandboxed/read-only filesystems). Fixes the long-standing
    intermittent "11/12 — Operation not permitted: data/testuser_positions.json".
    """
    from unittest.mock import patch
    import subprocess
    import tempfile
    from pathlib import Path

    tmp_data_dir = Path(tempfile.mkdtemp())

    with patch("assettrack.storage.get_data_dir", return_value=tmp_data_dir):
        app = AssetTrackApp(user="testuser", positions=[], rate=32.5)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert isinstance(pilot.app.screen, LoginScreen)

            pilot.app.screen.query_one("#user-input").value = "testuser"

            with patch("assettrack.tui.account_exists", return_value=True), \
                 patch("assettrack.tui.touchid_enrolled", return_value=True), \
                 patch("assettrack.tui.unlock_vault_with_touchid"), \
                 patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

                await pilot.press("enter")
                await pilot.pause(0.2)

                assert isinstance(pilot.app.screen, SECIdentityModal)
                pilot.app.screen.query_one("#sec-identity-cancel").press()
                await pilot.pause(0.2)

                assert isinstance(pilot.app.screen, OnboardingModal)

                await pilot.press("down", "down", "enter")
                await pilot.pause(0.2)

                assert isinstance(pilot.app.screen, DashboardScreen)
                assert pilot.app.screen.query_one("#holdings-table")


async def verify_refresh_action() -> None:
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("2")
        await pilot.pause(0.3)
        assert isinstance(pilot.app.screen, DashboardScreen)


async def verify_keyboard_navigation() -> None:
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        table = screen.query_one("#holdings-table")

        assert screen.focused == table
        assert table.cursor_coordinate.column == 0

        await pilot.press("right")
        await pilot.pause(0.1)
        assert table.cursor_coordinate.column == 1

        await pilot.press("left")
        await pilot.pause(0.1)
        assert table.cursor_coordinate.column == 0
        assert screen.focused == table


async def verify_modal_editing() -> None:
    from assettrack.tui import FieldEditModal, PositionActionsModal
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        table = screen.query_one("#holdings-table")
        
        table.focus()
        await pilot.pause(0.1)
        
        table.cursor_coordinate = (1, 0)
        await pilot.press("enter")
        await pilot.pause(0.1)
        
        assert isinstance(pilot.app.screen, FieldEditModal)
        
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, DashboardScreen)
        
        table.cursor_coordinate = (1, 4)
        await pilot.press("enter")
        await pilot.pause(0.1)
        
        assert isinstance(pilot.app.screen, PositionActionsModal)


async def verify_add_position_modal() -> None:
    """測試新增部位對話框：按 1 直接開啟、批次累積（儲存並繼續）、完成儲存。"""
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("1")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, AddPositionModal)

        modal = pilot.app.screen
        # 第一筆：MSFT → 儲存並繼續（加入待存清單，表單重置）
        modal.query_one("#add-symbol").value = "MSFT"
        modal.query_one("#add-qty").value = "15"
        modal.query_one("#add-cost").value = "420.0"
        modal.query_one("#confirm-next").focus()
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert len(modal._pending) == 1
        assert modal._pending[0].symbol == "MSFT"
        assert modal.query_one("#add-symbol").value == ""

        # 第二筆：NVDA → 完成儲存（整批回傳）
        modal.query_one("#add-symbol").value = "NVDA"
        modal.query_one("#add-qty").value = "5"
        modal.query_one("#confirm").focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, DashboardScreen)


async def verify_symbol_auto_inference() -> None:
    """測試 Symbol 輸入自動推斷市場/幣別（2330 → TW/TWD）。"""
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("1")
        await pilot.pause(0.2)
        modal = pilot.app.screen
        assert isinstance(modal, AddPositionModal)

        modal.query_one("#add-symbol").value = "2330"
        await pilot.pause(0.1)
        assert str(modal.query_one("#add-market").value) == "TW"
        assert modal.query_one("#add-curr").value == "TWD"

        modal.query_one("#add-symbol").value = "AAPL"
        await pilot.pause(0.1)
        assert str(modal.query_one("#add-market").value) == "US"
        assert modal.query_one("#add-curr").value == "USD"

        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, DashboardScreen)


async def verify_table_direct_ops() -> None:
    """測試 Holdings 表格直接操作：space 多選標記、e 編輯、x 刪除確認。"""
    from assettrack.tui import DeleteConfirmModal
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        table = screen.query_one("#holdings-table")
        table.focus()
        await pilot.pause(0.1)
        table.cursor_coordinate = (1, 0)  # row 0 是券商群組列，row 1 為第一筆部位

        await pilot.press("space")
        await pilot.pause(0.1)
        assert len(screen._marked) == 1
        await pilot.press("space")
        await pilot.pause(0.1)
        assert len(screen._marked) == 0

        await pilot.press("e")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, AddPositionModal)
        assert pilot.app.screen.position is not None  # 編輯模式
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, DashboardScreen)

        await pilot.press("x")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, DeleteConfirmModal)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, DashboardScreen)


async def verify_upcoming_events_screen() -> None:
    """測試重要日曆事件畫面 (UpcomingEventsScreen) 的載入與返回。"""
    from assettrack.tui import UpcomingEventsScreen
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause(0.5)
        assert isinstance(pilot.app.screen, UpcomingEventsScreen)
        
        screen = pilot.app.screen
        assert screen.query_one("#events-static")
        assert "t" in {binding.key for binding in screen.BINDINGS}

        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, DashboardScreen)


async def verify_active_etfs_screen() -> None:
    """測試主動式 ETF 畫面 (ActiveETFsScreen) 的載入與返回。"""
    from assettrack.tui import ActiveETFsScreen
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("6")
        await pilot.pause(0.5)
        assert isinstance(pilot.app.screen, ActiveETFsScreen)
        
        screen = pilot.app.screen
        tabs = screen.query_one("#etf-main-tabs")
        assert tabs.active == "tab-etf-advice"
        assert screen.query_one("#etf-analysis-content")
        assert screen.query_one("#etf-left-tabbed")
        assert screen.query_one("#etf-us-table")
        assert screen.query_one("#etf-13f-table")
        # bug#00091：台股主動式ETF排行已移除，TWD 表格不應存在。
        from textual.css.query import NoMatches
        try:
            screen.query_one("#etf-twd-table")
            raise AssertionError("#etf-twd-table should have been removed")
        except NoMatches:
            pass
        assert screen.query_one("#etf-holdings-table")
        assert screen.query_one("#etf-history-table")
        
        # Test Enter key on selected row
        await pilot.press("enter")
        await pilot.pause(0.1)
        
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, DashboardScreen)


def patch_workers() -> None:
    from unittest.mock import MagicMock
    from assettrack.tui import (
        UpcomingEventsScreen,
        DashboardScreen,
        ActiveETFsScreen,
    )
    # Stub out slow background network workers to avoid background thread race conditions
    from assettrack.tui import AssetTrackApp
    UpcomingEventsScreen.run_calendar_fetch = MagicMock()
    # bug#00096: login now kicks off analysis fetch immediately — stub it so the
    # headless harness stays hermetic (no network on mount).
    AssetTrackApp._background_data_refresh = MagicMock()
    DashboardScreen._do_refresh_worker = MagicMock()
    DashboardScreen._fetch_upcoming_events_worker = MagicMock()
    ActiveETFsScreen.run_background_fetch = MagicMock()
    ActiveETFsScreen.run_analysis_compute = MagicMock()
    ActiveETFsScreen.run_detail_fetch = MagicMock()
    from assettrack import tui as _tui
    _tui.etf_watchlist_is_configured = MagicMock(return_value=True)
    _tui.load_etf_watchlist = MagicMock(return_value=["NVDA"])


def main() -> int:
    patch_workers()
    checks = [
        ("imports", verify_imports),
        ("render_builders", verify_render_builders),
        ("environment_loading", verify_environment_loading),
        ("event_actuals_and_timezones", verify_event_actuals_and_timezones),
        ("dashboard_mounts", verify_dashboard_mounts),
        ("dashboard_scroll_layout", verify_dashboard_scroll_layout),
        ("bindings", verify_bindings),
        ("logout_modal", verify_logout_modal),
        ("refresh_action", verify_refresh_action),
        ("empty_positions", verify_empty_positions_onboarding_path),
        ("keyboard_navigation", verify_keyboard_navigation),
        ("modal_editing", verify_modal_editing),
        ("add_position_modal", verify_add_position_modal),
        ("symbol_auto_inference", verify_symbol_auto_inference),
        ("table_direct_ops", verify_table_direct_ops),
        ("upcoming_events_screen", verify_upcoming_events_screen),
        ("active_etfs_screen", verify_active_etfs_screen),
    ]
    passed = 0
    failed = 0
    for name, fn in checks:
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"  FAIL  {name}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{passed + failed} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
