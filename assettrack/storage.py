from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .auth import protected_sqlite, read_protected_text, write_protected_text
from .market_sessions import NYSESessionCalendar
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
# User decision D-04: every piece of downloaded point-in-time research data
# that feeds offline analysis/backtests (ETF cache + ETF/options/sector daily
# snapshots) and exact-session adjusted-close truth is retained for two years.
# This is a pruning window only; it does not affect in-memory freshness TTLs.
ANALYSIS_CACHE_RETENTION_DAYS: int = 730
BENCHMARK_TRUTH_RETENTION_DAYS: int = 730


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
        raw = json.loads(read_protected_text(path, user=user))
        if isinstance(raw, dict):
            defaults.update(raw)
    except Exception:
        pass
    return defaults


def save_user_preferences(preferences: dict, user: str = "default") -> None:
    """Persist per-user UI preferences as UTF-8 JSON."""
    path = get_preferences_path(user)
    write_protected_text(
        path, json.dumps(preferences, ensure_ascii=False, indent=2), user=user
    )


def get_event_history_path(user: str = "default") -> Path:
    """Return the retained earnings-event history path for one user."""
    return get_data_dir() / f"{user}_event_history.json"


def load_event_history(user: str = "default") -> list[dict]:
    """Load retained event metadata; malformed entries are ignored by callers."""
    path = get_event_history_path(user)
    if not path.exists():
        return []
    try:
        raw = json.loads(read_protected_text(path, user=user))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def save_event_history(events: list[dict], user: str = "default") -> None:
    """Persist retained event metadata so completed earnings do not disappear."""
    write_protected_text(
        get_event_history_path(user),
        json.dumps(events, ensure_ascii=False, indent=2),
        user=user,
    )


class Storage:
    def __init__(self, db_path: Optional[Path] = None, user: str = "default"):
        self.user = user
        self.db_path = db_path or get_db_path(user)
        self._init_db()

    def _init_db(self):
        with protected_sqlite(self.db_path, user=self.user) as con:
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

    def save_snapshot(self, snap: PortfolioSnapshot) -> int:
        with protected_sqlite(self.db_path, user=self.user) as con:
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
        return snap_id

    def get_latest_snapshot(self) -> Optional[PortfolioSnapshot]:
        with protected_sqlite(self.db_path, user=self.user) as con:
            cur = con.cursor()
            cur.execute(
                "SELECT id, timestamp, total_value, cash, by_broker, notes FROM snapshots ORDER BY timestamp DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            snap_id, ts, total, cash, by_broker_json, notes = row
            cur.execute("SELECT position_json FROM positions_history WHERE snapshot_id = ?", (snap_id,))
            pos_rows = cur.fetchall()

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
        with protected_sqlite(self.db_path, user=self.user) as con:
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
        return results

    def save_transaction(self, timestamp: datetime, broker: str, symbol: str, action: str, quantity: float, price: float, currency: str, commission: Optional[float] = None, realized_pnl: Optional[float] = None, notes: Optional[str] = None) -> int:
        with protected_sqlite(self.db_path, user=self.user) as con:
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
        return tx_id

    def get_all_transactions(self) -> list[dict]:
        with protected_sqlite(self.db_path, user=self.user) as con:
            cur = con.cursor()
            cur.execute(
                """
                SELECT id, timestamp, broker, symbol, action, quantity, price, currency, commission, realized_pnl, notes
                FROM transactions
                ORDER BY timestamp DESC
                """
            )
            rows = cur.fetchall()

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
        data = json.loads(read_protected_text(path, user=user))
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
    write_protected_text(path, json.dumps(data, indent=2), user=user)


def seal_user_files(user: str) -> None:
    """Rewrite personal files under the unlocked vault so legacy plaintext is sealed."""
    from .auth import BINARY_PREFIX, current_vault_user, is_encrypted_text
    from .performance import PortfolioPerformanceTracker

    if current_vault_user() != user:
        return
    positions, cash_positions = load_manual_positions(user)
    positions_path = get_positions_path(user)
    if positions_path.exists() and not is_encrypted_text(positions_path.read_text(encoding="utf-8")):
        save_manual_positions(positions, cash_positions, user=user)

    overlay_path = _quote_overlay_path(user)
    if overlay_path.exists() and not is_encrypted_text(overlay_path.read_text(encoding="utf-8")):
        save_quote_overlay(user, positions)

    history_path = get_event_history_path(user)
    if history_path.exists() and not is_encrypted_text(history_path.read_text(encoding="utf-8")):
        save_event_history(load_event_history(user), user)

    prefs_path = get_preferences_path(user)
    if prefs_path.exists() and not is_encrypted_text(prefs_path.read_text(encoding="utf-8")):
        save_user_preferences(load_user_preferences(user), user)

    db_path = get_db_path(user)
    if db_path.exists() and not db_path.read_bytes().startswith(BINARY_PREFIX):
        Storage(db_path=db_path, user=user)

    tracker = PortfolioPerformanceTracker(user=user, data_dir=get_data_dir())
    if tracker.path.exists() and not is_encrypted_text(tracker.path.read_text(encoding="utf-8")):
        tracker._write(tracker._read())


def _quote_overlay_path(user: str) -> Path:
    safe = (user or "default").replace("/", "_").replace("\\", "_")
    return get_data_dir() / f"{safe}_quote_overlay.json"


def _quote_overlay_key(position: Position) -> str:
    return "\x1f".join(
        (
            position.broker.lower(),
            (position.account or "").lower(),
            position.symbol.upper(),
            position.instrument_type,
        )
    )


def load_quote_overlay(user: str) -> dict:
    """Load last live quotes for first paint. Returns {} if missing/corrupt."""
    try:
        data = json.loads(read_protected_text(_quote_overlay_path(user), user=user))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_quote_overlay(user: str, positions: Iterable[Position]) -> None:
    """Persist per-share quotes for the next process start. Does not write empty prices."""
    quotes = {}
    for position in positions:
        if position.market_price is None:
            continue
        quotes[_quote_overlay_key(position)] = {
            "market_price": position.market_price,
            "prev_close": position.prev_close,
            "leverage_factor": position.leverage_factor,
        }
    payload = {
        "as_of": taiwan_now().isoformat(),
        "quotes": quotes,
    }
    try:
        write_protected_text(
            _quote_overlay_path(user),
            json.dumps(payload, indent=2, ensure_ascii=False),
            user=user,
        )
    except Exception:
        pass


def drop_quote_overlay_keys(user: str, keys: Iterable[tuple[str, str, str, str]]) -> None:
    """Drop overlay rows whose holding identity matches (bug#00046)."""
    wanted = {
        "\x1f".join((broker, account, symbol, instrument_type))
        for broker, account, symbol, instrument_type in keys
    }
    if not wanted:
        return
    data = load_quote_overlay(user)
    quotes = dict(data.get("quotes") or {})
    if not quotes:
        return
    kept = {key: value for key, value in quotes.items() if key not in wanted}
    if kept == quotes:
        return
    data["quotes"] = kept
    try:
        write_protected_text(
            _quote_overlay_path(user),
            json.dumps(data, indent=2, ensure_ascii=False),
            user=user,
        )
    except Exception:
        pass


def apply_quote_overlay(
    user: str,
    positions: Iterable[Position],
) -> tuple[list[Position], Optional[str]]:
    """Copy last per-share prices onto positions that have none.

    Recomputes `market_value` from the current quantity so an edited size does
    not reuse a stale cached value. Returns (copies, as_of) when any overlay
    applied, otherwise (original list, None).
    """
    data = load_quote_overlay(user)
    quotes = data.get("quotes") or {}
    as_of = data.get("as_of") if isinstance(data.get("as_of"), str) else None
    if not quotes:
        return list(positions), None
    applied = False
    out: list[Position] = []
    for position in positions:
        if position.market_price is not None or position.market_value is not None:
            out.append(position)
            continue
        cached = quotes.get(_quote_overlay_key(position)) or {}
        price = cached.get("market_price")
        if not isinstance(price, (int, float)):
            out.append(position)
            continue
        copy = position.model_copy(deep=True)
        copy.market_price = float(price)
        prev_close = cached.get("prev_close")
        if isinstance(prev_close, (int, float)):
            copy.prev_close = float(prev_close)
        leverage = cached.get("leverage_factor")
        if copy.instrument_type == "etf" and copy.leverage_factor is None:
            if isinstance(leverage, (int, float)):
                copy.leverage_factor = float(leverage)
        multiplier = (
            copy.multiplier
            if (copy.instrument_type == "option" and copy.multiplier is not None)
            else 1.0
        )
        if copy.quantity is not None:
            copy.market_value = copy.market_price * copy.quantity * multiplier
        out.append(copy)
        applied = True
    return out, (as_of if applied else None)



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
    safe = symbol.replace(".", "_").replace("/", "_").replace(":", "_")
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
    if not cached or cached.get("aum") is None:
        return False
    if cached.get("price") is None:
        return False
    if not cached.get("holdings_as_of_date"):
        return False
    if not (cached.get("holdings") or cached.get("asset_classes")):
        return False
    if cached.get("data_status") in ("partial", "error", "retryable"):
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
    """Delete per-ETF cache files older than the shared retention policy."""
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
    safe = symbol.replace(".", "_").replace("/", "_").replace(":", "_")
    return get_etf_history_dir() / f"{safe}.jsonl"


def _etf_portfolio_state_signature(snapshot: dict) -> tuple:
    """Compare disclosed allocation state without prices/AUM/fetch timestamps."""
    holdings = tuple(sorted(
        (
            str(item.get("symbol") or ""),
            round(float(item.get("weight") or 0.0), 6),
            str(item.get("instrument_type") or ""),
            str(item.get("option_type") or ""),
            str(item.get("expiration") or ""),
            str(item.get("strike") or ""),
        )
        for item in (snapshot.get("holdings") or [])
        if item.get("symbol") is not None
    ))
    asset_classes = tuple(sorted(
        (
            str(key),
            round(float(value or 0.0), 6),
        )
        for key, value in (snapshot.get("asset_classes") or {}).items()
    ))
    return holdings, asset_classes


def append_etf_daily_snapshot(
    symbol: str,
    holdings: list[dict],
    aum: float | None,
    snapshot_date: Optional[str] = None,
    asset_classes: Optional[dict] = None,
    replace_existing: bool = False,
    coalesce_unchanged: bool = False,
    metadata: Optional[dict] = None,
) -> None:
    """Append one real dated holdings snapshot for `symbol`, if one for that date
    doesn't already exist (idempotent — safe to call every time the screen refreshes,
    not just once a day). Stores the fields needed for trend math: symbol, weight,
    price, AUM, and asset_classes breakdown (bug#00103).

    ``metadata`` records provenance that belongs to the snapshot as a whole
    rather than to any holding — for SEC 13F that is `filing_date` and
    `accession` (bug#00124). Without it the history log only knows the *report*
    date, and the screen cannot tell the user when the data it is showing was
    actually published, only which quarter it describes. Keys whose value is
    None are dropped so an unknown filing date stays unknown instead of being
    written as a null that later reads like a real answer.

    ``coalesce_unchanged`` is for Yahoo ETF observations, whose disclosed top
    holdings can remain unchanged for many fetch dates. Consecutive identical
    allocation states are stored once with first/last-observed dates, while the
    newest price/share enrichment replaces the older copy. SEC 13F callers leave
    this disabled because distinct report periods must remain distinct filings.
    """
    date_str = snapshot_date or taiwan_now().strftime("%Y-%m-%d")
    path = _etf_history_path(symbol)

    try:
        existing_lines: list[str] = []
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get("date") == date_str:
                        if not replace_existing:
                            return  # already have today's real snapshot
                        continue
                except Exception:
                    pass
                existing_lines.append(line)

        snapshot_fields = (
            "symbol", "name", "weight", "price", "shares", "value",
            "issuer", "title_of_class", "cusip", "figi", "instrument_type",
            "option_type", "expiration", "strike", "amount_type",
        )
        slim_holdings = [
            {key: h.get(key) for key in snapshot_fields if h.get(key) is not None}
            for h in (holdings or [])
            if h.get("symbol") is not None and h.get("weight") is not None
        ]
        if not slim_holdings and not asset_classes:
            return  # nothing real to record

        payload = {"date": date_str, "aum": aum, "holdings": slim_holdings}
        if snapshot_date is None:
            # `date` above is the *Taiwan* calendar date this row was captured on,
            # which is not the US session it observed: a Taiwan Saturday morning
            # fetch carries Friday's US close, and Taiwan Saturday is not a
            # session at all.  The display layer and the legacy backtests keep
            # reading `date` unchanged; the Experiment Engine reads these two
            # fields instead so Forecast Records are keyed to real sessions and
            # never to a partially-observed one.  Only live captures get them —
            # a caller passing an explicit `snapshot_date` (SEC 13F report
            # periods) is describing a quarter, not a session.
            payload["session"] = us_session_date()
            payload["session_complete"] = us_session_complete()
        if asset_classes:
            payload["asset_classes"] = asset_classes
        for key, value in (metadata or {}).items():
            if value is not None:
                payload[key] = value

        if coalesce_unchanged:
            payload["first_observed_date"] = date_str
            payload["last_observed_date"] = date_str
            for index in range(len(existing_lines) - 1, -1, -1):
                try:
                    previous = json.loads(existing_lines[index])
                except Exception:
                    continue
                if (
                    _etf_portfolio_state_signature(previous)
                    == _etf_portfolio_state_signature(payload)
                ):
                    payload["first_observed_date"] = (
                        previous.get("first_observed_date")
                        or previous.get("date")
                        or date_str
                    )
                    existing_lines[index] = json.dumps(
                        payload, ensure_ascii=False
                    )
                    path.write_text("\n".join(existing_lines) + "\n")
                    return
                break

        line = json.dumps(payload, ensure_ascii=False)
        if replace_existing:
            path.write_text("\n".join(existing_lines + [line]) + "\n")
        else:
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
    """Drop snapshot lines older than the shared research retention policy."""
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


def _etf_watchlist_path(user: str) -> Path:
    safe = (user or "default").replace("/", "_")
    return get_data_dir() / f"{safe}_etf_watchlist.json"


def etf_watchlist_is_configured(user: str) -> bool:
    """True once the user has saved an ETF observation list (even if later emptied)."""
    return _etf_watchlist_path(user).exists()


def load_etf_watchlist(user: str) -> "list[str]":
    """Load the user's ETF observation-list tickers.

    This is the explicit list of US symbols whose active-ETF buy/sell activity
    the ETF screen will show. Missing or unreadable files return [].
    """
    p = _etf_watchlist_path(user)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        tickers = data.get("tickers", []) if isinstance(data, dict) else []
        return list(dict.fromkeys(
            str(t).strip().upper() for t in tickers if str(t).strip()
        ))
    except Exception:
        return []


def save_etf_watchlist(user: str, tickers: "Iterable[str]") -> None:
    """Persist the ETF observation list (uppercased, de-duped, insertion order)."""
    p = _etf_watchlist_path(user)
    uniq = list(dict.fromkeys(
        str(t).strip().upper() for t in tickers if str(t).strip()
    ))
    p.write_text(json.dumps({"tickers": uniq}, ensure_ascii=False, indent=2))


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
    captured_date = taiwan_now().strftime("%Y-%m-%d")
    date_str = snapshot_date or captured_date
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

        record = {
            "date": date_str,
            "session_date": date_str,
            "captured_date": captured_date,
            "spot_price": spot_price,
            "contracts": contracts,
        }
        # `date`/`session_date` above stay on whatever the options screens and
        # legacy backtests already read.  These two fields carry the real US
        # session and whether it had closed; the Experiment Engine reads only
        # them, so a Taiwan-Saturday capture of Friday's chain is recorded
        # against Friday rather than against a non-session.
        #
        # bug#00134: this used to be guarded by `snapshot_date is None`, copied
        # from the ETF store where an explicit date means a SEC 13F report
        # *period* rather than a session.  Options have no such import path —
        # the only caller passes the US session date returned by the live fetch
        # — so the guard was never true in production and the two fields were
        # never written.  `session_aligned_snapshots` then dropped every row,
        # `_latest_aligned_session` returned None, and the options family
        # emitted no forecast at all, ever.
        aligned_session = snapshot_date or us_session_date()
        record["session"] = aligned_session
        record["session_complete"] = (
            us_session_complete()
            if aligned_session == us_session_date()
            # An earlier session has closed by definition; only the current one
            # can still be in progress.
            else True
        )
        if earnings_date:
            record["earnings_date"] = earnings_date
        line = json.dumps(record, ensure_ascii=False)
        with path.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _option_snapshot_session_date(record: dict) -> Optional[str]:
    """Best-effort US session date for legacy options records."""
    explicit = record.get("session_date")
    if explicit:
        return str(explicit)[:10]
    captured = record.get("date")
    latest_trade = None
    for contract in record.get("contracts", []):
        raw = contract.get("lastTradeDate")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if latest_trade is None or parsed > latest_trade:
            latest_trade = parsed
    if latest_trade is None or _TZ_US_STORAGE is None:
        return captured
    inferred = latest_trade.astimezone(_TZ_US_STORAGE).date().isoformat()
    return captured if captured and inferred > captured else inferred


def load_options_daily_snapshots(underlying: str, since_date: Optional[str] = None) -> list[dict]:
    """Load one latest capture per real US options market session."""
    path = _options_history_path(underlying)
    if not path.exists():
        return []
    by_session: dict[str, dict] = {}
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            session = _option_snapshot_session_date(rec)
            if not session or (since_date and session < since_date):
                continue
            normalised = dict(rec)
            normalised["captured_date"] = rec.get("captured_date") or rec.get("date")
            normalised["session_date"] = session
            normalised["date"] = session
            by_session[session] = normalised
    except Exception:
        return []
    return [by_session[session] for session in sorted(by_session)]


def _latest_expected_us_session() -> str:
    """Most recent active/completed US weekday session (holiday calendar pending)."""
    if _TZ_US_STORAGE is None:
        return taiwan_now().strftime("%Y-%m-%d")
    now = datetime.now(_TZ_US_STORAGE)
    session = now.date()
    if now.weekday() >= 5 or now.time() < time(9, 30):
        session -= timedelta(days=1)
        while session.weekday() >= 5:
            session -= timedelta(days=1)
    return session.isoformat()


def options_symbol_fresh(underlying: str) -> bool:
    """Return True when the latest expected US session is already persisted."""
    snaps = load_options_daily_snapshots(underlying)
    if not snaps:
        return False
    return snaps[-1].get("date") == _latest_expected_us_session()


def prune_options_history(underlying: str, max_age_days: int = ANALYSIS_CACHE_RETENTION_DAYS) -> None:
    """Drop snapshot lines older than the shared research retention policy."""
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


_NYSE_SESSION_CALENDAR = NYSESessionCalendar()


def _us_now() -> datetime:
    """Current US Eastern time (aware); falls back to UTC if zone data is missing."""
    if _TZ_US_STORAGE is not None:
        return datetime.now(_TZ_US_STORAGE)
    return datetime.now(_timezone.utc)


def us_market_open_now() -> bool:
    """True during US regular trading hours (09:30–16:00 ET on a weekday)."""
    now = _us_now()
    if not _NYSE_SESSION_CALENDAR.is_session(now.date()):
        return False
    t = now.hour * 60 + now.minute
    return 570 <= t <= 960


def us_session_date() -> str:
    """Return the NYSE session key for freshly fetched US quote data."""
    now = _us_now()
    if (
        _NYSE_SESSION_CALENDAR.is_session(now.date())
        and (now.hour * 60 + now.minute) >= 570
    ):
        return now.date().isoformat()
    return _NYSE_SESSION_CALENDAR.latest_session_on_or_before(
        now.date() - _timedelta(days=1)
    ).isoformat()


def us_session_complete() -> bool:
    """True when the session identified by us_session_date() has already closed
    (i.e. the data being recorded is a real closing figure, not a partial
    intraday reading). The severity/statistics layer requires this so baselines
    aren't polluted by half-finished sessions."""
    now = _us_now()
    if (
        _NYSE_SESSION_CALENDAR.is_session(now.date())
        and (now.hour * 60 + now.minute) >= 570
    ):
        return (now.hour * 60 + now.minute) > 960  # past 16:00 ET today
    return True  # pre-open/weekend/holiday → carrying a prior completed session


def last_us_close_dt() -> datetime:
    """The most recent US regular-session close (16:00 ET on a weekday) that has
    already passed, returned as a Taiwan-naive datetime so it compares directly
    against cache `last_refreshed` stamps (which use taiwan_now()). Used to decide,
    while the market is closed, whether our cached data already reflects that close
    (未開盤→上一個收盤；已收盤→本日收盤)."""
    now_us = _us_now()
    today_close_passed = (
        _NYSE_SESSION_CALENDAR.is_session(now_us.date())
        and (now_us.hour * 60 + now_us.minute) >= 960
    )
    session = (
        now_us.date()
        if today_close_passed
        else _NYSE_SESSION_CALENDAR.latest_session_on_or_before(
            now_us.date() - _timedelta(days=1)
        )
    )
    close = datetime.combine(session, time(hour=16), tzinfo=now_us.tzinfo)
    if _TZ_TW_STORAGE is not None:
        return close.astimezone(_TZ_TW_STORAGE).replace(tzinfo=None)
    return close.astimezone(_timezone.utc).replace(tzinfo=None)


# ── Symbol adjusted-close truth used by the Experiment Engine ────────────────

def _symbol_adjusted_history_path(symbol: str) -> Path:
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "_"
        for ch in str(symbol).upper()
    )
    history_dir = get_data_dir() / "benchmark_cache" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir / f"{safe}.jsonl"


def append_symbol_daily_adjusted_closes(
    symbol: str,
    rows: Iterable[tuple[object, float]],
    *,
    source: str = "yfinance-auto-adjust",
) -> None:
    """Upsert real adjusted closes for any symbol by exact NYSE session."""
    path = _symbol_adjusted_history_path(symbol)
    by_date = {
        str(row.get("date")): row
        for row in load_symbol_daily_adjusted_closes(symbol)
        if row.get("date")
    }
    captured_at = taiwan_now().isoformat()
    for raw_date, raw_close in rows:
        try:
            if isinstance(raw_date, datetime):
                session = raw_date.date()
            elif isinstance(raw_date, date):
                session = raw_date
            else:
                session = date.fromisoformat(str(raw_date)[:10])
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if (
            not _NYSE_SESSION_CALENDAR.is_session(session)
            or close <= 0
        ):
            continue
        by_date[session.isoformat()] = {
            "date": session.isoformat(),
            "adjusted_close": close,
            "session_complete": True,
            "source": source,
            "quality": "valid",
            "captured_at": captured_at,
        }

    cutoff = (
        taiwan_now().date() - _timedelta(days=BENCHMARK_TRUTH_RETENTION_DAYS)
    ).isoformat()
    kept = [
        by_date[key]
        for key in sorted(by_date)
        if key >= cutoff
    ]
    try:
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept)
        )
    except Exception:
        pass


def load_symbol_daily_adjusted_closes(
    symbol: str,
    since_date: Optional[str] = None,
) -> list[dict]:
    """Read locally persisted adjusted closes; never fetch network data."""
    path = _symbol_adjusted_history_path(symbol)
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                session = str(row.get("date") or "")
                close = float(row.get("adjusted_close"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not session or close <= 0 or (since_date and session < since_date):
                continue
            rows.append(
                {
                    **row,
                    "date": session,
                    "adjusted_close": close,
                }
            )
    except Exception:
        return []
    rows.sort(key=lambda row: row["date"])
    return rows


def symbol_adjusted_close_available(symbol: str, session: str) -> bool:
    return any(
        row.get("date") == session and row.get("session_complete", True)
        for row in load_symbol_daily_adjusted_closes(symbol, since_date=session)
    )


# Backwards-compatible benchmark names.  SPY uses the same exact-session truth
# contract as sector-predictive member symbols; only its role in OutcomeSpec differs.
def append_benchmark_daily_closes(
    symbol: str,
    rows: Iterable[tuple[object, float]],
    *,
    source: str = "yfinance-auto-adjust",
) -> None:
    append_symbol_daily_adjusted_closes(symbol, rows, source=source)


def load_benchmark_daily_closes(
    symbol: str,
    since_date: Optional[str] = None,
) -> list[dict]:
    return load_symbol_daily_adjusted_closes(symbol, since_date=since_date)


def benchmark_close_available(symbol: str, session: str) -> bool:
    return symbol_adjusted_close_available(symbol, session)


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


def _sector_predictive_cache_path(user: str) -> Path:
    safe = (user or "default").replace("/", "_")
    return get_sector_config_dir() / f"{safe}_predictive.json"


def _sector_groups_signature(groups: dict[str, list[str]]) -> list:
    """JSON 穩定的板塊設定簽章；改名、增刪成分股都會觸發模型重建。"""
    return [
        [str(name), sorted({str(s).upper() for s in (symbols or [])})]
        for name, symbols in sorted(groups.items())
    ]


def load_sector_predictive_cache(user: str) -> dict:
    """讀取多年日線重建的類股個股條件機率模型；缺漏／毀損回空 dict。"""
    try:
        raw = json.loads(_sector_predictive_cache_path(user).read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def load_sector_predictive_model(
    user: str,
    groups: dict[str, list[str]],
) -> Optional[dict]:
    """只回傳與目前板塊設定完全相符的模型，避免增刪成分股後暫用舊 universe。"""
    cached = load_sector_predictive_cache(user)
    if cached.get("groups_signature") != _sector_groups_signature(groups):
        return None
    model = cached.get("model")
    return model if isinstance(model, dict) else None


def sector_predictive_cache_needs_refresh(
    user: str,
    groups: dict[str, list[str]],
) -> bool:
    """同一美股交易日、同一板塊設定只建一次；失敗後一小時再試，避免每分鐘重抓。"""
    from .sector_predictive import SECTOR_CONFIRMATION_POLICY_VERSION

    cached = load_sector_predictive_cache(user)
    signature = _sector_groups_signature(groups)
    session = us_session_date()
    model = cached.get("model") or {}
    # Schema migration: rebuild when confirmation policy version changes so
    # SMA5/20 risk fields are present instead of waiting out the throttle.
    if (
        model
        and (
            "sector_confirmation" not in model
            or (model.get("sector_confirmation") or {}).get("policy_version")
            != SECTOR_CONFIRMATION_POLICY_VERSION
        )
        and cached.get("groups_signature") == signature
    ):
        return True
    fresh = (
        model
        and model.get("sector_confirmation")
        and cached.get("built_for_session") == session
        and cached.get("groups_signature") == signature
    )
    if fresh:
        return False
    if (
        cached.get("attempted_for_session") == session
        and cached.get("attempted_groups_signature") == signature
    ):
        try:
            attempted = datetime.fromisoformat(cached.get("last_attempt_at") or "")
            if (taiwan_now() - attempted).total_seconds() < 3600:
                return False
        except (TypeError, ValueError):
            pass
    return True


def mark_sector_predictive_attempt(
    user: str,
    groups: dict[str, list[str]],
) -> None:
    """在長歷史下載前記錄嘗試時間；保留任何既有可用模型。"""
    payload = load_sector_predictive_cache(user)
    payload.update({
        "last_attempt_at": taiwan_now().isoformat(),
        "attempted_for_session": us_session_date(),
        "attempted_groups_signature": _sector_groups_signature(groups),
    })
    try:
        _sector_predictive_cache_path(user).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
    except Exception:
        pass


def save_sector_predictive_cache(
    user: str,
    groups: dict[str, list[str]],
    model: dict,
) -> None:
    """原子語意上只在完整模型存在時覆寫；下載失敗由呼叫端保留上一版。"""
    if not model or not model.get("patterns"):
        return
    payload = {
        "built_at": taiwan_now().isoformat(),
        "built_for_session": us_session_date(),
        "groups_signature": _sector_groups_signature(groups),
        "last_attempt_at": taiwan_now().isoformat(),
        "attempted_for_session": us_session_date(),
        "attempted_groups_signature": _sector_groups_signature(groups),
        "model": model,
    }
    try:
        _sector_predictive_cache_path(user).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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
    # bug#00085：marketcap 以「該股本地幣別」計價，必須連同 currency 一起保存，
    # 否則跨幣別成分股（如 .KS 韓元）會憑數值大小灌爆市值權重。
    "volume", "turnover", "marketcap", "currency", "weight",
    # 個股短線條件模型的「當下樣態」；全為真實日線衍生值，缺資料保留 None。
    "open", "high", "low", "ma30", "ma60", "streak", "candle_pattern",
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
    is written when no member carries a real day_pct (avoids recording an empty day).

    bug#00085 (item C): the record is keyed by the US *session* date (us_session_date)
    rather than the Taiwan calendar date, and carries `session_complete` so the
    statistics layer can require completed sessions instead of mixing partial
    intraday readings with real closes."""
    date_str = snapshot_date or us_session_date()
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
        "session_complete": us_session_complete(),
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
    """Drop snapshot lines older than the shared research retention policy."""
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
