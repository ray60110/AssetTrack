#!/usr/bin/env python3
"""Validate direction policies through one family-blind scorer.

This research script does not write production caches.  It:

1. Walks the current options directional-verdict rule over local snapshots.
2. Emits Always-Up and 5-session momentum claims on the same underlyings.
3. Settles every claim against yfinance auto-adjusted closes.
4. Scores all three Policy Versions with ``direction_forecast_validation.validate``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assettrack.calibration import normalise_option_snapshots
from assettrack.direction_forecast_validation import (
    SIGNED_RETURN_FLOOR,
    DirectionValidationReport,
    ForecastRecord,
    ValidationSpec,
    validate,
)
from assettrack.market_sessions import NYSESessionCalendar
from assettrack.options_analysis import compute_directional_verdicts
from assettrack.storage import load_options_daily_snapshots


CAL = NYSESessionCalendar()
OPTIONS_POLICY_ID = "options-directional-verdicts-v1"
ALWAYS_UP_POLICY_ID = "naive-always-up-v1"
MOMENTUM_POLICY_ID = "naive-momentum-5-v1"
DEFAULT_UNIVERSE = ("AMD", "INTC", "MU", "NVDA", "PLTR", "SPCX", "TSLA", "TSM")
DEFAULT_BENCHMARKS = ("QQQ", "SPY")
DEFAULT_HORIZONS = (1, 5, 10)
PRIMARY_HORIZON = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end-exclusive", default="2026-08-18")
    parser.add_argument(
        "--universe",
        default=",".join(DEFAULT_UNIVERSE),
        help="Comma-separated underlyings",
    )
    parser.add_argument("--output-stem", default="docs/direction_forecast_validation")
    return parser.parse_args()


def parse_universe(raw: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(part.strip().upper() for part in raw.split(",") if part.strip()))
    if not values:
        raise SystemExit("universe must not be empty")
    return values


def load_option_snapshots(universe: Iterable[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for symbol in universe:
        snaps = load_options_daily_snapshots(symbol)
        if snaps:
            out[symbol] = snaps
    return out


def emit_options_claims(
    snapshots_by_underlying: dict[str, list[dict]],
    horizons: tuple[int, ...],
) -> list[ForecastRecord]:
    claims: list[ForecastRecord] = []
    for symbol, snaps in snapshots_by_underlying.items():
        normalised = normalise_option_snapshots(snaps)
        print(f"  walking {symbol}: {len(normalised)} sessions", flush=True)
        for index, snapshot in enumerate(normalised):
            raw_date = snapshot.get("date")
            if not raw_date:
                continue
            session = date.fromisoformat(str(raw_date)[:10])
            if not CAL.is_session(session):
                continue
            report = compute_directional_verdicts(
                {symbol: normalised[: index + 1]},
                as_of=session.isoformat(),
            )
            direction = report["verdicts"].get(symbol, {}).get("direction")
            if direction not in ("多", "空"):
                continue
            mapped = "up" if direction == "多" else "down"
            for horizon in horizons:
                claims.append(
                    ForecastRecord(
                        policy_version_id=OPTIONS_POLICY_ID,
                        outcome_target=symbol,
                        entry_session=session,
                        horizon_sessions=horizon,
                        direction=mapped,
                    )
                )
    return claims


def download_prices(
    symbols: tuple[str, ...],
    start: str,
    end_exclusive: str,
) -> dict[tuple[str, date], float]:
    tickers = list(dict.fromkeys([*symbols, *DEFAULT_BENCHMARKS]))
    print(f"downloading yfinance closes for {', '.join(tickers)}", flush=True)
    frame = yf.download(
        tickers,
        start=start,
        end=end_exclusive,
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    prices: dict[tuple[str, date], float] = {}
    if isinstance(frame.columns, pd.MultiIndex):
        for symbol in tickers:
            if symbol not in frame.columns.get_level_values(0):
                continue
            close = frame[symbol]["Close"].dropna()
            for ts, value in close.items():
                session = pd.Timestamp(ts).date()
                if CAL.is_session(session):
                    prices[(symbol, session)] = float(value)
    else:
        close = frame["Close"].dropna()
        symbol = tickers[0]
        for ts, value in close.items():
            session = pd.Timestamp(ts).date()
            if CAL.is_session(session):
                prices[(symbol, session)] = float(value)
    return prices


def sessions_for(symbol: str, prices: dict[tuple[str, date], float]) -> list[date]:
    return sorted(session for (sym, session) in prices if sym == symbol)


def emit_always_up(
    symbols: tuple[str, ...],
    prices: dict[tuple[str, date], float],
    horizons: tuple[int, ...],
) -> list[ForecastRecord]:
    claims: list[ForecastRecord] = []
    for symbol in symbols:
        for session in sessions_for(symbol, prices):
            for horizon in horizons:
                claims.append(
                    ForecastRecord(
                        policy_version_id=ALWAYS_UP_POLICY_ID,
                        outcome_target=symbol,
                        entry_session=session,
                        horizon_sessions=horizon,
                        direction="up",
                    )
                )
    return claims


def emit_momentum(
    symbols: tuple[str, ...],
    prices: dict[tuple[str, date], float],
    horizons: tuple[int, ...],
    lookback: int = 5,
) -> list[ForecastRecord]:
    claims: list[ForecastRecord] = []
    for symbol in symbols:
        history = sessions_for(symbol, prices)
        by_pos = {session: index for index, session in enumerate(history)}
        for session in history:
            index = by_pos[session]
            if index < lookback:
                continue
            prior = history[index - lookback]
            now_px = prices[(symbol, session)]
            prior_px = prices[(symbol, prior)]
            if prior_px <= 0 or now_px <= 0:
                continue
            direction = "up" if now_px >= prior_px else "down"
            for horizon in horizons:
                claims.append(
                    ForecastRecord(
                        policy_version_id=MOMENTUM_POLICY_ID,
                        outcome_target=symbol,
                        entry_session=session,
                        horizon_sessions=horizon,
                        direction=direction,
                    )
                )
    return claims


def report_to_dict(report: DirectionValidationReport) -> dict:
    overall = report.overall
    return {
        "protocol_version": report.protocol_version,
        "policy_version_id": report.policy_version_id,
        "scoring_mode": report.scoring_mode,
        "verdict": report.verdict,
        "reason": report.reason,
        "data_hash": report.data_hash,
        "gate": {
            "passed": report.gate.passed,
            "profile": report.gate.profile,
            "primary_metric": report.gate.primary_metric,
            "floor": report.gate.floor,
            "improvement": report.gate.improvement,
            "improvement_ci_lower": report.gate.improvement_ci_lower,
            "failures": list(report.gate.failures),
        },
        "overall": {
            "horizon": overall.name,
            "claim_count": overall.claim_count,
            "matured_count": overall.matured_count,
            "void_count": overall.void_count,
            "independent_blocks": overall.independent_blocks,
            "coverage": overall.coverage,
            "hit_rate": overall.hit_rate,
            "baseline_hit_rate": overall.baseline_hit_rate,
            "mean_cost_adjusted_excess": overall.mean_cost_adjusted_excess,
            "excess_ci": [overall.excess_ci.lower, overall.excess_ci.upper],
            "first_entry_session": (
                overall.first_entry_session.isoformat() if overall.first_entry_session else None
            ),
            "last_entry_session": (
                overall.last_entry_session.isoformat() if overall.last_entry_session else None
            ),
        },
    }


def _fmt_pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_ret(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:+.3f}%"


def render_markdown(
    *,
    run_date: str,
    universe: tuple[str, ...],
    option_sessions: dict[str, int],
    reports: dict[str, dict],
    options_raw_n: int,
) -> str:
    lines = [
        "# 方向預測驗證結果",
        "",
        f"執行時間：{run_date}  ",
        f"協議：`direction-forecast-validation-v1`  ",
        f"宇集：{', '.join(universe)}  ",
        f"主前瞻期：+{PRIMARY_HORIZON} 個 NYSE session；基準 QQQ，成本 10 bps。  ",
        f"通過條件：去重疊獨立樣本 ≥ 30，且成本調整後超額報酬 95% CI 下界 > {SIGNED_RETURN_FLOOR:.4f}。",
        "",
        "## 凍結假說",
        "",
        "1. **options-directional-verdicts-v1**：現行 `compute_directional_verdicts`（Dollar Delta OI skew + 重定價殘差），看完結果後不調參。",
        "2. **naive-always-up-v1**：每個有收盤價的 session 都看多。",
        "3. **naive-momentum-5-v1**：過去 5 個 session 上漲則看多，否則看空。",
        "",
        "結算一律用 yfinance `auto_adjust=True` 收盤，不用期權快照裡的 spot。缺剛好 +h session 的收盤記 VOID，不拿下一根 K 線頂替。",
        "",
        "## 本機期權快照覆蓋",
        "",
        "| 標的 | 正規化後 session 數 |",
        "|---|---:|",
    ]
    for symbol, count in option_sessions.items():
        lines.append(f"| {symbol} | {count} |")
    lines += [
        "",
        f"現行期權規則在這些快照上發出 {options_raw_n} 筆（含 1／5／10 session）Forecast Record。",
        "",
        "## 主要結果（primary +5 session）",
        "",
        "| Policy Version | 判定 | 去重疊 n | 命中率 | 無條件上漲率 | 扣 10bps 後超額 | 95% CI | 原因 |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    order = (OPTIONS_POLICY_ID, ALWAYS_UP_POLICY_ID, MOMENTUM_POLICY_ID)
    labels = {
        OPTIONS_POLICY_ID: "現行期權方向",
        ALWAYS_UP_POLICY_ID: "永遠看多",
        MOMENTUM_POLICY_ID: "五日動能",
    }
    for policy_id in order:
        item = reports[policy_id]
        overall = item["overall"]
        ci = overall["excess_ci"]
        ci_text = (
            "—"
            if ci[0] is None
            else f"[{_fmt_ret(ci[0])}, {_fmt_ret(ci[1])}]"
        )
        lines.append(
            f"| {labels[policy_id]} | **{item['verdict']}** | "
            f"{overall['independent_blocks']} | {_fmt_pct(overall['hit_rate'])} | "
            f"{_fmt_pct(overall['baseline_hit_rate'])} | "
            f"{_fmt_ret(overall['mean_cost_adjusted_excess'])} | {ci_text} | "
            f"{item['reason']} |"
        )
    lines += [
        "",
        "## 既有外部驗證（不重跑）",
        "",
        "類股 `5 日中 3 日廣度同向` 規則已於 2026-08-15 用 2016–2026 yfinance 判定 **FAIL**"
        "（holdout 扣 10bps 後 signed return −0.036%，CI 跨 0）。見"
        " `docs/sector_consensus_yfinance_validation_summary.md`。",
        "",
        "## 決策含義",
        "",
        "UNDERPOWERED 不是 FAIL：表示測試尚未發生，不能據此調參或宣稱有效。",
        "FAIL 表示樣本足夠且未清過 Scheme B 地板。PASS 才是可提交 Promotion Gate 的證據，仍不自動升級 Champion。",
        "",
        "**宇集存活者偏誤：** Always-Up 的 PASS 是在 2026 年已選定的觀察清單上回溯 2016–2026。",
        "這不是 2016 年可交易的發現，不能解讀成「系統會選股」。它只證明：在這個已存活的清單上，",
        "買進持有相對 QQQ 有正超額；擇時（五日動能）即使享有同一偏誤也 FAIL。",
        "",
        "架構、刪除範圍與下一步見 `docs/direction_forecast_architecture.md`。",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    universe = parse_universe(args.universe)
    spec = ValidationSpec.scheme_b(
        horizons=DEFAULT_HORIZONS,
        primary_horizon=PRIMARY_HORIZON,
        benchmarks=DEFAULT_BENCHMARKS,
        cost_bps=10.0,
        min_independent_blocks=30,
        min_coverage=0.70,
    )

    print("loading local option snapshots", flush=True)
    snapshots = load_option_snapshots(universe)
    option_sessions = {
        symbol: len(normalise_option_snapshots(snaps))
        for symbol, snaps in snapshots.items()
    }
    print("emitting options claims", flush=True)
    option_claims = emit_options_claims(snapshots, DEFAULT_HORIZONS)
    print(f"  {len(option_claims)} options claims", flush=True)

    prices = download_prices(universe, args.start, args.end_exclusive)
    print(f"  {len(prices)} price points", flush=True)

    print("emitting naive baselines", flush=True)
    always_up = emit_always_up(universe, prices, DEFAULT_HORIZONS)
    momentum = emit_momentum(universe, prices, DEFAULT_HORIZONS)
    print(f"  always-up={len(always_up)} momentum={len(momentum)}", flush=True)

    reports: dict[str, dict] = {}
    empty_overall = {
        "horizon": f"+{PRIMARY_HORIZON}",
        "claim_count": 0,
        "matured_count": 0,
        "void_count": 0,
        "independent_blocks": 0,
        "coverage": None,
        "hit_rate": None,
        "baseline_hit_rate": None,
        "mean_cost_adjusted_excess": None,
        "excess_ci": [None, None],
        "first_entry_session": None,
        "last_entry_session": None,
    }
    jobs = (
        (OPTIONS_POLICY_ID, option_claims),
        (ALWAYS_UP_POLICY_ID, always_up),
        (MOMENTUM_POLICY_ID, momentum),
    )
    for policy_id, claims in jobs:
        if not claims:
            reports[policy_id] = {
                "protocol_version": spec.minimum_improvement_profile,
                "policy_version_id": policy_id,
                "scoring_mode": "direction_only",
                "verdict": "UNDERPOWERED",
                "reason": "no directional claims emitted",
                "data_hash": "",
                "gate": {
                    "passed": False,
                    "profile": spec.minimum_improvement_profile,
                    "primary_metric": "benchmark_adjusted_signed_return",
                    "floor": SIGNED_RETURN_FLOOR,
                    "improvement": None,
                    "improvement_ci_lower": None,
                    "failures": ["underpowered"],
                },
                "overall": empty_overall,
            }
            continue
        print(f"scoring {policy_id} n={len(claims)}", flush=True)
        report = validate(claims, prices, spec)
        print(f"  {report}", flush=True)
        reports[report.policy_version_id] = report_to_dict(report)

    payload = {
        "run_date": datetime.now().isoformat(timespec="seconds"),
        "universe": list(universe),
        "option_sessions": option_sessions,
        "options_raw_claim_count": len(option_claims),
        "spec": {
            "horizons": list(spec.horizons),
            "primary_horizon": spec.primary_horizon,
            "benchmarks": list(spec.benchmarks),
            "cost_bps": spec.cost_bps,
            "min_independent_blocks": spec.min_independent_blocks,
            "floor": SIGNED_RETURN_FLOOR,
        },
        "reports": reports,
    }
    stem = ROOT / args.output_stem
    stem.parent.mkdir(parents=True, exist_ok=True)
    stem.with_suffix(".json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown = render_markdown(
        run_date=payload["run_date"],
        universe=universe,
        option_sessions=option_sessions,
        reports=reports,
        options_raw_n=len(option_claims),
    )
    stem.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(f"wrote {stem.with_suffix('.md')} and {stem.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
