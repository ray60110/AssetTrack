from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .models import PortfolioSnapshot, Position, CashPosition


DB_NAME = "assettrack.db"
POSITIONS_FILE = "positions.json"

# Keychain service name for user authentication — single source of truth
KEYCHAIN_SERVICE: str = "assettrack_user_auth"


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
    """Return True if data["last_refreshed"] is from today (UTC)."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return data.get("last_refreshed", "").startswith(today)


# ── Per-ETF (holdings + history) ─────────────────────────────────────────────

def load_etf_symbol_cache(symbol: str) -> dict:
    """Load the per-ETF cache. Returns {} if missing or corrupt."""
    try:
        return json.loads(_etf_cache_path(symbol).read_text())
    except Exception:
        return {}


def save_etf_symbol_cache(symbol: str, data: dict) -> None:
    """Persist per-ETF cache and stamp last_refreshed."""
    data["last_refreshed"] = datetime.utcnow().isoformat()
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
    """Persist the global AUM/performance cache."""
    data["last_refreshed"] = datetime.utcnow().isoformat()
    try:
        _aum_perf_cache_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )
    except Exception:
        pass


def aum_perf_cache_fresh() -> bool:
    """Return True if today's AUM/performance cache exists."""
    return _is_cache_fresh(load_aum_perf_cache())


# ── 2-week cleanup ────────────────────────────────────────────────────────────

def cleanup_old_etf_caches(max_age_days: int = 14) -> None:
    """Delete per-ETF cache files older than max_age_days."""
    cutoff = datetime.utcnow() - _timedelta(days=max_age_days)
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
# top-level *.json glob above) so the 14-day per-ETF cache cleanup can't quietly
# delete the trend history out from under a 60-day analysis window.

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
) -> None:
    """Append one real dated holdings snapshot for `symbol`, if one for that date
    doesn't already exist (idempotent — safe to call every time the screen refreshes,
    not just once a day). Stores the fields needed for trend math: symbol, weight,
    the holding's real market price at fetch time (bug#00061 follow-up — lets
    analysis.py derive a real share-count delta instead of a fixed-average-price
    guess; None when a real price wasn't available for that holding), and the
    fund's AUM at that date.
    """
    date_str = snapshot_date or datetime.utcnow().strftime("%Y-%m-%d")
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
        if not slim_holdings:
            return  # nothing real to record

        line = json.dumps({"date": date_str, "aum": aum, "holdings": slim_holdings}, ensure_ascii=False)
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


def prune_etf_history(symbol: str, max_age_days: int = 65) -> None:
    """Drop snapshot lines older than max_age_days (bounds file growth while
    still comfortably covering a 60-day trend window)."""
    cutoff = (datetime.utcnow() - _timedelta(days=max_age_days)).strftime("%Y-%m-%d")
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


def _options_history_path(underlying: str) -> Path:
    safe = underlying.replace(".", "_").replace("/", "_")
    return get_options_history_dir() / f"{safe}.jsonl"


def append_options_daily_snapshot(
    underlying: str,
    contracts: list[dict],
    spot_price: float | None,
    snapshot_date: Optional[str] = None,
) -> None:
    """Append one real dated options-chain snapshot for `underlying`, if one for
    that date doesn't already exist (idempotent). `contracts` should be the slim,
    real fields needed for flow analysis: contractSymbol, type (call/put), strike,
    expiry, lastPrice, volume, openInterest — as returned by
    quotes.fetch_options_snapshot(). Nothing here is estimated or backfilled.
    """
    date_str = snapshot_date or datetime.utcnow().strftime("%Y-%m-%d")
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

        line = json.dumps(
            {"date": date_str, "spot_price": spot_price, "contracts": contracts},
            ensure_ascii=False,
        )
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
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return snaps[-1].get("date") == today


def prune_options_history(underlying: str, max_age_days: int = 65) -> None:
    """Drop snapshot lines older than max_age_days (same buffer policy as
    prune_etf_history — bounds file growth while covering the analysis window)."""
    cutoff = (datetime.utcnow() - _timedelta(days=max_age_days)).strftime("%Y-%m-%d")
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
