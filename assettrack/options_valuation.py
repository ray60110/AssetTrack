"""ATM option richness versus realized volatility.

This module answers whether call/put premiums look expensive or cheap
relative to trailing realized vol.  It does not forecast the underlying.
"""
from __future__ import annotations

import math
from datetime import date as date_cls, timedelta
from typing import Optional, Sequence

from .greeks import bs_greeks, bs_price, implied_vol
from .options_analysis import _dte_between, _quote_mid, compute_iv_percentile

FAIR_VOL_BAND = 0.03  # 3 vol points around IV − RV
RV_WINDOW = 20
MIN_RETURNS = 10
DOLLAR_EDGE_SPOT_PCT = 0.002
DOLLAR_EDGE_FLOOR = 0.10
EARNINGS_NOTE_MAX_DAYS = 10  # show remaining-days note when 0 <= days < 10
RICHNESS_HISTORY_DAYS = 90  # ATM IV − RV detail window; older snapshots are dropped
_RICHNESS_ZH = {"expensive": "偏貴", "cheap": "偏便宜", "fair": "合理"}
_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def annualized_realized_vol(
    closes: Sequence[float],
    *,
    min_returns: int = MIN_RETURNS,
    window: int = RV_WINDOW,
) -> Optional[float]:
    """Sample standard deviation of log returns, annualized with 252 sessions."""
    logs: list[float] = []
    for prev, price in zip(closes, closes[1:]):
        if prev and price and prev > 0 and price > 0:
            logs.append(math.log(price / prev))
    if len(logs) < min_returns:
        return None
    sample = logs[-window:] if len(logs) >= window else logs
    if len(sample) < min_returns:
        return None
    mean = sum(sample) / len(sample)
    var = sum((item - mean) ** 2 for item in sample) / (len(sample) - 1)
    if var < 0:
        return None
    return math.sqrt(var * 252.0)


def _vol_label(spread: Optional[float]) -> str:
    if spread is None:
        return "unknown"
    if spread >= FAIR_VOL_BAND:
        return "expensive"
    if spread <= -FAIR_VOL_BAND:
        return "cheap"
    return "fair"


def _dollar_label(edge: Optional[float], spot: float) -> str:
    if edge is None or not spot:
        return "unknown"
    band = max(DOLLAR_EDGE_FLOOR, DOLLAR_EDGE_SPOT_PCT * spot)
    if edge >= band:
        return "expensive"
    if edge <= -band:
        return "cheap"
    return "fair"


def _side_report(
    *,
    spot: float,
    strike: float,
    dte: int,
    option_type: str,
    market: Optional[float],
    realized_vol: float,
    r: float,
    low_confidence: bool,
) -> dict:
    model = (
        bs_price(spot, strike, dte, realized_vol, option_type, r=r)
        if market is not None
        else None
    )
    edge = (market - model) if (market is not None and model is not None) else None
    greeks = bs_greeks(spot, strike, dte, realized_vol, option_type, premium=market, r=r)
    iv = implied_vol(spot, strike, dte, market, option_type, r=r) if market else None
    return {
        "market": market,
        "model": model,
        "edge": edge,
        "iv": iv,
        "label": _dollar_label(edge, spot),
        "low_confidence": low_confidence,
        "delta": greeks.get("delta"),
        "vega": greeks.get("vega"),
        "theta": greeks.get("theta"),
    }


def assess_contract_richness(
    *,
    spot: float,
    strike: float,
    dte_days: float,
    option_type: str,
    market_price: float,
    realized_vol: float,
    r: float = 0.04,
) -> dict:
    """One contract's market mid versus Black-Scholes priced at realized vol."""
    if realized_vol is None or realized_vol <= 0 or market_price is None:
        return {"ready": False, "reason": "realized_vol_unavailable", "label": "unknown", "edge": None, "vega": None}
    side = _side_report(
        spot=spot,
        strike=strike,
        dte=int(dte_days),
        option_type=option_type,
        market=market_price,
        realized_vol=realized_vol,
        r=r,
        low_confidence=False,
    )
    side["ready"] = side["edge"] is not None
    side["reason"] = None if side["ready"] else "unpriced"
    return side


def days_to_earnings(as_of: str, earnings_date: Optional[str]) -> Optional[int]:
    """Calendar days from ``as_of`` to ``earnings_date``. Negative if already past."""
    if not as_of or not earnings_date:
        return None
    return _dte_between(as_of, earnings_date)


def earnings_remaining_note(
    days: Optional[int],
    *,
    window: int = EARNINGS_NOTE_MAX_DAYS,
) -> Optional[str]:
    """Human note for an approaching earnings date; hidden at ``window`` days or more."""
    if days is None or days < 0 or days >= window:
        return None
    if days == 0:
        return "財報今日"
    return f"財報剩{days}天"


def _as_iso_date(value) -> Optional[str]:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return str(iso())[:10]
        except Exception:
            return None
    text = str(value)
    return text[:10] if len(text) >= 10 else None


def _dated_pairs_as_of(
    dated_closes: Optional[Sequence[tuple]],
    as_of: str,
) -> list[tuple[str, float]]:
    """Unique (date, price) rows on or before ``as_of``, sorted — no look-ahead."""
    latest: dict[str, float] = {}
    for item in dated_closes or []:
        if not item or len(item) < 2:
            continue
        day, price = item[0], item[1]
        stamp = _as_iso_date(day)
        if not stamp or stamp > as_of or not price:
            continue
        try:
            px = float(price)
        except (TypeError, ValueError):
            continue
        if px > 0:
            latest[stamp] = px
    return sorted(latest.items(), key=lambda row: row[0])


def _closes_as_of(
    dated_closes: Optional[Sequence[tuple]],
    as_of: str,
) -> list[float]:
    """Prices on or before ``as_of`` — no look-ahead."""
    return [px for _, px in _dated_pairs_as_of(dated_closes, as_of)]


def rolling_realized_vol(
    dated_closes: Optional[Sequence[tuple]],
    as_of: str,
    *,
    window: int = RV_WINDOW,
) -> Optional[float]:
    """Annualized RV from the last ``window`` daily log returns ending on ``as_of``.

    Needs ``window + 1`` sessions on or before that day. Extra older closes are
    ignored so each day is a trailing 20-session window, not a longer sample.
    """
    if not as_of or window <= 0:
        return None
    pairs = _dated_pairs_as_of(dated_closes, as_of)
    if len(pairs) < window + 1:
        return None
    return annualized_realized_vol(
        [px for _, px in pairs[-(window + 1):]],
        min_returns=window,
        window=window,
    )


def invert_contract_iv_series(
    dated_spots: Sequence[tuple],
    *,
    strike: float,
    expiry: str,
    call_closes: Sequence[tuple] = (),
    put_closes: Sequence[tuple] = (),
    r: float = 0.04,
) -> dict[str, float]:
    """Invert one listed ATM pair's historical closes into a daily IV map."""
    calls = {stamp: px for stamp, px in _dated_pairs_as_of(call_closes, "9999-12-31")}
    puts = {stamp: px for stamp, px in _dated_pairs_as_of(put_closes, "9999-12-31")}
    out: dict[str, float] = {}
    for stamp, spot in _dated_pairs_as_of(dated_spots, "9999-12-31"):
        dte = _dte_between(stamp, expiry)
        if dte is None or dte <= 0 or not spot:
            continue
        ivs = []
        call_px = calls.get(stamp)
        put_px = puts.get(stamp)
        if call_px:
            iv = implied_vol(spot, strike, dte, call_px, "call", r=r)
            if iv:
                ivs.append(iv)
        if put_px:
            iv = implied_vol(spot, strike, dte, put_px, "put", r=r)
            if iv:
                ivs.append(iv)
        if ivs:
            out[stamp] = sum(ivs) / len(ivs)
    return out


def richness_series(
    snapshots: list[dict],
    *,
    r: float = 0.04,
    dated_closes: Optional[Sequence[tuple]] = None,
    as_of: Optional[str] = None,
    window_days: int = RICHNESS_HISTORY_DAYS,
    contract_iv_by_date: Optional[dict] = None,
) -> list[dict]:
    """Daily ATM IV − RV over the trailing ``window_days`` calendar days.

    Each day's RV is the trailing 20-session realized vol ending that day.
    ATM IV prefers that day's option-chain snapshot; if none, uses
    ``contract_iv_by_date`` (IV inverted from the live ATM contract history).
    """
    snaps = sorted(
        [row for row in (snapshots or []) if row],
        key=lambda row: str(row.get("date") or ""),
    )
    snap_by_date = {
        str(row.get("date") or "")[:10]: row
        for row in snaps
        if row.get("date")
    }
    end_s = as_of
    if not end_s:
        if dated_closes:
            pairs = _dated_pairs_as_of(dated_closes, "9999-12-31")
            end_s = pairs[-1][0] if pairs else None
        if not end_s and snaps:
            end_s = str(snaps[-1].get("date") or "")[:10]
    if not end_s:
        return []
    try:
        cutoff = (date_cls.fromisoformat(end_s) - timedelta(days=window_days)).isoformat()
    except ValueError:
        cutoff = end_s

    if dated_closes:
        sessions = [
            stamp
            for stamp, _ in _dated_pairs_as_of(dated_closes, end_s)
            if stamp >= cutoff
        ]
    else:
        sessions = [
            stamp for stamp in snap_by_date
            if cutoff <= stamp <= end_s
        ]

    contract_iv = contract_iv_by_date or {}
    last_earn: Optional[str] = None
    points: list[dict] = []
    for stamp in sessions:
        snap = snap_by_date.get(stamp)
        if snap and snap.get("earnings_date"):
            last_earn = _as_iso_date(snap.get("earnings_date"))
        realized_vol = rolling_realized_vol(dated_closes, stamp)
        if realized_vol is None and snap:
            realized_vol = annualized_realized_vol(
                [
                    float(row["spot_price"])
                    for row in snaps
                    if row.get("spot_price") and str(row.get("date") or "")[:10] <= stamp
                ]
            )
        if realized_vol is None:
            continue
        atm_iv = None
        iv_source = None
        if snap:
            report = assess_option_richness(snap, realized_vol=realized_vol, r=r)
            if report.get("ready") and report.get("atm_iv") is not None:
                atm_iv = report["atm_iv"]
                iv_source = "snapshot"
        if atm_iv is None and stamp in contract_iv:
            atm_iv = contract_iv[stamp]
            iv_source = "contract_history"
        vol_spread = (atm_iv - realized_vol) if atm_iv is not None else None
        earn_s = last_earn
        days = days_to_earnings(stamp, earn_s)
        points.append(
            {
                "date": stamp,
                "atm_iv": atm_iv,
                "realized_vol": realized_vol,
                "vol_spread": vol_spread,
                "richness": _vol_label(vol_spread),
                "iv_source": iv_source,
                "earnings_date": earn_s,
                "days_to_earnings": days,
                "earnings_note": earnings_remaining_note(days),
            }
        )
    return points


def format_richness_history(underlying: str, points: Sequence[dict]) -> str:
    """Watchlist-detail text: sparkline plus daily ATM IV, RV, spread, earnings."""
    title = f"{underlying}　每日 ATM IV − RV（近{RICHNESS_HISTORY_DAYS}天）"
    if not points:
        return (
            f"{title}\n"
            "尚無歷史點（資料收集中）。這不是股價漲跌預測。"
        )
    lines = [
        title,
        "這不是股價漲跌預測。正＝權利金相對近期實際波動偏貴。"
        "RV＝當日往前 20 個交易日對數報酬年化標準差。*＝無當日鏈，IV 由目前價平合約歷史價反解。",
        "",
        _spread_sparkline(points),
        "",
        "日期          ATM IV     RV      差距    判斷      財報",
    ]
    for point in points:
        iv = point.get("atm_iv")
        rv = point.get("realized_vol")
        spread = point.get("vol_spread")
        iv_s = f"{iv * 100:.0f}%" if iv is not None else "—"
        rv_s = f"{rv * 100:.0f}%" if rv is not None else "—"
        spread_s = f"{spread * 100:+.0f}pp" if spread is not None else "—"
        kind = point.get("richness") or ""
        label = _RICHNESS_ZH.get(kind, "—")
        if kind == "expensive":
            label = f"[red]{label}[/red]"
        elif kind == "cheap":
            label = f"[cyan]{label}[/cyan]"
        elif kind == "unknown":
            label = "—"
        note = point.get("earnings_note") or ""
        if note:
            note = f"[yellow]{note}[/yellow]"
        if point.get("iv_source") == "contract_history":
            iv_s += "*"
        lines.append(
            f"{point.get('date', '—')}   {iv_s:>6}  {rv_s:>6}  {spread_s:>7}  {label:<16} {note}"
        )
    return "\n".join(lines)


def _spread_sparkline(points: Sequence[dict]) -> str:
    spreads = [p.get("vol_spread") for p in points if p.get("vol_spread") is not None]
    if not spreads:
        return "走勢 —"
    lo, hi = min(spreads), max(spreads)
    n = len(_SPARK_CHARS) - 1
    if hi == lo:
        body = _SPARK_CHARS[n // 2] * len(spreads)
    else:
        body = "".join(
            _SPARK_CHARS[min(n, max(0, int(round((value - lo) / (hi - lo) * n))))]
            for value in spreads
        )
    return f"走勢 {body}　左舊右新"


def richness_from_history(
    snapshots: list[dict],
    *,
    r: float = 0.04,
    closes: Optional[Sequence[float]] = None,
) -> dict:
    """Convenience: RV from yfinance closes, else from snapshot spots."""
    rv = annualized_realized_vol(closes or [])
    if rv is None:
        spots = [s.get("spot_price") for s in snapshots or [] if s.get("spot_price")]
        rv = annualized_realized_vol([float(px) for px in spots if px])
    latest = snapshots[-1] if snapshots else None
    return assess_option_richness(
        latest,
        realized_vol=rv,
        r=r,
        history_snapshots=snapshots,
    )


def assess_option_richness(
    snapshot: Optional[dict],
    *,
    realized_vol: Optional[float],
    r: float = 0.04,
    history_snapshots: Optional[list] = None,
) -> dict:
    """ATM call/put richness for one underlying snapshot.

    ``straddle_edge`` is the dollar gap between the live ATM straddle mid and
    the same straddle priced with trailing realized vol.  Positive means the
    option market is charging more than that realized-vol model — an
    *indicative* premium, not a forecast of the stock and not a validated alpha.
    """
    empty = {
        "ready": False,
        "reason": "realized_vol_unavailable",
        "richness": "unknown",
        "atm_iv": None,
        "realized_vol": realized_vol,
        "vol_spread": None,
        "variance_premium": None,
        "iv_percentile": None,
        "straddle_market": None,
        "straddle_model": None,
        "straddle_edge": None,
        "call": None,
        "put": None,
        "dte": None,
        "atm_strike": None,
        "low_confidence": False,
    }
    if realized_vol is None or realized_vol <= 0:
        return empty
    atm = _select_atm_pair(snapshot)
    if atm is None:
        empty["reason"] = "atm_pair_unavailable"
        return empty

    call_side = _side_report(
        spot=atm["spot"],
        strike=atm["strike"],
        dte=atm["dte"],
        option_type="call",
        market=atm["call_mid"],
        realized_vol=realized_vol,
        r=r,
        low_confidence=atm["call_low_confidence"],
    )
    put_side = _side_report(
        spot=atm["spot"],
        strike=atm["strike"],
        dte=atm["dte"],
        option_type="put",
        market=atm["put_mid"],
        realized_vol=realized_vol,
        r=r,
        low_confidence=atm["put_low_confidence"],
    )
    ivs = [v for v in (call_side["iv"], put_side["iv"]) if v]
    if not ivs:
        ivs = [v for v in (atm["call_iv"], atm["put_iv"]) if v]
    atm_iv = (sum(ivs) / len(ivs)) if ivs else None
    vol_spread = (atm_iv - realized_vol) if atm_iv is not None else None
    variance_premium = (
        (atm_iv * atm_iv - realized_vol * realized_vol) if atm_iv is not None else None
    )
    straddle_market = (
        call_side["market"] + put_side["market"]
        if call_side["market"] is not None and put_side["market"] is not None
        else None
    )
    straddle_model = (
        call_side["model"] + put_side["model"]
        if call_side["model"] is not None and put_side["model"] is not None
        else None
    )
    straddle_edge = (
        straddle_market - straddle_model
        if straddle_market is not None and straddle_model is not None
        else None
    )
    percentile = None
    if history_snapshots:
        iv_pct = compute_iv_percentile(history_snapshots)
        if iv_pct.get("ready"):
            percentile = iv_pct.get("percentile")

    return {
        "ready": True,
        "reason": None,
        "richness": _vol_label(vol_spread),
        "atm_iv": atm_iv,
        "realized_vol": realized_vol,
        "vol_spread": vol_spread,
        "variance_premium": variance_premium,
        "iv_percentile": percentile,
        "straddle_market": straddle_market,
        "straddle_model": straddle_model,
        "straddle_edge": straddle_edge,
        "call": call_side,
        "put": put_side,
        "dte": atm["dte"],
        "atm_strike": atm["strike"],
        "low_confidence": atm["call_low_confidence"] or atm["put_low_confidence"],
    }


def _select_atm_pair(snapshot: Optional[dict]) -> Optional[dict]:
    if not snapshot:
        return None
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
        strike = min(common, key=lambda item: abs(item - spot))
        rank = abs(dte - 30)
        if best is None or rank < best[0]:
            best = (rank, expiry, dte, strike, calls[strike], puts[strike])
    if best is None:
        return None
    _, expiry, dte, strike, call, put = best
    call_mid, call_lc = _quote_mid(call, str(as_of)[:10])
    put_mid, put_lc = _quote_mid(put, str(as_of)[:10])
    if call_mid is None or put_mid is None:
        return None
    return {
        "spot": float(spot),
        "expiry": expiry,
        "dte": dte,
        "strike": strike,
        "call_mid": call_mid,
        "put_mid": put_mid,
        "call_low_confidence": call_lc,
        "put_low_confidence": put_lc,
        "call_iv": call.get("impliedVolatility"),
        "put_iv": put.get("impliedVolatility"),
        "call_symbol": call.get("contractSymbol"),
        "put_symbol": put.get("contractSymbol"),
    }
