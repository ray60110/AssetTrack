#!/usr/bin/env python3
"""Challenge sector direction algorithms on ten fixed yfinance sample windows.

The production sector snapshot history is deliberately not used.  The current
``detect_broad_flow`` rule and every challenger see the same adjusted-close
matrix, the same non-overlapping five-session anchors, and the same outcomes.

The ten samples are fixed one-year windows from 2016-08-15 through 2026-08-15.
Within each sample, an anchor is evaluated every twenty QQQ sessions so
twenty-session outcomes do not overlap.  A method is eligible for the TUI only
when it:

* has pooled accuracy strictly above 60%;
* beats the always-majority direction on its own emitted cases;
* remains above 60% balanced accuracy in the final two windows; and
* emits predictions for at least half of otherwise evaluable cases.

These gates are intentionally frozen before looking at challenger results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd


WINDOWS = tuple(
    (
        f"W{offset + 1}",
        f"{2016 + offset}-08-15",
        f"{2017 + offset}-08-15",
        "development" if offset < 6 else "selection" if offset < 8 else "final_holdout",
    )
    for offset in range(10)
)
HORIZON = 20
ANCHOR_STEP = 20
MIN_COVERAGE = 0.70
MIN_ACCURACY = 0.60
MIN_SIGNAL_COVERAGE = 0.50
BENCHMARK = "QQQ"
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20260815
USER_SELECTED_TUI_METHOD = "two_of_three_bullish"
NON_US_TAIWAN_SUFFIXES = (".TW", ".TWO")


@dataclass(frozen=True)
class Method:
    name: str
    target: str
    order: int
    description: str
    signal: Callable[[str, int, dict], Optional[int]]


def load_groups(path: Path) -> dict[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    source = raw.get("groups", raw) if isinstance(raw, dict) else {}
    groups: dict[str, list[str]] = {}
    for name, members in source.items():
        clean = list(dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in (members or [])
            if str(symbol).strip()
            and not str(symbol).strip().upper().endswith(NON_US_TAIWAN_SUFFIXES)
        ))
        if len(clean) >= 3:
            groups[str(name)] = clean
    if not groups:
        raise ValueError(f"no usable sector groups in {path}")
    return groups


def _normalise_close_frame(raw: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        first = set(map(str, raw.columns.get_level_values(0)))
        last = set(map(str, raw.columns.get_level_values(-1)))
        if "Close" in first:
            close = raw["Close"].copy()
        elif "Close" in last:
            close = raw.xs("Close", axis=1, level=-1).copy()
        else:
            raise ValueError("yfinance response has no Close field")
    else:
        if "Close" not in raw.columns or len(symbols) != 1:
            raise ValueError("unexpected yfinance response shape")
        close = raw[["Close"]].rename(columns={"Close": symbols[0]})
    close.columns = [str(column).upper() for column in close.columns]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.reindex(columns=symbols).sort_index().astype(float)


def download_adjusted_closes(
    symbols: list[str], start: str, end_exclusive: str
) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(
        tickers=symbols,
        start=start,
        end=end_exclusive,
        interval="1d",
        auto_adjust=True,
        actions=False,
        repair=False,
        keepna=True,
        prepost=False,
        ignore_tz=True,
        threads=False,
        progress=False,
        group_by="column",
        multi_level_index=True,
    )
    return _normalise_close_frame(raw, symbols)


def frame_hash(frame: pd.DataFrame) -> str:
    stable = frame.round(10).copy()
    stable.index = stable.index.strftime("%Y-%m-%d")
    return hashlib.sha256(stable.to_csv(index=True, na_rep="NA").encode()).hexdigest()


def _coverage_required(member_count: int) -> int:
    return max(3, math.ceil(member_count * MIN_COVERAGE))


def _member_return(context: dict, group: str, i: int, lookback: int) -> pd.Series:
    prices = context["prices"]
    members = context["groups"][group]
    if i < lookback:
        return pd.Series(dtype=float)
    start = prices.iloc[i - lookback][members]
    end = prices.iloc[i][members]
    return (end / start - 1.0).replace([np.inf, -np.inf], np.nan).dropna()


def _enough(values: pd.Series, group: str, context: dict) -> bool:
    return len(values) >= _coverage_required(len(context["groups"][group]))


def current_consensus_signal(group: str, i: int, context: dict) -> Optional[int]:
    daily = context["daily_returns"][context["groups"][group]]
    if i < 5:
        return None
    states: list[int] = []
    for row_i in range(i - 4, i + 1):
        values = daily.iloc[row_i].dropna()
        if not _enough(values, group, context):
            continue
        breadth = ((values > 0).sum() - (values < 0).sum()) / len(values)
        equal_return_pct = float(values.mean() * 100.0)
        if breadth >= 0.5 and equal_return_pct > 0.1:
            states.append(1)
        elif breadth <= -0.5 and equal_return_pct < -0.1:
            states.append(-1)
        else:
            states.append(0)
    if len(states) < 3:
        return None
    up, down = states.count(1), states.count(-1)
    if up >= 3 and up >= down:
        return 1
    if down >= 3:
        return -1
    return None


def _long_momentum_components(group: str, i: int, context: dict) -> Optional[tuple[float, float]]:
    """Six- and twelve-month returns ending one month before the signal."""
    end_i = i - 21
    if end_i < 252:
        return None
    r6 = _member_return(context, group, end_i, 126)
    r12 = _member_return(context, group, end_i, 252)
    if not _enough(r6, group, context) or not _enough(r12, group, context):
        return None
    return float(r6.mean()), float(r12.mean())


def _relative_momentum_ranks(i: int, context: dict) -> dict[str, int]:
    components = {
        group: _long_momentum_components(group, i, context)
        for group in context["groups"]
    }
    components = {group: value for group, value in components.items() if value is not None}
    if len(components) < 4:
        return {}

    def zscores(values: dict[str, float]) -> dict[str, float]:
        array = np.asarray(list(values.values()), dtype=float)
        std = float(array.std(ddof=0))
        if std <= 1e-12:
            return {key: 0.0 for key in values}
        mean = float(array.mean())
        return {key: (value - mean) / std for key, value in values.items()}

    z6 = zscores({group: value[0] for group, value in components.items()})
    z12 = zscores({group: value[1] for group, value in components.items()})
    ranked = sorted(
        components,
        key=lambda group: (0.5 * z6[group] + 0.5 * z12[group], group),
    )
    return {
        **{group: -1 for group in ranked[:2]},
        **{group: 1 for group in ranked[-2:]},
    }


def cross_sector_relative_momentum_signal(
    group: str, i: int, context: dict
) -> Optional[int]:
    return _relative_momentum_ranks(i, context).get(group)


def time_series_momentum_12m_signal(group: str, i: int, context: dict) -> Optional[int]:
    components = _long_momentum_components(group, i, context)
    if components is None:
        return None
    score = components[1]
    return 1 if score > 0 else -1 if score < 0 else None


def sma_5_150_signal(group: str, i: int, context: dict) -> Optional[int]:
    index = context["sector_indexes"][group]
    if i < 149:
        return None
    recent = index.iloc[i - 149:i + 1]
    if recent.notna().sum() < 150:
        return None
    sma5 = float(recent.iloc[-5:].mean())
    sma150 = float(recent.mean())
    return 1 if sma5 > sma150 else -1 if sma5 < sma150 else None


def breakout_50_signal(group: str, i: int, context: dict) -> Optional[int]:
    index = context["sector_indexes"][group]
    if i < 50 or pd.isna(index.iloc[i]):
        return None
    history = index.iloc[i - 50:i]
    if history.notna().sum() < 50:
        return None
    current = float(index.iloc[i])
    return 1 if current > float(history.max()) else -1 if current < float(history.min()) else None


def relative_momentum_breadth_signal(group: str, i: int, context: dict) -> Optional[int]:
    base = cross_sector_relative_momentum_signal(group, i, context)
    if base is None or i < 49:
        return None
    members = context["groups"][group]
    prices = context["prices"][members]
    current = prices.iloc[i]
    ma50 = prices.iloc[i - 49:i + 1].mean(axis=0, skipna=False)
    comparisons = (current / ma50 - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
    if not _enough(comparisons, group, context):
        return None
    above = float((comparisons > 0).mean())
    if base > 0 and above >= 0.60:
        return 1
    if base < 0 and above <= 0.40:
        return -1
    return None


def agreement_signal(group: str, i: int, context: dict) -> Optional[int]:
    """Emit only when legacy breadth and long relative-momentum breadth agree."""
    breadth_direction = current_consensus_signal(group, i, context)
    relative_direction = relative_momentum_breadth_signal(group, i, context)
    if breadth_direction is None or relative_direction is None:
        return None
    return breadth_direction if breadth_direction == relative_direction else None


def two_of_three_bullish_signal(
    group: str, i: int, context: dict
) -> Optional[int]:
    """Long-only 2-of-3 vote; any bearish vote forces abstention."""
    votes = (
        current_consensus_signal(group, i, context),
        relative_momentum_breadth_signal(group, i, context),
        sma_5_150_signal(group, i, context),
    )
    return 1 if votes.count(1) >= 2 and -1 not in votes else None


METHODS = (
    Method(
        "current_consensus", "absolute", 0,
        "現行 breadth ±0.5、等權日報酬 ±0.1%、近 5 日至少 3 日同向",
        current_consensus_signal,
    ),
    Method(
        "cross_sector_relative_momentum", "relative_qqq", 1,
        "6/12 月動能各半、排除最近 1 月；六板塊 top/bottom 2 預測相對 QQQ 強弱",
        cross_sector_relative_momentum_signal,
    ),
    Method(
        "time_series_momentum_12m", "absolute", 2,
        "12 月板塊等權動能、排除最近 1 月，預測未來 20-session 絕對方向",
        time_series_momentum_12m_signal,
    ),
    Method(
        "sma_5_150", "absolute", 3,
        "等權板塊指數 SMA5 與 SMA150 的相對位置",
        sma_5_150_signal,
    ),
    Method(
        "breakout_50", "absolute", 4,
        "等權板塊指數突破/跌破前 50-session 區間才發訊號",
        breakout_50_signal,
    ),
    Method(
        "relative_momentum_breadth", "relative_qqq", 5,
        "跨板塊相對動能再要求至少 60% 成分股位於 50MA 同側",
        relative_momentum_breadth_signal,
    ),
    Method(
        "agreement_absolute", "absolute", 6,
        "breadth 3-of-5 與相對動能＋50MA breadth 同向；評估板塊絕對漲跌",
        agreement_signal,
    ),
    Method(
        "agreement_relative_qqq", "relative_qqq", 7,
        "完全相同 AND 訊號；評估板塊相對 QQQ 強弱",
        agreement_signal,
    ),
    Method(
        "two_of_three_bullish", "absolute", 8,
        "breadth 3-of-5、相對動能＋50MA breadth、SMA5/150 至少兩票多方且零偏空票；只發多方候選",
        two_of_three_bullish_signal,
    ),
)


def _outcome(group: str, i: int, context: dict, target: str) -> Optional[int]:
    values = _member_return(context, group, i + HORIZON, HORIZON)
    if not _enough(values, group, context):
        return None
    sector_return = float(values.mean())
    if target == "relative_qqq":
        benchmark = context["prices"][BENCHMARK]
        market = float(benchmark.iloc[i + HORIZON] / benchmark.iloc[i] - 1.0)
        sector_return -= market
    return 1 if sector_return > 0 else -1 if sector_return < 0 else None


def _fixed_anchors(
    sessions: pd.DatetimeIndex, start: str, end_exclusive: str
) -> list[int]:
    # Keep one global phase across year boundaries.  Restarting the five-session
    # step every January could make late-December and early-January outcomes
    # overlap, which would quietly double-count the same market move.
    return [
        i for i in range(0, len(sessions), ANCHOR_STEP)
        if pd.Timestamp(start) <= sessions[i] < pd.Timestamp(end_exclusive)
    ]


def _balanced_accuracy(rows: list[dict]) -> Optional[float]:
    rates = []
    for actual in (-1, 1):
        same = [row for row in rows if row["actual"] == actual]
        if same:
            rates.append(sum(row["hit"] for row in same) / len(same))
    return float(np.mean(rates)) if len(rates) == 2 else None


def _date_cluster_bootstrap_ci(rows: list[dict]) -> tuple[Optional[float], Optional[float]]:
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)
    dates = sorted(by_date)
    if len(dates) < 2:
        return None, None
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    estimates = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sampled = rng.choice(dates, size=len(dates), replace=True)
        bootstrap_rows = [row for sampled_date in sampled for row in by_date[str(sampled_date)]]
        value = _balanced_accuracy(bootstrap_rows)
        if value is not None:
            estimates.append(value)
    if not estimates:
        return None, None
    low, high = np.quantile(estimates, [0.025, 0.975])
    return float(low), float(high)


def evaluate_method(method: Method, context: dict) -> dict:
    records: list[dict] = []
    sessions = context["sessions"]
    total_evaluable = 0
    for window, start, end_exclusive, purpose in WINDOWS:
        for i in _fixed_anchors(sessions, start, end_exclusive):
            if i < 273 or i + HORIZON >= len(sessions):
                continue
            for group in context["groups"]:
                actual = _outcome(group, i, context, method.target)
                if actual is None:
                    continue
                total_evaluable += 1
                predicted = method.signal(group, i, context)
                if predicted is None:
                    continue
                records.append({
                    "window": window,
                    "purpose": purpose,
                    "date": sessions[i].date().isoformat(),
                    "group": group,
                    "prediction": predicted,
                    "actual": actual,
                    "hit": predicted == actual,
                })

    window_results: list[dict] = []
    for window, start, end_exclusive, purpose in WINDOWS:
        rows = [row for row in records if row["window"] == window]
        hits = sum(row["hit"] for row in rows)
        actual_up = sum(row["actual"] > 0 for row in rows) / len(rows) if rows else None
        baseline = max(actual_up, 1.0 - actual_up) if actual_up is not None else None
        accuracy = hits / len(rows) if rows else None
        window_results.append({
            "window": window,
            "start": start,
            "end_exclusive": end_exclusive,
            "purpose": purpose,
            "signals": len(rows),
            "accuracy": accuracy,
            "balanced_accuracy": _balanced_accuracy(rows),
            "majority_baseline": baseline,
            "edge_vs_majority": (
                accuracy - baseline if accuracy is not None and baseline is not None else None
            ),
        })
    hits = sum(row["hit"] for row in records)
    accuracy = hits / len(records) if records else None
    actual_up_rate = (
        sum(row["actual"] > 0 for row in records) / len(records) if records else None
    )
    majority_baseline = (
        max(actual_up_rate, 1.0 - actual_up_rate) if actual_up_rate is not None else None
    )
    signal_coverage = len(records) / total_evaluable if total_evaluable else 0.0
    balanced_accuracy = _balanced_accuracy(records)
    holdout_records = [row for row in records if row["purpose"] == "final_holdout"]
    holdout_accuracy = (
        sum(row["hit"] for row in holdout_records) / len(holdout_records)
        if holdout_records else None
    )
    holdout_balanced_accuracy = _balanced_accuracy(holdout_records)
    holdout_actual_up = (
        sum(row["actual"] > 0 for row in holdout_records) / len(holdout_records)
        if holdout_records else None
    )
    holdout_majority = (
        max(holdout_actual_up, 1.0 - holdout_actual_up)
        if holdout_actual_up is not None else None
    )
    holdout_ci = _date_cluster_bootstrap_ci(holdout_records)
    nonworse_windows = sum(
        row["accuracy"] is not None
        and row["majority_baseline"] is not None
        and row["accuracy"] >= row["majority_baseline"]
        for row in window_results
    )
    holdout_up_n = sum(row["actual"] > 0 for row in holdout_records)
    holdout_down_n = sum(row["actual"] < 0 for row in holdout_records)
    direction_breakdown = {}
    for label, direction in (("up", 1), ("down", -1)):
        same = [row for row in records if row["prediction"] == direction]
        direction_breakdown[label] = {
            "signals": len(same),
            "accuracy": (
                sum(row["hit"] for row in same) / len(same) if same else None
            ),
        }
    eligible = bool(
        accuracy is not None
        and accuracy > MIN_ACCURACY
        and majority_baseline is not None
        and accuracy > majority_baseline
        and signal_coverage >= MIN_SIGNAL_COVERAGE
        and holdout_accuracy is not None and holdout_accuracy > MIN_ACCURACY
        and holdout_balanced_accuracy is not None and holdout_balanced_accuracy > MIN_ACCURACY
        and holdout_majority is not None and holdout_accuracy > holdout_majority
        and holdout_ci[0] is not None and holdout_ci[0] > 0.50
        and holdout_up_n >= 20 and holdout_down_n >= 20
    )
    return {
        "name": method.name,
        "order": method.order,
        "target": method.target,
        "description": method.description,
        "signals": len(records),
        "evaluable_cases": total_evaluable,
        "signal_coverage": signal_coverage,
        "accuracy": accuracy,
        "balanced_accuracy": balanced_accuracy,
        "actual_up_rate": actual_up_rate,
        "majority_baseline": majority_baseline,
        "edge_vs_majority": (
            accuracy - majority_baseline
            if accuracy is not None and majority_baseline is not None else None
        ),
        "nonworse_windows": nonworse_windows,
        "holdout_signals": len(holdout_records),
        "holdout_up_n": holdout_up_n,
        "holdout_down_n": holdout_down_n,
        "holdout_accuracy": holdout_accuracy,
        "holdout_balanced_accuracy": holdout_balanced_accuracy,
        "holdout_majority_baseline": holdout_majority,
        "holdout_balanced_accuracy_ci": holdout_ci,
        "prediction_direction_breakdown": direction_breakdown,
        "windows": window_results,
        "eligible_for_tui": eligible,
    }


def run(groups: dict[str, list[str]], closes: pd.DataFrame) -> dict:
    if BENCHMARK not in closes or closes[BENCHMARK].dropna().empty:
        raise ValueError("QQQ history is required")
    sessions = closes[BENCHMARK].dropna().index
    # Cross-market groups are aligned as-of each US session.  A maximum three-day
    # carry handles local holidays without carrying stale delisted prices forever.
    prices = closes.reindex(sessions).ffill(limit=3)
    daily_returns = prices.pct_change(fill_method=None)
    sector_indexes = {}
    for group, members in groups.items():
        member_returns = daily_returns[members]
        required = _coverage_required(len(members))
        sector_return = member_returns.mean(axis=1).where(
            member_returns.notna().sum(axis=1) >= required
        )
        sector_indexes[group] = (1.0 + sector_return).cumprod()
    context = {
        "groups": groups,
        "sessions": sessions,
        "prices": prices,
        "daily_returns": daily_returns,
        "sector_indexes": sector_indexes,
    }
    results = [evaluate_method(method, context) for method in METHODS]
    current = results[0]
    first_eligible_challenger = next(
        (row["name"] for row in results[1:] if row["eligible_for_tui"]), None
    )
    return {
        "run_date": date.today().isoformat(),
        "windows": [
            {"window": name, "start": start, "end_exclusive": end, "purpose": purpose}
            for name, start, end, purpose in WINDOWS
        ],
        "horizon_sessions": HORIZON,
        "anchor_step_sessions": ANCHOR_STEP,
        "min_member_coverage": MIN_COVERAGE,
        "tui_gate": {
            "accuracy_strictly_above": MIN_ACCURACY,
            "final_holdout_balanced_accuracy_strictly_above": MIN_ACCURACY,
            "final_holdout_cluster_ci_lower_above": 0.50,
            "signal_coverage_at_least": MIN_SIGNAL_COVERAGE,
            "must_beat_own_majority_baseline": True,
        },
        "current_method_passed": current["eligible_for_tui"],
        "first_eligible_challenger": first_eligible_challenger,
        "user_selected_tui_method": USER_SELECTED_TUI_METHOD,
        "methods": results,
    }


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: dict) -> str:
    lines = [
        "# 類股演算法：十個固定 yfinance 樣本窗驗證",
        "",
        f"執行日期：{report['run_date']}  ",
        f"資料 SHA-256：`{report['data_sha256']}`  ",
        f"資料範圍：{report['data_first_date']}～{report['data_last_date']}",
        "",
        "## 固定流程",
        "",
        "- 十個固定年度窗：2016-08-15～2026-08-15；W1–W6 development、W7–W8 selection、W9–W10 final holdout。",
        "- 每窗沿全期固定相位、每隔 20 個 QQQ 交易 session 取樣；預測未來第 20 個 session，結果不重疊。",
        "- 六個目前設定的類股群組一起評估；所有方法使用完全相同的行情矩陣與 anchor。",
        "- 現行方法與絕對趨勢 challenger 預測板塊等權報酬方向；輪動法預測相對 QQQ 超額方向。",
        "- TUI gate：全期與 W9–W10 命中率 >60%、holdout balanced accuracy >60% 且 date-cluster bootstrap 95% CI 下界 >50%、勝過 majority baseline、訊號覆蓋 ≥50%。",
        "",
        "## 依序結果",
        "",
        "| 順序 | 方法 | 目標 | 訊號/可評估 | 覆蓋 | 全期命中 | 全期 BA | 多數基準 | W9–10 命中 | W9–10 BA（95% CI） | TUI |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["methods"]:
        lines.append(
            f"| {row['order']} | `{row['name']}` | {row['target']} | "
            f"{row['signals']}/{row['evaluable_cases']} | {_pct(row['signal_coverage'])} | "
            f"{_pct(row['accuracy'])} | {_pct(row['balanced_accuracy'])} | "
            f"{_pct(row['majority_baseline'])} | {_pct(row['holdout_accuracy'])} | "
            f"{_pct(row['holdout_balanced_accuracy'])} "
            f"({_pct(row['holdout_balanced_accuracy_ci'][0])}～{_pct(row['holdout_balanced_accuracy_ci'][1])}) | "
            f"{'PASS' if row['eligible_for_tui'] else 'FAIL'} |"
        )
    lines += ["", "## 十窗明細", ""]
    for row in report["methods"]:
        lines += [
            f"### {row['order']}. `{row['name']}`",
            "",
            row["description"],
            "",
            (
                "方向拆分：預測 up "
                f"{row['prediction_direction_breakdown']['up']['signals']} 次／"
                f"命中 {_pct(row['prediction_direction_breakdown']['up']['accuracy'])}；"
                "預測 down "
                f"{row['prediction_direction_breakdown']['down']['signals']} 次／"
                f"命中 {_pct(row['prediction_direction_breakdown']['down']['accuracy'])}。"
            ),
            "",
            "| 窗 | 用途 | 訊號數 | 命中率 | balanced accuracy | majority edge |",
            "|---|---|---:|---:|---:|---:|",
        ]
        lines.extend(
            f"| {window['window']} | {window['purpose']} | {window['signals']} | "
            f"{_pct(window['accuracy'])} | {_pct(window['balanced_accuracy'])} | "
            f"{_pct(window['edge_vs_majority'])} |"
            for window in row["windows"]
        )
        lines.append("")
    winner = report["first_eligible_challenger"]
    selected = report.get("user_selected_tui_method")
    lines += [
        "## 決策",
        "",
        (
            f"現行規則 {'PASS' if report['current_method_passed'] else 'FAIL'}。"
            + (f"第一個通過的 challenger 是 `{winner}`，可進入 TUI 接線。" if winner
               else "沒有 challenger 通過預先固定的 >60% 與穩定性 gate。")
        ),
        (
            f"產品決策覆寫：使用者於 2026-08-16 明確選擇 `{selected}` 作為實驗性多方 gate；"
            "此接線不改變上表 FAIL 判定；偏空票只作風險警示，TUI 必須揭露其仍待 forward shadow 驗證。"
            if selected else ""
        ),
        "",
        "## 限制",
        "",
        "- 這是目前成分清單回套歷史，仍有存活者偏誤；不等同 point-in-time 指數成分。",
        "- yfinance 沒有可靠逐日歷史市值，因此全部使用等權，避免以今日市值倒灌歷史。",
        "- 十個年度窗是穩定性檢查；holdout CI 以日期為 cluster，但跨窗 regime 仍可能相依。",
        "- 候選方法通過本輪也只代表可進 TUI shadow/提示，不代表保證獲利。",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--groups-file", type=Path,
        default=Path("data/sector_cache/sector_groups.json"),
    )
    parser.add_argument("--start", default="2015-07-01")
    parser.add_argument(
        "--end-exclusive", default=(date.today() + timedelta(days=1)).isoformat()
    )
    parser.add_argument(
        "--price-cache", type=Path,
        default=Path("data/research/sector_algorithm_samples_closes.csv.gz"),
    )
    parser.add_argument("--reuse-price-cache", action="store_true")
    parser.add_argument(
        "--output-md", type=Path,
        default=Path("docs/sector_algorithm_samples_yfinance_results.md"),
    )
    parser.add_argument(
        "--output-json", type=Path,
        default=Path("docs/sector_algorithm_samples_yfinance_results.json"),
    )
    args = parser.parse_args(argv)

    groups = load_groups(args.groups_file)
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
        closes = download_adjusted_closes(symbols, args.start, args.end_exclusive)
        if closes.empty or closes[BENCHMARK].dropna().empty:
            print("No usable QQQ history downloaded.", file=sys.stderr)
            return 2
        args.price_cache.parent.mkdir(parents=True, exist_ok=True)
        closes.to_csv(args.price_cache, compression="gzip", date_format="%Y-%m-%d")
        print(f"Wrote ignored research cache {args.price_cache}")

    report = run(groups, closes)
    usable = closes.dropna(how="all")
    report.update({
        "data_sha256": frame_hash(closes),
        "data_first_date": usable.index[0].date().isoformat() if len(usable) else None,
        "data_last_date": usable.index[-1].date().isoformat() if len(usable) else None,
    })
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_markdown(report), encoding="utf-8")
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {args.output_md}")
    print(f"Wrote {args.output_json}")
    for row in report["methods"]:
        print(
            f"{row['order']}. {row['name']}: accuracy={_pct(row['accuracy'])}, "
            f"coverage={_pct(row['signal_coverage'])}, "
            f"holdout_BA={_pct(row['holdout_balanced_accuracy'])}, "
            f"{'PASS' if row['eligible_for_tui'] else 'FAIL'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
