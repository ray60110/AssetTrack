#!/usr/bin/env python3
"""Score B/C/D option-method policies on recent NYSE sessions.

Frozen before looking at hit rates.  Does not write production caches or
change the TUI.  Scores through ``direction_forecast_validation.validate``.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime
from math import log
from pathlib import Path
from statistics import median
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
from assettrack.options_analysis import _dte_between
from assettrack.storage import load_options_daily_snapshots

CAL = NYSESessionCalendar()
UNIVERSE = ("AMD", "INTC", "MU", "NVDA", "PLTR", "SPCX", "TSLA", "TSM")
PRIMARY_HORIZON = 5
HORIZONS = (5,)
COST_BPS = 10.0
MIN_CROSS_SECTION = 4
TARGET_DTE = 30
IV_MIN, IV_MAX = 0.05, 2.0
OTM_PUT_TARGET = 0.95
OTM_CALL_TARGET = 1.05
ROLLING = 20
PRICE_START = "2025-01-01"
PRICE_END_EXCLUSIVE = "2026-08-19"
RECENT_FIRST_ENTRY = date(2026, 7, 23)
INDEX_12M_FIRST_ENTRY = date(2025, 8, 18)

B_SMIRK_ID = "b-watchlist-smirk-median-v1"
B_SPREAD_ID = "b-watchlist-iv-spread-median-v1"
B_DUAL_ID = "b-watchlist-dual-confirm-v1"
C_XING_ID = "c-watchlist-xing-smirk-v1"
C_SKEW_ID = "c-cboe-skew-spy-v1"
D_VRP_ID = "d-vix-vrp-spy-v1"
D_LOCAL_PCR_ID = "d-watchlist-volume-pcr-spy-v1"


def extract_surface_features(snapshot: dict, target_dte: int = TARGET_DTE) -> Optional[dict]:
    """Xing smirk and Bali/Cremers IV spread on the ~30 DTE expiry."""
    spot = snapshot.get("spot_price")
    as_of = snapshot.get("date")
    contracts = snapshot.get("contracts") or []
    if not spot or spot <= 0 or not as_of or not contracts:
        return None

    by_exp: dict[str, list] = {}
    for contract in contracts:
        expiry = contract.get("expiry")
        if expiry:
            by_exp.setdefault(str(expiry), []).append(contract)

    best = None
    for expiry, rows in by_exp.items():
        calls = {
            float(row["strike"]): row
            for row in rows
            if row.get("type") == "call" and row.get("strike") is not None
        }
        puts = {
            float(row["strike"]): row
            for row in rows
            if row.get("type") == "put" and row.get("strike") is not None
        }
        common = set(calls) & set(puts)
        if not common:
            continue
        dte = _dte_between(str(as_of)[:10], expiry)
        if dte is None or dte <= 0:
            continue
        atm = min(common, key=lambda strike: abs(strike - spot))
        rank = abs(dte - target_dte)
        if best is None or rank < best[0]:
            best = (rank, expiry, dte, atm, calls, puts)
    if best is None:
        return None

    _, expiry, dte, atm, calls, puts = best
    atm_call_iv = _clean_iv(calls[atm].get("impliedVolatility"))
    atm_put_iv = _clean_iv(puts[atm].get("impliedVolatility"))
    if atm_call_iv is None or atm_put_iv is None:
        return None

    put_strike = min(puts, key=lambda strike: abs(strike / spot - OTM_PUT_TARGET))
    call_strike = min(calls, key=lambda strike: abs(strike / spot - OTM_CALL_TARGET))
    if not (0.90 <= put_strike / spot <= 0.98):
        return None
    if not (1.02 <= call_strike / spot <= 1.10):
        return None
    otm_put_iv = _clean_iv(puts[put_strike].get("impliedVolatility"))
    otm_call_iv = _clean_iv(calls[call_strike].get("impliedVolatility"))
    if otm_put_iv is None:
        return None

    return {
        "expiry": expiry,
        "dte": dte,
        "atm_strike": atm,
        "atm_call_iv": atm_call_iv,
        "atm_put_iv": atm_put_iv,
        "otm_put_iv": otm_put_iv,
        "otm_call_iv": otm_call_iv,
        "smirk": otm_put_iv - atm_call_iv,
        "iv_spread": atm_call_iv - atm_put_iv,
        "rr25_approx": None if otm_call_iv is None else otm_put_iv - otm_call_iv,
    }


def _clean_iv(value) -> Optional[float]:
    try:
        iv = float(value)
    except (TypeError, ValueError):
        return None
    if iv < IV_MIN or iv > IV_MAX:
        return None
    return iv


def load_normalised() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for symbol in UNIVERSE:
        snaps = normalise_option_snapshots(load_options_daily_snapshots(symbol))
        if snaps:
            out[symbol] = snaps
    return out


def daily_watchlist_rows(snapshots: dict[str, list[dict]]) -> dict[date, list[dict]]:
    by_session: dict[date, list[dict]] = defaultdict(list)
    for symbol, snaps in snapshots.items():
        for snapshot in snaps:
            raw = snapshot.get("date")
            if not raw:
                continue
            session = date.fromisoformat(str(raw)[:10])
            if not CAL.is_session(session):
                continue
            features = extract_surface_features(snapshot)
            put_vol = sum(float(c.get("volume") or 0.0) for c in snapshot.get("contracts") or [] if c.get("type") == "put")
            call_vol = sum(float(c.get("volume") or 0.0) for c in snapshot.get("contracts") or [] if c.get("type") == "call")
            row = {
                "symbol": symbol,
                "session": session,
                "features": features,
                "put_volume": put_vol,
                "call_volume": call_vol,
            }
            by_session[session].append(row)
    return dict(by_session)


def emit_cross_section_claims(rows_by_session: dict[date, list[dict]]) -> dict[str, list[ForecastRecord]]:
    smirk_claims: list[ForecastRecord] = []
    spread_claims: list[ForecastRecord] = []
    dual_claims: list[ForecastRecord] = []
    for session, rows in sorted(rows_by_session.items()):
        scored = [row for row in rows if row["features"] is not None]
        if len(scored) < MIN_CROSS_SECTION:
            continue
        smirk_cut = median(row["features"]["smirk"] for row in scored)
        spread_cut = median(row["features"]["iv_spread"] for row in scored)
        for row in scored:
            smirk_dir = _side(row["features"]["smirk"], smirk_cut, high="down", low="up")
            spread_dir = _side(row["features"]["iv_spread"], spread_cut, high="up", low="down")
            if smirk_dir:
                smirk_claims.append(_claim(B_SMIRK_ID, row["symbol"], session, smirk_dir))
            if spread_dir:
                spread_claims.append(_claim(B_SPREAD_ID, row["symbol"], session, spread_dir))
            if smirk_dir and spread_dir and smirk_dir == spread_dir:
                dual_claims.append(_claim(B_DUAL_ID, row["symbol"], session, smirk_dir))
    xing_claims = [
        ForecastRecord(
            policy_version_id=C_XING_ID,
            outcome_target=item.outcome_target,
            entry_session=item.entry_session,
            horizon_sessions=item.horizon_sessions,
            direction=item.direction,
        )
        for item in smirk_claims
    ]
    return {
        B_SMIRK_ID: smirk_claims,
        B_SPREAD_ID: spread_claims,
        B_DUAL_ID: dual_claims,
        C_XING_ID: xing_claims,
    }


def _side(value: float, cut: float, *, high: str, low: str) -> Optional[str]:
    if value > cut:
        return high
    if value < cut:
        return low
    return None


def _claim(policy: str, symbol: str, session: date, direction: str) -> ForecastRecord:
    return ForecastRecord(
        policy_version_id=policy,
        outcome_target=symbol,
        entry_session=session,
        horizon_sessions=PRIMARY_HORIZON,
        direction=direction,  # type: ignore[arg-type]
    )


def download_index_frame() -> pd.DataFrame:
    tickers = ["SPY", "QQQ", "^VIX", "^SKEW", *UNIVERSE]
    print(f"downloading yfinance {', '.join(tickers)}", flush=True)
    frame = yf.download(
        tickers,
        start=PRICE_START,
        end=PRICE_END_EXCLUSIVE,
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    closes = {}
    for symbol in tickers:
        if isinstance(frame.columns, pd.MultiIndex):
            if symbol not in frame.columns.get_level_values(0):
                continue
            series = frame[symbol]["Close"]
        else:
            series = frame["Close"]
        closes[symbol] = series
    out = pd.DataFrame(closes).dropna(how="all")
    out.index = pd.to_datetime(out.index).date
    return out


def price_panel(frame: pd.DataFrame, symbols: Iterable[str]) -> dict[tuple[str, date], float]:
    prices: dict[tuple[str, date], float] = {}
    for symbol in symbols:
        if symbol not in frame.columns:
            continue
        for session, value in frame[symbol].dropna().items():
            if CAL.is_session(session):
                prices[(symbol, session)] = float(value)
    return prices


def emit_skew_claims(frame: pd.DataFrame) -> list[ForecastRecord]:
    skew = frame["^SKEW"].dropna()
    sessions = [session for session in skew.index if CAL.is_session(session)]
    claims: list[ForecastRecord] = []
    for index, session in enumerate(sessions):
        prior = sessions[max(0, index - ROLLING) : index]
        if len(prior) < ROLLING:
            continue
        cut = median(float(skew[item]) for item in prior)
        value = float(skew[session])
        direction = "down" if value > cut else "up" if value < cut else None
        if direction is None:
            continue
        claims.append(_claim(C_SKEW_ID, "SPY", session, direction))
    return claims


def emit_vrp_claims(frame: pd.DataFrame) -> list[ForecastRecord]:
    spy = frame["SPY"].dropna()
    vix = frame["^VIX"].dropna()
    sessions = [session for session in spy.index if CAL.is_session(session) and session in vix.index]
    log_ret = {}
    for index, session in enumerate(sessions):
        if index == 0:
            continue
        prev = sessions[index - 1]
        if spy[prev] > 0 and spy[session] > 0:
            log_ret[session] = float(log(spy[session] / spy[prev]))
    claims: list[ForecastRecord] = []
    for index, session in enumerate(sessions):
        hist = [log_ret[item] for item in sessions[max(1, index - 19) : index + 1] if item in log_ret]
        if len(hist) < 20 or session not in vix.index:
            continue
        rv = 252.0 * pd.Series(hist).var(ddof=1)
        iv = (float(vix[session]) / 100.0) ** 2
        vrp = iv - rv
        prior_vrp = []
        for look in range(index - ROLLING, index):
            if look < 1:
                continue
            look_session = sessions[look]
            look_hist = [
                log_ret[item]
                for item in sessions[max(1, look - 19) : look + 1]
                if item in log_ret
            ]
            if len(look_hist) < 20 or look_session not in vix.index:
                continue
            look_rv = 252.0 * pd.Series(look_hist).var(ddof=1)
            look_iv = (float(vix[look_session]) / 100.0) ** 2
            prior_vrp.append(look_iv - look_rv)
        if len(prior_vrp) < ROLLING:
            continue
        cut = median(prior_vrp)
        direction = "up" if vrp > cut else "down" if vrp < cut else None
        if direction is None:
            continue
        claims.append(_claim(D_VRP_ID, "SPY", session, direction))
    return claims


def emit_local_pcr_claims(rows_by_session: dict[date, list[dict]]) -> list[ForecastRecord]:
    series: list[tuple[date, float]] = []
    for session, rows in sorted(rows_by_session.items()):
        put_vol = sum(row["put_volume"] for row in rows)
        call_vol = sum(row["call_volume"] for row in rows)
        if call_vol <= 0 or put_vol < 0:
            continue
        series.append((session, put_vol / call_vol))
    claims: list[ForecastRecord] = []
    for index, (session, value) in enumerate(series):
        window = [item[1] for item in series[max(0, index - ROLLING) : index]]
        if len(window) < 5:
            continue
        cut = median(window)
        direction = "up" if value > cut else "down" if value < cut else None
        if direction is None:
            continue
        claims.append(_claim(D_LOCAL_PCR_ID, "SPY", session, direction))
    return claims


def filter_entries(claims: list[ForecastRecord], first: date, last: date) -> list[ForecastRecord]:
    return [item for item in claims if first <= item.entry_session <= last]


def last_entry_date(prices: dict[tuple[str, date], float], symbol: str, horizon: int) -> Optional[date]:
    sessions = sorted(session for (sym, session) in prices if sym == symbol)
    if len(sessions) <= horizon:
        return None
    return sessions[-1 - horizon]


def report_to_dict(report: DirectionValidationReport) -> dict:
    overall = report.overall
    return {
        "policy_version_id": report.policy_version_id,
        "verdict": report.verdict,
        "reason": report.reason,
        "independent_blocks": overall.independent_blocks,
        "hit_rate": overall.hit_rate,
        "baseline_hit_rate": overall.baseline_hit_rate,
        "mean_cost_adjusted_excess": overall.mean_cost_adjusted_excess,
        "excess_ci": [overall.excess_ci.lower, overall.excess_ci.upper],
        "claim_count": overall.claim_count,
        "matured_count": overall.matured_count,
        "void_count": overall.void_count,
        "first_entry_session": (
            overall.first_entry_session.isoformat() if overall.first_entry_session else None
        ),
        "last_entry_session": (
            overall.last_entry_session.isoformat() if overall.last_entry_session else None
        ),
    }


def score_policy(
    claims: list[ForecastRecord],
    prices: dict[tuple[str, date], float],
    spec: ValidationSpec,
) -> Optional[dict]:
    if not claims:
        return None
    report = validate(claims, prices, spec)
    return report_to_dict(report)


def _fmt_pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_ret(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:+.3f}%"


def render_markdown(payload: dict) -> str:
    lines = [
        "# B／C／D 近期命中率",
        "",
        f"執行時間：{payload['run_at']}  ",
        "協議：`direction-forecast-validation-v1`；主前瞻期 +5 NYSE session；成本 10 bps。  ",
        "假說在看命中率之前凍結，看完不調參。",
        "",
        "## 凍結定義",
        "",
        "- **B** 觀察層數值若當成方向：每日在觀察清單上做橫截面中位排序。"
        "smirk（OTM 0.95 put IV − ATM call IV）高於當日中位 → 看空；IV spread（ATM call IV − ATM put IV）高於當日中位 → 看多。"
        "雙確認＝兩訊號同向才發訊號。B 本身不是方向策略；此列是「若把觀察數字當建議」。",
        "- **C** 大宇集 Xing smirk：本機沒有數百檔歷史鏈。可行替代是 **CBOE SKEW → SPY**"
        "（SKEW 高於近 20 日中位 → 看空）。另報同一套 smirk 中位排序在 8 檔日鏈上的結果，標為 C 的「現有資料近似」。",
        "- **D** 大盤風險：主訊號 **VRP = (VIX/100)² − 過去 20 日 SPY 對數報酬年化變異**；VRP 高於近 20 日中位 → SPY 看多。"
        "Cboe 免費 equity PCR CSV 停在 2019-10-04，**無法**做 2026 近期 Cboe PCR。"
        "另報觀察清單 put/call **成交量**比（非 Cboe）對 SPY 的反向解讀，僅作代理。",
        "- 到期日選 DTE 最接近 30 且有 ATM call/put；IV 限 5%–200%，以免短天期垃圾 IV。",
        "",
        "## 近期窗（與期權日鏈同一段）",
        "",
        f"進場 {payload['recent_first']}～{payload['recent_last']}；結算用 yfinance 調整收盤，最後可成熟進場日為 {payload['last_maturable_entry']}。",
        "",
        "| 策略 | 標的 | 判定 | 去重疊 n | 命中率 | 無條件上漲率 | 扣成本超額 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in payload["recent_rows"]:
        lines.append(
            f"| {row['label']} | {row['target']} | **{row['verdict']}** | {row['n']} | "
            f"{row['hit']} | {row['base']} | {row['excess']} |"
        )
    lines += [
        "",
        "## 近一年（僅指數層 C／D，為了對照樣本是否夠）",
        "",
        f"進場 {payload['year_first']}～{payload['recent_last']}。",
        "",
        "| 策略 | 標的 | 判定 | 去重疊 n | 命中率 | 無條件上漲率 | 扣成本超額 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in payload["year_rows"]:
        lines.append(
            f"| {row['label']} | {row['target']} | **{row['verdict']}** | {row['n']} | "
            f"{row['hit']} | {row['base']} | {row['excess']} |"
        )
    lines += [
        "",
        "## 讀法",
        "",
        "- n<30 一律 **UNDERPOWERED**：命中率可報，但不能當導入依據，也不能調參。",
        f"- Scheme B 地板：扣成本超額 95% CI 下界 > {SIGNED_RETURN_FLOOR:.4f}。",
        "- 橫截面 8 檔不是 Xing（2010）的數百檔五分位；SKEW 也不是個股 smirk。",
        "- 本機 PCR 不是 Cboe equity PCR。",
        "",
    ]
    return "\n".join(lines)


def table_row(label: str, target: str, report: Optional[dict]) -> dict:
    if report is None:
        return {
            "label": label,
            "target": target,
            "verdict": "NO_SIGNAL",
            "n": 0,
            "hit": "—",
            "base": "—",
            "excess": "—",
            "raw": None,
        }
    return {
        "label": label,
        "target": target,
        "verdict": report["verdict"],
        "n": report["independent_blocks"],
        "hit": _fmt_pct(report["hit_rate"]),
        "base": _fmt_pct(report["baseline_hit_rate"]),
        "excess": _fmt_ret(report["mean_cost_adjusted_excess"]),
        "raw": report,
    }


def main() -> None:
    snapshots = load_normalised()
    rows_by_session = daily_watchlist_rows(snapshots)
    watch_claims = emit_cross_section_claims(rows_by_session)
    frame = download_index_frame()
    names_prices = price_panel(frame, [*UNIVERSE, "QQQ", "SPY"])
    spy_prices = price_panel(frame, ["SPY", "QQQ"])
    last_entry = last_entry_date(names_prices, "NVDA", PRIMARY_HORIZON)
    if last_entry is None:
        raise SystemExit("not enough prices to settle +5 sessions")

    spec = ValidationSpec.scheme_b(
        horizons=HORIZONS,
        primary_horizon=PRIMARY_HORIZON,
        benchmarks=("QQQ",),
        cost_bps=COST_BPS,
        bootstrap_samples=5_000,
    )
    spec_spy = ValidationSpec.scheme_b(
        horizons=HORIZONS,
        primary_horizon=PRIMARY_HORIZON,
        benchmarks=("QQQ",),
        cost_bps=COST_BPS,
        bootstrap_samples=5_000,
    )

    skew_all = emit_skew_claims(frame)
    vrp_all = emit_vrp_claims(frame)
    pcr_all = emit_local_pcr_claims(rows_by_session)

    recent_policies = {
        "B smirk 中位排序": (watch_claims[B_SMIRK_ID], names_prices, spec, "8 檔"),
        "B IV spread 中位排序": (watch_claims[B_SPREAD_ID], names_prices, spec, "8 檔"),
        "B 雙確認（主）": (watch_claims[B_DUAL_ID], names_prices, spec, "8 檔"),
        "C Xing smirk 8 檔近似": (watch_claims[C_XING_ID], names_prices, spec, "8 檔"),
        "C CBOE SKEW→SPY（主）": (skew_all, spy_prices, spec_spy, "SPY"),
        "D VIX VRP→SPY（主）": (vrp_all, spy_prices, spec_spy, "SPY"),
        "D 本機成交量 PCR→SPY": (pcr_all, spy_prices, spec_spy, "SPY"),
    }

    recent_rows = []
    recent_raw = {}
    for label, (claims, prices, used_spec, target) in recent_policies.items():
        clipped = filter_entries(claims, RECENT_FIRST_ENTRY, last_entry)
        report = score_policy(clipped, prices, used_spec)
        recent_rows.append(table_row(label, target, report))
        recent_raw[label] = report
        print(f"recent {label}: {report}", flush=True)

    year_labels = {
        "C CBOE SKEW→SPY（主）": (skew_all, spy_prices, spec_spy, "SPY"),
        "D VIX VRP→SPY（主）": (vrp_all, spy_prices, spec_spy, "SPY"),
    }
    year_rows = []
    year_raw = {}
    for label, (claims, prices, used_spec, target) in year_labels.items():
        clipped = filter_entries(claims, INDEX_12M_FIRST_ENTRY, last_entry)
        report = score_policy(clipped, prices, used_spec)
        year_rows.append(table_row(label, target, report))
        year_raw[label] = report
        print(f"year {label}: {report}", flush=True)

    payload = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "recent_first": RECENT_FIRST_ENTRY.isoformat(),
        "recent_last": last_entry.isoformat(),
        "last_maturable_entry": last_entry.isoformat(),
        "year_first": INDEX_12M_FIRST_ENTRY.isoformat(),
        "recent_rows": recent_rows,
        "year_rows": year_rows,
        "recent_raw": recent_raw,
        "year_raw": year_raw,
        "cboe_pcr_note": "Cboe free PCR CSV ends 2019-10-04; recent Cboe PCR hit rate not computed.",
        "frozen": {
            "horizon": PRIMARY_HORIZON,
            "cost_bps": COST_BPS,
            "min_cross_section": MIN_CROSS_SECTION,
            "target_dte": TARGET_DTE,
            "rolling": ROLLING,
        },
    }
    stem = ROOT / "docs" / "options_bcd_recent_hit_rates"
    stem.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    stem.with_suffix(".md").write_text(render_markdown(payload), encoding="utf-8")
    print(f"wrote {stem}.md", flush=True)


if __name__ == "__main__":
    main()
