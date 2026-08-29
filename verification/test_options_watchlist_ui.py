"""Options watchlist and dashboard show observation only — no directional advice."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from rich.panel import Panel

import assettrack.options_analysis as options_analysis
from assettrack import tui


FORBIDDEN_ADVICE_TOKENS = (
    "generate_options_recommendations",
    "generate_grouped_analysis_card",
    "compute_directional_verdicts",
    "assess_option_forecast",
    "看多",
    "看空",
    "期權預測",
)


def test_options_watchlist_keeps_analysis_panel_without_forecast() -> None:
    css = tui.OptionsWatchlistScreen.DEFAULT_CSS
    compose_src = inspect.getsource(tui.OptionsWatchlistScreen.compose)
    run_src = inspect.getsource(tui.OptionsWatchlistScreen._run_analysis)
    render_src = inspect.getsource(tui.OptionsWatchlistScreen._render_portfolio)
    screen_src = inspect.getsource(tui.OptionsWatchlistScreen)

    assert "#ow-portfolio" in css
    assert "ow-portfolio" in compose_src
    assert "#ow-verdicts" not in css
    assert "ow-verdicts" not in compose_src
    assert "richness_from_history" in run_src
    assert "跨式溢價" in render_src
    assert "DataTable" in compose_src
    assert "cursor_type" in screen_src or "cursor_type" in inspect.getsource(
        tui.OptionsWatchlistScreen.on_mount
    )
    assert "波動貴賤" in render_src
    assert "OptionRichnessHistoryScreen" in inspect.getsource(tui)
    assert "on_data_table_cell_selected" in inspect.getsource(
        tui.OptionsWatchlistScreen
    )
    assert "earnings_remaining_note" in run_src
    assert "財報剩" in render_src
    for token in FORBIDDEN_ADVICE_TOKENS:
        assert token not in run_src
        assert token not in render_src


def test_options_watchlist_does_not_show_contract_price_tables() -> None:
    css = tui.OptionsWatchlistScreen.DEFAULT_CSS
    compose_src = inspect.getsource(tui.OptionsWatchlistScreen.compose)
    screen_src = inspect.getsource(tui.OptionsWatchlistScreen)

    for token in (
        "ow-calls-table",
        "ow-puts-table",
        "ow-list-table",
        "ow-right-col",
        "ow-body",
    ):
        assert token not in css
        assert token not in compose_src

    assert "_render_greeks" not in screen_src
    assert "_render_list" not in screen_src
    assert "build_contract_view" not in screen_src


def test_options_analysis_does_not_build_per_contract_price_list() -> None:
    assert not hasattr(options_analysis, "build_contract_view")


def test_tui_does_not_import_options_advice_generators() -> None:
    source = inspect.getsource(tui)
    assert "generate_options_recommendations" not in source
    assert "generate_grouped_analysis_card" not in source


def test_dashboard_options_panel_is_observed_regime_not_forecast() -> None:
    panel_src = inspect.getsource(tui.DashboardScreen._build_options_flow_panel)
    for token in FORBIDDEN_ADVICE_TOKENS:
        assert token not in panel_src

    snapshots = {
        "AAA": [
            {"date": "2026-08-03", "spot_price": 100.0, "contracts": []},
            {"date": "2026-08-10", "spot_price": 90.0, "contracts": []},
        ]
    }
    screen = SimpleNamespace(_user="alice", _positions=[], _rf_rate=0.04)
    inputs = tui._DashboardAnalysisInputs(
        active_params={},
        etf_snapshots={},
        options_underlyings=["AAA"],
        options_snapshots=snapshots,
        sector_groups={},
        sector_snapshots={},
    )

    panel = tui.DashboardScreen._build_options_flow_panel(screen, inputs)

    assert isinstance(panel, Panel)
    text = str(panel.renderable)
    title = str(panel.title or "")
    assert "看多" not in text
    assert "看空" not in text
    assert "未來預測" not in text
    assert "期權樣態" in title or "樣態" in text or "資料" in text
    assert "貴" in text or "便宜" in text or "公允" in text or "資料" in text


def test_watchlist_cursor_stays_on_richness_column() -> None:
    assert tui.OptionsWatchlistScreen.RICHNESS_COLUMN_INDEX == 4
    source = inspect.getsource(tui.OptionsWatchlistScreen)
    assert "RICHNESS_COLUMN_INDEX" in source
    assert "OptionRichnessHistoryScreen" in source
    mount_src = inspect.getsource(tui.OptionsWatchlistScreen.on_mount)
    assert "RICHNESS_HISTORY_DAYS" in mount_src
    assert "max_age_days" in mount_src
    assert "on_data_table_cell_selected" in source
    history_src = inspect.getsource(tui.OptionRichnessHistoryScreen)
    assert "format_richness_history" in history_src
    assert "看多" not in history_src
    assert "看空" not in history_src
