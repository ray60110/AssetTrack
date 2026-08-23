#!/usr/bin/env python3
"""Sample ten 2-week QQQ moves from the past year and score the live 2-of-3 gate.

Protocol is frozen before looking at hit rates:

* Horizon is 10 NYSE sessions (~two calendar weeks).
* Market move = QQQ close-to-close return over that horizon.
* Candidate windows are the non-overlapping 10-session blocks that end on the
  last available QQQ session and sit inside the trailing year.
* A window counts as a 大盤變動 when |QQQ return| is at or above the median
  of those candidate windows.
* Ten windows are drawn uniformly without replacement (seed 20260817).
* Signal date is the window start.  Features use only prices on or before that
  date.  The scored target is the same sector's equal-weight 10-session return.
* Production ``assess_sector_composite`` is the only vote aggregator.
  Long-only (TUI) and diagnostic both-direction scores are both reported.
  The pre-registered pass line is diagnostic hit rate strictly above 55%.

Vote A in this reconstruction is equal-weight 3-of-5, matching the existing
yfinance study.  Live TUI Vote A uses snapshot cap-weighted return.  Votes B
and C already match production.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from assettrack.backtest_stats import binom_sf, wilson_interval
from assettrack.sector_analysis import assess_sector_composite


ROOT = Path(__file__).resolve().parents[1]
SAMPLES_SCRIPT = ROOT / "scripts" / "validate_sector_algorithm_samples_yfinance.py"
SPEC = importlib.util.spec_from_file_location("sector_samples", SAMPLES_SCRIPT)
assert SPEC and SPEC.loader
samples = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = samples
SPEC.loader.exec_module(samples)

HORIZON = 10
SAMPLE_N = 10
EXPANDED_SAMPLE_N = 13
SEED = 20260817
HIT_THRESHOLD = 0.55
MIN_HISTORY = 273
BENCHMARK = "QQQ"


def composite_stance(group: str, i: int, context: dict) -> dict:
    """Map reconstructed votes through the live TUI aggregator."""
    breadth = samples.current_consensus_signal(group, i, context)
    momentum = samples.relative_momentum_breadth_signal(group, i, context)
    trend = samples.sma_5_150_signal(group, i, context)
    fast = sma_5_20_signal(group, i, context)
    flows = {
        group: {
            "ready": breadth in (1, -1),
            "direction": {1: "up", -1: "down"}.get(breadth, "none"),
        }
    }
    confirmations = {
        group: {
            "ready": momentum in (1, -1),
            "direction": {1: "up", -1: "down"}.get(momentum, "none"),
            "trend_ready": trend in (1, -1),
            "trend_direction": {1: "up", -1: "down"}.get(trend, "none"),
            "fast_trend_ready": fast in (1, -1),
            "fast_trend_direction": {1: "up", -1: "down"}.get(fast, "none"),
        }
    }
    assessment = assess_sector_composite(flows, confirmations)[group]
    status = assessment["status"]
    prediction = (
        1 if status == "bullish_candidate"
        else -1 if status == "risk_alert"
        else None
    )
    return {
        "status": status,
        "prediction": prediction,
        "up_votes": assessment["up_votes"],
        "down_votes": assessment["down_votes"],
        "votes": {
            "breadth_3_of_5": {1: "up", -1: "down"}.get(breadth, "none"),
            "relative_momentum_50ma": {1: "up", -1: "down"}.get(momentum, "none"),
            "sma_5_150": {1: "up", -1: "down"}.get(trend, "none"),
            "sma_5_20": {1: "up", -1: "down"}.get(fast, "none"),
        },
    }


def legacy_champion_stance(group: str, i: int, context: dict) -> dict:
    """Pre-change aggregator: 2-of-3 longs; any two lagging down votes warn."""
    breadth = samples.current_consensus_signal(group, i, context)
    momentum = samples.relative_momentum_breadth_signal(group, i, context)
    trend = samples.sma_5_150_signal(group, i, context)
    votes = {
        "breadth_3_of_5": {1: "up", -1: "down"}.get(breadth, "none"),
        "relative_momentum_50ma": {1: "up", -1: "down"}.get(momentum, "none"),
        "sma_5_150": {1: "up", -1: "down"}.get(trend, "none"),
    }
    up_votes = sum(value == "up" for value in votes.values())
    down_votes = sum(value == "down" for value in votes.values())
    if up_votes >= 2 and down_votes == 0:
        status, prediction = "bullish_candidate", 1
    elif down_votes >= 2:
        status, prediction = "risk_alert", -1
    else:
        status, prediction = "abstain", None
    return {
        "status": status,
        "prediction": prediction,
        "up_votes": up_votes,
        "down_votes": down_votes,
        "votes": votes,
    }


def sma_5_20_signal(group: str, i: int, context: dict) -> Optional[int]:
    """Short-horizon trend: equal-weight sector SMA5 versus SMA20."""
    index = context["sector_indexes"][group]
    if i < 19:
        return None
    recent = index.iloc[i - 19:i + 1]
    if recent.notna().sum() < 20:
        return None
    sma5 = float(recent.iloc[-5:].mean())
    sma20 = float(recent.mean())
    return 1 if sma5 > sma20 else -1 if sma5 < sma20 else None


def build_context(groups: dict[str, list[str]], closes: pd.DataFrame) -> dict:
    if BENCHMARK not in closes or closes[BENCHMARK].dropna().empty:
        raise ValueError("QQQ history is required")
    sessions = closes[BENCHMARK].dropna().index
    prices = closes.reindex(sessions).ffill(limit=3)
    daily_returns = prices.pct_change(fill_method=None)
    sector_indexes = {}
    for group, members in groups.items():
        member_returns = daily_returns[members]
        required = samples._coverage_required(len(members))
        sector_return = member_returns.mean(axis=1).where(
            member_returns.notna().sum(axis=1) >= required
        )
        sector_indexes[group] = (1.0 + sector_return).cumprod()
    return {
        "groups": groups,
        "sessions": sessions,
        "prices": prices,
        "daily_returns": daily_returns,
        "sector_indexes": sector_indexes,
    }


def _forward_return(series: pd.Series, i: int, horizon: int = HORIZON) -> Optional[float]:
    if i < 0 or i + horizon >= len(series):
        return None
    start = series.iloc[i]
    end = series.iloc[i + horizon]
    if pd.isna(start) or pd.isna(end) or float(start) == 0.0:
        return None
    return float(end / start - 1.0)


def candidate_signal_indexes(context: dict, eval_start: str) -> list[int]:
    sessions = context["sessions"]
    last_signal = len(sessions) - 1 - HORIZON
    first_signal = next(
        (
            i for i in range(MIN_HISTORY, last_signal + 1)
            if sessions[i] >= pd.Timestamp(eval_start)
        ),
        None,
    )
    if first_signal is None or last_signal < first_signal:
        return []
    indexes = list(range(last_signal, first_signal - 1, -HORIZON))
    indexes.reverse()
    return indexes


def window_row(context: dict, i: int) -> dict:
    sessions = context["sessions"]
    qqq = _forward_return(context["prices"][BENCHMARK], i)
    return {
        "index": i,
        "signal_date": sessions[i].date().isoformat(),
        "outcome_date": sessions[i + HORIZON].date().isoformat(),
        "qqq_return": qqq,
        "qqq_direction": (
            1 if qqq is not None and qqq > 0
            else -1 if qqq is not None and qqq < 0
            else None
        ),
        "abs_qqq_return": None if qqq is None else abs(qqq),
    }


def eligible_move_windows(windows: list[dict]) -> list[dict]:
    magnitudes = [row["abs_qqq_return"] for row in windows if row["abs_qqq_return"] is not None]
    if not magnitudes:
        return []
    median = float(np.median(magnitudes))
    return [
        row for row in windows
        if row["abs_qqq_return"] is not None and row["abs_qqq_return"] >= median
    ]


def sample_windows(windows: list[dict], n: int = SAMPLE_N, seed: int = SEED) -> list[dict]:
    """Uniform sample from above-median QQQ moves; fill by magnitude if short."""
    eligible = eligible_move_windows(windows)
    remaining = [row for row in windows if row not in eligible]
    remaining.sort(key=lambda row: row["abs_qqq_return"] or -1.0, reverse=True)
    rng = np.random.default_rng(seed)
    chosen: list[dict] = []
    if eligible:
        take = min(n, len(eligible))
        picks = rng.choice(len(eligible), size=take, replace=False)
        chosen.extend(eligible[int(i)] for i in sorted(picks))
    for row in remaining:
        if len(chosen) >= n:
            break
        chosen.append(row)
    chosen.sort(key=lambda row: row["index"])
    return chosen[:n]


def _sector_outcome(group: str, i: int, context: dict) -> Optional[float]:
    values = samples._member_return(context, group, i + HORIZON, HORIZON)
    if not samples._enough(values, group, context):
        return None
    return float(values.mean())


def evaluate_windows(
    context: dict,
    windows: list[dict],
    stance_fn=composite_stance,
) -> list[dict]:
    records = []
    for window in windows:
        i = window["index"]
        for group in context["groups"]:
            actual_return = _sector_outcome(group, i, context)
            if actual_return is None:
                continue
            actual = 1 if actual_return > 0 else -1 if actual_return < 0 else None
            if actual is None:
                continue
            stance = stance_fn(group, i, context)
            prediction = stance["prediction"]
            records.append({
                **window,
                "group": group,
                "status": stance["status"],
                "prediction": prediction,
                "actual": actual,
                "sector_return": actual_return,
                "up_votes": stance["up_votes"],
                "down_votes": stance["down_votes"],
                "votes": stance["votes"],
                "hit": prediction == actual if prediction is not None else None,
            })
    return records


def _rate(rows: list[dict], key: str = "hit") -> Optional[float]:
    usable = [row for row in rows if row.get(key) is not None]
    if not usable:
        return None
    return sum(bool(row[key]) for row in usable) / len(usable)


def _up_rate(rows: list[dict]) -> Optional[float]:
    if not rows:
        return None
    return sum(row["actual"] > 0 for row in rows) / len(rows)


def score_records(records: list[dict], label: str) -> dict:
    directional = [row for row in records if row["prediction"] is not None]
    long_only = [row for row in records if row["status"] == "bullish_candidate"]
    risk_only = [row for row in records if row["status"] == "risk_alert"]
    abstain = [row for row in records if row["status"] == "abstain"]
    diagnostic_hits = sum(bool(row["hit"]) for row in directional)
    long_hits = sum(bool(row["hit"]) for row in long_only)
    risk_hits = sum(bool(row["hit"]) for row in risk_only)
    diagnostic_n = len(directional)
    long_n = len(long_only)
    risk_n = len(risk_only)
    diagnostic_accuracy = _rate(directional)
    long_accuracy = _rate(long_only)
    risk_accuracy = _rate(risk_only)
    actual_up = _up_rate(directional)
    unfiltered_up = _up_rate(records)
    majority = (
        max(actual_up, 1.0 - actual_up) if actual_up is not None else None
    )
    diagnostic_ci = wilson_interval(diagnostic_hits, diagnostic_n) if diagnostic_n else (None, None)
    long_ci = wilson_interval(long_hits, long_n) if long_n else (None, None)
    p_gt_threshold = (
        binom_sf(diagnostic_hits, diagnostic_n, HIT_THRESHOLD) if diagnostic_n else None
    )
    return {
        "label": label,
        "evaluable_cases": len(records),
        "directional_signals": diagnostic_n,
        "bullish_candidates": long_n,
        "risk_alerts": risk_n,
        "abstain": len(abstain),
        "coverage": diagnostic_n / len(records) if records else 0.0,
        "diagnostic_hits": diagnostic_hits,
        "diagnostic_accuracy": diagnostic_accuracy,
        "diagnostic_wilson_95": diagnostic_ci,
        "p_value_vs_55pct": p_gt_threshold,
        "passed_55pct": bool(
            diagnostic_accuracy is not None and diagnostic_accuracy > HIT_THRESHOLD
        ),
        "long_only_hits": long_hits,
        "long_only_accuracy": long_accuracy,
        "long_only_wilson_95": long_ci,
        "risk_alert_hits": risk_hits,
        "risk_alert_accuracy": risk_accuracy,
        "actual_up_rate": actual_up,
        "unfiltered_up_rate": unfiltered_up,
        "majority_baseline": majority,
        "edge_vs_majority": (
            diagnostic_accuracy - majority
            if diagnostic_accuracy is not None and majority is not None
            else None
        ),
        "long_only_edge_vs_unfiltered": (
            long_accuracy - unfiltered_up
            if long_accuracy is not None and unfiltered_up is not None
            else None
        ),
    }


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: dict) -> str:
    sampled = report["sampled_score"]
    census = report["census_score"]
    lines = [
        "# 類股 2-of-3：過去一年十次兩週大盤變動抽樣驗證",
        "",
        f"執行日期：{report['run_date']}  ",
        f"資料 SHA-256：`{report['data_sha256']}`  ",
        f"資料範圍：{report['data_first_date']}～{report['data_last_date']}  ",
        f"評估年：{report['eval_start']}～{report['eval_end']}（訊號日；outcome 再往後 {HORIZON} 個 session）",
        "",
        "## 預先固定的流程",
        "",
        "- 以 QQQ 過去一年切成不重疊的 10-session（約兩週）視窗，從最後一個可結算視窗往回對齊。",
        "- 「大盤變動」= 該視窗 |QQQ 報酬| ≥ 全候補視窗中位數。",
        f"- 亂數種子 {SEED}，從合格視窗均勻抽 {SAMPLE_N} 個；不足則依 |QQQ 報酬| 由大補齊。",
        "- 訊號日 = 視窗起點，只用當日及以前的價格。目標 = 該板塊等權 10-session 報酬方向。",
        "- 三票與畫面相同：breadth 3-of-5、相對動能＋50MA breadth、SMA5/150；投票由 `assess_sector_composite` 執行。",
        "- 主結果：有方向的案例（多方候選預測漲、風險警示視為預測跌）命中率是否 **嚴格高於 55%**。",
        "- 棄權不計入命中率。TUI 只對多方候選給方向建議；風險警示在產品上不是放空建議，此處僅作診斷空方票。",
        "",
        "## 結論",
        "",
    ]
    passed = sampled["passed_55pct"]
    lines.append(
        f"**十次抽樣{'通過' if passed else '未通過'} 55% 門檻。** "
        f"診斷命中率 {_pct(sampled['diagnostic_accuracy'])}"
        f"（{sampled['diagnostic_hits']}/{sampled['directional_signals']}），"
        f"Wilson 95% CI "
        f"{_pct(sampled['diagnostic_wilson_95'][0])}～{_pct(sampled['diagnostic_wilson_95'][1])}。"
        f"多數基準 {_pct(sampled['majority_baseline'])}，"
        f"超額 {_pct(sampled['edge_vs_majority'])}。"
    )
    lines += [
        "",
        f"TUI 實際會顯示的多方候選命中率 {_pct(sampled['long_only_accuracy'])}"
        f"（{sampled['long_only_hits']}/{sampled['bullish_candidates']}）。"
        f"風險警示若當成空方預測，命中率 {_pct(sampled['risk_alert_accuracy'])}"
        f"（{sampled['risk_alert_hits']}/{sampled['risk_alerts']}）。"
        f"棄權 {sampled['abstain']}/{sampled['evaluable_cases']}，覆蓋 {_pct(sampled['coverage'])}。",
        "",
        "## 抽樣視窗",
        "",
        f"候補不重疊視窗 {report['candidate_windows']} 個；"
        f"|QQQ| 中位數 {_pct(report['median_abs_qqq_return'])}；"
        f"合格大盤變動 {report['eligible_windows']} 個。",
        "",
        "| # | 訊號日 | 結算日 | QQQ 兩週 | 方向 | 多方候選 | 風險警示 | 棄權 | 診斷命中 |",
        "|---:|---|---|---:|---|---:|---:|---:|---|",
    ]
    for i, window in enumerate(report["sampled_windows"], start=1):
        rows = [row for row in report["sampled_records"] if row["index"] == window["index"]]
        hits = [row for row in rows if row["hit"] is True]
        directional = [row for row in rows if row["prediction"] is not None]
        qqq_dir = "漲" if window["qqq_direction"] == 1 else "跌" if window["qqq_direction"] == -1 else "—"
        lines.append(
            f"| {i} | {window['signal_date']} | {window['outcome_date']} | "
            f"{window['qqq_return'] * 100:+.2f}% | {qqq_dir} | "
            f"{sum(row['status'] == 'bullish_candidate' for row in rows)} | "
            f"{sum(row['status'] == 'risk_alert' for row in rows)} | "
            f"{sum(row['status'] == 'abstain' for row in rows)} | "
            f"{len(hits)}/{len(directional) if directional else 0} |"
        )
    lines += [
        "",
        "## 十窗逐板塊明細",
        "",
        "| 訊號日 | 板塊 | 三票 | 狀態 | 預測 | 實際 | 板塊兩週 |",
        "|---|---|---|---|---|---|---:|",
    ]
    vote_abbr = {
        "breadth_3_of_5": "A",
        "relative_momentum_50ma": "B",
        "sma_5_150": "C",
    }
    for row in report["sampled_records"]:
        votes = "".join(
            f"{vote_abbr[key]}{'↑' if value == 'up' else '↓' if value == 'down' else '·'}"
            for key, value in row["votes"].items()
        )
        pred = {1: "漲", -1: "跌"}.get(row["prediction"], "棄權")
        actual = {1: "漲", -1: "跌"}[row["actual"]]
        status = {
            "bullish_candidate": "多方候選",
            "risk_alert": "風險警示",
            "abstain": "棄權",
        }[row["status"]]
        lines.append(
            f"| {row['signal_date']} | {row['group']} | {votes} | {status} | "
            f"{pred} | {actual} | {row['sector_return'] * 100:+.2f}% |"
        )
    lines += [
        "",
        "## 同年全部不重疊兩週窗（穩健性，非主結果）",
        "",
        f"診斷命中率 {_pct(census['diagnostic_accuracy'])}"
        f"（{census['diagnostic_hits']}/{census['directional_signals']}），"
        f"Wilson 95% CI {_pct(census['diagnostic_wilson_95'][0])}～{_pct(census['diagnostic_wilson_95'][1])}；"
        f"多數基準 {_pct(census['majority_baseline'])}；"
        f"多方候選 {_pct(census['long_only_accuracy'])}；"
        f"覆蓋 {_pct(census['coverage'])}。",
        "",
        "## 限制",
        "",
        "- 這是目前成分清單回套歷史，仍有存活者偏誤。",
        "- Vote A 在此重建為等權 3-of-5；TUI 直播用快照市值加權報酬。B、C 兩票與生產相同。",
        "- 十個視窗樣本很小，同日六板塊高度相關，Wilson CI 會很寬。",
        "- 風險警示在產品上不是放空建議；把它當成空方預測只為回答「漲或跌」這題。",
        "- 命中率高於 55% 仍可能低於「永遠猜多數方向」的無技能基準。",
        "",
    ]
    return "\n".join(lines)


def render_challenger_markdown(report: dict) -> str:
    challenger = report["challenger"]
    decision = challenger["decision"]
    lines = [
        "# 類股風險警示 challenger：breadth 下跌 + SMA5/20",
        "",
        f"執行日期：{report['run_date']}  ",
        f"資料 SHA-256：`{report['data_sha256']}`",
        "",
        "## 預先鎖定的規則（未看 holdout 前寫下）",
        "",
        "- 多方候選維持現行 2-of-3：breadth 3-of-5、相對動能＋50MA breadth、SMA5/150；至少兩票多方且零偏空。",
        "- 風險警示改為雙重即時確認：breadth 3-of-5 為空，且等權板塊指數 SMA5 < SMA20。",
        "- 慢速票（6/12 月動能、SMA150）只繼續當多方煞車，不再單獨構成風險警示。",
        "- 先前十窗已看過，只當 development；決策看未抽中的 14 窗。另加 3 窗（剩餘合格大盤變動 + 1 個次大窗）作小樣本對照。",
        "- 導入條件：未見窗風險警示命中率 > 20% 且高於現行規則，多方命中率不下降。",
        "",
        f"## 決策：{'導入' if decision['import_into_product'] else '不導入'}",
        "",
        f"風險改善={decision['risk_improved']}；多方維持={decision['long_kept']}。",
        "",
        "## 對照",
        "",
        "| 集合 | 規則 | 診斷 | 多方 | 風險警示 |",
        "|---|---|---|---|---|",
    ]
    for label, key in (
        ("已看過十窗（development）", "burned_10"),
        ("新增三窗", "added_3"),
        ("未抽中十四窗（holdout）", "holdout_unseen"),
        ("十三窗合計", "expanded_13"),
        ("全年二十四窗", "census"),
    ):
        block = challenger[key]
        dates = "、".join(row["signal_date"] for row in block["windows"])
        for policy in ("champion", "challenger"):
            score = block[policy]
            lines.append(
                f"| {label} | {policy} | "
                f"{_pct(score['diagnostic_accuracy'])} "
                f"({score['diagnostic_hits']}/{score['directional_signals']}) | "
                f"{_pct(score['long_only_accuracy'])} "
                f"({score['long_only_hits']}/{score['bullish_candidates']}) | "
                f"{_pct(score['risk_alert_accuracy'])} "
                f"({score['risk_alert_hits']}/{score['risk_alerts']}) |"
            )
        lines.append(f"| | 視窗 | {dates} | | |")
    lines += ["", "## 限制", "",
              "- 未見窗仍與 development 同年、同成分清單，不是第二個市場。",
              "- SMA5/20 是事前固定的短線趨勢，不是從十窗誤報列微調出來的門檻。",
              ""]
    return "\n".join(lines)


def run(groups: dict[str, list[str]], closes: pd.DataFrame, eval_end: str) -> dict:
    context = build_context(groups, closes)
    eval_end_ts = pd.Timestamp(eval_end)
    eval_start = (eval_end_ts - pd.DateOffset(years=1)).date().isoformat()
    indexes = candidate_signal_indexes(context, eval_start)
    candidates = [window_row(context, i) for i in indexes]
    candidates = [row for row in candidates if row["qqq_return"] is not None]
    eligible = eligible_move_windows(candidates)
    sampled = sample_windows(candidates)
    sampled_records = evaluate_windows(context, sampled)
    census_records = evaluate_windows(context, candidates)
    median = (
        float(np.median([row["abs_qqq_return"] for row in candidates]))
        if candidates else None
    )
    last_session = context["sessions"][-1].date().isoformat()
    challenger_report = build_challenger_report(context, candidates)
    return {
        "run_date": date.today().isoformat(),
        "eval_start": eval_start,
        "eval_end": last_session,
        "horizon_sessions": HORIZON,
        "sample_n": SAMPLE_N,
        "seed": SEED,
        "hit_threshold": HIT_THRESHOLD,
        "min_history_sessions": MIN_HISTORY,
        "candidate_windows": len(candidates),
        "eligible_windows": len(eligible),
        "median_abs_qqq_return": median,
        "sampled_windows": sampled,
        "sampled_records": sampled_records,
        "census_records": census_records,
        "sampled_score": score_records(sampled_records, "sampled_10"),
        "census_score": score_records(census_records, "all_nonoverlap_year"),
        "challenger": challenger_report,
    }


def _indexes(windows: list[dict]) -> set[int]:
    return {row["index"] for row in windows}


def _policy_pair(context: dict, windows: list[dict], label: str) -> dict:
    return {
        "windows": [
            {"signal_date": row["signal_date"], "outcome_date": row["outcome_date"],
             "qqq_return": row["qqq_return"]}
            for row in windows
        ],
        "champion": score_records(
            evaluate_windows(context, windows, legacy_champion_stance), f"{label}_champion"
        ),
        "challenger": score_records(
            evaluate_windows(context, windows, composite_stance), f"{label}_challenger"
        ),
    }


def decide_import(holdout: dict) -> dict:
    """Pre-registered gate: unseen-window risk hit rate must rise above 20%
    and above the champion, without lowering long-only accuracy."""
    champion = holdout["champion"]
    challenger = holdout["challenger"]
    risk_defined = (
        challenger["risk_alert_accuracy"] is not None and challenger["risk_alerts"] > 0
    )
    risk_improved = bool(
        risk_defined
        and challenger["risk_alert_accuracy"] > 0.20
        and (
            champion["risk_alert_accuracy"] is None
            or challenger["risk_alert_accuracy"] > champion["risk_alert_accuracy"]
        )
    )
    long_kept = (
        challenger["long_only_accuracy"] is not None
        and champion["long_only_accuracy"] is not None
        and challenger["long_only_accuracy"] >= champion["long_only_accuracy"]
    )
    return {
        "import_into_product": bool(risk_improved and long_kept),
        "risk_improved": risk_improved,
        "long_kept": long_kept,
        "reasons": {
            "challenger_risk_accuracy": challenger["risk_alert_accuracy"],
            "champion_risk_accuracy": champion["risk_alert_accuracy"],
            "challenger_risk_n": challenger["risk_alerts"],
            "challenger_long_accuracy": challenger["long_only_accuracy"],
            "champion_long_accuracy": champion["long_only_accuracy"],
        },
    }


def build_challenger_report(context: dict, candidates: list[dict]) -> dict:
    burned = sample_windows(candidates, n=SAMPLE_N, seed=SEED)
    expanded = sample_windows(candidates, n=EXPANDED_SAMPLE_N, seed=SEED)
    burned_idx = _indexes(burned)
    holdout_3 = [row for row in expanded if row["index"] not in burned_idx]
    holdout_14 = [row for row in candidates if row["index"] not in burned_idx]
    holdout = _policy_pair(context, holdout_14, "holdout_unseen")
    return {
        "rule": (
            "longs unchanged 2-of-3; risk alert only when breadth 3-of-5 is down "
            "and equal-weight SMA5 < SMA20"
        ),
        "burned_10": _policy_pair(context, burned, "burned_10"),
        "added_3": _policy_pair(context, holdout_3, "added_3"),
        "holdout_unseen": holdout,
        "expanded_13": _policy_pair(context, expanded, "expanded_13"),
        "census": _policy_pair(context, candidates, "census"),
        "decision": decide_import(holdout),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--groups-file", type=Path,
        default=Path("data/sector_cache/sector_groups.json"),
    )
    parser.add_argument("--start", default="2015-07-01")
    parser.add_argument(
        "--end-exclusive", default=(date.today() + timedelta(days=1)).isoformat(),
    )
    parser.add_argument(
        "--price-cache", type=Path,
        default=Path("data/research/sector_algorithm_samples_closes.csv.gz"),
    )
    parser.add_argument("--reuse-price-cache", action="store_true")
    parser.add_argument(
        "--output-md", type=Path,
        default=Path("docs/sector_two_week_market_samples_results.md"),
    )
    parser.add_argument(
        "--output-json", type=Path,
        default=Path("docs/sector_two_week_market_samples_results.json"),
    )
    parser.add_argument(
        "--challenger-md", type=Path,
        default=Path("docs/sector_risk_challenger_two_week_results.md"),
    )
    args = parser.parse_args(argv)

    groups = samples.load_groups(args.groups_file)
    symbols = list(dict.fromkeys(
        symbol for members in groups.values() for symbol in members
    ))
    if BENCHMARK not in symbols:
        symbols.append(BENCHMARK)
    if args.reuse_price_cache:
        closes = pd.read_csv(args.price_cache, index_col=0, parse_dates=True)
        closes.columns = [str(column).upper() for column in closes.columns]
        closes = closes.reindex(columns=symbols)
    else:
        print(f"Downloading {len(symbols)} symbols directly from yfinance...", flush=True)
        closes = samples.download_adjusted_closes(symbols, args.start, args.end_exclusive)
        if closes.empty or closes[BENCHMARK].dropna().empty:
            print("No usable QQQ history downloaded.", file=sys.stderr)
            return 2
        args.price_cache.parent.mkdir(parents=True, exist_ok=True)
        closes.to_csv(args.price_cache, compression="gzip", date_format="%Y-%m-%d")

    report = run(groups, closes, args.end_exclusive)
    usable = closes.dropna(how="all")
    report.update({
        "data_sha256": samples.frame_hash(closes),
        "data_first_date": usable.index[0].date().isoformat() if len(usable) else None,
        "data_last_date": usable.index[-1].date().isoformat() if len(usable) else None,
    })
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_markdown(report), encoding="utf-8")
    args.challenger_md.write_text(render_challenger_markdown(report), encoding="utf-8")
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sampled = report["sampled_score"]
    census = report["census_score"]
    decision = report["challenger"]["decision"]
    holdout = report["challenger"]["holdout_unseen"]
    added = report["challenger"]["added_3"]
    print(f"Wrote {args.output_md}")
    print(f"Wrote {args.challenger_md}")
    print(f"Wrote {args.output_json}")
    print(
        f"sampled diagnostic={_pct(sampled['diagnostic_accuracy'])} "
        f"({sampled['diagnostic_hits']}/{sampled['directional_signals']}) "
        f"pass55={sampled['passed_55pct']} "
        f"long={_pct(sampled['long_only_accuracy'])} "
        f"baseline={_pct(sampled['majority_baseline'])}"
    )
    print(
        f"census diagnostic={_pct(census['diagnostic_accuracy'])} "
        f"({census['diagnostic_hits']}/{census['directional_signals']}) "
        f"long={_pct(census['long_only_accuracy'])}"
    )
    print(
        "holdout14 champion "
        f"risk={_pct(holdout['champion']['risk_alert_accuracy'])} "
        f"({holdout['champion']['risk_alert_hits']}/{holdout['champion']['risk_alerts']}) "
        f"long={_pct(holdout['champion']['long_only_accuracy'])} | "
        "challenger "
        f"risk={_pct(holdout['challenger']['risk_alert_accuracy'])} "
        f"({holdout['challenger']['risk_alert_hits']}/{holdout['challenger']['risk_alerts']}) "
        f"long={_pct(holdout['challenger']['long_only_accuracy'])}"
    )
    print(
        "added3 champion "
        f"risk={_pct(added['champion']['risk_alert_accuracy'])} "
        f"({added['champion']['risk_alert_hits']}/{added['champion']['risk_alerts']}) | "
        "challenger "
        f"risk={_pct(added['challenger']['risk_alert_accuracy'])} "
        f"({added['challenger']['risk_alert_hits']}/{added['challenger']['risk_alerts']})"
    )
    print(
        f"import_into_product={decision['import_into_product']} "
        f"risk_improved={decision['risk_improved']} long_kept={decision['long_kept']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
