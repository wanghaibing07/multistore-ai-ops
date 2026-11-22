from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


STATUSES = {"unused", "validating", "purchasing", "waiting_fulfillment", "delivered_pending_warranty", "completed", "needs_manual_review", "revoked"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            c = conn
            c.executescript("""
            CREATE TABLE IF NOT EXISTS sku_settings (
                sku_key TEXT PRIMARY KEY, title TEXT NOT NULL, product_type TEXT NOT NULL,
                product_id TEXT NOT NULL, reference_price_usd REAL NOT NULL,
                max_cost_usd REAL NOT NULL, goods_no TEXT
            );
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY, sku_key TEXT NOT NULL, channel TEXT NOT NULL,
                batch_id TEXT, goods_no TEXT, external_order_id TEXT, remark TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, buyer_email TEXT, buyer_email_masked TEXT,
                idempotency_key TEXT, request_payload_json TEXT, upstream_order_code TEXT,
                upstream_status TEXT, cost_usd REAL, delivery_json TEXT, warranty_json TEXT,
                screenshot_path TEXT, screenshot_content_type TEXT, screenshot_size INTEGER,
                last_error_code TEXT, last_error_message TEXT, last_polled_at TEXT,
                submitted_at TEXT, completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,
                from_status TEXT, to_status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY, sku_key TEXT NOT NULL, channel TEXT NOT NULL,
                goods_no TEXT, batch_label TEXT NOT NULL, count INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_keys (
                dedupe_key TEXT PRIMARY KEY, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_codes_status ON codes(status);
            CREATE INDEX IF NOT EXISTS idx_codes_filter ON codes(channel, sku_key, created_at);
            """)
            columns = {r["name"] for r in c.execute("PRAGMA table_info(codes)")}
            if "goods_no" not in columns:
                c.execute("ALTER TABLE codes ADD COLUMN goods_no TEXT")
        finally:
            conn.close()

    def seed_skus(self, skus: dict[str, dict[str, Any]]) -> None:
        with self.transaction(immediate=True) as c:
            for sku_key, s in skus.items():
                c.execute("""INSERT INTO sku_settings(sku_key,title,product_type,product_id,reference_price_usd,max_cost_usd,goods_no)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(sku_key) DO UPDATE SET title=excluded.title,product_type=excluded.product_type,
                    product_id=excluded.product_id,reference_price_usd=excluded.reference_price_usd""",
                    (sku_key, s["title"], s["product_type"], s["product_id"], s["reference_price_usd"], s["max_cost_usd"], s.get("goods_no")))

    def get_sku(self, sku_key: str) -> Optional[dict[str, Any]]:
        with self.connect() as c:
            row = c.execute("SELECT * FROM sku_settings WHERE sku_key=?", (sku_key,)).fetchone()
            return dict(row) if row else None

    def get_code(self, code: str) -> Optional[dict[str, Any]]:
        with self.connect() as c:
            row = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
            return dict(row) if row else None

    def insert_code(self, row: dict[str, Any]) -> None:
        columns = ",".join(row)
        marks = ",".join("?" for _ in row)
        with self.transaction(immediate=True) as c:
            c.execute(f"INSERT INTO codes({columns}) VALUES({marks})", tuple(row.values()))
            c.execute("INSERT INTO audit_events(code,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)", (row["code"], None, row["status"], "issued", row["created_at"]))

    def claim_for_purchase(self, code: str, payload: dict[str, Any], buyer_email: Optional[str], masked: Optional[str]) -> tuple[str, Optional[dict[str, Any]]]:
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
            if not row:
                return "missing", None
            current = dict(row)
            if current["status"] != "unused":
                return "busy", current
            key = "plan2-" + secrets.token_urlsafe(18)
            t = now_iso()
            c.execute("""UPDATE codes SET status='purchasing', buyer_email=?, buyer_email_masked=?, idempotency_key=?,
                request_payload_json=?, submitted_at=?, updated_at=?, last_error_code=NULL,last_error_message=NULL WHERE code=?""",
                (buyer_email, masked, key, json.dumps(payload, ensure_ascii=False), t, t, code))
            c.execute("INSERT INTO audit_events(code,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)", (code, "unused", "purchasing", "redeem_claimed", t))
            current = dict(c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone())
            return "claimed", current

    def transition(self, code: str, to_status: str, reason: str, updates: Optional[dict[str, Any]] = None, allowed: Optional[tuple[str, ...]] = None) -> bool:
        updates = updates or {}
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT status FROM codes WHERE code=?", (code,)).fetchone()
            if not row or (allowed and row["status"] not in allowed):
                return False
            old = row["status"]
            values = {"status": to_status, "updated_at": now_iso(), **updates}
            set_sql = ",".join(f"{k}=?" for k in values)
            c.execute(f"UPDATE codes SET {set_sql} WHERE code=?", tuple(values.values()) + (code,))
            c.execute("INSERT INTO audit_events(code,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)", (code, old, to_status, reason, values["updated_at"]))
            return True

    def audit(self, code: str) -> list[dict[str, Any]]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit_events WHERE code=? ORDER BY event_id", (code,))]

    def claim_notification(self, dedupe_key: str) -> bool:
        with self.transaction(immediate=True) as c:
            result = c.execute(
                "INSERT OR IGNORE INTO notification_keys(dedupe_key, created_at) VALUES(?, ?)",
                (dedupe_key, now_iso()),
            )
            return result.rowcount == 1

    def list_codes(self, where: str = "", params: tuple[Any, ...] = (), limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        with self.connect() as c:
            total = c.execute("SELECT COUNT(*) FROM codes" + (" WHERE " + where if where else ""), params).fetchone()[0]
            rows = c.execute("SELECT * FROM codes" + (" WHERE " + where if where else "") + " ORDER BY created_at DESC LIMIT ? OFFSET ?", params + (limit, offset)).fetchall()
            return [dict(r) for r in rows], total

