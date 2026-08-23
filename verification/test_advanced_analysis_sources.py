"""bug#00123 — AdvancedAnalysis 全部標的持平／買賣 0 的根因修正。

根因：Yahoo `topHoldings` 依基金揭露頻率更新（且不揭露股數），實測 32 檔 ETF 在 16 個
交易日內權重完全沒變過，於是「權重與股數方向必須一致」的規則永遠得到 flat，AUM 也同步
凍結，`value_delta` 退化成恰好 0.0，整張表被印成「持平／$0」。

本檔涵蓋三條修正線：
  A. 以 SEC 13F 逐季真實申報股數產生方向訊號（跨申報人以 CUSIP join）
  B. ARK 官方每日完整持股 CSV（含真實股數與市值）
  C. 「來源未更新」不得偽裝成「持平／$0」
以及使用者要求的精確部位分層排序（持有 → 追蹤類股 → 其他）。
"""
import unittest
from unittest.mock import patch

from textual.app import App

from assettrack.analysis import (
    build_ticker_name_index,
    compute_institution_trends,
    compute_symbol_trends,
    normalize_company_name,
    normalize_institution_snapshots,
    resolve_position_ticker,
)
from assettrack.ark_holdings import (
    fetch_official_daily_holdings,
    is_official_daily_source,
    parse_ark_holdings_csv,
)
from assettrack import analysis, tui


def _frozen_yahoo_snapshots(dates, weight=5.0, aum=6_000_000_000, extra=()):
    """A source that republishes a byte-identical portfolio every day.

    `extra` adds further holdings — these also seed the offline
    company-name → ticker index the 13F section resolves against.
    """
    holdings = [{"symbol": "AAPL", "weight": weight, "name": "Apple Inc"}]
    holdings += [dict(item) for item in extra]
    return [{"date": date, "aum": aum, "holdings": holdings} for date in dates]


_TSLA_IN_INDEX = ({"symbol": "TSLA", "weight": 1.0, "name": "Tesla Inc"},)


class SourceStalenessTests(unittest.TestCase):
    """C：來源沒有發布新狀態 ≠ 本期沒有交易。"""

    def test_unchanged_source_is_reported_as_stale_not_as_a_zero_measurement(self):
        report = compute_symbol_trends(
            {"ETF1": _frozen_yahoo_snapshots(
                ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"],
            )},
            window_days=14,
            as_of="2026-07-24",
        )

        symbol = report["symbols"]["AAPL"]
        self.assertEqual(symbol["status"], "source_unchanged")
        self.assertTrue(symbol["source_unchanged"])
        # The old code returned exactly 0.0 here — identical AUM x identical
        # weight — and the screen printed it as a measured "no net change".
        self.assertIsNone(symbol["net_value_delta"])
        self.assertIsNone(report["flow_totals"]["net_value_delta"])

        freshness = report["source_freshness"]
        self.assertTrue(freshness["all_sources_unchanged"])
        self.assertEqual(freshness["sources_unchanged"], 1)
        self.assertEqual(freshness["sources_state_changed"], 0)
        self.assertEqual(freshness["oldest_state_since"], "2026-07-20")
        self.assertEqual(freshness["max_unchanged_days"], 3)

    def test_moving_aum_still_yields_a_real_period_change(self):
        """A stale *portfolio* with a live AUM is still a real measurement —
        the staleness rule must not swallow genuine period changes."""
        snapshots = _frozen_yahoo_snapshots(["2026-07-22", "2026-07-23"])
        snapshots[-1]["aum"] = 6_100_000_000

        report = compute_symbol_trends(
            {"ETF1": snapshots}, window_days=14, as_of="2026-07-24",
        )
        symbol = report["symbols"]["AAPL"]
        self.assertEqual(symbol["net_value_delta"], 5_000_000)
        self.assertNotEqual(symbol["status"], "source_unchanged")


class ReportedShareSignalTests(unittest.TestCase):
    """A：申報股數是精確揭露值，不需要權重背書。"""

    def _two_quarters(self, shares0, shares1, value0=1_000_000.0, value1=1_000_000.0):
        def snapshot(date, shares, value):
            return {
                "date": date,
                "aum": 100_000_000.0,
                "holdings": [{
                    "symbol": "0001:SH", "cusip": "0001", "amount_type": "SH",
                    "issuer": "EXAMPLE CORP", "name": "EXAMPLE CORP COM",
                    "instrument_type": "stock",
                    "weight": 1.0, "shares": shares, "value": value,
                }],
            }
        return [
            snapshot("2025-12-31", shares0, value0),
            snapshot("2026-03-31", shares1, value1),
        ]

    def test_weight_agreement_rule_would_reject_every_real_13f_trade(self):
        """Regression guard: the ETF path's 0.5pp weight bar is unreachable for
        a filing whose positions each sit far below 0.5% of the book."""
        snapshots = {"FILER": self._two_quarters(100_000, 200_000)}
        etf_style = compute_symbol_trends(
            snapshots, window_days=4000, as_of="2026-03-31", endpoint_k=1,
        )
        self.assertEqual(etf_style["symbols"]["0001:SH"]["consensus"], "flat")

        reported = compute_symbol_trends(
            snapshots, window_days=4000, as_of="2026-03-31", endpoint_k=1,
            flat_threshold_pp=0.0, reported_share_signal=True,
        )
        self.assertEqual(reported["symbols"]["0001:SH"]["consensus"], "up")

    def test_relative_threshold_filters_immaterial_restatements(self):
        for shares1, expected in ((103_000, "flat"), (110_000, "up"), (90_000, "down")):
            with self.subTest(shares1=shares1):
                report = compute_symbol_trends(
                    {"FILER": self._two_quarters(100_000, shares1)},
                    window_days=4000, as_of="2026-03-31", endpoint_k=1,
                    flat_threshold_pp=0.0, reported_share_signal=True,
                    rel_share_threshold=0.05,
                )
                self.assertEqual(
                    report["symbols"]["0001:SH"]["consensus"], expected,
                )

    def test_trade_value_cannot_contradict_the_direction_beside_it(self):
        """Shares cut in half while the price triples: the exposure delta is
        positive, but the filer plainly sold. |value_delta| used to be printed
        as 賣出總額 next to a positive net."""
        report = compute_symbol_trends(
            {"FILER": self._two_quarters(
                100_000, 50_000, value0=1_000_000.0, value1=1_500_000.0,
            )},
            window_days=4000, as_of="2026-03-31", endpoint_k=1,
            flat_threshold_pp=0.0, reported_share_signal=True,
        )
        symbol = report["symbols"]["0001:SH"]
        self.assertEqual(symbol["consensus"], "down")
        self.assertGreater(symbol["net_value_delta"], 0)          # revaluation
        self.assertLess(symbol["confirmed_net_trade_value"], 0)   # what traded
        self.assertIsNone(symbol["buy_trade_value"])
        self.assertAlmostEqual(symbol["sell_trade_value"], 50_000 * 30.0)


class InstitutionJoinTests(unittest.TestCase):
    """A：不同申報人對同一檔證券必須併成同一列，否則永遠形成不了共識。"""

    def _filer(self, key_symbol, figi, shares0, shares1):
        def snapshot(date, shares):
            return {
                "date": date,
                "aum": 50_000_000.0,
                "holdings": [{
                    "symbol": key_symbol, "cusip": "88160R101", "figi": figi,
                    "amount_type": "SH", "issuer": "TESLA INC",
                    "name": "TESLA INC COM", "instrument_type": "stock",
                    "weight": 2.0, "shares": shares, "value": shares * 300.0,
                }],
            }
        return [snapshot("2025-12-31", shares0), snapshot("2026-03-31", shares1)]

    def test_cusip_join_lets_filers_with_and_without_figi_agree(self):
        report = compute_institution_trends({
            # One filer keys on FIGI, the other on CUSIP — the stored ids differ
            # for the very same security.
            "13F:A": self._filer("BBG000N9MNX3:SH", "BBG000N9MNX3", 10_000, 20_000),
            "13F:B": self._filer("88160R101:SH", None, 5_000, 9_000),
        })
        self.assertEqual(report["report_dates"], ["2025-12-31", "2026-03-31"])
        self.assertEqual(len(report["symbols"]), 1)
        joined = report["symbols"]["88160R101:SH"]
        self.assertEqual(joined["etfs_evaluated"], 2)
        self.assertEqual(joined["consensus"], "up")
        self.assertEqual(joined["consensus_pct"], 100.0)

    def test_normalization_does_not_mutate_the_stored_snapshots(self):
        original = self._filer("BBG000N9MNX3:SH", "BBG000N9MNX3", 1, 2)
        normalize_institution_snapshots(original)
        self.assertEqual(original[0]["holdings"][0]["symbol"], "BBG000N9MNX3:SH")

    def test_single_quarter_filer_is_not_comparable(self):
        one = self._filer("88160R101:SH", None, 5_000, 9_000)[:1]
        self.assertEqual(compute_institution_trends({"13F:A": one})["symbols"], {})


class TickerResolutionTests(unittest.TestCase):
    """13F 只有發行人名稱與 CUSIP，要靠本機 ETF 快照離線解析成代碼。"""

    INDEX_SOURCE = {
        "ETF1": [{
            "date": "2026-07-20", "aum": 1.0,
            "holdings": [
                {"symbol": "TSM", "name": "Taiwan Semiconductor Manufacturing Co Ltd ADR"},
                {"symbol": "NVDA", "name": "NVIDIA Corp"},
            ],
        }],
    }

    def test_sec_abbreviations_resolve_to_the_provider_spelling(self):
        index = build_ticker_name_index(self.INDEX_SOURCE)
        self.assertEqual(
            resolve_position_ticker(
                {"symbol": "874039100:SH", "issuer": "TAIWAN SEMICONDUCTOR MFG LTD"},
                index,
            ),
            "TSM",
        )

    def test_unmatched_issuer_returns_none_rather_than_a_guess(self):
        index = build_ticker_name_index(self.INDEX_SOURCE)
        self.assertIsNone(resolve_position_ticker(
            {"symbol": "000000000:SH", "issuer": "SOME PRIVATE HOLDCO"}, index,
        ))

    def test_ambiguous_names_are_dropped(self):
        index = build_ticker_name_index({"ETF1": [{
            "date": "2026-07-20", "aum": 1.0,
            "holdings": [
                {"symbol": "AAA", "name": "Example Inc"},
                {"symbol": "BBB", "name": "Example Corp"},
            ],
        }]})
        self.assertNotIn(normalize_company_name("Example Inc"), index)


class PositionOrderingTests(unittest.TestCase):
    """使用者要求：持有部位 → 追蹤類股 → 其他（字母）。"""

    def _key(self, symbol, **info):
        info.setdefault("position", {"symbol": symbol})
        return tui.position_display_sort_key(
            symbol, info, held={"AMD", "NVDA"}, tracked={"QCOM"}, name_index={},
        )

    def test_tiers_are_held_then_tracked_then_others(self):
        self.assertEqual(self._key("AMD")[0], 0)
        self.assertEqual(self._key("QCOM")[0], 1)
        self.assertEqual(self._key("ZZZZ")[0], 2)

    def test_ordering_is_held_tracked_alphabetical(self):
        symbols = ["ZZZZ", "QCOM", "AAAA", "NVDA", "AMD"]
        ordered = sorted(symbols, key=lambda s: self._key(s))
        self.assertEqual(ordered, ["AMD", "NVDA", "QCOM", "AAAA", "ZZZZ"])

    def test_within_a_tier_real_signals_outrank_flat_rows(self):
        signal = self._key("ZZZA", consensus="up", net_value_delta=1.0)
        flat = self._key("AAAA", consensus="flat", net_value_delta=9_999.0)
        self.assertLess(signal, flat)

    def test_thirteen_f_position_is_tiered_by_its_resolved_ticker(self):
        key = tui.position_display_sort_key(
            "88160R101:SH",
            {"position": {"issuer": "NVIDIA CORPORATION"}},
            held={"NVDA"}, tracked=set(),
            name_index=build_ticker_name_index({"E": [{
                "date": "2026-07-20", "aum": 1.0,
                "holdings": [{"symbol": "NVDA", "name": "NVIDIA Corp"}],
            }]}),
        )
        self.assertEqual(key[0], 0)


class ArkOfficialHoldingsTests(unittest.TestCase):
    """B：官方每日完整持股（含真實股數）。"""

    CSV = (
        'date,fund,company,ticker,cusip,shares,"market value ($)",weight (%)\n'
        '07/24/2026,ARKK,TESLA INC,TSLA,88160R101,"3,500,000","$1,120,000,000.00",10.19\n'
        '07/24/2026,ARKK,TEMPUS AI INC,TEM,88023B103,"9,000,000","$360,000,000.00",5.58\n'
        '07/24/2026,ARKK,ADVANCED MICRO DEVICES,AMD,007903107,"1,200,000","$324,000,000.00",4.98\n'
        '07/24/2026,ARKK,CRISPR THERAPEUTICS,CRSP,H17182108,"5,000,000","$310,000,000.00",4.87\n'
        '07/24/2026,ARKK,ROBINHOOD MARKETS,HOOD,770700102,"4,000,000","$290,000,000.00",4.49\n'
        ',,,,,,,\n'
        'The content of this document is for informational purposes only.,,,,,,,\n'
    ).encode()

    def test_only_funds_with_a_daily_file_take_the_official_path(self):
        self.assertTrue(is_official_daily_source("ARKK"))
        self.assertFalse(is_official_daily_source("JEPI"))

    def test_parse_yields_real_shares_values_and_derived_price(self):
        parsed = parse_ark_holdings_csv(self.CSV)
        self.assertEqual(parsed["as_of_date"], "2026-07-24")
        self.assertEqual(len(parsed["holdings"]), 5)  # disclaimer rows dropped
        self.assertEqual(parsed["holdings_source"], "ark_official_daily")
        tsla = parsed["holdings"][0]
        self.assertEqual(tsla["symbol"], "TSLA")
        self.assertEqual(tsla["shares"], 3_500_000)
        self.assertEqual(tsla["value"], 1_120_000_000.0)
        self.assertEqual(tsla["price"], 320.0)

    def test_official_daily_shares_produce_the_signal_yahoo_cannot(self):
        day1 = parse_ark_holdings_csv(self.CSV)
        day2 = parse_ark_holdings_csv(
            self.CSV.replace(b'"3,500,000","$1,120,000,000.00",10.19',
                             b'"4,200,000","$1,344,000,000.00",11.90')
        )
        report = compute_symbol_trends(
            {"ARKK": [
                {"date": "2026-07-23", "aum": day1["aum"], "holdings": day1["holdings"]},
                {"date": "2026-07-24", "aum": day2["aum"], "holdings": day2["holdings"]},
            ]},
            window_days=14, as_of="2026-07-24",
        )
        self.assertEqual(report["symbols"]["TSLA"]["consensus"], "up")
        self.assertEqual(report["etfs_ready_count"], 1)

    def test_error_page_and_truncated_file_are_rejected(self):
        self.assertIsNone(parse_ark_holdings_csv(b"<html><body>404</body></html>"))
        self.assertIsNone(parse_ark_holdings_csv(
            b'date,company,ticker,shares,"market value ($)"\n'
            b'07/24/2026,TESLA,TSLA,100,"$1.00"\n'
        ))

    def test_network_failure_falls_back_instead_of_fabricating(self):
        def boom(url):
            raise OSError("connection reset")
        self.assertIsNone(fetch_official_daily_holdings("ARKK", get_bytes=boom))


class AdvancedAnalysisScreenSourceTests(unittest.IsolatedAsyncioTestCase):
    async def _render(self, etf_snapshots, filer_snapshots=None, user="default"):
        filer_snapshots = filer_snapshots or {}

        def loader(symbol, *args, **kwargs):
            if symbol in filer_snapshots:
                return filer_snapshots[symbol]
            return etf_snapshots

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.AdvancedAnalysisScreen(user))

        with patch.object(tui, "active_etf_symbols", return_value=["ETF1"]), \
                patch.object(tui, "load_etf_daily_snapshots", side_effect=loader), \
                patch.object(tui, "hedge_fund_records", return_value=[
                    {"id": entity} for entity in filer_snapshots
                ]), \
                patch.object(tui, "user_priority_symbols",
                             return_value=({"TSLA"}, set())), \
                patch.object(tui, "_active_params", return_value={"etf": {}}):
            app = HostApp()
            async with app.run_test(size=(200, 50)) as pilot:
                await pilot.pause()
                table = app.screen.query_one("#aa-table")
                return [
                    [str(cell) for cell in table.get_row_at(index)]
                    for index in range(table.row_count)
                ]

    async def test_stale_source_row_says_so_instead_of_flat_and_zero(self):
        rows = await self._render(_frozen_yahoo_snapshots(
            ["2026-07-20", "2026-07-21", "2026-07-22"],
        ))
        aapl = next(row for row in rows if "AAPL" in row[0] or "Apple" in row[0])
        self.assertIn("來源未更新", aapl[-1])
        self.assertNotIn("持平", aapl[-1])
        # Buy / sell / net must not claim a measured zero.
        self.assertEqual(aapl[-2], "—")
        self.assertEqual(aapl[-3], "—")
        self.assertEqual(aapl[-4], "—")

    async def test_thirteen_f_section_renders_real_buy_sell_amounts(self):
        def filing(date, shares):
            return {
                "date": date, "aum": 10_000_000.0,
                "holdings": [{
                    "symbol": "88160R101:SH", "cusip": "88160R101",
                    "amount_type": "SH", "issuer": "TESLA INC",
                    "name": "TESLA INC COM", "instrument_type": "stock",
                    "weight": 3.0, "shares": shares, "value": shares * 300.0,
                }],
            }

        rows = await self._render(
            _frozen_yahoo_snapshots(["2026-07-20", "2026-07-21"], extra=_TSLA_IN_INDEX),
            filer_snapshots={
                "13F:A": [filing("2025-12-31", 10_000), filing("2026-03-31", 20_000)],
                "13F:B": [filing("2025-12-31", 4_000), filing("2026-03-31", 9_000)],
            },
        )
        tesla = next(
            row for row in rows
            if "13F 申報" in row[1] and "TESLA" in row[0].upper()
        )
        self.assertIn("看漲", tesla[-1])
        self.assertNotEqual(tesla[-4], "$0")
        # It is one of the user's holdings, so it must be marked and hoisted
        # above the unmarked 13F rows.
        self.assertIn("★", tesla[0])

    async def test_option_rows_never_claim_a_direction_on_the_underlying(self):
        def filing(date, shares):
            return {
                "date": date, "aum": 10_000_000.0,
                "holdings": [{
                    "symbol": "88160R101:PUT", "cusip": "88160R101",
                    "option_type": "PUT", "instrument_type": "option",
                    "issuer": "TESLA INC", "name": "PUT TESLA INC COM",
                    "expiration": None, "strike": None,
                    "weight": 3.0, "shares": shares, "value": shares * 30.0,
                }],
            }

        rows = await self._render(
            _frozen_yahoo_snapshots(["2026-07-20", "2026-07-21"], extra=_TSLA_IN_INDEX),
            filer_snapshots={
                "13F:A": [filing("2025-12-31", 10_000), filing("2026-03-31", 20_000)],
                "13F:B": [filing("2025-12-31", 4_000), filing("2026-03-31", 9_000)],
            },
        )
        put = next(
            row for row in rows
            if "13F 申報" in row[1] and "TESLA" in row[0].upper()
        )
        # 13F does not disclose whether the manager bought or wrote the option,
        # so "more PUTs" is not evidence of a bearish view on TSLA.
        self.assertIn("增持", put[-1])
        self.assertNotIn("看漲", put[-1])
        self.assertNotIn("看跌", put[-1])
        self.assertNotIn("PUT PUT", put[0])


if __name__ == "__main__":
    unittest.main()


# ─────────────────────────────────────────────────────────────────────────────
# bug#00124 — 觀察區間、資料年代標示、allocation
# ─────────────────────────────────────────────────────────────────────────────

class ObservationWindowTests(unittest.TestCase):
    """為什麼視窗以「申報期數」而非日曆天數計。"""

    def test_calendar_windows_drift_in_and_out_of_usefulness(self):
        """13F 季末後 45 天才申報，第二新一期的年齡在 136~227 天之間游移。
        4 個月低於 136 天下限、6 個月每年有四段空窗 —— 這是選 12 個月的理由。"""
        import datetime as dt
        quarter_ends = [
            dt.date(y, m, d)
            for y in (2024, 2025, 2026, 2027)
            for (m, d) in ((3, 31), (6, 30), (9, 30), (12, 31))
        ]

        def periods_in_window(as_of, window_days):
            cutoff = as_of - dt.timedelta(days=window_days)
            published = [
                q for q in quarter_ends
                if q + dt.timedelta(days=analysis.INSTITUTION_FILING_DEADLINE_DAYS)
                <= as_of
            ]
            return [q for q in published if q >= cutoff]

        def comparable_ratio(window_days):
            start, end = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
            days = (end - start).days + 1
            ok = sum(
                1 for i in range(days)
                if len(periods_in_window(start + dt.timedelta(days=i), window_days)) >= 2
            )
            return ok / days

        self.assertEqual(comparable_ratio(122), 0.0)          # 4 個月：永遠湊不到 2 期
        self.assertLess(comparable_ratio(183), 0.6)           # 6 個月：約半年可用
        self.assertEqual(comparable_ratio(243), 1.0)          # 8 個月：剛好不空窗
        self.assertEqual(comparable_ratio(365), 1.0)          # 12 個月：本專案選用

        # 今天正好落在 6 個月視窗的空窗期內 —— 若採用它，畫面會再次全空。
        today = dt.date(2026, 7, 28)
        self.assertEqual(len(periods_in_window(today, 183)), 1)
        self.assertEqual(len(periods_in_window(today, 365)), 3)

    def test_retained_history_does_not_widen_the_comparison(self):
        """保留 4 期是為了算連續同向季數，比較口徑必須仍是最新兩期 —— 否則季度
        交易訊號會被悄悄換成年度部位漂移。"""
        def filing(date, shares):
            return {
                "date": date, "aum": 10_000_000.0,
                "holdings": [{
                    "symbol": "0001:SH", "cusip": "0001", "amount_type": "SH",
                    "issuer": "EXAMPLE CORP", "name": "EXAMPLE CORP COM",
                    "instrument_type": "stock", "weight": 1.0,
                    "shares": shares, "value": shares * 100.0,
                }],
            }
        snaps = {"13F:A": [
            filing("2025-06-30", 1_000), filing("2025-09-30", 2_000),
            filing("2025-12-31", 4_000), filing("2026-03-31", 8_000),
        ]}
        report = compute_institution_trends(snaps, today="2026-07-28")
        self.assertEqual(report["report_dates"], ["2025-12-31", "2026-03-31"])
        self.assertEqual(len(report["retained_report_dates"]), 4)
        # 4,000 → 8,000 是本季的交易；不可變成 1,000 → 8,000 的一年變化。
        self.assertEqual(report["raw_contributions"][0]["share_delta"], 4_000)

    def test_consecutive_same_direction_quarters_are_counted(self):
        def filing(date, shares):
            return {
                "date": date, "aum": 10_000_000.0,
                "holdings": [{
                    "symbol": "0001:SH", "cusip": "0001", "amount_type": "SH",
                    "issuer": "EXAMPLE CORP", "name": "EXAMPLE CORP COM",
                    "instrument_type": "stock", "weight": 1.0,
                    "shares": shares, "value": shares * 100.0,
                }],
            }
        rising = compute_institution_trends({"13F:A": [
            filing("2025-06-30", 1_000), filing("2025-09-30", 2_000),
            filing("2025-12-31", 4_000), filing("2026-03-31", 8_000),
        ]}, today="2026-07-28")["symbols"]["0001:SH"]
        self.assertEqual(rising["transitions_evaluated"], 3)
        self.assertEqual(rising["same_direction_quarters"], 3)

        one_off = compute_institution_trends({"13F:A": [
            filing("2025-06-30", 8_000), filing("2025-09-30", 8_000),
            filing("2025-12-31", 8_000), filing("2026-03-31", 16_000),
        ]}, today="2026-07-28")["symbols"]["0001:SH"]
        self.assertEqual(one_off["same_direction_quarters"], 1)


class ProvenanceTests(unittest.TestCase):
    """使用者要求 1：要看得出這是什麼時候的資料。"""

    def _snaps(self, filing_dates=None):
        filing_dates = filing_dates or {}
        def filing(date, shares):
            row = {
                "date": date, "aum": 10_000_000.0,
                "holdings": [{
                    "symbol": "0001:SH", "cusip": "0001", "amount_type": "SH",
                    "issuer": "EXAMPLE CORP", "name": "EXAMPLE CORP COM",
                    "instrument_type": "stock", "weight": 1.0,
                    "shares": shares, "value": shares * 100.0,
                }],
            }
            if date in filing_dates:
                row["filing_date"] = filing_dates[date]
            return row
        return {"13F:A": [filing("2025-12-31", 1_000), filing("2026-03-31", 2_000)]}

    def test_report_date_filing_date_age_and_next_update_are_all_reported(self):
        prov = compute_institution_trends(
            self._snaps({"2026-03-31": "2026-05-11"}), today="2026-07-28",
        )["provenance"]
        self.assertEqual(prov["report_date_to"], "2026-03-31")
        self.assertEqual(prov["data_age_days"], 119)
        self.assertEqual(prov["next_report_date"], "2026-06-30")
        self.assertEqual(prov["next_filing_due"], "2026-08-14")
        latest = prov["periods"][-1]
        self.assertEqual(latest["filing_date"], "2026-05-11")
        self.assertFalse(latest["filing_date_estimated"])

    def test_missing_filing_date_falls_back_to_the_deadline_and_says_so(self):
        prov = compute_institution_trends(
            self._snaps(), today="2026-07-28",
        )["provenance"]
        latest = prov["periods"][-1]
        self.assertEqual(latest["filing_date"], "2026-05-15")  # 2026-03-31 + 45d
        self.assertTrue(latest["filing_date_estimated"])

    def test_trade_timing_is_an_interval_because_13f_has_no_trade_date(self):
        prov = compute_institution_trends(self._snaps(), today="2026-07-28")["provenance"]
        self.assertFalse(prov["trade_date_disclosed"])
        self.assertEqual(prov["trade_window_from"], "2025-12-31")
        self.assertEqual(prov["trade_window_to"], "2026-03-31")

    def test_storage_persists_the_filing_date_for_future_runs(self):
        import tempfile, json, pathlib
        from unittest.mock import patch
        from assettrack import storage
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                storage, "_etf_history_path",
                lambda symbol: pathlib.Path(tmp) / f"{symbol}.jsonl",
            ):
                storage.append_etf_daily_snapshot(
                    "13F_TEST",
                    [{"symbol": "X", "weight": 1.0, "shares": 10, "value": 100}],
                    1_000.0, snapshot_date="2026-03-31",
                    metadata={"filing_date": "2026-05-11", "accession": None},
                )
                line = json.loads(
                    (pathlib.Path(tmp) / "13F_TEST.jsonl").read_text().strip()
                )
        self.assertEqual(line["filing_date"], "2026-05-11")
        # An unknown accession must stay absent, not be written as a null that
        # later reads like a real answer.
        self.assertNotIn("accession", line)


class AllocationTests(unittest.TestCase):
    """使用者要求 2：看得出 allocation。"""

    def test_allocation_is_value_over_combined_aum_not_an_average_of_weights(self):
        def filer(entity_aum, weight_start, weight_end):
            def snap(date, weight):
                return {
                    "date": date, "aum": entity_aum,
                    "holdings": [{
                        "symbol": "0001:SH", "cusip": "0001", "amount_type": "SH",
                        "issuer": "EXAMPLE CORP", "name": "EXAMPLE CORP COM",
                        "instrument_type": "stock", "weight": weight,
                        "shares": weight * 1_000,
                        "value": entity_aum * weight / 100.0,
                    }],
                }
            return [snap("2025-12-31", weight_start), snap("2026-03-31", weight_end)]

        report = compute_institution_trends({
            "13F:BIG": filer(900_000_000.0, 1.0, 2.0),
            "13F:SMALL": filer(100_000_000.0, 10.0, 10.0),
        }, today="2026-07-28")
        alloc = report["symbols"]["0001:SH"]

        # 期初 (900M×1% + 100M×10%) / 1,000M = 1.9%；期末 (900M×2% + 100M×10%) / 1,000M = 2.8%
        self.assertAlmostEqual(alloc["allocation_start_pct"], 1.9, places=6)
        self.assertAlmostEqual(alloc["allocation_end_pct"], 2.8, places=6)
        self.assertAlmostEqual(alloc["allocation_delta_pp"], 0.9, places=6)
        # 若改用「權重平均」會得到 5.5% → 6.0%，把 1 億的部位當成跟 9 億同等重要。
        self.assertNotAlmostEqual(alloc["allocation_start_pct"], 5.5, places=3)

    def test_allocation_is_none_when_any_denominator_is_missing(self):
        report = compute_symbol_trends(
            {"ETF1": [
                {"date": "2026-07-22", "aum": None,
                 "holdings": [{"symbol": "AAPL", "weight": 5.0}]},
                {"date": "2026-07-23", "aum": None,
                 "holdings": [{"symbol": "AAPL", "weight": 6.0}]},
            ]},
            window_days=14, as_of="2026-07-24",
        )
        self.assertIsNone(report["symbols"]["AAPL"]["allocation_start_pct"])
        self.assertIsNone(report["symbols"]["AAPL"]["allocation_delta_pp"])
