import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional


class ArbSQLiteStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                venue TEXT NOT NULL,
                trade_id TEXT,
                market_key TEXT,
                order_id TEXT,
                client_order_id TEXT,
                side TEXT,
                action TEXT,
                price REAL,
                quantity REAL,
                status TEXT,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                venue TEXT NOT NULL,
                trade_id TEXT,
                market_key TEXT,
                order_id TEXT,
                fill_price REAL,
                fill_qty REAL,
                fee REAL,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS pnl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                trade_id TEXT,
                market_key TEXT,
                expected_pnl REAL,
                realized_pnl REAL,
                status TEXT,
                metadata_json TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS skips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                detail TEXT,
                market_key_k TEXT,
                market_key_p TEXT,
                strategy TEXT,
                edge_liquido_pct REAL,
                liq_k REAL,
                liq_p REAL,
                metadata_json TEXT
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_orders_trade ON orders(trade_id);",
            "CREATE INDEX IF NOT EXISTS idx_fills_trade ON fills(trade_id);",
            "CREATE INDEX IF NOT EXISTS idx_pnl_trade ON pnl(trade_id);",
            "CREATE INDEX IF NOT EXISTS idx_skips_reason ON skips(reason_code);",
        ]
        with self._lock:
            cur = self._conn.cursor()
            for sql in ddl:
                cur.execute(sql)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _meta(metadata: Optional[dict[str, Any]]) -> str:
        return json.dumps(metadata or {}, ensure_ascii=True, separators=(",", ":"))

    def record_order(
        self,
        *,
        ts_utc: str,
        venue: str,
        trade_id: str,
        market_key: str,
        order_id: str,
        client_order_id: str,
        side: str,
        action: str,
        price: float,
        quantity: float,
        status: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO orders (
                    ts_utc, venue, trade_id, market_key, order_id, client_order_id,
                    side, action, price, quantity, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_utc,
                    venue,
                    trade_id,
                    market_key,
                    order_id,
                    client_order_id,
                    side,
                    action,
                    float(price),
                    float(quantity),
                    status,
                    self._meta(metadata),
                ),
            )
            self._conn.commit()

    def record_fill(
        self,
        *,
        ts_utc: str,
        venue: str,
        trade_id: str,
        market_key: str,
        order_id: str,
        fill_price: float,
        fill_qty: float,
        fee: float,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO fills (
                    ts_utc, venue, trade_id, market_key, order_id, fill_price, fill_qty, fee, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_utc,
                    venue,
                    trade_id,
                    market_key,
                    order_id,
                    float(fill_price),
                    float(fill_qty),
                    float(fee),
                    self._meta(metadata),
                ),
            )
            self._conn.commit()

    def record_pnl(
        self,
        *,
        ts_utc: str,
        trade_id: str,
        market_key: str,
        expected_pnl: float,
        realized_pnl: float,
        status: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pnl (
                    ts_utc, trade_id, market_key, expected_pnl, realized_pnl, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_utc,
                    trade_id,
                    market_key,
                    float(expected_pnl),
                    float(realized_pnl),
                    status,
                    self._meta(metadata),
                ),
            )
            self._conn.commit()

    def record_skip(
        self,
        *,
        ts_utc: str,
        reason_code: str,
        detail: str,
        market_key_k: str,
        market_key_p: str,
        strategy: str = "",
        edge_liquido_pct: Optional[float] = None,
        liq_k: Optional[float] = None,
        liq_p: Optional[float] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO skips (
                    ts_utc, reason_code, detail, market_key_k, market_key_p, strategy,
                    edge_liquido_pct, liq_k, liq_p, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_utc,
                    reason_code,
                    detail,
                    market_key_k,
                    market_key_p,
                    strategy,
                    None if edge_liquido_pct is None else float(edge_liquido_pct),
                    None if liq_k is None else float(liq_k),
                    None if liq_p is None else float(liq_p),
                    self._meta(metadata),
                ),
            )
            self._conn.commit()

