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
    AdjustPositionsModal,
    AddPositionModal,
    PerformanceHistoryScreen,
    _build_broker_panel,
    _build_holdings_table,
    _build_metrics_panel,
    _build_pnl_panel,
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
    from assettrack.cli import add, edit, history, refresh_snapshot, remove_position  # noqa: F401
    from assettrack.cli import main  # noqa: F401
    assert callable(run_tui_dashboard)


def verify_render_builders() -> None:
    positions = _sample_positions()
    rate = 32.5
    weights = _calc_weights(positions, rate)
    metrics = _build_metrics_panel(positions, rate)
    holdings = _build_holdings_table(positions, rate, weights)
    broker = _build_broker_panel(positions, rate)
    pnl = _build_pnl_panel(positions, rate)
    assert metrics is not None
    assert holdings is not None
    assert broker is not None
    assert pnl is not None
    assert len(weights) == 2


async def verify_dashboard_mounts() -> None:
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = pilot.app.screen
        assert isinstance(screen, DashboardScreen)
        assert screen.query_one("#tui-header")
        assert screen.query_one("#metrics-row")
        assert screen.query_one("#holdings-scroll")
        assert screen.query_one("#sidebar-nav")
        assert screen.query_one("#broker-dist")
        assert screen.query_one("#pnl-leaderboard")


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
        await pilot.press("down", "down", "down", "down", "down")
        await pilot.press("enter")
        await pilot.pause()
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
    """Empty portfolio mounts after mocked TUI login and onboarding selection."""
    from unittest.mock import patch
    import subprocess
    import os

    test_positions_path = "data/testuser_positions.json"
    if os.path.exists(test_positions_path):
        os.remove(test_positions_path)

    app = AssetTrackApp(user="testuser", positions=[], rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, LoginScreen)
        
        pilot.app.screen.query_one("#user-input").value = "testuser"
        
        with patch("keyring.get_password", return_value="pwd123"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            
            await pilot.press("enter")
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
        sidebar = screen.query_one("#sidebar-nav")
        table = screen.query_one("#holdings-table")
        
        assert screen.focused == sidebar
        
        await pilot.press("right")
        await pilot.pause(0.1)
        assert screen.focused == table
        
        assert table.cursor_coordinate.column == 1
        await pilot.press("left")
        await pilot.pause(0.1)
        assert table.cursor_coordinate.column == 0
        assert screen.focused == table

        await pilot.press("left")
        await pilot.pause(0.1)
        assert screen.focused == sidebar


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
    """測試新增部位對話框 (AddPositionModal) 的欄位與資料提交。"""
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("1")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, AdjustPositionsModal)
        
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, AddPositionModal)
        
        modal = pilot.app.screen
        modal.query_one("#add-symbol").value = "MSFT"
        modal.query_one("#add-qty").value = "15"
        modal.query_one("#add-cost").value = "420.0"
        
        modal.query_one("#confirm").focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, DashboardScreen)


async def verify_performance_history_screen() -> None:
    """測試績效歷史畫面 (PerformanceHistoryScreen) 的加載與分析操作。"""
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, PerformanceHistoryScreen)
        
        screen = pilot.app.screen
        assert screen.query_one("#perf-period")
        assert screen.query_one("#perf-benchmark")
        
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, DashboardScreen)


async def verify_upcoming_events_screen() -> None:
    """測試重要日曆事件畫面 (UpcomingEventsScreen) 的載入與返回。"""
    from assettrack.tui import UpcomingEventsScreen
    positions = _sample_positions()
    app = AssetTrackApp(user="testuser", positions=positions, rate=32.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("5")
        await pilot.pause(0.5)
        assert isinstance(pilot.app.screen, UpcomingEventsScreen)
        
        screen = pilot.app.screen
        assert screen.query_one("#events-static")
        
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, DashboardScreen)


def main() -> int:
    checks = [
        ("imports", verify_imports),
        ("render_builders", verify_render_builders),
        ("dashboard_mounts", verify_dashboard_mounts),
        ("bindings", verify_bindings),
        ("logout_modal", verify_logout_modal),
        ("refresh_action", verify_refresh_action),
        ("empty_positions", verify_empty_positions_onboarding_path),
        ("keyboard_navigation", verify_keyboard_navigation),
        ("modal_editing", verify_modal_editing),
        ("add_position_modal", verify_add_position_modal),
        ("performance_history_screen", verify_performance_history_screen),
        ("upcoming_events_screen", verify_upcoming_events_screen),
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
