from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from assettrack import tui


class _RecordingApp:
    def __init__(self) -> None:
        self.callbacks: list[tuple[object, tuple[object, ...]]] = []
        self.fetch_activity: dict[str, str] = {}

    def call_from_thread(self, callback, *args) -> None:
        self.callbacks.append((callback, args))

    def _set_fetch_active(self, key: str, label: str) -> None:
        self.fetch_activity[key] = label

    def _clear_fetch_active(self, key: str) -> None:
        self.fetch_activity.pop(key, None)


def test_quote_refresh_schedules_only_one_full_dashboard_render() -> None:
    """A quote refresh should publish its final state once, not render twice."""
    app = _RecordingApp()
    render = MagicMock(name="render_all")
    screen = SimpleNamespace(
        _loading=False,
        _rate=32.0,
        _rf_rate=0.04,
        _positions=[],
        _cash_positions=[],
        _underlying_prices={},
        app=app,
        _render_all=render,
        _maybe_record_performance_valuation=MagicMock(),
        _fetch_upcoming_events_worker=MagicMock(),
        _events_fetched=True,
    )

    with patch.object(tui, "fetch_usdtwd_rate", return_value=32.0), patch(
        "assettrack.quotes.fetch_risk_free_rate", return_value=0.04
    ):
        tui.DashboardScreen._do_refresh_worker.__wrapped__(
            screen, load_from_disk=False
        )

    assert [callback for callback, _ in app.callbacks].count(render) == 1


def test_event_fetch_publishes_one_targeted_ui_update() -> None:
    """Finishing one collection must not enqueue a second full render."""
    app = _RecordingApp()
    on_events_fetched = MagicMock(name="on_events_fetched")
    render = MagicMock(name="render_all")
    screen = SimpleNamespace(
        _fetching_events=False,
        _positions=[],
        app=app,
        _on_events_fetched=on_events_fetched,
        _render_all=render,
        _event_symbols=MagicMock(return_value=()),
    )

    with patch.object(tui, "fetch_earnings_calendar", return_value={}), patch(
        "assettrack.shared.get_upcoming_macro_events", return_value=[]
    ):
        tui.DashboardScreen._fetch_upcoming_events_worker.__wrapped__(screen)

    assert [callback for callback, _ in app.callbacks] == [on_events_fetched]


def test_event_completion_does_not_trigger_full_dashboard_render() -> None:
    refresh_events_panel = MagicMock()
    render_all = MagicMock()
    screen = SimpleNamespace(
        _upcoming_events=[],
        _events_fetched=False,
        _events_last_attempt_symbols=("AAPL",),
        _refresh_events_panel=refresh_events_panel,
        _render_all=render_all,
    )

    tui.DashboardScreen._on_events_fetched(screen, [])

    refresh_events_panel.assert_called_once_with()
    render_all.assert_not_called()


def test_quote_refresh_skips_event_collection_while_cache_is_fresh() -> None:
    app = _RecordingApp()
    fetch_events = MagicMock()
    screen = SimpleNamespace(
        _loading=False,
        _rate=32.0,
        _rf_rate=0.04,
        _positions=[],
        _cash_positions=[],
        _underlying_prices={},
        app=app,
        _render_all=MagicMock(),
        _maybe_record_performance_valuation=MagicMock(),
        _fetch_upcoming_events_worker=fetch_events,
        _events_refresh_due=MagicMock(return_value=False),
        _events_fetched=True,
    )

    with patch.object(tui, "fetch_usdtwd_rate", return_value=32.0), patch.object(
        tui, "load_manual_positions", return_value=([], [])
    ), patch("assettrack.quotes.fetch_risk_free_rate", return_value=0.04):
        tui.DashboardScreen._do_refresh_worker.__wrapped__(
            screen, load_from_disk=True
        )

    fetch_events.assert_not_called()


def test_event_refresh_uses_six_hour_ttl() -> None:
    screen = SimpleNamespace(
        _fetching_events=False,
        _positions=[],
        _events_last_attempt_symbols=(),
        _events_fetched=True,
        _events_symbols=(),
        _events_last_fetched_at=1_000.0,
        _event_symbols=MagicMock(return_value=()),
    )

    with patch.object(tui.time, "monotonic", return_value=1_060.0):
        assert not tui.DashboardScreen._events_refresh_due(screen)
    with patch.object(
        tui.time,
        "monotonic",
        return_value=1_000.0 + tui._EVENTS_REFRESH_INTERVAL_SECONDS + 1,
    ):
        assert tui.DashboardScreen._events_refresh_due(screen)


def test_dashboard_maintenance_defers_unused_13f_collection() -> None:
    """Dashboard maintenance should not collect 13F data used only by ETF pages."""
    app = SimpleNamespace(
        _user="alice",
        _set_fetch_active=MagicMock(),
        _clear_fetch_active=MagicMock(),
        _fetch_activity={},
    )

    with patch.object(
        tui, "ensure_active_etf_universe", return_value={"records": []}
    ), patch.object(tui, "ensure_hedge_fund_filings") as ensure_13f, patch.object(
        tui, "_watchlist_underlyings", return_value=([], set(), set())
    ), patch.object(tui, "load_manual_positions", return_value=([], [])), patch(
        "assettrack.storage.load_sector_groups", return_value={}
    ):
        tui.AssetTrackApp._background_data_refresh.__wrapped__(app)

    ensure_13f.assert_not_called()


def test_dashboard_analysis_reads_each_snapshot_source_once_per_generation() -> None:
    screen = SimpleNamespace(_user="alice", _positions=[], _rf_rate=0.04)

    with patch.object(tui, "active_etf_symbols", return_value=["ARKK"]), patch.object(
        tui, "load_etf_daily_snapshots", return_value=[]
    ) as load_etf, patch.object(
        tui,
        "_watchlist_underlyings",
        return_value=(["AAPL"], set(), set()),
    ), patch.object(
        tui, "load_options_daily_snapshots", return_value=[]
    ) as load_options, patch(
        "assettrack.storage.load_sector_groups",
        return_value={"Tech": ["AAPL"]},
    ) as load_groups, patch(
        "assettrack.storage.load_sector_daily_snapshots", return_value=[]
    ) as load_sector, patch.object(tui, "_active_params", return_value={}):
        inputs = tui._load_dashboard_analysis_inputs("alice", [])
        tui.DashboardScreen._build_etf_conclusions_panel(screen, inputs)
        tui.DashboardScreen._build_options_flow_panel(screen, inputs)
        tui.DashboardScreen._build_sector_consensus_panel(screen, inputs)

    assert load_etf.call_count == 1
    assert load_options.call_count == 1
    assert load_groups.call_count == 1
    assert load_sector.call_count == 1


def test_dashboard_analysis_generation_is_reused_within_ttl() -> None:
    widget = MagicMock()
    inputs = tui._DashboardAnalysisInputs(
        active_params={},
        etf_snapshots={},
        options_underlyings=[],
        options_snapshots={},
        sector_groups={},
        sector_snapshots={},
    )
    screen = SimpleNamespace(
        _user="alice",
        _positions=[],
        _rf_rate=0.04,
        _analysis_signature=None,
        _analysis_last_rendered_at=0.0,
        _analysis_input_signature=MagicMock(return_value=((), 0.04)),
        query_one=MagicMock(return_value=widget),
        _build_etf_conclusions_panel=MagicMock(return_value="etf"),
        _build_options_flow_panel=MagicMock(return_value="options"),
        _build_sector_consensus_panel=MagicMock(return_value="sector"),
    )

    with patch.object(
        tui, "_load_dashboard_analysis_inputs", return_value=inputs
    ) as load_inputs:
        with patch.object(tui.time, "monotonic", return_value=10_000.0):
            tui.DashboardScreen._refresh_analysis_panels(screen)
            tui.DashboardScreen._refresh_analysis_panels(screen)

    load_inputs.assert_called_once_with("alice", [])


def test_options_fresh_cache_does_not_repeat_full_analysis() -> None:
    screen = SimpleNamespace(
        _run_analysis=MagicMock(),
        _render_portfolio=MagicMock(),
    )

    tui.OptionsWatchlistScreen._on_fetch_complete(
        screen,
        analysis_changed=False,
    )

    screen._run_analysis.assert_not_called()
    screen._render_portfolio.assert_called_once_with()


def test_sector_fresh_cache_does_not_recompute_unchanged_flows() -> None:
    screen = SimpleNamespace(
        summaries={"Tech": {}},
        _recompute_flows=MagicMock(),
        _render_groups=MagicMock(),
        _set_header=MagicMock(),
        _updated_at=None,
    )

    tui.SectorAnalysisScreen._on_fetch_complete(
        screen,
        {"Tech": {}},
        from_cache=True,
        cached_at="2026-08-15T12:30:00",
    )

    screen._recompute_flows.assert_not_called()
    screen._render_groups.assert_called_once_with()


def test_ui_rate_helper_does_not_fetch() -> None:
    with patch.object(tui, "fetch_usdtwd_rate") as fetch, patch.object(
        tui, "cached_usdtwd_rate", return_value=31.5
    ):
        assert tui._get_cached_usdtwd_rate() == 31.5
    fetch.assert_not_called()


def test_metrics_panel_does_not_fetch_beta() -> None:
    from datetime import datetime

    from assettrack.models import Position

    position = Position(
        broker="ft",
        symbol="NVDA",
        quantity=1,
        avg_cost=10,
        market_price=12,
        market_value=12,
        currency="USD",
        last_updated=datetime.utcnow(),
    )
    with patch.object(tui, "fetch_beta") as fetch, patch.object(
        tui, "cached_beta", return_value=1.2
    ):
        tui._build_metrics_panel([position], 32.0)
    fetch.assert_not_called()


def test_start_dashboard_defers_research_ingest() -> None:
    app = SimpleNamespace(
        set_interval=MagicMock(),
        push_screen=MagicMock(),
        _background_data_refresh=MagicMock(),
        _bg_refresh_timer_started=False,
        _handle_dashboard_exit=MagicMock(),
    )
    with patch.object(tui, "_get_cached_usdtwd_rate", return_value=32.0):
        tui.AssetTrackApp._start_dashboard(app, "alice", [], [])
    app._background_data_refresh.assert_not_called()
    assert app._research_ingest_kicked is False


def test_quote_worker_kicks_research_ingest_once() -> None:
    kickoff = MagicMock(name="kickoff")
    app = _RecordingApp()
    app._kickoff_research_ingest_once = kickoff
    screen = SimpleNamespace(
        _loading=False,
        _rate=32.0,
        _rf_rate=0.04,
        _positions=[],
        _cash_positions=[],
        _underlying_prices={},
        _user="alice",
        app=app,
        _render_all=MagicMock(),
        _maybe_record_performance_valuation=MagicMock(),
        _fetch_upcoming_events_worker=MagicMock(),
        _events_fetched=True,
        _live_quotes_ready=False,
        _overlay_quotes_active=False,
    )
    with patch.object(tui, "fetch_usdtwd_rate", return_value=32.0), patch(
        "assettrack.quotes.fetch_risk_free_rate", return_value=0.04
    ):
        tui.DashboardScreen._do_refresh_worker.__wrapped__(
            screen, load_from_disk=False
        )
    assert [callback for callback, _ in app.callbacks].count(screen._render_all) == 1
    assert any(callback is kickoff for callback, _ in app.callbacks)


def test_kickoff_research_ingest_runs_only_once() -> None:
    app = SimpleNamespace(_research_ingest_kicked=False)
    app._background_data_refresh = MagicMock()
    tui.AssetTrackApp._kickoff_research_ingest_once(app)
    tui.AssetTrackApp._kickoff_research_ingest_once(app)
    app._background_data_refresh.assert_called_once()


def test_earnings_calendar_caps_worker_count() -> None:
    from assettrack.quotes import earnings_calendar_workers

    assert earnings_calendar_workers(15) == 4
    assert earnings_calendar_workers(2) == 2
    assert earnings_calendar_workers(0) == 1


def test_fetch_earnings_calendar_opens_at_most_four_workers() -> None:
    from assettrack import quotes

    with patch("concurrent.futures.ThreadPoolExecutor") as pool:
        pool.return_value.__enter__.return_value.map.return_value = []
        quotes.fetch_earnings_calendar([f"S{i}" for i in range(12)])
    assert pool.call_args.kwargs["max_workers"] == 4


def test_quote_overlay_recomputes_value_from_current_quantity(tmp_path, monkeypatch) -> None:
    from datetime import datetime

    from assettrack.models import Position
    from assettrack import storage

    monkeypatch.setattr(storage, "get_data_dir", lambda: tmp_path)
    from assettrack import auth

    class _MemoryKeyring:
        def __init__(self) -> None:
            self.secrets = {}

        def get_password(self, service, account):
            return self.secrets.get((service, account))

        def set_password(self, service, account, value):
            self.secrets[(service, account)] = value

        def delete_password(self, service, account):
            self.secrets.pop((service, account), None)

    keyring = _MemoryKeyring()
    monkeypatch.setattr(auth.keyring, "get_password", keyring.get_password)
    monkeypatch.setattr(auth.keyring, "set_password", keyring.set_password)
    monkeypatch.setattr(auth.keyring, "delete_password", keyring.delete_password)
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    auth.lock_vault()
    auth.register_account("alice", "correct-horse")
    auth.unlock_vault("alice", "correct-horse")
    live = Position(
        broker="ft",
        account="ira",
        symbol="NVDA",
        quantity=10,
        avg_cost=1,
        market_price=100,
        market_value=1000,
        prev_close=90,
        currency="USD",
        last_updated=datetime.utcnow(),
    )
    storage.save_quote_overlay("alice", [live])
    edited = live.model_copy(
        update={
            "quantity": 20,
            "market_price": None,
            "market_value": None,
            "prev_close": None,
        }
    )
    overlay, as_of = storage.apply_quote_overlay("alice", [edited])
    try:
        assert as_of
        assert overlay[0].market_price == 100
        assert overlay[0].market_value == 2000
        assert overlay[0].prev_close == 90
    finally:
        auth.lock_vault()


def test_quote_overlay_drop_prevents_stale_first_paint(tmp_path, monkeypatch) -> None:
    from datetime import datetime

    from assettrack.models import Position
    from assettrack import storage

    monkeypatch.setattr(storage, "get_data_dir", lambda: tmp_path)
    from assettrack import auth

    class _MemoryKeyring:
        def __init__(self) -> None:
            self.secrets = {}

        def get_password(self, service, account):
            return self.secrets.get((service, account))

        def set_password(self, service, account, value):
            self.secrets[(service, account)] = value

        def delete_password(self, service, account):
            self.secrets.pop((service, account), None)

    keyring = _MemoryKeyring()
    monkeypatch.setattr(auth.keyring, "get_password", keyring.get_password)
    monkeypatch.setattr(auth.keyring, "set_password", keyring.set_password)
    monkeypatch.setattr(auth.keyring, "delete_password", keyring.delete_password)
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    auth.lock_vault()
    auth.register_account("alice", "correct-horse")
    auth.unlock_vault("alice", "correct-horse")
    live = Position(
        broker="ft",
        symbol="MU",
        quantity=1,
        market_price=50,
        market_value=50,
        currency="USD",
        last_updated=datetime.utcnow(),
    )
    storage.save_quote_overlay("alice", [live])
    storage.drop_quote_overlay_keys("alice", [tui._pos_key(live)])
    bare = live.model_copy(update={"market_price": None, "market_value": None})
    overlay, as_of = storage.apply_quote_overlay("alice", [bare])
    try:
        assert as_of is None
        assert overlay[0].market_price is None
    finally:
        auth.lock_vault()


def test_cached_usdtwd_rate_uses_disk_without_network(tmp_path, monkeypatch) -> None:
    import json

    from assettrack import quotes

    path = tmp_path / "quote_warmup_cache.json"
    path.write_text(json.dumps({"usdtwd": {"rate": 31.25, "fetched_at": 1}}))
    monkeypatch.setattr(quotes, "_warmup_cache_path", lambda: path)
    quotes._exchange_rate_cache.clear()
    with patch.object(quotes.yf, "Ticker") as ticker:
        assert quotes.cached_usdtwd_rate() == 31.25
        ticker.assert_not_called()


def test_cached_beta_ignores_expired_disk(tmp_path, monkeypatch) -> None:
    import json

    from assettrack import quotes

    path = tmp_path / "quote_warmup_cache.json"
    path.write_text(json.dumps({
        "betas": {"NVDA": {"beta": 9.9, "fetched_at": 1.0}},
    }))
    monkeypatch.setattr(quotes, "_warmup_cache_path", lambda: path)
    quotes._beta_cache.clear()
    with patch.object(quotes.yf, "Ticker") as ticker:
        assert quotes.cached_beta("NVDA") is None
        ticker.assert_not_called()

