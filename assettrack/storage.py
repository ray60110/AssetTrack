from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .models import PortfolioSnapshot, Position, CashPosition

# ─────────────────────────────────────────────────────────────────────────────
# Taiwan-time helper for the ETF/options daily-snapshot system
# ─────────────────────────────────────────────────────────────────────────────
# bug#00061 follow-up (user decision): all "is this cached/snapshotted for
# *today* yet" freshness checks and date-stamping for the ETF holdings and
# options-chain daily snapshots used to be based on datetime.utcnow(). Since
# this app is used from Taiwan (UTC+8), UTC midnight lands at 8am Taiwan time —
# so "today" as the system understood it was flipping mid-morning, not at
# local midnight, which made "did we already collect today's snapshot"
# needlessly confusing. Switched to Taiwan local time for this system only
# (not other unrelated timestamps elsewhere in the app, e.g. position
# last_updated or the earnings calendar, which weren't part of this request).

import zoneinfo as _zoneinfo

try:
    _TZ_TW_STORAGE = _zoneinfo.ZoneInfo("Asia/Taipei")
except Exception:
    _TZ_TW_STORAGE = None  # type: ignore[assignment]

try:
    _TZ_US_STORAGE = _zoneinfo.ZoneInfo("America/New_York")
except Exception:
    _TZ_US_STORAGE = None  # type: ignore[assignment]


def taiwan_now() -> datetime:
    """Current time in Taiwan (UTC+8), returned as a naive datetime so it drops
    in as a direct replacement for the old datetime.utcnow() call sites (date
    arithmetic / .strftime() / .isoformat() all keep working the same way —
    only the wall-clock value itself shifts to Taiwan time). Falls back to
    real UTC if the "Asia/Taipei" zone data isn't available in this
    environment, rather than crashing."""
    if _TZ_TW_STORAGE is not None:
        return datetime.now(_TZ_TW_STORAGE).replace(tzinfo=None)
    return datetime.utcnow()


DB_NAME = "assettrack.db"
POSITIONS_FILE = "positions.json"

# Keychain service name for user authentication — single source of truth
KEYCHAIN_SERVICE: str = "assettrack_user_auth"

# ── Local analysis-cache retention ───────────────────────────────────────────
# bug#00090 (user decision): every piece of downloaded data that feeds the
# offline analysis/backtests (per-ETF cache + ETF/options/sector daily-snapshot
# history) is retained locally for 365 days, so the walk-forward backtests have
# up to a year of real accumulated snapshots to validate against. This is a
# retention/pruning window only — it is unrelated to the in-memory refetch
# freshness TTLs (beta/risk-free/FRED/quote), which govern how often live data
# is re-pulled, not how long it is kept on disk.
ANALYSIS_CACHE_RETENTION_DAYS: int = 365


def get_data_dir() -> Path:
    """Return a user-writable data directory for this app."""
    # Simple: put everything next to the package or in ~/.local/share/assettrack
    # For maximum simplicity during early dev, use a local 'data/' folder.
    data_dir = Path.cwd() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_db_path(user: str = "default") -> Path:
    if user == "default":
        old_path = get_data_dir() / DB_NAME
        if old_path.exists():
            return old_path
        return get_data_dir() / "default_assettrack.db"
    return get_data_dir() / f"{user}_assettrack.db"


def get_positions_path(user: str = "default") -> Path:
    if user == "default":
        old_path = Path.cwd() / POSITIONS_FILE
        if old_path.exists():
            return old_path
        return get_data_dir() / "default_positions.json"
    return get_data_dir() / f"{user}_positions.json"


def get_preferences_path(user: str = "default") -> Path:
    """Return the per-user UI preference file path."""
    return get_data_dir() / f"{user}_preferences.json"


def load_user_preferences(user: str = "default") -> dict:
    """Load per-user UI preferences, returning safe defaults when unavailable."""
    path = get_preferences_path(user)
    defaults = {"event_timezone": "Asia/Taipei"}
    if not path.exists():
        return defaults
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict):
            defaults.update(raw)
    except Exception:
        pass
    return defaults


def save_user_preferences(preferences: dict, user: str = "default") -> None:
    """Persist per-user UI preferences as UTF-8 JSON."""
    path = get_preferences_path(user)
    path.write_text(json.dumps(preferences, ensure_ascii=False, indent=2))


def get_event_history_path(user: str = "default") -> Path:
    """Return the retained earnings-event history path for one user."""
    return get_data_dir() / f"{user}_event_history.json"


def load_event_history(user: str = "default") -> list[dict]:
    """Load retained event metadata; malformed entries are ignored by callers."""
    path = get_event_history_path(user)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def save_event_history(events: list[dict], user: str = "default") -> None:
    """Persist retained event metadata so completed earnings do not disappear."""
    get_event_history_path(user).write_text(
        json.dumps(events, ensure_ascii=False, indent=2)
    )


class Storage:
    def __init__(self, db_path: Optional[Path] = None, user: str = "default"):
        self.db_path = db_path or get_db_path(user)
        self._init_db()

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_value REAL NOT NULL,
                cash REAL,
                by_broker TEXT,
                notes TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER,
                position_json TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                broker TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                commission REAL,
                realized_pnl REAL,
                notes TEXT
            )
        """)
        con.commit()
        con.close()

    def save_snapshot(self, snap: PortfolioSnapshot) -> int:
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        by_broker_json = json.dumps(snap.by_broker)
        cur.execute(
            """
            INSERT INTO snapshots (timestamp, total_value, cash, by_broker, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                snap.timestamp.isoformat(),
                snap.total_value,
                snap.cash,
                by_broker_json,
                snap.notes,
            ),
        )
        snap_id = cur.lastrowid
        for pos in snap.positions:
            cur.execute(
                "INSERT INTO positions_history (snapshot_id, position_json) VALUES (?, ?)",
                (snap_id, json.dumps(pos.to_dict())),
            )
        con.commit()
        con.close()
        return snap_id

    def get_latest_snapshot(self) -> Optional[PortfolioSnapshot]:
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute(
            "SELECT id, timestamp, total_value, cash, by_broker, notes FROM snapshots ORDER BY timestamp DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            con.close()
            return None
        snap_id, ts, total, cash, by_broker_json, notes = row
        cur.execute("SELECT position_json FROM positions_history WHERE snapshot_id = ?", (snap_id,))
        pos_rows = cur.fetchall()
        con.close()

        positions = [Position.model_validate(json.loads(r[0])) for r in pos_rows]
        return PortfolioSnapshot(
            timestamp=datetime.fromisoformat(ts),
            total_value=total,
            cash=cash or 0.0,
            by_broker=json.loads(by_broker_json) if by_broker_json else {},
            positions=positions,
            notes=notes or "",
        )

    def get_snapshots_since(self, since: datetime) -> list[PortfolioSnapshot]:
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, timestamp, total_value, cash, by_broker, notes
            FROM snapshots
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (since.isoformat(),),
        )
        rows = cur.fetchall()
        results = []
        for row in rows:
            snap_id, ts, total, cash, by_broker_json, notes = row
            cur.execute("SELECT position_json FROM positions_history WHERE snapshot_id = ?", (snap_id,))
            pos_rows = cur.fetchall()
            positions = [Position.model_validate(json.loads(r[0])) for r in pos_rows]
            results.append(
                PortfolioSnapshot(
                    timestamp=datetime.fromisoformat(ts),
                    total_value=total,
                    cash=cash or 0.0,
                    by_broker=json.loads(by_broker_json) if by_broker_json else {},
                    positions=positions,
                    notes=notes or "",
                )
            )
        con.close()
        return results

    def save_transaction(self, timestamp: datetime, broker: str, symbol: str, action: str, quantity: float, price: float, currency: str, commission: Optional[float] = None, realized_pnl: Optional[float] = None, notes: Optional[str] = None) -> int:
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO transactions (timestamp, broker, symbol, action, quantity, price, currency, commission, realized_pnl, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp.isoformat(),
                broker,
                symbol,
                action,
                quantity,
                price,
                currency,
                commission,
                realized_pnl,
                notes,
            ),
        )
        tx_id = cur.lastrowid
        con.commit()
        con.close()
        return tx_id

    def get_all_transactions(self) -> list[dict]:
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, timestamp, broker, symbol, action, quantity, price, currency, commission, realized_pnl, notes
            FROM transactions
            ORDER BY timestamp DESC
            """
        )
        rows = cur.fetchall()
        con.close()
        
        results = []
        for r in rows:
            results.append({
                "id": r[0],
                "timestamp": datetime.fromisoformat(r[1]),
                "broker": r[2],
                "symbol": r[3],
                "action": r[4],
                "quantity": r[5],
                "price": r[6],
                "currency": r[7],
                "commission": r[8],
                "realized_pnl": r[9],
                "notes": r[10],
            })
        return results


def load_manual_positions(user: str = "default") -> tuple[list[Position], list[CashPosition]]:
    """Load positions and cash_positions from JSON file.
    Returns (positions, cash_positions). Backward compatible with files lacking cash_positions.
    """
    path = get_positions_path(user)
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "positions" in data:
            positions = [Position.model_validate(p) for p in data["positions"]]
            cash_raw = data.get("cash_positions", [])
            cash_positions = [CashPosition.model_validate(c) for c in cash_raw]
            return positions, cash_positions
        if isinstance(data, list):
            return [Position.model_validate(p) for p in data], []
    except Exception:
        return [], []
    return [], []


def save_manual_positions(
    positions: Iterable[Position],
    cash_positions: Iterable[CashPosition] | None = None,
    user: str = "default",
) -> None:
    """Save positions and cash_positions to JSON file."""
    path = get_positions_path(user)
    data = {
        "positions": [p.to_dict() for p in positions],
        "cash_positions": [c.to_dict() for c in (cash_positions or [])],
        "last_manual_update": datetime.utcnow().isoformat(),
    }
    path.write_text(json.dumps(data, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Per-ETF Cache System
# ─────────────────────────────────────────────────────────────────────────────
# Architecture:
#   data/etf_cache/ARKK.json          ← per-ETF: holdings + history + name
#   data/etf_cache/_aum_perf.json     ← global: AUM + performance for all
#
# Daily refresh: first access of the day triggers a fetch and writes new files.
# 2-week cleanup: files older than 14 days are deleted on screen startup.

from datetime import timedelta as _timedelta


def get_etf_cache_dir() -> Path:
    """Return and create the per-ETF cache subdirectory."""
    d = get_data_dir() / "etf_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _etf_cache_path(symbol: str) -> Path:
    safe = symbol.replace(".", "_").replace("/", "_")
    return get_etf_cache_dir() / f"{safe}.json"


def _aum_perf_cache_path() -> Path:
    return get_etf_cache_dir() / "_aum_perf.json"


def _is_cache_fresh(data: dict) -> bool:
    """Return True if data["last_refreshed"] is from today (Taiwan time)."""
    today = taiwan_now().strftime("%Y-%m-%d")
    return data.get("last_refreshed", "").startswith(today)


# ── Per-ETF (holdings + history) ─────────────────────────────────────────────

def load_etf_symbol_cache(symbol: str) -> dict:
    """Load the per-ETF cache. Returns {} if missing or corrupt."""
    try:
        return json.loads(_etf_cache_path(symbol).read_text())
    except Exception:
        return {}


def save_etf_symbol_cache(symbol: str, data: dict) -> None:
    """Persist per-ETF cache and stamp last_refreshed (Taiwan time)."""
    data["last_refreshed"] = taiwan_now().isoformat()
    try:
        _etf_cache_path(symbol).write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )
    except Exception:
        pass


def etf_symbol_cache_fresh(symbol: str) -> bool:
    """Return True if today's per-ETF cache exists, is dated today, and actually
    holds a fetched price.

    bug#00058: this used to only check that the "holdings"/"aum" *keys* were
    present. A failed performance fetch still writes those keys (with price=None),
    so a single bad refresh got treated as "fresh" for the rest of the day and
    YTD/1Y stayed blank until the cache aged out tomorrow. Requiring a real price
    means a failed attempt is retried on the next background fetch instead of
    being locked in for the whole day.
    """
    cached = load_etf_symbol_cache(symbol)
    if not cached or "holdings" not in cached or "aum" not in cached:
        return False
    if cached.get("price") is None:
        return False
    return _is_cache_fresh(cached)


# ── Global AUM + performance cache ────────────────────────────────────────────

def load_aum_perf_cache() -> dict:
    """Load the global AUM/performance cache. Returns {} if missing."""
    try:
        return json.loads(_aum_perf_cache_path().read_text())
    except Exception:
        return {}


def save_aum_perf_cache(data: dict) -> None:
    """Persist the global AUM/performance cache (Taiwan time)."""
    data["last_refreshed"] = taiwan_now().isoformat()
    try:
        _aum_perf_cache_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )
    except Exception:
        pass


def aum_perf_cache_fresh() -> bool:
    """Return True if today's AUM/performance cache exists."""
    return _is_cache_fresh(load_aum_perf_cache())


# ── Per-ETF cache retention cleanup ──────────────────────────────────────────

def cleanup_old_etf_caches(max_age_days: int = ANALYSIS_CACHE_RETENTION_DAYS) -> None:
    """Delete per-ETF cache files older than max_age_days (default 365 days)."""
    cutoff = taiwan_now() - _timedelta(days=max_age_days)
    for f in get_etf_cache_dir().glob("*.json"):
        try:
            ts_str = json.loads(f.read_text()).get("last_refreshed", "")
            if ts_str and datetime.fromisoformat(ts_str) < cutoff:
                f.unlink()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Per-ETF daily snapshot history (進階分析 / advanced trend analysis)
# ─────────────────────────────────────────────────────────────────────────────
# bug#00060: 60-day buy/sell trend analysis needs *real* day-over-day holdings
# history. The per-ETF cache above only ever holds a single overwritten snapshot
# ("today"), so it can't answer "did this holding's weight go up over the last
# N days". This module appends one real dated line per symbol per day whenever a
# fresh yfinance holdings fetch succeeds — never backfilled, never fabricated.
# Stored under etf_cache/history/ (NOT matched by cleanup_old_etf_caches()'s
# top-level *.json glob above) so the per-ETF cache cleanup can't quietly
# delete the trend history out from under the analysis window.

def get_etf_history_dir() -> Path:
    """Return and create the daily-snapshot-history subdirectory."""
    d = get_etf_cache_dir() / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _etf_history_path(symbol: str) -> Path:
    safe = symbol.replace(".", "_").replace("/", "_")
    return get_etf_history_dir() / f"{safe}.jsonl"


def append_etf_daily_snapshot(
    symbol: str,
    holdings: list[dict],
    aum: float | None,
    snapshot_date: Optional[str] = None,
    asset_classes: Optional[dict] = None,
) -> None:
    """Append one real dated holdings snapshot for `symbol`, if one for that date
    doesn't already exist (idempotent — safe to call every time the screen refreshes,
    not just once a day). Stores the fields needed for trend math: symbol, weight,
    price, AUM, and asset_classes breakdown (bug#00103).
    """
    date_str = snapshot_date or taiwan_now().strftime("%Y-%m-%d")
    path = _etf_history_path(symbol)

    try:
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get("date") == date_str:
                        return  # already have today's real snapshot
                except Exception:
                    continue

        slim_holdings = [
            {"symbol": h.get("symbol"), "weight": h.get("weight"), "price": h.get("price")}
            for h in (holdings or [])
            if h.get("symbol") is not None and h.get("weight") is not None
        ]
        if not slim_holdings and not asset_classes:
            return  # nothing real to record

        payload = {"date": date_str, "aum": aum, "holdings": slim_holdings}
        if asset_classes:
            payload["asset_classes"] = asset_classes

        line = json.dumps(payload, ensure_ascii=False)
        with path.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_etf_daily_snapshots(symbol: str, since_date: Optional[str] = None) -> list[dict]:
    """Load this symbol's real daily snapshots, sorted ascending by date.
    `since_date` (YYYY-MM-DD) filters out anything older, when given."""
    path = _etf_history_path(symbol)
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if since_date and rec.get("date", "") < since_date:
                continue
            out.append(rec)
    except Exception:
        return []
    out.sort(key=lambda r: r.get("date", ""))
    return out


def prune_etf_history(symbol: str, max_age_days: int = ANALYSIS_CACHE_RETENTION_DAYS) -> None:
    """Drop snapshot lines older than max_age_days (default 365-day retention so a
    full year of real snapshots stays available for the walk-forward backtest)."""
    cutoff = (taiwan_now() - _timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    path = _etf_history_path(symbol)
    if not path.exists():
        return
    try:
        kept = [
            line for line in path.read_text().splitlines()
            if line.strip() and json.loads(line).get("date", "") >= cutoff
        ]
        path.write_text("\n".join(kept) + ("\n" if kept else ""))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Per-underlying daily options-chain snapshot history (期權觀察清單)
# ─────────────────────────────────────────────────────────────────────────────
# bug#00061: 選擇權觀察清單需要偵測「建倉」（未平倉量隨時間變化）與「價格大幅波動」，
# 這兩者都必須靠比對「真實逐日快照」才能算出來 —— yfinance 的 option_chain() 跟
# funds_data 一樣，只回傳單一時間點的即時快照，沒有歷史序列。架構完全比照
# etf_cache/history/：只追加真實資料、同日去重、獨立資料夾不受其他清理邏輯影響。

def get_options_history_dir() -> Path:
    """Return and create the options daily-snapshot-history subdirectory."""
    d = get_data_dir() / "options_cache" / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _options_watchlist_path(user: str) -> Path:
    safe = (user or "default").replace("/", "_")
    return get_data_dir() / f"{safe}_options_watchlist.json"


def load_options_watchlist(user: str) -> "list[str]":
    """Load the user's *extra* options-watchlist tickers (bug#00066).

    These are tickers the user explicitly added beyond their held-position
    underlyings; the watchlist screen shows position underlyings ∪ these extras.
    Returns an uppercased, de-duplicated list; [] if none saved yet.
    """
    p = _options_watchlist_path(user)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        tickers = data.get("tickers", []) if isinstance(data, dict) else []
        return sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    except Exception:
        return []


def save_options_watchlist(user: str, tickers: "Iterable[str]") -> None:
    """Persist the user's extra options-watchlist tickers (uppercased, de-duped)."""
    p = _options_watchlist_path(user)
    try:
        uniq = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
        p.write_text(json.dumps({"tickers": uniq}, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _options_history_path(underlying: str) -> Path:
    safe = underlying.replace(".", "_").replace("/", "_")
    return get_options_history_dir() / f"{safe}.jsonl"


def append_options_daily_snapshot(
    underlying: str,
    contracts: list[dict],
    spot_price: float | None,
    snapshot_date: Optional[str] = None,
    earnings_date: Optional[str] = None,
) -> None:
    """Append one real dated options-chain snapshot for `underlying`, if one for
    that date doesn't already exist (idempotent). `contracts` should be the slim,
    real fields needed for flow analysis: contractSymbol, type (call/put), strike,
    expiry, lastPrice, volume, openInterest — as returned by
    quotes.fetch_options_snapshot(). Nothing here is estimated or backfilled.

    `earnings_date` (YYYY-MM-DD, bug#00068) is the next scheduled earnings date as
    known ON this snapshot's day; recording it daily means that even after earnings
    passes, the divergence analysis can still tell an option move straddled an
    earnings event (yfinance only reliably reports the *upcoming* date).
    """
    date_str = snapshot_date or taiwan_now().strftime("%Y-%m-%d")
    path = _options_history_path(underlying)

    try:
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get("date") == date_str:
                        return  # already have today's real snapshot
                except Exception:
                    continue

        if not contracts:
            return  # nothing real to record

        record = {"date": date_str, "spot_price": spot_price, "contracts": contracts}
        if earnings_date:
            record["earnings_date"] = earnings_date
        line = json.dumps(record, ensure_ascii=False)
        with path.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_options_daily_snapshots(underlying: str, since_date: Optional[str] = None) -> list[dict]:
    """Load this underlying's real daily options-chain snapshots, sorted ascending
    by date. `since_date` (YYYY-MM-DD) filters out anything older, when given."""
    path = _options_history_path(underlying)
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if since_date and rec.get("date", "") < since_date:
                continue
            out.append(rec)
    except Exception:
        return []
    out.sort(key=lambda r: r.get("date", ""))
    return out


def options_symbol_fresh(underlying: str) -> bool:
    """Return True if today's real options snapshot for this underlying already
    exists (checks the last recorded date in its history log — mirrors
    etf_symbol_cache_fresh's role but doesn't need a separate "current" cache
    file since the raw contract list itself IS the display data here)."""
    snaps = load_options_daily_snapshots(underlying)
    if not snaps:
        return False
    today = taiwan_now().strftime("%Y-%m-%d")
    return snaps[-1].get("date") == today


def prune_options_history(underlying: str, max_age_days: int = ANALYSIS_CACHE_RETENTION_DAYS) -> None:
    """Drop snapshot lines older than max_age_days (same 365-day retention as
    prune_etf_history — keeps a full year of real option-chain snapshots)."""
    cutoff = (taiwan_now() - _timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    path = _options_history_path(underlying)
    if not path.exists():
        return
    try:
        kept = [
            line for line in path.read_text().splitlines()
            if line.strip() and json.loads(line).get("date", "") >= cutoff
        ]
        path.write_text("\n".join(kept) + ("\n" if kept else ""))
    except Exception:
        pass


def remove_options_daily_snapshot(underlying: str, date_str: str) -> None:
    """bug#00116：只移除某一天的快照列，保留其他所有歷史。供「重抓今日快照」使用——
    刪掉今天那一筆後，options_symbol_fresh() 轉為 False，背景抓取即會重新抓當天最新
    資料再 append 回去（等於刷新今天，且完全不動到過去累積的歷史）。"""
    path = _options_history_path(underlying)
    if not path.exists():
        return
    try:
        kept = [
            line for line in path.read_text().splitlines()
            if line.strip() and json.loads(line).get("date", "") != date_str
        ]
        path.write_text("\n".join(kept) + ("\n" if kept else ""))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Sector / thematic-group daily snapshot history (類股板塊分析 sector_analysis)
# ─────────────────────────────────────────────────────────────────────────────
# 類股板塊分析需要偵測某個板塊族群是否「普遍」上漲或下跌（市場對整族股票的共同買
# 進/賣出）。跟 ETF / 期權快照相同：yfinance 只回傳當下即時報價，沒有歷史廣度序
# 列，所以必須從系統開始運行後逐日真實累積每日快照，才能算出「每日累計」的廣度趨
# 勢。架構完全比照 etf_cache/history/：只追加真實資料、同日去重、獨立資料夾。

# 內建預設板塊類股群組。全球/美股概念股為主（使用者可於畫面自建群組並增刪成分）。
# 成分股為 curated 常見代表名單，非窮舉；使用者可依需求自行調整。
DEFAULT_SECTOR_GROUPS: dict[str, list[str]] = {
    "CPU 處理器": ["INTC", "AMD", "NVDA", "ARM", "QCOM"],
    "功率半導體": ["ON", "MPWR", "IFX.DE", "STM", "NXPI", "WOLF"],
    "光通訊": ["COHR", "LITE", "AAOI", "POET", "CIEN", "INFN"],
    "存儲記憶體 (HBM/DRAM)": ["MU", "005930.KS", "000660.KS", "WDC", "STX"],
    "SaaS 雲端軟體": ["CRM", "NOW", "SNOW", "DDOG", "TEAM", "WDAY", "HUBS", "NET"],
    "科技七巨頭": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"],
}


def get_sector_config_dir() -> Path:
    """Return and create the sector_analysis config/cache root."""
    d = get_data_dir() / "sector_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_sector_history_dir() -> Path:
    """Return and create the sector daily-snapshot-history subdirectory."""
    d = get_sector_config_dir() / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sector_groups_path(user: str) -> Path:
    safe = (user or "default").replace("/", "_")
    return get_sector_config_dir() / f"{safe}_sector_groups.json"


def load_sector_groups(user: str) -> dict[str, list[str]]:
    """Load this user's sector groups. First run (no saved file) seeds the built-in
    DEFAULT_SECTOR_GROUPS into the user's file as a *starting point*; after that the
    saved file is authoritative — the user may freely rename, add, or delete groups
    (including the seeded defaults) and those edits fully persist (a deleted default
    does not reappear). Returns {group_name: [symbol, ...]} uppercased/de-duped.

    An empty saved file ({}) is a valid authoritative state (user deleted all groups)
    and is returned as-is; defaults are only re-seeded when no file exists at all."""
    p = _sector_groups_path(user)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            saved = data.get("groups", {}) if isinstance(data, dict) else {}
            return {
                str(name): [str(s).strip().upper() for s in members if str(s).strip()]
                for name, members in saved.items()
                if isinstance(members, list)
            }
        except Exception:
            # Corrupt file: return in-memory defaults but don't overwrite the file
            # (it may be recoverable), and don't fabricate a save.
            return {name: list(members) for name, members in DEFAULT_SECTOR_GROUPS.items()}

    # First run — seed defaults into the user's file so subsequent edits/deletes stick.
    groups = {name: list(members) for name, members in DEFAULT_SECTOR_GROUPS.items()}
    save_sector_groups(user, groups)
    return groups


def save_sector_groups(user: str, groups: dict[str, list[str]]) -> None:
    """Persist the full set of this user's sector groups (used by the extension
    phase's create/edit feature)."""
    p = _sector_groups_path(user)
    try:
        clean = {
            str(name): [str(s).strip().upper() for s in (members or []) if str(s).strip()]
            for name, members in groups.items()
        }
        p.write_text(json.dumps({"groups": clean}, ensure_ascii=False, indent=2))
    except Exception:
        pass


# ── Market session (US regular hours) for the sector refresh cadence ──────────
# Sector concept stocks are predominantly US-listed, so the US regular session
# (09:30–16:00 ET, Mon–Fri) governs the sector module's refresh cadence.

from datetime import timezone as _timezone


def _us_now() -> datetime:
    """Current US Eastern time (aware); falls back to UTC if zone data is missing."""
    if _TZ_US_STORAGE is not None:
        return datetime.now(_TZ_US_STORAGE)
    return datetime.now(_timezone.utc)


def us_market_open_now() -> bool:
    """True during US regular trading hours (09:30–16:00 ET on a weekday)."""
    now = _us_now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 570 <= t <= 960


def last_us_close_dt() -> datetime:
    """The most recent US regular-session close (16:00 ET on a weekday) that has
    already passed, returned as a Taiwan-naive datetime so it compares directly
    against cache `last_refreshed` stamps (which use taiwan_now()). Used to decide,
    while the market is closed, whether our cached data already reflects that close
    (未開盤→上一個收盤；已收盤→本日收盤)."""
    now_us = _us_now()
    close = now_us.replace(hour=16, minute=0, second=0, microsecond=0)
    if close > now_us:
        close -= _timedelta(days=1)
    while close.weekday() >= 5:  # Sat/Sun → step back to Friday
        close -= _timedelta(days=1)
    if _TZ_TW_STORAGE is not None:
        return close.astimezone(_TZ_TW_STORAGE).replace(tzinfo=None)
    return close.astimezone(_timezone.utc).replace(tzinfo=None)


# ── Sector live-summaries cache (avoid re-fetching on every screen entry) ─────

def _sector_summaries_cache_path(user: str) -> Path:
    safe = (user or "default").replace("/", "_")
    return get_sector_config_dir() / f"{safe}_summaries.json"


def load_sector_summaries_cache(user: str) -> dict:
    """Load the last-fetched sector summaries cache. Returns {} if missing/corrupt.
    Shape: {"last_refreshed": iso, "summaries": {group_name: summarize_group(...)}}"""
    try:
        return json.loads(_sector_summaries_cache_path(user).read_text())
    except Exception:
        return {}


def save_sector_summaries_cache(user: str, summaries: dict) -> None:
    """Persist the full sector summaries so re-entering the screen shows data
    instantly (no reload). Stamped with taiwan_now(). Only called with real fetched
    data (caller guards against saving an all-empty/failed fetch)."""
    payload = {"last_refreshed": taiwan_now().isoformat(), "summaries": summaries}
    try:
        _sector_summaries_cache_path(user).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
    except Exception:
        pass


def sector_cache_needs_refresh(user: str) -> bool:
    """Decide whether the sector summaries cache should be refetched now.

      • US market OPEN  → stale once the cache is ≥ 60s old (開盤中每 60 秒更新最新價)。
      • US market CLOSED → stale only if the cache predates the most recent close
        (未開盤沿用上一個收盤資料、已收盤抓取本日收盤各一次；期間都用快取不重抓)。

    A missing/unstamped/corrupt cache always needs a refresh. The screen keeps
    showing the previous cached data until a fresh fetch actually completes."""
    cached = load_sector_summaries_cache(user)
    ts = cached.get("last_refreshed")
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(ts)
    except Exception:
        return True
    if us_market_open_now():
        return (taiwan_now() - last).total_seconds() >= 60
    return last < last_us_close_dt()


def _sector_history_path(group: str) -> Path:
    safe = group.replace(".", "_").replace("/", "_").replace(" ", "_")
    return get_sector_history_dir() / f"{safe}.jsonl"


# Full per-member fields archived each day (使用者要求：當日所有板塊、板塊個股的資訊
# 都需記錄在系統空間，以便日後進一步分析，保留 180 日)。breadth 引擎只需 day_pct /
# marketcap，其餘欄位純為日後分析保存。
_SECTOR_MEMBER_FIELDS = (
    "symbol", "price", "prev_close", "day_pct", "week_pct", "month_pct",
    "volume", "turnover", "marketcap", "weight",
)


def append_sector_daily_snapshot(
    group: str,
    summary: dict,
    snapshot_date: Optional[str] = None,
) -> None:
    """Upsert today's dated snapshot for `group` from a summarize_group() result.

    Unlike the old first-write-wins behaviour, the same-date line is *replaced* with
    the latest data on every refresh, so during a live session the day's record keeps
    tracking the newest values and, once the market has closed, naturally holds that
    day's closing figures (使用者要求「已收盤依本日收盤最後資訊」).

    The full per-member fields (_SECTOR_MEMBER_FIELDS) plus the group-level aggregates
    are archived for later analysis. None values are kept, never fabricated. Nothing
    is written when no member carries a real day_pct (avoids recording an empty day)."""
    date_str = snapshot_date or taiwan_now().strftime("%Y-%m-%d")
    path = _sector_history_path(group)

    members_full = [
        {k: m.get(k) for k in _SECTOR_MEMBER_FIELDS}
        for m in (summary.get("members") or [])
        if m.get("symbol") is not None
    ]
    if not any(m.get("day_pct") is not None for m in members_full):
        return  # nothing real to record today yet

    rec = {
        "date": date_str,
        "as_of": taiwan_now().isoformat(),
        "total_marketcap": summary.get("total_marketcap"),
        "capw_day": summary.get("capw_day"),
        "capw_week": summary.get("capw_week"),
        "capw_month": summary.get("capw_month"),
        "n_up": summary.get("n_up"),
        "n_down": summary.get("n_down"),
        "n_rated": summary.get("n_rated"),
        "breadth": summary.get("breadth"),
        "members": members_full,
    }

    try:
        kept: list[str] = []
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get("date") == date_str:
                        continue  # drop the old same-date line (upsert)
                except Exception:
                    continue
                kept.append(line)
        kept.append(json.dumps(rec, ensure_ascii=False))
        path.write_text("\n".join(kept) + "\n")
    except Exception:
        pass


def load_sector_daily_snapshots(group: str, since_date: Optional[str] = None) -> list[dict]:
    """Load this group's real daily snapshots, sorted ascending by date."""
    path = _sector_history_path(group)
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if since_date and rec.get("date", "") < since_date:
                continue
            out.append(rec)
    except Exception:
        return []
    out.sort(key=lambda r: r.get("date", ""))
    return out


def sector_group_fresh(group: str) -> bool:
    """True if today's (Taiwan time) real snapshot for this group already exists."""
    today = taiwan_now().strftime("%Y-%m-%d")
    path = _sector_history_path(group)
    if not path.exists():
        return False
    try:
        for line in path.read_text().splitlines():
            if line.strip() and json.loads(line).get("date") == today:
                return True
    except Exception:
        return False
    return False


def prune_sector_history(group: str, max_age_days: int = ANALYSIS_CACHE_RETENTION_DAYS) -> None:
    """Drop snapshot lines older than max_age_days. Retention is 365 days per the
    user's requirement (bug#00090) so the archived sector/constituent records
    stay available for the walk-forward backtest well beyond the short breadth window."""
    cutoff = (taiwan_now() - _timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    path = _sector_history_path(group)
    if not path.exists():
        return
    try:
        kept = [
            line for line in path.read_text().splitlines()
            if line.strip() and json.loads(line).get("date", "") >= cutoff
        ]
        path.write_text("\n".join(kept) + ("\n" if kept else ""))
    except Exception:
        pass


# ── Legacy monolithic file (kept for backward compat, no longer written) ──────

def load_active_etf_data() -> dict:
    """Legacy loader — returns empty dict; per-ETF cache is used instead."""
    return {}


def save_active_etf_holdings(data: dict) -> None:
    """Legacy stub — no-op; per-ETF cache functions are used instead."""
    pass


def etf_cache_needs_refresh(data: dict) -> bool:
    """Legacy check — kept for import compatibility. Always returns True."""
    return True
