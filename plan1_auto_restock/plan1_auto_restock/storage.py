from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from .models import CanbosoAccountItem

SHANGHAI_TZ = timezone(timedelta(hours=8))

SENSITIVE_ORDER_KEYS = frozenset(
    {
        "cards",
        "card_list",
        "secrets",
        "secret_list",
        "stock_secrets",
        "accounts",
        "password",
        "card_content",
        "content",
    }
)


def now_shanghai_str() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_shanghai_prefix() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")


def format_ts_shanghai(ts: int | float | str | None) -> str:
    if ts in (None, "", 0, "0"):
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def sanitize_order_meta(raw: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in raw.items():
        if str(key).lower() in SENSITIVE_ORDER_KEYS:
            continue
        if isinstance(value, dict):
            sanitized[str(key)] = sanitize_order_meta(value)
        elif isinstance(value, list):
            sanitized[str(key)] = [
                sanitize_order_meta(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            sanitized[str(key)] = value
    return sanitized


@dataclass(frozen=True)
class RestockIntentRecord:
    idempotency_key: str
    sku_id: str
    goods_no_16688: str
    supplier: str
    product_id: str
    product_name: str
    requested_qty: int
    estimated_cost_usd: Decimal
    status: str
    replay_attempts: int
    canboso_order_code: str
    actual_qty: int
    bonus_qty: int
    actual_amount: Decimal
    currency: str
    amount_usd: Decimal
    amount_cny: Decimal
    upload_import_count: int
    upload_dup_count: int
    error_message: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SkuStateRecord:
    sku_id: str
    baseline_cost_usd: Decimal | None
    last_cost_usd: Decimal | None
    last_sell_price_cny: Decimal | None
    last_restock_at: int
    last_stock_16688: int | None
    last_upstream_available: int | None
    auto_off_shelved: bool
    circuit_broken: bool
    circuit_reason: str
    updated_at: str


@dataclass(frozen=True)
class VaultItemRecord:
    item_id: int
    idempotency_key: str
    canboso_order_code: str
    supplier: str
    sku_id: str
    goods_no_16688: str
    card_index: int
    card_line: str
    upload_status: str


class DualDatabaseStore:
    """Manages isolated ledger.db (zero secrets) and secrets_vault.db (raw delivered credentials)."""

    def __init__(self, ledger_db_path: Path, secrets_vault_db_path: Path) -> None:
        self.ledger_db_path = ledger_db_path.resolve()
        self.secrets_vault_db_path = secrets_vault_db_path.resolve()
        if self.ledger_db_path == self.secrets_vault_db_path:
            raise ValueError("ledger_db_path 与 secrets_vault_db_path 必须是不同文件")

        self.ledger_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets_vault_db_path.parent.mkdir(parents=True, exist_ok=True)

        self._ledger = sqlite3.connect(self.ledger_db_path)
        self._ledger.row_factory = sqlite3.Row
        self._vault = sqlite3.connect(self.secrets_vault_db_path)
        self._vault.row_factory = sqlite3.Row
        self._init_schemas()

    def close(self) -> None:
        self._ledger.close()
        self._vault.close()

    def __enter__(self) -> DualDatabaseStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _init_schemas(self) -> None:
        with self._ledger:
            self._ledger.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS restock_intents (
                    idempotency_key TEXT PRIMARY KEY,
                    sku_id TEXT NOT NULL,
                    goods_no_16688 TEXT NOT NULL,
                    supplier TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL DEFAULT '',
                    requested_qty INTEGER NOT NULL,
                    estimated_cost_usd TEXT NOT NULL,
                    status TEXT NOT NULL,
                    replay_attempts INTEGER NOT NULL DEFAULT 0,
                    canboso_order_code TEXT NOT NULL DEFAULT '',
                    actual_qty INTEGER NOT NULL DEFAULT 0,
                    bonus_qty INTEGER NOT NULL DEFAULT 0,
                    actual_amount TEXT NOT NULL DEFAULT '0',
                    currency TEXT NOT NULL DEFAULT 'USD',
                    amount_usd TEXT NOT NULL DEFAULT '0',
                    amount_cny TEXT NOT NULL DEFAULT '0',
                    upload_import_count INTEGER NOT NULL DEFAULT 0,
                    upload_dup_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_intents_status ON restock_intents(status);
                CREATE INDEX IF NOT EXISTS idx_intents_created ON restock_intents(created_at);

                CREATE TABLE IF NOT EXISTS sku_state (
                    sku_id TEXT PRIMARY KEY,
                    baseline_cost_usd TEXT,
                    last_cost_usd TEXT,
                    last_sell_price_cny TEXT,
                    last_restock_at INTEGER NOT NULL DEFAULT 0,
                    last_stock_16688 INTEGER,
                    last_upstream_available INTEGER,
                    auto_off_shelved INTEGER NOT NULL DEFAULT 0,
                    circuit_broken INTEGER NOT NULL DEFAULT 0,
                    circuit_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders_16688 (
                    trade_no TEXT PRIMARY KEY,
                    goods_no TEXT NOT NULL DEFAULT '',
                    goods_name TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    amount_cny TEXT NOT NULL DEFAULT '0',
                    status INTEGER NOT NULL DEFAULT -1,
                    status_label TEXT NOT NULL DEFAULT '',
                    created_at_ts INTEGER NOT NULL DEFAULT 0,
                    paid_at_ts INTEGER NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS complaints_16688 (
                    complaint_no TEXT PRIMARY KEY,
                    trade_no TEXT NOT NULL DEFAULT '',
                    status INTEGER NOT NULL DEFAULT -1,
                    status_label TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    alerted_status INTEGER NOT NULL DEFAULT -99,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    sku_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS alert_throttle (
                    throttle_key TEXT PRIMARY KEY,
                    last_sent_ts INTEGER NOT NULL,
                    last_level TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
        with self._vault:
            self._vault.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS vault_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    canboso_order_code TEXT NOT NULL DEFAULT '',
                    supplier TEXT NOT NULL,
                    sku_id TEXT NOT NULL,
                    goods_no_16688 TEXT NOT NULL,
                    card_index INTEGER NOT NULL,
                    card_line TEXT NOT NULL,
                    user_field TEXT NOT NULL DEFAULT '',
                    password_field TEXT NOT NULL DEFAULT '',
                    verify_email TEXT NOT NULL DEFAULT '',
                    expiry_text TEXT NOT NULL DEFAULT '',
                    other_info TEXT NOT NULL DEFAULT '',
                    upload_status TEXT NOT NULL DEFAULT 'PENDING_UPLOAD',
                    uploaded_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(idempotency_key, card_index)
                );
                CREATE INDEX IF NOT EXISTS idx_vault_status ON vault_items(upload_status);
                """
            )

    def record_event(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        sku_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        safe_detail = sanitize_order_meta(detail or {})
        with self._ledger:
            self._ledger.execute(
                """
                INSERT INTO events(created_at, level, event_type, sku_id, message, detail_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now_shanghai_str(),
                    level.upper(),
                    event_type,
                    sku_id,
                    message,
                    json.dumps(safe_detail, ensure_ascii=False),
                ),
            )

    def should_send_throttled_alert(
        self,
        throttle_key: str,
        *,
        cooldown_seconds: int,
        now_ts: int | None = None,
    ) -> bool:
        if cooldown_seconds <= 0:
            return True
        current_ts = int(time.time()) if now_ts is None else int(now_ts)
        row = self._ledger.execute(
            "SELECT last_sent_ts FROM alert_throttle WHERE throttle_key=?",
            (throttle_key,),
        ).fetchone()
        if row is None:
            return True
        return (current_ts - int(row["last_sent_ts"])) >= cooldown_seconds

    def mark_throttled_alert_sent(
        self,
        throttle_key: str,
        *,
        level: str,
        message: str,
        now_ts: int | None = None,
    ) -> None:
        current_ts = int(time.time()) if now_ts is None else int(now_ts)
        now_text = now_shanghai_str()
        with self._ledger:
            self._ledger.execute(
                """
                INSERT INTO alert_throttle(throttle_key, last_sent_ts, last_level, last_message, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(throttle_key) DO UPDATE
                SET last_sent_ts=?, last_level=?, last_message=?, updated_at=?
                """,
                (
                    throttle_key,
                    current_ts,
                    level.upper(),
                    message[:500],
                    now_text,
                    current_ts,
                    level.upper(),
                    message[:500],
                    now_text,
                ),
            )

    def create_restock_intent(
        self,
        *,
        idempotency_key: str,
        sku_id: str,
        goods_no_16688: str,
        supplier: str,
        product_id: str,
        product_name: str,
        requested_qty: int,
        estimated_cost_usd: Decimal,
    ) -> None:
        ts = now_shanghai_str()
        with self._ledger:
            self._ledger.execute(
                """
                INSERT INTO restock_intents (
                    idempotency_key, sku_id, goods_no_16688, supplier, product_id, product_name,
                    requested_qty, estimated_cost_usd, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    idempotency_key,
                    sku_id,
                    goods_no_16688,
                    supplier,
                    product_id,
                    product_name,
                    int(requested_qty),
                    str(estimated_cost_usd),
                    ts,
                    ts,
                ),
            )

    def mark_intent_uncertain(self, idempotency_key: str, error_message: str, *, increment_replay: bool = False) -> None:
        with self._ledger:
            if increment_replay:
                self._ledger.execute(
                    """
                    UPDATE restock_intents
                    SET status='UNCERTAIN_REPLAY', replay_attempts=replay_attempts + 1,
                        error_message=?, updated_at=?
                    WHERE idempotency_key=?
                    """,
                    (error_message[:500], now_shanghai_str(), idempotency_key),
                )
            else:
                self._ledger.execute(
                    """
                    UPDATE restock_intents
                    SET status='UNCERTAIN_REPLAY', error_message=?, updated_at=?
                    WHERE idempotency_key=?
                    """,
                    (error_message[:500], now_shanghai_str(), idempotency_key),
                )

    def mark_intent_failed_pre_purchase(self, idempotency_key: str, error_message: str) -> None:
        with self._ledger:
            self._ledger.execute(
                """
                UPDATE restock_intents
                SET status='FAILED_PRE_PURCHASE', error_message=?, updated_at=?
                WHERE idempotency_key=?
                """,
                (error_message[:500], now_shanghai_str(), idempotency_key),
            )

    def save_vault_accounts(
        self,
        *,
        idempotency_key: str,
        canboso_order_code: str,
        supplier: str,
        sku_id: str,
        goods_no_16688: str,
        accounts: Sequence[CanbosoAccountItem],
        card_lines: Sequence[str],
    ) -> int:
        if len(accounts) != len(card_lines):
            raise ValueError("accounts 与 card_lines 数量不一致")
        ts = now_shanghai_str()
        inserted = 0
        with self._vault:
            for idx, (account, card_line) in enumerate(zip(accounts, card_lines)):
                cursor = self._vault.execute(
                    """
                    INSERT OR IGNORE INTO vault_items (
                        idempotency_key, canboso_order_code, supplier, sku_id, goods_no_16688,
                        card_index, card_line, user_field, password_field, verify_email,
                        expiry_text, other_info, upload_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_UPLOAD', ?)
                    """,
                    (
                        idempotency_key,
                        canboso_order_code,
                        supplier,
                        sku_id,
                        goods_no_16688,
                        idx,
                        card_line,
                        account.user,
                        account.password,
                        account.verify_email or "",
                        account.expiry_text or "",
                        account.other_info or "",
                        ts,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def mark_intent_purchased(
        self,
        *,
        idempotency_key: str,
        canboso_order_code: str,
        product_name: str,
        actual_qty: int,
        bonus_qty: int,
        actual_amount: Decimal,
        currency: str,
        amount_usd: Decimal,
        amount_cny: Decimal,
    ) -> None:
        with self._ledger:
            self._ledger.execute(
                """
                UPDATE restock_intents
                SET status='PURCHASED',
                    canboso_order_code=?,
                    product_name=CASE WHEN ?!='' THEN ? ELSE product_name END,
                    actual_qty=?,
                    bonus_qty=?,
                    actual_amount=?,
                    currency=?,
                    amount_usd=?,
                    amount_cny=?,
                    error_message='',
                    updated_at=?
                WHERE idempotency_key=?
                """,
                (
                    canboso_order_code,
                    product_name,
                    product_name,
                    int(actual_qty),
                    int(bonus_qty),
                    str(actual_amount),
                    currency,
                    str(amount_usd),
                    str(amount_cny),
                    now_shanghai_str(),
                    idempotency_key,
                ),
            )

    def mark_intent_upload_failed(self, idempotency_key: str, error_message: str) -> None:
        with self._ledger:
            self._ledger.execute(
                """
                UPDATE restock_intents
                SET status='FAILED_UPLOAD', error_message=?, updated_at=?
                WHERE idempotency_key=?
                """,
                (error_message[:500], now_shanghai_str(), idempotency_key),
            )

    def mark_intent_uploaded(self, idempotency_key: str, *, import_count: int, dup_count: int) -> None:
        ts = now_shanghai_str()
        with self._vault:
            self._vault.execute(
                """
                UPDATE vault_items
                SET upload_status='UPLOADED', uploaded_at=?
                WHERE idempotency_key=? AND upload_status='PENDING_UPLOAD'
                """,
                (ts, idempotency_key),
            )
        with self._ledger:
            self._ledger.execute(
                """
                UPDATE restock_intents
                SET status='UPLOADED',
                    upload_import_count=?,
                    upload_dup_count=?,
                    error_message='',
                    updated_at=?
                WHERE idempotency_key=?
                """,
                (int(import_count), int(dup_count), ts, idempotency_key),
            )

    def get_pending_vault_items(self, idempotency_key: str) -> list[VaultItemRecord]:
        rows = self._vault.execute(
            """
            SELECT id, idempotency_key, canboso_order_code, supplier, sku_id, goods_no_16688,
                   card_index, card_line, upload_status
            FROM vault_items
            WHERE idempotency_key=? AND upload_status='PENDING_UPLOAD'
            ORDER BY card_index ASC
            """,
            (idempotency_key,),
        ).fetchall()
        return [
            VaultItemRecord(
                item_id=int(row["id"]),
                idempotency_key=str(row["idempotency_key"]),
                canboso_order_code=str(row["canboso_order_code"]),
                supplier=str(row["supplier"]),
                sku_id=str(row["sku_id"]),
                goods_no_16688=str(row["goods_no_16688"]),
                card_index=int(row["card_index"]),
                card_line=str(row["card_line"]),
                upload_status=str(row["upload_status"]),
            )
            for row in rows
        ]

    def list_intents_by_statuses(self, statuses: Sequence[str]) -> list[RestockIntentRecord]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self._ledger.execute(
            f"SELECT * FROM restock_intents WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            tuple(statuses),
        ).fetchall()
        return [self._row_to_intent(row) for row in rows]

    def list_completed_purchases(self) -> list[RestockIntentRecord]:
        return self.list_intents_by_statuses(("PURCHASED", "UPLOADED", "FAILED_UPLOAD"))

    def today_spent_usd(self, date_prefix: str | None = None) -> Decimal:
        prefix = date_prefix or today_shanghai_prefix()
        rows = self._ledger.execute(
            """
            SELECT status, estimated_cost_usd, amount_usd
            FROM restock_intents
            WHERE created_at LIKE ? AND status != 'FAILED_PRE_PURCHASE'
            """,
            (f"{prefix}%",),
        ).fetchall()
        total = Decimal("0")
        for row in rows:
            if row["status"] in ("PURCHASED", "UPLOADED", "FAILED_UPLOAD"):
                total += Decimal(str(row["amount_usd"] or "0"))
            else:
                total += Decimal(str(row["estimated_cost_usd"] or "0"))
        return total

    def get_sku_state(self, sku_id: str) -> SkuStateRecord | None:
        row = self._ledger.execute("SELECT * FROM sku_state WHERE sku_id=?", (sku_id,)).fetchone()
        if row is None:
            return None
        return SkuStateRecord(
            sku_id=str(row["sku_id"]),
            baseline_cost_usd=Decimal(row["baseline_cost_usd"]) if row["baseline_cost_usd"] else None,
            last_cost_usd=Decimal(row["last_cost_usd"]) if row["last_cost_usd"] else None,
            last_sell_price_cny=Decimal(row["last_sell_price_cny"]) if row["last_sell_price_cny"] else None,
            last_restock_at=int(row["last_restock_at"] or 0),
            last_stock_16688=int(row["last_stock_16688"]) if row["last_stock_16688"] is not None else None,
            last_upstream_available=int(row["last_upstream_available"])
            if row["last_upstream_available"] is not None
            else None,
            auto_off_shelved=bool(row["auto_off_shelved"]),
            circuit_broken=bool(row["circuit_broken"]),
            circuit_reason=str(row["circuit_reason"] or ""),
            updated_at=str(row["updated_at"]),
        )

    def upsert_sku_state(
        self,
        *,
        sku_id: str,
        baseline_cost_usd: Decimal | None = None,
        last_cost_usd: Decimal | None = None,
        last_sell_price_cny: Decimal | None = None,
        last_restock_at: int | None = None,
        last_stock_16688: int | None = None,
        last_upstream_available: int | None = None,
        auto_off_shelved: bool | None = None,
        circuit_broken: bool | None = None,
        circuit_reason: str | None = None,
    ) -> None:
        existing = self.get_sku_state(sku_id)
        merged_baseline = baseline_cost_usd if baseline_cost_usd is not None else (
            existing.baseline_cost_usd if existing else None
        )
        merged_cost = last_cost_usd if last_cost_usd is not None else (existing.last_cost_usd if existing else None)
        merged_price = (
            last_sell_price_cny if last_sell_price_cny is not None else (existing.last_sell_price_cny if existing else None)
        )
        merged_restock = (
            int(last_restock_at) if last_restock_at is not None else (existing.last_restock_at if existing else 0)
        )
        merged_stock = (
            last_stock_16688 if last_stock_16688 is not None else (existing.last_stock_16688 if existing else None)
        )
        merged_avail = (
            last_upstream_available
            if last_upstream_available is not None
            else (existing.last_upstream_available if existing else None)
        )
        merged_off = (
            auto_off_shelved if auto_off_shelved is not None else (existing.auto_off_shelved if existing else False)
        )
        merged_broken = (
            circuit_broken if circuit_broken is not None else (existing.circuit_broken if existing else False)
        )
        merged_reason = (
            circuit_reason if circuit_reason is not None else (existing.circuit_reason if existing else "")
        )
        with self._ledger:
            self._ledger.execute(
                """
                INSERT INTO sku_state (
                    sku_id, baseline_cost_usd, last_cost_usd, last_sell_price_cny,
                    last_restock_at, last_stock_16688, last_upstream_available,
                    auto_off_shelved, circuit_broken, circuit_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku_id) DO UPDATE SET
                    baseline_cost_usd=excluded.baseline_cost_usd,
                    last_cost_usd=excluded.last_cost_usd,
                    last_sell_price_cny=excluded.last_sell_price_cny,
                    last_restock_at=excluded.last_restock_at,
                    last_stock_16688=excluded.last_stock_16688,
                    last_upstream_available=excluded.last_upstream_available,
                    auto_off_shelved=excluded.auto_off_shelved,
                    circuit_broken=excluded.circuit_broken,
                    circuit_reason=excluded.circuit_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    sku_id,
                    str(merged_baseline) if merged_baseline is not None else None,
                    str(merged_cost) if merged_cost is not None else None,
                    str(merged_price) if merged_price is not None else None,
                    merged_restock,
                    merged_stock,
                    merged_avail,
                    1 if merged_off else 0,
                    1 if merged_broken else 0,
                    merged_reason[:300],
                    now_shanghai_str(),
                ),
            )

    def upsert_16688_order(self, order: dict[str, Any], *, status_label: str) -> None:
        trade_no = str(order.get("trade_no") or "").strip()
        if not trade_no:
            return
        safe_meta = sanitize_order_meta(order)
        with self._ledger:
            self._ledger.execute(
                """
                INSERT INTO orders_16688 (
                    trade_no, goods_no, goods_name, quantity, amount_cny,
                    status, status_label, created_at_ts, paid_at_ts, meta_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_no) DO UPDATE SET
                    goods_no=excluded.goods_no,
                    goods_name=excluded.goods_name,
                    quantity=excluded.quantity,
                    amount_cny=excluded.amount_cny,
                    status=excluded.status,
                    status_label=excluded.status_label,
                    paid_at_ts=excluded.paid_at_ts,
                    meta_json=excluded.meta_json,
                    updated_at=excluded.updated_at
                """,
                (
                    trade_no,
                    str(order.get("goods_no") or ""),
                    str(order.get("goods_name") or order.get("name") or ""),
                    int(order.get("quantity") or order.get("num") or 1),
                    str(
                        order.get("total_amount")
                        or order.get("pay_price")
                        or order.get("total_price")
                        or order.get("goods_total_amount")
                        or order.get("amount")
                        or "0"
                    ),
                    int(order.get("status") if order.get("status") is not None else -1),
                    status_label,
                    int(order.get("create_time") or order.get("created_at") or 0),
                    int(order.get("success_time") or order.get("pay_time") or order.get("paid_at") or 0),
                    json.dumps(safe_meta, ensure_ascii=False),
                    now_shanghai_str(),
                ),
            )

    def record_complaint_and_check_alert(
        self,
        *,
        complaint_no: str,
        trade_no: str,
        status: int,
        status_label: str,
        reason: str,
    ) -> bool:
        """Upserts a 16688 complaint and returns True only if this status needs a fresh alert."""
        ts = now_shanghai_str()
        existing = self._ledger.execute(
            "SELECT alerted_status FROM complaints_16688 WHERE complaint_no=?",
            (complaint_no,),
        ).fetchone()
        should_alert = existing is None or int(existing["alerted_status"]) != int(status)
        with self._ledger:
            if existing is None:
                self._ledger.execute(
                    """
                    INSERT INTO complaints_16688 (
                        complaint_no, trade_no, status, status_label, reason,
                        alerted_status, first_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (complaint_no, trade_no, int(status), status_label, reason[:500], int(status), ts, ts),
                )
            else:
                self._ledger.execute(
                    """
                    UPDATE complaints_16688
                    SET trade_no=?, status=?, status_label=?, reason=?, alerted_status=?, updated_at=?
                    WHERE complaint_no=?
                    """,
                    (trade_no, int(status), status_label, reason[:500], int(status), ts, complaint_no),
                )
        return should_alert

    def summary(self) -> dict[str, Any]:
        intent_rows = self._ledger.execute(
            "SELECT status, COUNT(*) AS cnt FROM restock_intents GROUP BY status"
        ).fetchall()
        vault_rows = self._vault.execute(
            "SELECT upload_status, COUNT(*) AS cnt FROM vault_items GROUP BY upload_status"
        ).fetchall()
        orders_count = self._ledger.execute("SELECT COUNT(*) FROM orders_16688").fetchone()[0]
        open_complaints = self._ledger.execute(
            "SELECT COUNT(*) FROM complaints_16688 WHERE status IN (0, 1)"
        ).fetchone()[0]
        sku_rows = self._ledger.execute("SELECT * FROM sku_state ORDER BY sku_id ASC").fetchall()
        return {
            "today_spent_usd": str(self.today_spent_usd()),
            "intents_by_status": {str(row["status"]): int(row["cnt"]) for row in intent_rows},
            "vault_by_status": {str(row["upload_status"]): int(row["cnt"]) for row in vault_rows},
            "synced_16688_orders": int(orders_count),
            "open_16688_complaints": int(open_complaints),
            "sku_states": [dict(row) for row in sku_rows],
        }

    @staticmethod
    def _row_to_intent(row: sqlite3.Row) -> RestockIntentRecord:
        return RestockIntentRecord(
            idempotency_key=str(row["idempotency_key"]),
            sku_id=str(row["sku_id"]),
            goods_no_16688=str(row["goods_no_16688"]),
            supplier=str(row["supplier"]),
            product_id=str(row["product_id"]),
            product_name=str(row["product_name"]),
            requested_qty=int(row["requested_qty"]),
            estimated_cost_usd=Decimal(str(row["estimated_cost_usd"] or "0")),
            status=str(row["status"]),
            replay_attempts=int(row["replay_attempts"] or 0),
            canboso_order_code=str(row["canboso_order_code"] or ""),
            actual_qty=int(row["actual_qty"] or 0),
            bonus_qty=int(row["bonus_qty"] or 0),
            actual_amount=Decimal(str(row["actual_amount"] or "0")),
            currency=str(row["currency"] or "USD"),
            amount_usd=Decimal(str(row["amount_usd"] or "0")),
            amount_cny=Decimal(str(row["amount_cny"] or "0")),
            upload_import_count=int(row["upload_import_count"] or 0),
            upload_dup_count=int(row["upload_dup_count"] or 0),
            error_message=str(row["error_message"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
