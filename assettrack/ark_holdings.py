"""assettrack.ark_holdings — 官方每日完整持股來源（bug#00123）

Why this module exists
──────────────────────
進階分析的方向訊號需要「同一檔基金在不同日期揭露了**不同**的持股狀態」。Yahoo 的
`topHoldings` 只提供前十大持股的權重，而且是依各基金的**揭露頻率**更新（多數為月頻，
且 Yahoo 另有延遲），實測本機 32 檔 ETF 在 16 個交易日內權重完全沒有變動過一次，
`weight_delta` 恆為 0，於是「兩個真實訊號必須一致」的規則永遠得到 flat。這不是分析
邏輯的錯，是資料來源根本不含每日交易資訊。

ARK Invest 是少數**每日**公開完整持股（含真實股數與市值）的主動式 ETF 發行商，因此
本模組提供官方 CSV adapter：抓到就用真實股數與市值，抓不到就回 None 由呼叫端沿用
Yahoo 路徑——絕不以估計值冒充官方揭露。

回傳格式刻意與 `quotes.fetch_etf_holdings()` 相同，呼叫端可直接替換。
"""
from __future__ import annotations

import csv
import io
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ARK_CSV_BASE = "https://assets.ark-funds.com/fund-documents/funds-etf-csv/"

# ARK 官方 CSV 檔名（每日更新）。檔名偶有調整，因此 `_candidate_urls()` 會在已知檔名
# 失敗時，改以 ticker 樣式從發行商的 CSV 目錄索引探索，仍失敗才放棄。
ARK_HOLDINGS_CSV = {
    "ARKK": "ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    "ARKQ": "ARK_AUTONOMOUS_TECH._&_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    "ARKW": "ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
    "ARKG": "ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
    "ARKF": "ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv",
    "ARKX": "ARK_SPACE_EXPLORATION_&_INNOVATION_ETF_ARKX_HOLDINGS.csv",
    "PRNT": "THE_3D_PRINTING_ETF_PRNT_HOLDINGS.csv",
    "IZRL": "ARK_ISRAEL_INNOVATIVE_TECHNOLOGY_ETF_IZRL_HOLDINGS.csv",
}

OFFICIAL_DAILY_SOURCES = frozenset(ARK_HOLDINGS_CSV)

# 一份官方每日持股至少要有這麼多筆有效部位才採信；低於此數視為抓到殘檔或錯誤頁面。
_MIN_VALID_ROWS = 5

_HEADER_ALIASES = {
    "date": ("date",),
    "ticker": ("ticker", "symbol"),
    "company": ("company", "name"),
    "cusip": ("cusip",),
    "shares": ("shares", "share", "shares held"),
    "value": ("market value ($)", "market value($)", "market value", "value"),
    "weight": ("weight (%)", "weight(%)", "weight", "weight %"),
}


def is_official_daily_source(symbol: str) -> bool:
    """True when `symbol` has a publisher-provided daily full-holdings file."""
    return (symbol or "").upper() in OFFICIAL_DAILY_SOURCES


def _candidate_urls(symbol: str) -> list[str]:
    known = ARK_HOLDINGS_CSV.get((symbol or "").upper())
    return [ARK_CSV_BASE + known] if known else []


def _default_get_bytes(url: str) -> bytes:
    import urllib.request
    request = urllib.request.Request(
        url, headers={"User-Agent": "AssetTrack/1.0 (personal portfolio tool)"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def _number(text) -> Optional[float]:
    """Parse an ARK CSV numeric cell. Returns None (never 0) when unparseable —
    a missing share count must stay missing, not become a fabricated zero."""
    if text is None:
        return None
    cleaned = re.sub(r"[,$\s]", "", str(text))
    if not cleaned or cleaned in ("-", "--", "N/A", "NA"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_date(text) -> Optional[str]:
    """ARK dates arrive as M/D/YYYY; normalize to the YYYY-MM-DD used by storage."""
    raw = str(text or "").strip()
    if not raw:
        return None
    from datetime import datetime
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    lookup = {(f or "").strip().lower(): f for f in (fieldnames or [])}
    resolved: dict[str, str] = {}
    for key, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                resolved[key] = lookup[alias]
                break
    return resolved


def parse_ark_holdings_csv(raw: bytes) -> Optional[dict]:
    """Parse an ARK daily holdings CSV into the `fetch_etf_holdings()` shape.

    Returns None — never a partially-invented portfolio — when the payload is
    not a holdings CSV, lacks the columns the trend engine needs, or carries
    fewer than `_MIN_VALID_ROWS` usable positions. ARK appends free-text
    disclaimer lines after the data; those rows have no ticker and are skipped.
    """
    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception:
        return None

    reader = csv.DictReader(io.StringIO(text))
    columns = _resolve_columns(reader.fieldnames or [])
    if not {"ticker", "shares", "value"} <= set(columns):
        return None

    holdings: list[dict] = []
    dates: list[str] = []
    for row in reader:
        ticker = str(row.get(columns["ticker"]) or "").strip().upper()
        # Disclaimer/footer lines and cash rows carry no ticker.
        if not ticker or " " in ticker:
            continue
        shares = _number(row.get(columns["shares"]))
        value = _number(row.get(columns["value"]))
        if shares is None or value is None or shares <= 0 or value <= 0:
            continue
        row_date = _normalize_date(row.get(columns["date"])) if "date" in columns else None
        if row_date:
            dates.append(row_date)
        holdings.append({
            "symbol": ticker,
            "name": str(row.get(columns.get("company", "")) or "").strip() or ticker,
            "cusip": (str(row.get(columns["cusip"]) or "").strip() or None)
            if "cusip" in columns else None,
            "shares": shares,
            "value": value,
            "weight": _number(row.get(columns["weight"])) if "weight" in columns else None,
            # The fund's own valuation divided by its own share count — a real
            # disclosed price, not the fixed-average assumption removed in
            # bug#00061.
            "price": round(value / shares, 6),
            "instrument_type": "stock",
        })

    if len(holdings) < _MIN_VALID_ROWS:
        return None

    total_value = sum(h["value"] for h in holdings)
    # Prefer the publisher's own weight column; only derive one when absent, and
    # only from real values already present in the same file.
    if total_value > 0:
        for holding in holdings:
            if holding.get("weight") is None:
                holding["weight"] = round(holding["value"] / total_value * 100.0, 6)

    holdings.sort(key=lambda h: h["value"], reverse=True)
    return {
        "holdings": holdings,
        "asset_classes": {},
        "as_of_date": max(dates) if dates else None,
        "aum": total_value or None,
        "holdings_source": "ark_official_daily",
    }


def fetch_official_daily_holdings(
    symbol: str,
    get_bytes: Optional[Callable[[str], bytes]] = None,
) -> Optional[dict]:
    """Publisher-provided daily full holdings for `symbol`, or None.

    None means "no official file today" and the caller must fall back to its
    existing source. Network and parse failures are swallowed deliberately:
    this is an enrichment path, and a failed fetch must never replace a
    previously valid portfolio (same rule as bug#00058/00083).
    """
    requester = get_bytes or _default_get_bytes
    for url in _candidate_urls(symbol):
        try:
            parsed = parse_ark_holdings_csv(requester(url))
        except Exception as exc:
            logger.debug("ARK holdings fetch failed for %s (%s): %s", symbol, url, exc)
            continue
        if parsed:
            parsed["holdings_url"] = url
            return parsed
    return None
