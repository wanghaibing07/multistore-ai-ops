from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


STATUSES = {"unused", "validating", "purchasing", "waiting_fulfillment", "delivered_pending_warranty", "completed", "needs_manual_review", "revoked"}

LIST_CODES_ORDER_BY: dict[str, str] = {
    "created_at_desc": "created_at DESC, code DESC",
    "used_at_desc": (
        "CASE WHEN submitted_at IS NOT NULL AND TRIM(submitted_at) <> '' THEN 0 ELSE 1 END ASC, "
        "submitted_at DESC, updated_at DESC, created_at DESC, code DESC"
    ),
}


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
            CREATE TABLE IF NOT EXISTS agiso_sku_mappings (
                mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
                num_iid TEXT NOT NULL DEFAULT '',
                sku_id TEXT NOT NULL DEFAULT '',
                outer_iid TEXT NOT NULL DEFAULT '',
                outer_sku_id TEXT NOT NULL DEFAULT '',
                sku_props_contains TEXT NOT NULL DEFAULT '',
                sku_key TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agiso_deliveries (
                delivery_key TEXT PRIMARY KEY,
                tid TEXT NOT NULL,
                oid TEXT NOT NULL,
                seq INTEGER NOT NULL,
                num_iid TEXT NOT NULL DEFAULT '',
                sku_id TEXT NOT NULL DEFAULT '',
                sku_key TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE,
                buyer_nick TEXT NOT NULL DEFAULT '',
                delivery_status TEXT NOT NULL,
                result_created INTEGER NOT NULL DEFAULT 0,
                card_sent INTEGER NOT NULL DEFAULT 0,
                ww_sent INTEGER NOT NULL DEFAULT 0,
                dummy_sent INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agiso_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE,
                source TEXT NOT NULL,
                from_platform TEXT NOT NULL DEFAULT '',
                aopic INTEGER,
                tid TEXT NOT NULL DEFAULT '',
                oid TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                payload_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_codes_status ON codes(status);
            CREATE INDEX IF NOT EXISTS idx_codes_filter ON codes(channel, sku_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_codes_submitted_at ON codes(submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_codes_external_order ON codes(external_order_id);
            CREATE INDEX IF NOT EXISTS idx_audit_events_code_reason ON audit_events(code, reason);
            CREATE INDEX IF NOT EXISTS idx_agiso_deliveries_tid ON agiso_deliveries(tid);
            CREATE INDEX IF NOT EXISTS idx_agiso_deliveries_oid ON agiso_deliveries(oid);
            CREATE INDEX IF NOT EXISTS idx_agiso_events_created ON agiso_events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_agiso_events_tid ON agiso_events(tid);
            """)
            columns = {r["name"] for r in c.execute("PRAGMA table_info(codes)")}
            if "goods_no" not in columns:
                c.execute("ALTER TABLE codes ADD COLUMN goods_no TEXT")
            delivery_columns = {r["name"] for r in c.execute("PRAGMA table_info(agiso_deliveries)")}
            if "result_created" not in delivery_columns:
                c.execute("ALTER TABLE agiso_deliveries ADD COLUMN result_created INTEGER NOT NULL DEFAULT 0")
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

    def claim_purchase_retry(self, code: str, idempotency_key: str, reason: str, trade_no: str = "") -> str:
        with self.transaction(immediate=True) as c:
            row = c.execute(
                "SELECT status, upstream_order_code, idempotency_key, request_payload_json, external_order_id FROM codes WHERE code=?",
                (code,),
            ).fetchone()
            if (not row or row["status"] != "needs_manual_review" or row["upstream_order_code"]
                    or row["idempotency_key"] != idempotency_key or not row["request_payload_json"]):
                return "RETRY_NOT_AVAILABLE"
            existing = (row["external_order_id"] or "").strip()
            if trade_no and existing and existing != trade_no:
                return "ORDER_NOT_ELIGIBLE"
            t = now_iso()
            c.execute(
                """UPDATE codes SET status='purchasing', external_order_id=?, last_error_code=NULL,
                   last_error_message=NULL, updated_at=? WHERE code=?""",
                (trade_no or row["external_order_id"], t, code),
            )
            c.execute(
                "INSERT INTO audit_events(code,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)",
                (code, "needs_manual_review", "purchasing", reason, t),
            )
            return ""

    def canboso_purchase_totals(self, shanghai_day: str) -> dict[str, Any]:
        with closing(self.connect()) as c:
            row = c.execute(
                """
                WITH purchases AS (
                    SELECT c.upstream_order_code, MAX(c.cost_usd) AS gross_usd,
                           MAX(CASE WHEN lower(c.upstream_status) IN ('cancelled', 'canceled', 'refunded') THEN 1 ELSE 0 END) AS cancelled,
                           MIN(julianday(a.created_at)) AS accepted_at
                    FROM audit_events a JOIN codes c ON c.code=a.code
                    WHERE a.reason IN ('shared_order_accepted', 'exclusive_delivered', 'canboso_purchase_cancelled')
                      AND COALESCE(c.upstream_order_code, '') <> '' AND c.cost_usd IS NOT NULL
                    GROUP BY c.upstream_order_code
                )
                SELECT COUNT(*) AS order_count, ROUND(COALESCE(SUM(gross_usd), 0), 2) AS gross_usd,
                       COALESCE(SUM(cancelled), 0) AS cancelled_count,
                       ROUND(COALESCE(SUM(cancelled * gross_usd), 0), 2) AS cancelled_gross_usd
                FROM purchases WHERE date(accepted_at, '+8 hours')=?
                """,
                (shanghai_day,),
            ).fetchone()
            return dict(row)

    def latest_revoke_audit(self, code: str) -> Optional[dict[str, Any]]:
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM audit_events WHERE code=? AND to_status='revoked' ORDER BY event_id DESC LIMIT 1",
                (code,),
            ).fetchone()
            return dict(row) if row else None

    def claim_notification(self, dedupe_key: str) -> bool:
        with self.transaction(immediate=True) as c:
            result = c.execute(
                "INSERT OR IGNORE INTO notification_keys(dedupe_key, created_at) VALUES(?, ?)",
                (dedupe_key, now_iso()),
            )
            return result.rowcount == 1

    def has_notification(self, dedupe_key: str) -> bool:
        with closing(self.connect()) as c:
            row = c.execute("SELECT 1 FROM notification_keys WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            return row is not None

    def clear_notification(self, dedupe_key: str) -> None:
        with self.transaction(immediate=True) as c:
            c.execute("DELETE FROM notification_keys WHERE dedupe_key=?", (dedupe_key,))

    def list_codes(
        self,
        where: str = "",
        params: tuple[Any, ...] = (),
        limit: int = 20,
        offset: int = 0,
        order_by: str = "created_at_desc",
    ) -> tuple[list[dict[str, Any]], int]:
        order_sql = LIST_CODES_ORDER_BY.get(order_by)
        if not order_sql:
            raise ValueError(f"Unsupported order_by: {order_by}")
        with self.connect() as c:
            total = c.execute("SELECT COUNT(*) FROM codes" + (" WHERE " + where if where else ""), params).fetchone()[0]
            rows = c.execute(
                "SELECT * FROM codes" + (" WHERE " + where if where else "") + f" ORDER BY {order_sql} LIMIT ? OFFSET ?",
                params + (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows], total

    def list_agiso_sku_mappings(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agiso_sku_mappings"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY mapping_id ASC"
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]

    def create_agiso_sku_mapping(self, data: dict[str, Any]) -> dict[str, Any]:
        t = now_iso()
        with self.transaction(immediate=True) as c:
            cursor = c.execute(
                """INSERT INTO agiso_sku_mappings(
                    num_iid, sku_id, outer_iid, outer_sku_id, sku_props_contains,
                    sku_key, enabled, note, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    data.get("num_iid", ""),
                    data.get("sku_id", ""),
                    data.get("outer_iid", ""),
                    data.get("outer_sku_id", ""),
                    data.get("sku_props_contains", ""),
                    data["sku_key"],
                    1 if data.get("enabled", True) else 0,
                    data.get("note", ""),
                    t,
                    t,
                ),
            )
            row = c.execute("SELECT * FROM agiso_sku_mappings WHERE mapping_id=?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def delete_agiso_sku_mapping(self, mapping_id: int) -> bool:
        with self.transaction(immediate=True) as c:
            result = c.execute("DELETE FROM agiso_sku_mappings WHERE mapping_id=?", (mapping_id,))
            return result.rowcount == 1

    def ensure_agiso_delivery(
        self,
        *,
        delivery_key: str,
        tid: str,
        oid: str,
        seq: int,
        num_iid: str,
        sku_id: str,
        sku_key: str,
        buyer_nick: str,
        remark: str,
        goods_no: Optional[str],
        code_factory: Callable[[], str],
    ) -> tuple[bool, dict[str, Any]]:
        with self.transaction(immediate=True) as c:
            existing = c.execute("SELECT * FROM agiso_deliveries WHERE delivery_key=?", (delivery_key,)).fetchone()
            if existing:
                return False, dict(existing)
            t = now_iso()
            code = ""
            for _ in range(16):
                candidate = code_factory()
                if not c.execute("SELECT 1 FROM codes WHERE code=?", (candidate,)).fetchone():
                    code = candidate
                    break
            if not code:
                raise RuntimeError("Failed to generate unique redemption code")
            c.execute(
                """INSERT INTO codes(
                    code, sku_key, channel, batch_id, goods_no, external_order_id,
                    remark, status, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (code, sku_key, "taobao", None, goods_no, tid, remark, "unused", t, t),
            )
            c.execute(
                "INSERT INTO audit_events(code,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)",
                (code, None, "unused", "agiso_issued", t),
            )
            c.execute(
                """INSERT INTO agiso_deliveries(
                    delivery_key, tid, oid, seq, num_iid, sku_id, sku_key, code,
                    buyer_nick, delivery_status, result_created, card_sent, ww_sent,
                    dummy_sent, last_error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (delivery_key, tid, oid, seq, num_iid, sku_id, sku_key, code, buyer_nick, "issued", 0, 0, 0, 0, None, t, t),
            )
            row = c.execute("SELECT * FROM agiso_deliveries WHERE delivery_key=?", (delivery_key,)).fetchone()
            return True, dict(row)

    def list_agiso_deliveries_by_tid(self, tid: str) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM agiso_deliveries WHERE tid=? ORDER BY oid ASC, seq ASC",
                (tid,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_agiso_deliveries_for_refund(self, tid: str, oid: str = "") -> list[dict[str, Any]]:
        with self.connect() as c:
            if oid:
                rows = c.execute(
                    "SELECT * FROM agiso_deliveries WHERE oid=? ORDER BY seq ASC",
                    (oid,),
                ).fetchall()
                if rows:
                    return [dict(r) for r in rows]
                if tid and oid != tid:
                    return []
            if tid:
                rows = c.execute(
                    "SELECT * FROM agiso_deliveries WHERE tid=? ORDER BY oid ASC, seq ASC",
                    (tid,),
                ).fetchall()
                return [dict(r) for r in rows]
            return []

    def find_codes_by_external_order(self, tid: str) -> list[dict[str, Any]]:
        if not tid:
            return []
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM codes WHERE external_order_id=? ORDER BY created_at ASC",
                (tid,),
            ).fetchall()
            return [dict(r) for r in rows]

    def bind_code_external_order(self, code: str, tid: str, reason: str = "agiso_card_bound") -> str:
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT status, external_order_id FROM codes WHERE code=?", (code,)).fetchone()
            if not row:
                return "missing"
            existing = (row["external_order_id"] or "").strip()
            if existing == tid:
                return "already_bound"
            if existing:
                return "conflict"
            t = now_iso()
            c.execute("UPDATE codes SET external_order_id=?, updated_at=? WHERE code=?", (tid, t, code))
            c.execute(
                "INSERT INTO audit_events(code,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)",
                (code, row["status"], row["status"], reason, t),
            )
            return "bound"

    def has_16688_sale_binding(self, code: str, trade_no: str) -> bool:
        with closing(self.connect()) as c:
            return c.execute(
                """SELECT 1 FROM codes
                   WHERE code=? AND channel='16688' AND external_order_id=?
                   AND EXISTS (
                       SELECT 1 FROM audit_events
                       WHERE code=codes.code
                       AND reason IN ('channel16688_sold_sync', 'channel16688_order_verified')
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM audit_events
                       WHERE code=codes.code AND reason='16688_order_rejected_live'
                   )""",
                (code, trade_no),
            ).fetchone() is not None

    def claim_agiso_trade_delivery(self, tid: str, stale_seconds: int = 60, force_retry: bool = False) -> list[dict[str, Any]]:
        with self.transaction(immediate=True) as c:
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM agiso_deliveries WHERE tid=? AND delivery_status != 'revoked' ORDER BY oid ASC, seq ASC",
                    (tid,),
                ).fetchall()
            ]
            if not rows:
                return []
            now_dt = datetime.now(timezone.utc).astimezone()
            for row in rows:
                if row["delivery_status"] == "delivering":
                    try:
                        age = (now_dt - datetime.fromisoformat(row["updated_at"])).total_seconds()
                    except ValueError:
                        age = stale_seconds + 1
                    if age < stale_seconds:
                        return []
            if not force_retry and all(row["delivery_status"] == "delivered" for row in rows):
                return []
            t = now_iso()
            if force_retry:
                c.execute(
                    "UPDATE agiso_deliveries SET delivery_status='delivering', updated_at=? WHERE tid=? AND delivery_status != 'revoked'",
                    (t, tid),
                )
            else:
                c.execute(
                    "UPDATE agiso_deliveries SET delivery_status='delivering', updated_at=? WHERE tid=? AND delivery_status NOT IN ('delivered', 'revoked')",
                    (t, tid),
                )
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM agiso_deliveries WHERE tid=? AND delivery_status != 'revoked' ORDER BY oid ASC, seq ASC",
                    (tid,),
                ).fetchall()
            ]

    def update_agiso_trade_deliveries(self, tid: str, updates: dict[str, Any], exclude_revoked: bool = True) -> list[dict[str, Any]]:
        values = {"updated_at": now_iso(), **updates}
        set_sql = ",".join(f"{key}=?" for key in values)
        where_sql = "tid=? AND delivery_status != 'revoked'" if exclude_revoked else "tid=?"
        with self.transaction(immediate=True) as c:
            c.execute(f"UPDATE agiso_deliveries SET {set_sql} WHERE {where_sql}", tuple(values.values()) + (tid,))
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM agiso_deliveries WHERE tid=? ORDER BY oid ASC, seq ASC",
                    (tid,),
                ).fetchall()
            ]

    def mark_agiso_delivery_revoked(self, code: str) -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "UPDATE agiso_deliveries SET delivery_status='revoked', updated_at=? WHERE code=?",
                (now_iso(), code),
            )

    def mark_agiso_delivery_restored(self, code: str) -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "UPDATE agiso_deliveries SET delivery_status='delivered', updated_at=? WHERE code=? AND delivery_status='revoked'",
                (now_iso(), code),
            )

    def record_agiso_native_delivery(
        self,
        *,
        tid: str,
        oid: str,
        code: str,
        sku_key: str,
        buyer_nick: str = "",
        num_iid: str = "",
        sku_id: str = "",
        delivery_status: str = "delivered",
    ) -> dict[str, Any]:
        resolved_oid = oid.strip() or tid.strip()
        with self.transaction(immediate=True) as c:
            existing = c.execute("SELECT * FROM agiso_deliveries WHERE code=?", (code,)).fetchone()
            if existing:
                return dict(existing)
            next_seq = int(
                c.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM agiso_deliveries WHERE tid=? AND oid=?",
                    (tid, resolved_oid),
                ).fetchone()[0]
            )
            t = now_iso()
            delivery_key = f"native:{tid}:{resolved_oid}:{code}"
            c.execute(
                """INSERT INTO agiso_deliveries(
                    delivery_key, tid, oid, seq, num_iid, sku_id, sku_key, code,
                    buyer_nick, delivery_status, result_created, card_sent, ww_sent,
                    dummy_sent, last_error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    delivery_key,
                    tid,
                    resolved_oid,
                    next_seq,
                    num_iid,
                    sku_id,
                    sku_key,
                    code,
                    buyer_nick,
                    delivery_status,
                    1,
                    1,
                    0,
                    1,
                    None,
                    t,
                    t,
                ),
            )
            row = c.execute("SELECT * FROM agiso_deliveries WHERE delivery_key=?", (delivery_key,)).fetchone()
            return dict(row)

    def find_pending_unmatched_refund(self, tid: str, oid: str = "") -> list[dict[str, Any]]:
        if not tid:
            return []
        with self.connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM agiso_events WHERE tid=? AND aopic IN (256, 65536, 32768) ORDER BY event_id ASC",
                    (tid,),
                ).fetchall()
            ]
        pending: list[dict[str, Any]] = []
        for row in rows:
            row_oid = (row.get("oid") or "").strip()
            if oid and row_oid and row_oid not in {oid, tid}:
                continue
            aopic = int(row["aopic"]) if row.get("aopic") is not None else 0
            if aopic in (256, 65536) and row.get("status") == "refund_no_code":
                pending.append(row)
            elif aopic == 32768:
                closed_rid = ""
                try:
                    closed_rid = str((json.loads(row.get("payload_summary") or "{}")).get("RefundId") or "").strip()
                except ValueError:
                    closed_rid = ""
                remaining: list[dict[str, Any]] = []
                for item in pending:
                    if int(item.get("aopic") or 0) != 256:
                        remaining.append(item)
                        continue
                    item_rid = ""
                    try:
                        item_rid = str((json.loads(item.get("payload_summary") or "{}")).get("RefundId") or "").strip()
                    except ValueError:
                        item_rid = ""
                    if closed_rid and item_rid == closed_rid:
                        continue
                    if not closed_rid and not item_rid and (item.get("oid") or "").strip() == row_oid:
                        continue
                    remaining.append(item)
                pending = remaining
        return pending

    def has_agiso_refund_success(self, tid: str, oid: str = "") -> bool:
        if not tid:
            return False
        with self.connect() as c:
            if oid:
                row = c.execute(
                    "SELECT 1 FROM agiso_events WHERE tid=? AND aopic=65536 AND (oid=? OR oid='' OR oid=tid) LIMIT 1",
                    (tid, oid),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT 1 FROM agiso_events WHERE tid=? AND aopic=65536 LIMIT 1",
                    (tid,),
                ).fetchone()
            return row is not None

    def record_agiso_event(
        self,
        *,
        source: str,
        from_platform: str = "",
        aopic: Optional[int] = None,
        tid: str = "",
        oid: str = "",
        status: str,
        detail: str = "",
        payload_summary: str = "",
        event_key: Optional[str] = None,
    ) -> int:
        t = now_iso()
        with self.transaction(immediate=True) as c:
            if event_key:
                c.execute(
                    """INSERT INTO agiso_events(
                        event_key, source, from_platform, aopic, tid, oid, status, detail, payload_summary, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_key) DO UPDATE SET
                        status=excluded.status,
                        detail=excluded.detail,
                        payload_summary=excluded.payload_summary""",
                    (event_key, source, from_platform, aopic, tid, oid, status, detail[:500], payload_summary[:1000], t),
                )
                row = c.execute("SELECT event_id FROM agiso_events WHERE event_key=?", (event_key,)).fetchone()
                return int(row["event_id"])
            cursor = c.execute(
                """INSERT INTO agiso_events(
                    event_key, source, from_platform, aopic, tid, oid, status, detail, payload_summary, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (None, source, from_platform, aopic, tid, oid, status, detail[:500], payload_summary[:1000], t),
            )
            return int(cursor.lastrowid)

    def list_agiso_events(
        self,
        where: str = "",
        params: tuple[Any, ...] = (),
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        with self.connect() as c:
            total = c.execute("SELECT COUNT(*) FROM agiso_events" + (" WHERE " + where if where else ""), params).fetchone()[0]
            rows = c.execute(
                "SELECT * FROM agiso_events" + (" WHERE " + where if where else "") + " ORDER BY event_id DESC LIMIT ? OFFSET ?",
                params + (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows], total

    def agiso_delivery_counts(self) -> dict[str, int]:
        with self.connect() as c:
            rows = c.execute("SELECT delivery_status, COUNT(*) AS cnt FROM agiso_deliveries GROUP BY delivery_status").fetchall()
            counts = {r["delivery_status"]: int(r["cnt"]) for r in rows}
            total = sum(counts.values())
            return {
                "issued": counts.get("issued", 0),
                "delivering": counts.get("delivering", 0),
                "delivered": counts.get("delivered", 0),
                "partial": counts.get("partial", 0),
                "failed": counts.get("failed", 0),
                "revoked": counts.get("revoked", 0),
                "total": total,
            }

    def agiso_event_count(self) -> int:
        with self.connect() as c:
            return int(c.execute("SELECT COUNT(*) FROM agiso_events").fetchone()[0])

    def list_reconciliation_rows(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        channel: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["COALESCE(c.upstream_order_code, '') <> ''"]
        params: list[Any] = []
        channel_val = (channel or "").strip()
        if channel_val:
            channel_low = channel_val.lower()
            if channel_low in ("qianniu", "taobao", "千牛", "淘宝"):
                channel_val = "taobao"
            elif channel_low in ("16688", "16688_store", "16688自营", "16688卖货"):
                channel_val = "16688"
            clauses.append("c.channel = ?")
            params.append(channel_val)
        start_val = (start_date or "").strip()
        if start_val:
            clauses.append("substr(COALESCE(NULLIF(c.submitted_at, ''), c.created_at), 1, 10) >= ?")
            params.append(start_val)
        end_val = (end_date or "").strip()
        if end_val:
            clauses.append("substr(COALESCE(NULLIF(c.submitted_at, ''), c.created_at), 1, 10) <= ?")
            params.append(end_val)
        sql = (
            "SELECT "
            "c.code, "
            "c.sku_key, "
            "s.title AS sku_title, "
            "s.product_type AS product_type, "
            "c.channel, "
            "c.external_order_id, "
            "ad.tid AS agiso_tid, "
            "ad.oid AS agiso_oid, "
            "c.goods_no, "
            "c.upstream_order_code, "
            "c.upstream_status, "
            "c.status, "
            "c.cost_usd, "
            "c.submitted_at, "
            "c.completed_at, "
            "c.created_at, "
            "c.updated_at "
            "FROM codes c "
            "LEFT JOIN sku_settings s ON s.sku_key = c.sku_key "
            "LEFT JOIN agiso_deliveries ad ON ad.code = c.code "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(NULLIF(c.submitted_at, ''), c.created_at) DESC, c.code DESC"
        )
        with closing(self.connect()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            sku_title = (row["sku_title"] or "").strip() or row["sku_key"]
            external_order_id = (row["external_order_id"] or "").strip() or (row["agiso_tid"] or "").strip()
            items.append(
                {
                    "code": row["code"],
                    "sku_key": row["sku_key"],
                    "sku_title": sku_title,
                    "product_type": row["product_type"] or "",
                    "channel": row["channel"],
                    "external_order_id": external_order_id,
                    "agiso_tid": (row["agiso_tid"] or "").strip(),
                    "agiso_oid": (row["agiso_oid"] or "").strip(),
                    "goods_no": (row["goods_no"] or "").strip(),
                    "upstream_order_code": row["upstream_order_code"],
                    "upstream_status": (row["upstream_status"] or "").strip(),
                    "status": row["status"],
                    "cost_usd": row["cost_usd"],
                    "quantity": 1,
                    "source": "tg_fatbunny",
                    "seller": "FatBunny Hub (方案二)",
                    "submitted_at": (row["submitted_at"] or "").strip(),
                    "completed_at": (row["completed_at"] or "").strip(),
                    "created_at": (row["created_at"] or "").strip(),
                    "updated_at": (row["updated_at"] or "").strip(),
                }
            )
        return items

