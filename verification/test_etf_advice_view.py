"""ETF 建議頁：觀察清單過濾、買賣期間、新鮮度用詞。"""
import json
import unittest
from unittest.mock import patch

from textual.app import App

from assettrack.analysis import (
    build_ticker_name_index,
    etf_source_freshness_lines,
    etf_stance_phrase,
    etf_stance_recommendation,
    generate_etf_recommendations,
    holding_display_symbol,
    holding_on_watchlist,
    normalize_etf_watchlist_symbol,
    partition_etf_recommendations,
    recommendation_symbol,
    render_etf_advice_view,
    suggested_etf_watchlist,
    watchlist_etf_activity,
)
from assettrack.models import Position
from assettrack.shared import Recommendation
from assettrack import storage


def _symbol_info(consensus, pct, evaluated=4, n_up=3, n_down=0, first="2026-08-10", last="2026-08-22"):
    return {
        "consensus": consensus,
        "consensus_pct": pct,
        "etfs_up": [f"E{i}" for i in range(n_up)],
        "etfs_down": [f"D{i}" for i in range(n_down)],
        "etfs_evaluated": evaluated,
        "est_total_value_delta": 8_000_000 if consensus == "up" else -8_000_000,
        "first_date": first,
        "last_date": last,
    }


def _report(symbols=None, freshness=None, contribs=None):
    return {
        "window_days": 14,
        "as_of": "2026-08-23",
        "etfs_ready_count": 4,
        "etfs_total_count": 8,
        "etfs_ready_pct": 50.0,
        "asset_classes": {"classes": {}},
        "symbols": symbols or {},
        "raw_contributions": contribs or [],
        "source_freshness": freshness or {
            "sources_unchanged": 0,
            "sources_total": 8,
            "all_sources_unchanged": False,
        },
    }


def _tilt(stance="long", window_days=14):
    return {
        "window_days": window_days,
        "aggregate": {
            "etfs_long": 5,
            "etfs_short": 1,
            "etfs_neutral": 2,
            "etfs_evaluated": 8,
            "breadth": 0.5,
            "stance": stance,
        },
    }


class NormalizeWatchlistTests(unittest.TestCase):
    def test_accepts_us_and_rejects_taiwan(self):
        self.assertEqual(normalize_etf_watchlist_symbol(" nvda "), "NVDA")
        self.assertEqual(normalize_etf_watchlist_symbol("$BRK.B"), "BRK.B")
        self.assertIsNone(normalize_etf_watchlist_symbol("2330.TW"))
        self.assertIsNone(normalize_etf_watchlist_symbol(""))

    def test_suggestions_skip_taiwan_and_use_option_underlying(self):
        positions = [
            Position(broker="m", symbol="NVDA", instrument_type="stock",
                     quantity=1, currency="USD", market="US"),
            Position(broker="m", symbol="2330.TW", instrument_type="stock",
                     quantity=1, currency="TWD", market="TW"),
            Position(
                broker="m", symbol="TSLA260918C00300000", instrument_type="option",
                quantity=1, currency="USD", market="US",
                underlying="TSLA", option_type="call",
            ),
        ]
        self.assertEqual(suggested_etf_watchlist(positions), ["NVDA", "TSLA"])


class WatchlistActivityTests(unittest.TestCase):
    def test_only_watched_symbols_and_period_are_listed(self):
        report = _report(
            symbols={
                "NVDA": _symbol_info("up", 75),
                "AAPL": _symbol_info("down", 80),
            },
        )
        rows = watchlist_etf_activity(report, ["NVDA", "AMD"])
        self.assertEqual([row["symbol"] for row in rows], ["NVDA", "AMD"])
        self.assertTrue(rows[0]["has_trade"])
        self.assertEqual(rows[0]["first_date"], "2026-08-10")
        self.assertEqual(rows[0]["last_date"], "2026-08-22")
        self.assertFalse(rows[1]["has_trade"])

    def test_holding_filter_hides_unwatched_names(self):
        watch = {"NVDA"}
        self.assertTrue(holding_on_watchlist({"symbol": "NVDA"}, watch))
        self.assertFalse(holding_on_watchlist({"symbol": "AAPL"}, watch))
        self.assertFalse(holding_on_watchlist({"symbol": "NVDA"}, set()))

    def test_holdings_detail_prefers_ticker_over_cusip(self):
        """ARK / 13F rows carry CUSIP; the Symbol column must still show TSLA."""
        self.assertEqual(
            holding_display_symbol({
                "symbol": "TSLA",
                "name": "TESLA INC",
                "cusip": "88160R101",
                "figi": "BBG000N9MNX3",
            }),
            "TSLA",
        )
        index = build_ticker_name_index({
            "ARKK": [{"date": "x", "holdings": [
                {"symbol": "TSLA", "name": "TESLA INC"},
            ]}],
        })
        self.assertEqual(
            holding_display_symbol({
                "symbol": "88160R101:SH",
                "cusip": "88160R101",
                "name": "TESLA INC",
            }, name_index=index),
            "TSLA",
        )
        self.assertEqual(
            holding_display_symbol({"cusip": "88160R101"}),
            "88160R101",
        )


class StorageWatchlistTests(unittest.TestCase):
    def test_configured_after_save(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(storage, "get_data_dir", return_value=Path(tmp)):
                self.assertFalse(storage.etf_watchlist_is_configured("ray"))
                self.assertEqual(storage.load_etf_watchlist("ray"), [])
                storage.save_etf_watchlist("ray", ["nvda", "NVDA", "AMD"])
                self.assertTrue(storage.etf_watchlist_is_configured("ray"))
                self.assertEqual(storage.load_etf_watchlist("ray"), ["NVDA", "AMD"])
                payload = json.loads((Path(tmp) / "ray_etf_watchlist.json").read_text())
                self.assertEqual(payload["tickers"], ["NVDA", "AMD"])


class RecommendationSymbolTests(unittest.TestCase):
    def test_reads_ticker_from_symbol_and_scale_ids(self):
        self.assertEqual(
            recommendation_symbol(Recommendation(
                rec_id="etf_sym:NVDA", category="etf", direction="多",
                verdict="", basis="",
            )),
            "NVDA",
        )
        self.assertEqual(
            recommendation_symbol(Recommendation(
                rec_id="etf_scale:ARKK:TSLA", category="etf", direction="多",
                verdict="", basis="",
            )),
            "TSLA",
        )
        self.assertIsNone(recommendation_symbol(Recommendation(
            rec_id="etf_ac:stock", category="etf", direction="多",
            verdict="", basis="",
        )))


class PartitionEtfRecommendationsTests(unittest.TestCase):
    def test_held_and_tracked_come_before_unrelated(self):
        nvda = Recommendation(
            rec_id="etf_sym:NVDA", category="etf", direction="多",
            verdict="NVDA", basis="",
        )
        amd = Recommendation(
            rec_id="etf_sym:AMD", category="etf", direction="空",
            verdict="AMD", basis="",
        )
        rotation = Recommendation(
            rec_id="etf_ac:stock", category="etf", direction="多",
            verdict="stock", basis="",
        )
        related, other = partition_etf_recommendations(
            [nvda, amd, rotation], held={"NVDA"}, tracked={"AMD"},
        )
        self.assertEqual([r.rec_id for r in related], ["etf_sym:NVDA", "etf_sym:AMD"])
        self.assertEqual([r.rec_id for r in other], ["etf_ac:stock"])


class StanceWordingTests(unittest.TestCase):
    def test_stance_uses_window_not_daily(self):
        rec = etf_stance_recommendation(_tilt())[0]
        self.assertIn("近 14 日主動選股傾向", rec.verdict)
        self.assertNotIn("每日", rec.verdict)
        self.assertIn("近 14 日主動選股傾向", etf_stance_phrase(_tilt()))

    def test_insufficient_stance_stays_honest(self):
        rec = etf_stance_recommendation({
            "window_days": 14,
            "aggregate": {"etfs_evaluated": 0, "stance": "insufficient"},
        })[0]
        self.assertIn("資料累積中", rec.verdict)
        self.assertNotIn("每日", rec.verdict)


class FreshnessLinesTests(unittest.TestCase):
    def test_stale_sources_are_not_called_flat_or_no_trade(self):
        lines = etf_source_freshness_lines(_report(freshness={
            "sources_unchanged": 2,
            "sources_total": 8,
            "oldest_state_since": "2026-07-01",
            "max_unchanged_days": 20,
            "all_sources_unchanged": False,
        }))
        text = " ".join(lines)
        self.assertIn("2 檔", text)
        self.assertIn("未更新不是今日無交易", text)
        self.assertNotIn("持平", text)


class AdviceViewTests(unittest.TestCase):
    def test_watchlist_hides_other_symbols_and_annotates_period(self):
        report = _report(symbols={
            "NVDA": _symbol_info("up", 75),
            "AAPL": _symbol_info("down", 75),
        })
        positions = [Position(
            broker="manual", symbol="NVDA", instrument_type="stock",
            quantity=10, currency="USD", market="US",
        )]
        markup, mapping = render_etf_advice_view(
            report, _tilt(),
            positions=positions,
            watchlist=["NVDA"],
        )
        self.assertIn("觀察清單的 ETF 買賣", markup)
        self.assertIn("NVDA", markup)
        self.assertIn("2026-08-10～2026-08-22", markup)
        self.assertIn("與你目前偏多的部位方向一致", markup)
        self.assertNotIn("AAPL", markup)
        self.assertNotIn("可留意", markup)
        self.assertTrue(mapping)
        nvda = generate_etf_recommendations(report, positions=positions)
        nvda_rec = next(r for r in nvda if r.rec_id == "etf_sym:NVDA")
        self.assertIn("2026-08-10～2026-08-22", nvda_rec.verdict)

    def test_empty_watchlist_asks_for_setup(self):
        markup, _ = render_etf_advice_view(_report(), _tilt(), watchlist=[])
        self.assertIn("尚未設定觀察清單", markup)
        self.assertNotIn("可留意", markup)

    def test_watchlist_no_trade_still_has_formula_detail(self):
        report = _report(symbols={
            "NVDA": _symbol_info("flat", 0.0, n_up=0, n_down=0),
        })
        _, mapping = render_etf_advice_view(report, _tilt(), watchlist=["NVDA"])
        rec = mapping["r0"]
        self.assertTrue(rec.detail_sections)
        text = " ".join(
            f"{sec.get('formula','')} {sec.get('substitution','')} {sec.get('explanation','')}"
            for sec in rec.detail_sections
        )
        self.assertIn("NVDA", text)
        self.assertIn("2026-08-10～2026-08-22", text)
        self.assertIn("雙真實訊號", text)


class ActiveETFsLandingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pressing_six_lands_on_advice_tab(self):
        from assettrack import tui

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.ActiveETFsScreen("default", 32.5))

        with patch.object(tui.ActiveETFsScreen, "run_background_fetch"), \
                patch.object(tui.ActiveETFsScreen, "run_analysis_compute"), \
                patch.object(tui, "load_active_etf_universe", return_value=[]), \
                patch.object(tui, "hedge_fund_records", return_value=[]), \
                patch.object(tui, "cleanup_old_etf_caches"), \
                patch.object(tui, "user_priority_symbols", return_value=(set(), set())), \
                patch.object(tui, "load_manual_positions", return_value=([], [])), \
                patch.object(tui, "etf_watchlist_is_configured", return_value=True), \
                patch.object(tui, "load_etf_watchlist", return_value=["NVDA"]):
            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                screen = app.screen
                tabs = screen.query_one("#etf-main-tabs")
                self.assertEqual(tabs.active, "tab-etf-advice")
                self.assertTrue(screen.query_one("#etf-analysis-content"))

    async def test_holdings_detail_shows_ticker_not_cusip(self):
        from textual.widgets import DataTable
        from assettrack import tui

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.ActiveETFsScreen("default", 32.5))

        with patch.object(tui.ActiveETFsScreen, "run_background_fetch"), \
                patch.object(tui.ActiveETFsScreen, "run_analysis_compute"), \
                patch.object(tui, "load_active_etf_universe", return_value=[]), \
                patch.object(tui, "hedge_fund_records", return_value=[]), \
                patch.object(tui, "cleanup_old_etf_caches"), \
                patch.object(tui, "user_priority_symbols", return_value=(set(), set())), \
                patch.object(tui, "load_manual_positions", return_value=([], [])), \
                patch.object(tui, "etf_watchlist_is_configured", return_value=True), \
                patch.object(tui, "load_etf_watchlist", return_value=["TSLA"]):
            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                screen = app.screen
                screen._watchlist = ["TSLA"]
                screen.etf_cache = {
                    "ARKK": {
                        "holdings": [{
                            "symbol": "TSLA",
                            "name": "TESLA INC",
                            "cusip": "88160R101",
                            "weight": 9.55,
                            "shares": 1000,
                            "value": 350_000,
                        }],
                        "holdings_as_of_date": "2026-08-22",
                        "source_type": "etf",
                    }
                }
                screen._render_holdings("ARKK")
                table = screen.query_one("#etf-holdings-table", DataTable)
                rendered = " ".join(
                    " ".join(str(cell) for cell in table.get_row_at(index))
                    for index in range(table.row_count)
                )
                self.assertIn("TSLA", rendered)
                self.assertNotIn("88160R101", rendered)

    async def test_first_visit_requires_watchlist_editor(self):
        from assettrack import tui

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.ActiveETFsScreen("default", 32.5))

        with patch.object(tui.ActiveETFsScreen, "run_background_fetch"), \
                patch.object(tui.ActiveETFsScreen, "run_analysis_compute"), \
                patch.object(tui, "load_active_etf_universe", return_value=[]), \
                patch.object(tui, "hedge_fund_records", return_value=[]), \
                patch.object(tui, "cleanup_old_etf_caches"), \
                patch.object(tui, "user_priority_symbols", return_value=(set(), set())), \
                patch.object(tui, "load_manual_positions", return_value=([], [])), \
                patch.object(tui, "etf_watchlist_is_configured", return_value=False), \
                patch.object(tui, "load_etf_watchlist", return_value=[]):
            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.EtfWatchlistEditor)


if __name__ == "__main__":
    unittest.main()
