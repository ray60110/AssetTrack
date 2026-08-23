"""bug#00125 迴歸測試：確認「預估展望」不再出現寫死的 +14 天 fallback。

問題背景（使用者審查）：快照累積天數不足時（例如剛上線只有 6 天、跨 5 日曆天），
所有 h≥7 的前瞻樣本數都是 0，`find_best_horizon_confidence` 舊版會回傳寫死的
`best_horizon=14`，導致畫面上**每一檔標的**都印出「預估展望：未來 +14 天」——那個 14
其實是 OPTIONS_FLOW_WINDOW_DAYS（回看觀察視窗）被誤用成前瞻期的預設值。

同時舊版守門條件 `direction and has_conf and not meets_thr` 在 `has_conf=False`
（無任何統計證據）時永遠不成立，於是 n=0 的方向會照樣以看多/看空呈現。
"""
from __future__ import annotations

import pytest

from assettrack.backtest_stats import (
    find_best_horizon_confidence,
    has_backtest_evidence,
    evaluable_horizons,
    max_evaluable_horizon,
    horizon_coverage_note,
    confidence_percentage_info,
    direction_significance,
)
from assettrack.options_analysis import (
    VERDICT_HORIZONS, _em_dte, _em_formula, _em_iv_note,
)


def _empty_report(horizons=VERDICT_HORIZONS) -> dict:
    """模擬「快照太短，所有前瞻期都沒有樣本」的回測結果。"""
    return {
        "horizons": list(horizons),
        "min_signals": 20,
        "total_snapshot_days": 6,
        "first_date": "2026-07-23",
        "last_date": "2026-07-28",
        "by_horizon": {
            h: {
                "baseline_up_rate": None, "baseline_n": 0,
                "bullish_n": 0, "bullish_hit_rate": None,
                "bearish_n": 0, "bearish_hit_rate": None,
                "evaluated_signals": 0, "ready": False,
                "significance": {"up": None, "down": None, "num_tests": 1},
            }
            for h in horizons
        },
    }


def _short_span_report() -> dict:
    """只有 h=1 有樣本（跨 5 日曆天的真實情形），長前瞻期全空。"""
    rep = _empty_report()
    rep["by_horizon"][1] = {
        "baseline_up_rate": 0.4, "baseline_n": 5,
        "bullish_n": 0, "bullish_hit_rate": None,
        "bearish_n": 4, "bearish_hit_rate": 0.75,
        "evaluated_signals": 4, "ready": False,
        "significance": {
            "up": None,
            "down": {"n": 4, "ess": 4, "hits": 3, "hits_ess": 3, "hit_rate": 0.75,
                     "baseline_rate": 0.6, "ci_lo": 0.30, "ci_hi": 0.95,
                     "p_value": 0.48, "significant_95": False, "significant_adj": False,
                     "alpha_adj": 0.05, "num_tests": 1},
            "num_tests": 1,
        },
    }
    rep["by_horizon"][5] = {
        "baseline_up_rate": 0.0, "baseline_n": 1,
        "bullish_n": 0, "bullish_hit_rate": None,
        "bearish_n": 0, "bearish_hit_rate": None,
        "evaluated_signals": 0, "ready": False,
        "significance": {"up": None, "down": None, "num_tests": 1},
    }
    return rep


class TestNoFabricatedHorizon:
    def test_no_samples_returns_none_not_14(self):
        """核心迴歸：完全沒有前瞻樣本時，best_horizon 必須是 None，不能是 14。"""
        info = find_best_horizon_confidence(_empty_report(), "down")
        assert info["best_horizon"] is None, "不得回填寫死的 14 天"
        assert info["confidence_pct"] is None
        assert info["meets_threshold"] is False
        assert has_backtest_evidence(info) is False

    def test_explicit_fallback_is_opt_in(self):
        """需要 fallback 的呼叫端必須明示，預設不給。"""
        info = find_best_horizon_confidence(_empty_report(), "down", fallback_horizon=14)
        assert info["best_horizon"] == 14

    def test_short_horizon_evidence_is_used(self):
        """h=1 有真樣本時就該用 h=1，而不是退回捏造的 14。"""
        info = find_best_horizon_confidence(_short_span_report(), "down")
        assert info["best_horizon"] == 1
        assert info["confidence_pct"] is not None
        assert has_backtest_evidence(info) is True

    def test_default_horizons_include_short_ones(self):
        """候選集合須含 1／5，與 calibration.DEFAULT_HORIZONS 對齊。"""
        assert 1 in VERDICT_HORIZONS and 5 in VERDICT_HORIZONS


class TestEvaluableHorizons:
    def test_only_horizons_with_samples(self):
        assert evaluable_horizons(_short_span_report(), "down", VERDICT_HORIZONS) == (1,)
        assert evaluable_horizons(_empty_report(), "down", VERDICT_HORIZONS) == ()

    def test_max_evaluable_horizon(self):
        # baseline_n 也算涵蓋：h=1 與 h=5 都有 baseline
        assert max_evaluable_horizon(_short_span_report(), VERDICT_HORIZONS) == 5
        assert max_evaluable_horizon(_empty_report(), VERDICT_HORIZONS) is None

    def test_coverage_note_states_real_limit(self):
        note = horizon_coverage_note(_short_span_report(), VERDICT_HORIZONS)
        assert "快照 6 天" in note and "跨 5 日曆天" in note
        assert "+5 天" in note
        assert "+14" not in note


class TestConfidenceGate:
    def test_no_evidence_fails_gate(self):
        """無證據＝未通過門檻（不是通過）。"""
        info = find_best_horizon_confidence(_empty_report(), "down")
        assert not (has_backtest_evidence(info) and info.get("meets_threshold"))

    def test_tiny_sample_cap_cannot_pass(self):
        """n<5 的信心水準上限恰好是 60.0；嚴格 > 60 才算過，避免門檻形同虛設。"""
        rep = _short_span_report()
        rep["by_horizon"][1]["significance"]["down"]["n"] = 3
        rep["by_horizon"][1]["significance"]["down"]["p_value"] = 0.0
        rep["by_horizon"][1]["bearish_n"] = 3
        info = find_best_horizon_confidence(rep, "down")
        assert info["confidence_pct"] == 60.0
        assert info["meets_threshold"] is False, "n=3 不該因為上限剛好等於門檻而過關"


class TestConfidencePercentageHonesty:
    """bug#00125: 信心水準不得用寫死的 50% 掩蓋「比基準更差」或「無法計算」。"""

    def _sig_report(self, hit_rate, baseline, p_value, n=30):
        return {"by_horizon": {10: {"significance": {"down": {
            "n": n, "ess": 3, "hits": int(hit_rate * n), "hits_ess": 2,
            "hit_rate": hit_rate, "baseline_rate": baseline,
            "ci_lo": 0.2, "ci_hi": 0.6, "p_value": p_value,
            "significant_95": p_value < 0.05, "significant_adj": p_value < 0.05,
            "alpha_adj": 0.05, "num_tests": 1}, "up": None, "num_tests": 1}}}}

    def test_worse_than_baseline_is_not_shown_as_50(self):
        """命中率低於基準時 p→1、raw_conf→0，舊版會顯示成『50%』（像擲硬幣）。"""
        rep = self._sig_report(hit_rate=0.30, baseline=0.60, p_value=0.99)
        info = confidence_percentage_info(rep, 10, "down")
        assert info["confidence_pct"] < 50.0, "低於基準不得被抬到 50%"
        assert info["below_baseline"] is True
        assert "低於無技能基準" in info["confidence_str"]
        assert info["confidence_pct"] is not None  # 仍可計算，只是很差

    def test_better_than_baseline_has_no_warning(self):
        rep = self._sig_report(hit_rate=0.80, baseline=0.50, p_value=0.001)
        info = confidence_percentage_info(rep, 10, "down")
        assert info["below_baseline"] is False
        assert "低於無技能基準" not in info["confidence_str"]
        assert info["meets_threshold"] if "meets_threshold" in info else True

    def test_uncomputable_returns_none_not_50(self):
        rep = {"by_horizon": {10: {"significance": {"down": {
            "n": 8, "ess": 2, "hit_rate": None, "baseline_rate": None,
            "ci_lo": 0.0, "ci_hi": 1.0, "p_value": None}, "up": None}}}}
        info = confidence_percentage_info(rep, 10, "down")
        assert info["confidence_pct"] is None, "算不出來不得回填 50"


class TestExpectedMoveLabel:
    """bug#00125: Expected Move 的天數與公式必須反映實際計算，不得寫死 30 天／IV 公式。"""

    def test_dte_reflects_actual_expiry(self):
        assert _em_dte({"dte": 7}) == 7
        assert _em_dte({"dte": 58}) == 58
        assert _em_dte(None) == 0

    def test_formula_switches_when_no_atm_iv(self):
        with_iv = _em_formula({"atm_iv": 0.45})
        without = _em_formula({"atm_iv": None})
        assert "ATM_IV" in with_iv and "跨式" not in with_iv
        assert "跨式近似" in without and "非 BS 公式" in without

    def test_low_confidence_is_surfaced(self):
        note = _em_iv_note({"atm_iv": None, "low_confidence": True})
        assert "跨式近似" in note and "⚠️" in note
        clean = _em_iv_note({"atm_iv": 0.4, "low_confidence": False})
        assert "⚠️" not in clean


class TestCrossSectionalESS:
    """bug#00125: ESS 須同時對「時間」與「跨標的」去相關。

    彙總回測把多檔高度同向的標的池在一起時，同一天的 6 檔其實是同一個市場日的一次觀測。
    舊 ESS=floor(n/h) 只除以 horizon，會把「5 個交易日 × 6 檔」當成 30 筆獨立樣本。
    """

    def test_same_day_signals_do_not_multiply_evidence(self):
        # 24 筆訊號但只落在 4 個不同日期（6 檔標的 × 4 天）
        pooled = direction_significance(24, 0.96, 0.8, horizon=1, distinct_dates=4)
        assert pooled["ess"] == 4, "同一天的多檔標的不得算成多次獨立觀測"
        assert pooled["significant_95"] is False

    def test_without_dates_falls_back_to_old_behaviour(self):
        old = direction_significance(24, 0.96, 0.8, horizon=1, distinct_dates=None)
        assert old["ess"] == 24

    def test_correction_only_tightens(self):
        """提供日期資訊只能讓 ESS 變小或不變，絕不放寬。"""
        for n, h, dd in [(24, 1, 4), (30, 5, 30), (10, 2, 100), (4, 1, 4)]:
            with_dates = direction_significance(n, 0.7, 0.5, horizon=h, distinct_dates=dd)
            without = direction_significance(n, 0.7, 0.5, horizon=h, distinct_dates=None)
            assert with_dates["ess"] <= without["ess"]
            assert with_dates["p_value"] >= without["p_value"] - 1e-12

    def test_single_underlying_unaffected(self):
        """單一標的逐日訊號：n 與不同日期數相同，修正前後一致。"""
        a = direction_significance(4, 0.75, 0.6, horizon=1, distinct_dates=4)
        b = direction_significance(4, 0.75, 0.6, horizon=1, distinct_dates=None)
        assert a["ess"] == b["ess"] == 4

    def test_ess_never_below_one(self):
        s = direction_significance(2, 1.0, 0.5, horizon=60, distinct_dates=2)
        assert s["ess"] == 1

    def test_prepurged_samples_are_not_divided_by_horizon_twice(self):
        s = direction_significance(
            20,
            0.70,
            0.50,
            horizon=5,
            distinct_dates=20,
            overlap_purged=True,
        )
        assert s["ess"] == 20


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
