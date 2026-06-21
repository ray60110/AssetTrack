from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .models import PortfolioSnapshot, Position


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


def load_manual_positions(user: str = "default") -> list[Position]:
    path = get_positions_path(user)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "positions" in data:
            return [Position.model_validate(p) for p in data["positions"]]
        if isinstance(data, list):
            return [Position.model_validate(p) for p in data]
    except Exception:
        return []
    return []


def save_manual_positions(positions: Iterable[Position], user: str = "default"):
    path = get_positions_path(user)
    data = {"positions": [p.to_dict() for p in positions], "last_manual_update": datetime.utcnow().isoformat()}
    path.write_text(json.dumps(data, indent=2))
