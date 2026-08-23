"""Live active-ETF discovery and SEC Form 13F institutional holdings.

The public interface intentionally stays small:

* ``load_active_etf_universe`` / ``refresh_active_etf_universe``
* ``load_hedge_fund_cache`` / ``refresh_hedge_fund_filings``
* ``classify_holdings``

ETF membership is discovered from Yahoo's ETF screener (AUM > USD 5B) and
validated from the fund's own description as actively managed.  The four hedge
fund targets are identified by their SEC CIKs, but every position and historical
change is parsed from live Form 13F filings; no holdings are embedded here.
"""
from __future__ import annotations

import json
import gzip
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from .storage import (
    append_etf_daily_snapshot,
    get_data_dir,
    load_etf_symbol_cache,
    taiwan_now,
)
from .sec_identity import SECIdentityMissingError, build_sec_user_agent


MIN_ACTIVE_ETF_AUM = 5_000_000_000.0
ACTIVE_UNIVERSE_MAX_RESULTS = 250

# Stable filer identifiers are configuration, not portfolio data.  Names and
# holdings are refreshed from SEC submissions and information-table XML.
HEDGE_FUND_TARGETS: tuple[dict[str, str], ...] = (
    {"id": "13F:1350694", "name": "Bridgewater Associates, LP", "cik": "1350694"},
    {"id": "13F:1423053", "name": "Citadel Advisors LLC", "cik": "1423053"},
    {"id": "13F:1273087", "name": "Millennium Management LLC", "cik": "1273087"},
    {"id": "13F:1791786", "name": "Elliott Investment Management L.P.", "cik": "1791786"},
)

_ACTIVE_RE = re.compile(r"\b(active|actively[- ]managed|active management)\b", re.I)
_PASSIVE_RE = re.compile(r"\b(passive|tracks? (?:the performance of )?an? index|index-tracking)\b", re.I)
_SEC_REQUEST_LOCK = threading.Lock()
_SEC_LAST_REQUEST_AT = 0.0


class SECConfigurationError(RuntimeError):
    """Raised when SEC programmatic-access identification is not configured."""


def _universe_path() -> Path:
    return get_data_dir() / "active_etf_universe.json"


def _institution_dir() -> Path:
    path = get_data_dir() / "institution_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _institution_path(entity_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", entity_id)
    return _institution_dir() / f"{safe}.json"


def classify_holdings(asset_classes: Optional[dict], holdings: Optional[list[dict]] = None) -> str:
    """Classify a fund from its actual portfolio composition, not its ticker.

    Yahoo's asset-class percentages describe the whole portfolio and therefore
    take precedence over the curated top-holdings list.
    """
    ac = asset_classes or {}
    stock = float(ac.get("stockPosition") or 0.0)
    bond = float(ac.get("bondPosition") or 0.0)
    cash = float(ac.get("cashPosition") or 0.0)
    other = float(ac.get("otherPosition") or 0.0)
    preferred = float(ac.get("preferredPosition") or 0.0)
    convertible = float(ac.get("convertiblePosition") or 0.0)

    if other >= 20.0:
        return "衍生性／另類"
    if stock >= 60.0:
        return "股票型"
    if bond >= 60.0:
        return "債券型"
    if cash >= 60.0:
        return "現金／短債"
    if stock >= 20.0 and bond >= 20.0:
        return "多重資產"

    dominant = max(
        (
            (stock, "股票型"),
            (bond, "債券型"),
            (cash, "現金／短債"),
            (preferred, "特別股"),
            (convertible, "可轉債"),
            (other, "衍生性／另類"),
        ),
        default=(0.0, "未分類"),
    )
    if dominant[0] > 0:
        return dominant[1]
    return "股票型（持股明細）" if holdings else "未分類"


def _is_explicitly_active(name: str, description: str) -> bool:
    text = f"{name} {description}".strip()
    return bool(_ACTIVE_RE.search(text)) and not bool(_PASSIVE_RE.search(text))


def _migrate_cached_universe() -> list[dict]:
    """One-time migration from already-downloaded ETF caches.

    This contains no ticker list.  It lets an existing installation remain
    useful during an offline launch; the next successful screener refresh
    replaces it with a fully discovered universe.
    """
    records: list[dict] = []
    cache_dir = get_data_dir() / "etf_cache"
    for path in cache_dir.glob("*.json"):
        if path.name.startswith("_") or path.stem.endswith(("_TW", "_TWO")):
            continue
        symbol = path.stem.replace("_", ".")
        cached = load_etf_symbol_cache(symbol)
        aum = cached.get("aum")
        if not isinstance(aum, (int, float)) or aum <= MIN_ACTIVE_ETF_AUM:
            continue
        records.append({
            "id": symbol,
            "symbol": symbol,
            "name": cached.get("name") or symbol,
            "aum": float(aum),
            "category": classify_holdings(
                cached.get("asset_classes"), cached.get("holdings")),
            "source_type": "etf",
            "source": "既有真實快取（等待動態 universe 更新）",
        })
    return sorted(records, key=lambda item: (-item["aum"], item["symbol"]))


def load_active_etf_universe() -> list[dict]:
    try:
        payload = json.loads(_universe_path().read_text())
        records = payload.get("records") if isinstance(payload, dict) else None
        if isinstance(records, list):
            return [
                item for item in records
                if isinstance(item, dict)
                and item.get("symbol")
                and float(item.get("aum") or 0.0) > MIN_ACTIVE_ETF_AUM
            ]
    except Exception:
        pass
    return _migrate_cached_universe()


def active_etf_symbols() -> list[str]:
    return [item["symbol"] for item in load_active_etf_universe()]


def ensure_active_etf_universe() -> dict:
    """Refresh discovery once per Taiwan calendar day; otherwise use the cache."""
    try:
        payload = json.loads(_universe_path().read_text())
        if str(
            payload.get("last_checked") or payload.get("last_refreshed") or ""
        ).startswith(
            taiwan_now().strftime("%Y-%m-%d")
        ):
            return {
                "records": load_active_etf_universe(),
                "status": payload.get("status", "ok"),
                "error": payload.get("error"),
            }
    except Exception:
        pass
    return refresh_active_etf_universe()


def refresh_active_etf_universe() -> dict:
    """Discover US active ETFs with AUM strictly above USD 5B.

    On a network/provider failure, returns the last known universe with an error
    status instead of replacing good cached membership with an empty list.
    """
    import yfinance as yf

    previous = load_active_etf_universe()
    try:
        query = yf.ETFQuery("and", [
            yf.ETFQuery("is-in", [
                "exchange", "PCX", "NMS", "NYQ", "NGM", "NCM", "ASE",
            ]),
            yf.ETFQuery("gt", ["fundnetassets", MIN_ACTIVE_ETF_AUM]),
        ])
        response = yf.screen(
            query,
            size=ACTIVE_UNIVERSE_MAX_RESULTS,
            sortField="fundnetassets",
            sortAsc=False,
        )
        quotes = response.get("quotes") or []

        def inspect(quote: dict) -> Optional[dict]:
            symbol = str(quote.get("symbol") or "").upper()
            raw_aum = quote.get("fundNetAssets")
            if raw_aum is None:
                raw_aum = quote.get("fundnetassets")
            if not symbol or not isinstance(raw_aum, (int, float)):
                return None
            aum = float(raw_aum)
            if aum <= MIN_ACTIVE_ETF_AUM:
                return None
            name = str(quote.get("longName") or quote.get("shortName") or symbol)
            info: dict = {}
            try:
                info = yf.Ticker(symbol).info or {}
            except Exception:
                pass
            description = str(
                info.get("longBusinessSummary")
                or info.get("fundStrategy")
                or quote.get("longBusinessSummary")
                or ""
            )
            if not _is_explicitly_active(name, description):
                return None
            cached = load_etf_symbol_cache(symbol)
            return {
                "id": symbol,
                "symbol": symbol,
                "name": name,
                "aum": aum,
                "category": classify_holdings(
                    cached.get("asset_classes"), cached.get("holdings")),
                "source_type": "etf",
                "source": "Yahoo ETF screener + fund description",
            }

        with ThreadPoolExecutor(max_workers=4) as executor:
            discovered = [item for item in executor.map(inspect, quotes) if item]
        discovered.sort(key=lambda item: (-item["aum"], item["symbol"]))
        if not discovered:
            raise RuntimeError("screener returned no explicitly active ETFs")

        payload = {
            "last_refreshed": taiwan_now().isoformat(),
            "last_checked": taiwan_now().isoformat(),
            "minimum_aum": MIN_ACTIVE_ETF_AUM,
            "status": "ok",
            "error": None,
            "records": discovered,
        }
        _universe_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return {"records": discovered, "status": "ok", "error": None}
    except Exception as exc:
        result = {
            "records": previous,
            "status": "stale" if previous else "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            _universe_path().write_text(json.dumps({
                "last_checked": taiwan_now().isoformat(),
                "minimum_aum": MIN_ACTIVE_ETF_AUM,
                **result,
            }, ensure_ascii=False, indent=2))
        except Exception:
            pass
        return result


def load_hedge_fund_cache(entity_id: str) -> dict:
    try:
        data = json.loads(_institution_path(entity_id).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def hedge_fund_records() -> list[dict]:
    records = []
    for target in HEDGE_FUND_TARGETS:
        cached = load_hedge_fund_cache(target["id"])
        records.append({
            "id": target["id"],
            "symbol": target["id"],
            "name": cached.get("name") or target["name"],
            "aum": cached.get("aum"),
            "category": "13F 對沖基金",
            "source_type": "13f",
            "cik": target["cik"],
            "report_date": cached.get("report_date"),
            "filing_date": cached.get("filing_date"),
            "data_status": cached.get("data_status", "waiting"),
        })
    return records


def _sec_headers(user: str | None = None) -> dict[str, str]:
    if user is not None:
        try:
            user_agent = build_sec_user_agent(user)
        except SECIdentityMissingError as exc:
            raise SECConfigurationError(str(exc)) from exc
    else:
        # Compatibility for headless/library callers. The TUI always supplies
        # its authenticated AssetTrack account and never uses this global path.
        allowed = os.getenv("ASSETTRACK_ALLOW_SEC_USER_AGENT", "").strip().lower()
        if allowed in {"1", "true", "yes"}:
            user_agent = os.getenv("SEC_USER_AGENT", "").strip()
        else:
            user_agent = ""
    if not user_agent:
        raise SECConfigurationError(
            "SEC_USER_AGENT 未設定；請提供名稱與聯絡信箱"
        )
    return {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }


def _get_url(
    url: str,
    *,
    timeout: int = 20,
    user: str | None = None,
) -> bytes:
    global _SEC_LAST_REQUEST_AT
    with _SEC_REQUEST_LOCK:
        wait = 0.13 - (time.monotonic() - _SEC_LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers=_sec_headers(user))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            encoding = str(response.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            payload = gzip.decompress(payload)
        elif encoding == "deflate":
            payload = zlib.decompress(payload)
        _SEC_LAST_REQUEST_AT = time.monotonic()
        return payload


def _get_json(
    url: str,
    *,
    get_bytes: Optional[Callable[[str], bytes]] = None,
) -> dict:
    return json.loads((get_bytes or _get_url)(url).decode("utf-8"))


def _recent_13f_filings(
    cik: str,
    maximum: int = 5,
    *,
    get_bytes: Optional[Callable[[str], bytes]] = None,
) -> list[dict]:
    data = _get_json(
        f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
        get_bytes=get_bytes,
    )
    recent = (data.get("filings") or {}).get("recent") or {}
    columns = {
        key: value for key, value in recent.items()
        if isinstance(value, list)
    }
    count = max((len(value) for value in columns.values()), default=0)
    filings = []
    for index in range(count):
        row = {
            key: values[index] if index < len(values) else None
            for key, values in columns.items()
        }
        if row.get("form") not in ("13F-HR", "13F-HR/A"):
            continue
        if not row.get("accessionNumber") or not row.get("reportDate"):
            continue
        filings.append(row)

    # Keep the latest filing for each report period so a restatement replaces,
    # rather than duplicates, the original report.
    by_period: dict[str, dict] = {}
    for filing in filings:
        period = filing["reportDate"]
        prior = by_period.get(period)
        if prior is None or (
            filing.get("filingDate") or "",
            filing.get("accessionNumber") or "",
        ) > (
            prior.get("filingDate") or "",
            prior.get("accessionNumber") or "",
        ):
            by_period[period] = filing
    return sorted(
        by_period.values(),
        key=lambda item: item["reportDate"],
        reverse=True,
    )[:maximum]


def _information_table_url(
    cik: str,
    filing: dict,
    *,
    get_bytes: Optional[Callable[[str], bytes]] = None,
) -> str:
    accession = filing["accessionNumber"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    index = _get_json(f"{base}/index.json", get_bytes=get_bytes)
    items = ((index.get("directory") or {}).get("item") or [])
    primary = filing.get("primaryDocument")
    candidates = [
        item for item in items
        if str(item.get("name") or "").lower().endswith(".xml")
        and item.get("name") != primary
    ]
    if not candidates:
        raise ValueError(f"13F information-table XML not found for {filing['accessionNumber']}")
    candidates.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
    return f"{base}/{candidates[0]['name']}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _descendant_text(element: ET.Element, name: str) -> Optional[str]:
    wanted = name.lower()
    for child in element.iter():
        if _local_name(child.tag).lower() == wanted and child.text:
            value = child.text.strip()
            if value:
                return value
    return None


def _number(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_13f_information_table(xml_bytes: bytes, report_date: str) -> list[dict]:
    """Parse exact security-level positions from a modern SEC 13F XML table."""
    root = ET.fromstring(xml_bytes)
    holdings: list[dict] = []
    for row in root.iter():
        if _local_name(row.tag).lower() != "infotable":
            continue
        issuer = _descendant_text(row, "nameOfIssuer") or "Unknown issuer"
        title = _descendant_text(row, "titleOfClass") or ""
        cusip = _descendant_text(row, "cusip") or ""
        figi = _descendant_text(row, "figi")
        put_call = (_descendant_text(row, "putCall") or "").upper()
        shares = _number(_descendant_text(row, "sshPrnamt"))
        value = _number(_descendant_text(row, "value"))
        amount_type = _descendant_text(row, "sshPrnamtType") or "SH"
        instrument_type = "option" if put_call in ("PUT", "CALL") else "stock"
        key_base = figi or cusip or f"{issuer}|{title}"
        position_id = f"{key_base}:{put_call or amount_type}"
        label = f"{issuer} {title}".strip()
        if put_call:
            label = f"{put_call} {label}"
        holdings.append({
            "symbol": position_id,
            "name": label,
            "issuer": issuer,
            "title_of_class": title,
            "cusip": cusip or None,
            "figi": figi,
            "instrument_type": instrument_type,
            "option_type": put_call or None,
            # Form 13F does not disclose contract strike or expiration.  Keep
            # these explicitly null so callers cannot mistake an option class
            # position for an exact contract/time bucket.
            "expiration": None,
            "strike": None,
            "shares": shares,
            "value": value,
            "amount_type": amount_type,
            "report_date": report_date,
        })
    return holdings


def fetch_hedge_fund_filings(
    target: dict[str, str],
    *,
    maximum_filings: int = 5,
    get_bytes: Optional[Callable[[str], bytes]] = None,
    user: str | None = None,
) -> dict:
    """Fetch and normalize recent quarterly 13F holdings for one manager."""
    if get_bytes is not None:
        requester = get_bytes
    elif user is not None:
        requester = lambda url: _get_url(url, user=user)
    else:
        requester = _get_url
    filings = _recent_13f_filings(
        target["cik"],
        maximum=maximum_filings,
        get_bytes=requester,
    )
    snapshots = []
    for filing in reversed(filings):
        url = _information_table_url(
            target["cik"],
            filing,
            get_bytes=requester,
        )
        holdings = parse_13f_information_table(
            requester(url), filing["reportDate"])
        total_value = sum(
            float(item["value"]) for item in holdings
            if isinstance(item.get("value"), (int, float))
        )
        if total_value > 0:
            for item in holdings:
                value = item.get("value")
                item["weight"] = (
                    round(float(value) / total_value * 100.0, 6)
                    if isinstance(value, (int, float)) else None
                )
        snapshots.append({
            "date": filing["reportDate"],
            "filing_date": filing.get("filingDate"),
            "accession": filing.get("accessionNumber"),
            "aum": total_value or None,
            "holdings": holdings,
        })
    if not snapshots:
        raise ValueError(f"No 13F-HR filings found for CIK {target['cik']}")
    latest = snapshots[-1]
    return {
        "id": target["id"],
        "name": target["name"],
        "cik": target["cik"],
        "source_type": "13f",
        "category": "13F 對沖基金",
        "aum": latest["aum"],
        "holdings": sorted(
            latest["holdings"],
            key=lambda item: float(item.get("value") or 0.0),
            reverse=True,
        ),
        "report_date": latest["date"],
        "filing_date": latest.get("filing_date"),
        "holdings_as_of_date": latest["date"],
        "snapshots": snapshots,
        "data_status": "ok",
        "status_message": "SEC 13F 季度申報；非即時交易資料",
        "last_checked": taiwan_now().isoformat(),
        "last_refreshed": taiwan_now().isoformat(),
    }


def refresh_hedge_fund_filings(user: str | None = None) -> dict:
    """Refresh all configured institutions, preserving prior good cache on error."""
    results: dict[str, dict] = {}
    for target in HEDGE_FUND_TARGETS:
        data: Optional[dict] = None
        errors: list[str] = []
        attempts = 0
        for attempt in range(1, 4):
            attempts = attempt
            try:
                data = fetch_hedge_fund_filings(target, user=user)
                break
            except Exception as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                if isinstance(exc, SECConfigurationError):
                    break
                if attempt < 3:
                    time.sleep(0.5 * attempt)

        if data is not None:
            try:
                for snapshot in data.pop("snapshots"):
                    append_etf_daily_snapshot(
                        target["id"],
                        snapshot["holdings"],
                        snapshot.get("aum"),
                        snapshot_date=snapshot["date"],
                        replace_existing=True,
                        # bug#00124: the report date says which quarter this
                        # describes; only the filing date says when it became
                        # public. A 13F row needs both for the user to judge
                        # how stale the signal actually is.
                        metadata={
                            "filing_date": snapshot.get("filing_date"),
                            "accession": snapshot.get("accession"),
                        },
                    )
                from .etf_trades import derive_trade_history_from_snapshots
                data["history"] = derive_trade_history_from_snapshots(
                    target["id"]
                )
                data["fetch_attempts"] = attempts
                data["last_fetch_error"] = errors[-1] if errors else None
                _institution_path(target["id"]).write_text(
                    json.dumps(data, ensure_ascii=False, indent=2))
                results[target["id"]] = data
            except Exception as exc:
                # Persistence/history failures are handled like transport
                # failures so a previous valid cache is never destroyed.
                errors.append(
                    f"persist: {type(exc).__name__}: {exc}"
                )
                data = None

        if data is None:
            cached = load_hedge_fund_cache(target["id"])
            had_positions = bool(cached.get("holdings"))
            cached.setdefault("id", target["id"])
            cached.setdefault("name", target["name"])
            cached.setdefault("cik", target["cik"])
            cached.setdefault("source_type", "13f")
            cached.setdefault("category", "13F 對沖基金")
            cached["data_status"] = "retryable" if had_positions else "error"
            if errors and (
                "SEC_USER_AGENT" in errors[-1]
                or "尚未建立 SEC 識別" in errors[-1]
            ):
                cached["status_message"] = (
                    "目前帳號尚未建立 SEC 識別資訊；"
                    "請依畫面引導設定名稱與聯絡信箱"
                )
            else:
                cached["status_message"] = (
                    "SEC 更新失敗；保留前次有效申報，稍後自動重試"
                    if had_positions
                    else "SEC 更新失敗；稍後自動重試"
                )
            cached["fetch_attempts"] = attempts
            cached["last_fetch_error"] = errors[-1] if errors else "unknown"
            cached["last_checked"] = taiwan_now().isoformat()
            _institution_path(target["id"]).write_text(
                json.dumps(cached, ensure_ascii=False, indent=2))
            results[target["id"]] = cached
    return results


def ensure_hedge_fund_filings(user: str | None = None) -> dict:
    """Check SEC at most once per Taiwan day; 13F positions update quarterly."""
    today = taiwan_now().strftime("%Y-%m-%d")
    cached = {
        target["id"]: load_hedge_fund_cache(target["id"])
        for target in HEDGE_FUND_TARGETS
    }
    if all(
        item
        and item.get("data_status") == "ok"
        and bool(item.get("holdings"))
        and str(
            item.get("last_checked") or item.get("last_refreshed") or ""
        ).startswith(today)
        for item in cached.values()
    ):
        return cached
    if user is None:
        return refresh_hedge_fund_filings()
    return refresh_hedge_fund_filings(user=user)
