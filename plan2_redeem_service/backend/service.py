from __future__ import annotations

import json
import logging
import math
import re
import secrets
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Optional

from .agiso import (
    AOPIC_AUTO_SEND_SUCCESS,
    AOPIC_BUYER_PAY,
    AOPIC_BUYER_PAY_SIMPLE,
    AOPIC_REFUND_CLOSED,
    AOPIC_REFUND_CREATED,
    AOPIC_REFUND_SUCCESS,
    AgisoError,
    PLAN2_CODE_RE,
    build_create_order_result_content,
    build_redeem_url,
    extract_sent_card_entries,
    format_trade_delivery_message,
    match_agiso_sku,
    parse_env_restock_rules,
    parse_env_sku_rules,
    rule_has_matcher,
)
from .config import Settings
from .db import Ledger, now_iso
from .gateways import UpstreamError, UpstreamTimeout, parse_env_channel16688_restock_rules
from .telegram_forwarder import build_telegram_forwarder, sanitize_tg_error


logger = logging.getLogger(__name__)

CHANNEL16688_OPENAPI_PAUSED_KEY = "channel16688:restock-openapi-paused"
CHANNEL16688_OPENAPI_NEGATED_PAUSE_RE = re.compile(
    r"(?:未|没|没有|不|非)\s*(?:暂停|关闭|停用|停服|下线)\s*open\s*api|"
    r"open\s*api\s*(?:接口|服务)?\s*(?:未|没|没有|不|非)\s*(?:暂停|关闭|停用|停服|下线)|"
    r"open\s*api\s*(?:接口|服务)?\s*已恢复|已恢复\s*open\s*api",
    re.IGNORECASE,
)
CHANNEL16688_OPENAPI_PAUSED_RE = re.compile(
    r"(?:暂停|关闭|停用|停服|下线|停止|不再恢复|暂不开放)\s*open\s*api|"
    r"open\s*api\s*(?:接口|服务)?\s*(?:已|暂时|临时|短时间|目前|当前)?\s*(?:暂停|关闭|停用|停服|下线|维护|不可用|暂不可用|不再恢复|停止|暂不开放|超负载)|"
    r"超负载.*open\s*api|open\s*api.*超负载",
    re.IGNORECASE,
)
CHANNEL16688_TRADE_NO_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
CANBOSO_CANCELLED_STATUSES = {"cancelled", "canceled", "refunded"}

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._%+-]{0,62}[A-Za-z0-9])?@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9](?:[A-Za-z0-9-]{0,57}[A-Za-z0-9])?)\Z",
    re.IGNORECASE,
)


def is_valid_email(value: str) -> bool:
    if not value or len(value) > 254 or "@" not in value:
        return False
    local_part, _ = value.split("@", 1)
    if ".." in local_part:
        return False
    return bool(EMAIL_RE.match(value))


IN_STOCK_SQL = "status='unused' AND COALESCE(TRIM(external_order_id), '') = ''"
NON_IN_STOCK_SQL = f"NOT ({IN_STOCK_SQL})"
DISPATCHED_UNUSED_SQL = "status='unused' AND COALESCE(TRIM(external_order_id), '') <> ''"
PENDING_WARRANTY_ACK_SQL = (
    "status='completed' AND sku_key='ultra_exclusive_admin' "
    "AND warranty_json IS NOT NULL AND json_valid(warranty_json) = 1 "
    "AND json_extract(warranty_json, '$.status') = 'submitted'"
)
MANUAL_ATTENTION_SQL = (
    f"(status IN ('needs_manual_review', 'delivered_pending_warranty') OR ({PENDING_WARRANTY_ACK_SQL}))"
)
COMPLETED_ADMIN_SQL = f"(status='completed' AND NOT ({PENDING_WARRANTY_ACK_SQL}))"
SKUS = {
    "ultra_shared_slot": {"title": "Google Ultra x20 共享车位", "product_type": "slot", "product_id": "6ac26aaeec73a42d6430a837", "reference_price_usd": 13.47, "max_cost_usd": 16.0},
    "ultra_exclusive_admin": {"title": "Google Ultra x20 独享整组（管理员版）", "product_type": "account", "product_id": "6ae03419c0080f9e15f695a8", "reference_price_usd": 67.50, "max_cost_usd": 72.0},
}


def normalize_display_email(value: Optional[str]) -> Optional[str]:
    cleaned = (value or "").strip().lower()
    return cleaned if "@" in cleaned else None


def mask_email(value: Optional[str]) -> Optional[str]:
    return normalize_display_email(value)


def format_warranty_forward_template(order_code: str, admin_email: str) -> str:
    clean_order = (order_code or "").strip()
    clean_email = (admin_email or "").strip()
    order_line = f"• Mã đơn: {clean_order}".rstrip()
    email_line = f"• Gmail chủ nhóm: {clean_email}".rstrip()
    return (
        "Chào shop ạ,\n"
        "Em gửi thông tin đăng ký bảo hành gói Family:\n"
        "\n"
        f"{order_line}\n"
        f"{email_line}\n"
        "\n"
        "Em đã gửi lời mời vào nhóm Family cho tài khoản shop.\n"
        "Nhờ shop kiểm tra và ghi nhận bảo hành giúp em nhé.\n"
        "Cảm ơn shop nhiều ạ!"
    )


class Mailer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sent: list[dict[str, Any]] = []

    def send(self, subject: str, body: str, attachment: Optional[tuple[str, bytes, str]] = None) -> None:
        if not self.settings.merchant_email:
            return
        if self.settings.execution_mode == "dry_run":
            self.sent.append({"subject": subject, "body": body, "attachment": bool(attachment)})
            return
        if not self.settings.smtp_host:
            raise RuntimeError("SMTP is not configured")
        import smtplib
        import ssl
        from_addr = self.settings.smtp_from or self.settings.smtp_username
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = self.settings.merchant_email
        msg["Subject"] = subject
        msg.set_content(body, subtype="plain", charset="utf-8")
        if attachment:
            filename, data, content_type = attachment
            maintype, subtype = content_type.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        context = ssl.create_default_context()
        if self.settings.smtp_port == 465:
            with smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port, timeout=20, context=context) as smtp:
                if self.settings.smtp_username:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(msg, from_addr=from_addr, to_addrs=[self.settings.merchant_email])
            return
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            if self.settings.smtp_username:
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
            smtp.send_message(msg, from_addr=from_addr, to_addrs=[self.settings.merchant_email])


class Service:
    POLL_FAILURE_THRESHOLD = 3

    def __init__(
        self,
        ledger: Ledger,
        settings: Settings,
        canboso: Any,
        channel: Any,
        mailer: Mailer,
        agiso: Any = None,
        tg_forwarder: Any = None,
    ):
        self.ledger, self.settings, self.canboso, self.channel, self.mailer, self.agiso = ledger, settings, canboso, channel, mailer, agiso
        if tg_forwarder is not None:
            self.tg_forwarder = tg_forwarder
        else:
            self.tg_forwarder = build_telegram_forwarder(settings)
        self._restock_lock = threading.Lock()
        self._channel16688_restock_lock = threading.Lock()
        self._warranty_locks_guard = threading.Lock()
        self._warranty_locks: dict[str, threading.Lock] = {}
        ledger.seed_skus(SKUS)

    def issue(self, sku_key: str, channel: str, external_order_id: str = "", remark: str = "", batch_id: Optional[str] = None, goods_no: Optional[str] = None) -> dict[str, Any]:
        sku = self.ledger.get_sku(sku_key)
        if not sku:
            raise ValueError("UNKNOWN_SKU")
        code = self._new_code(sku_key)
        t = now_iso()
        self.ledger.insert_code({"code": code, "sku_key": sku_key, "channel": channel, "batch_id": batch_id, "goods_no": goods_no or sku.get("goods_no"), "external_order_id": external_order_id, "remark": remark, "status": "unused", "created_at": t, "updated_at": t})
        return self._code_row(code)

    def _generate_unique_codes(self, sku_key: str, count: int) -> list[str]:
        codes: list[str] = []
        seen: set[str] = set()
        while len(codes) < count:
            needed = count - len(codes)
            candidates: list[str] = []
            while len(candidates) < needed:
                candidate = self._new_code(sku_key)
                if candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
            placeholders = ",".join("?" for _ in candidates)
            with closing(self.ledger.connect()) as conn:
                existing = {
                    row["code"]
                    for row in conn.execute(
                        f"SELECT code FROM codes WHERE code IN ({placeholders})",
                        tuple(candidates),
                    )
                }
            for candidate in candidates:
                if candidate not in existing:
                    codes.append(candidate)
        return codes

    def _persist_batch(
        self,
        batch_id: str,
        sku_key: str,
        channel: str,
        goods_no: Optional[str],
        label: str,
        codes: list[str],
        remark: str = "",
        audit_reason: str = "issued",
    ) -> None:
        timestamp = now_iso()
        with self.ledger.transaction(immediate=True) as conn:
            conn.executemany(
                """INSERT INTO codes(
                    code, sku_key, channel, batch_id, goods_no,
                    external_order_id, remark, status, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [
                    (code, sku_key, channel, batch_id, goods_no, "", remark, "unused", timestamp, timestamp)
                    for code in codes
                ],
            )
            conn.executemany(
                "INSERT INTO audit_events(code,from_status,to_status,reason,created_at) VALUES(?,?,?,?,?)",
                [(code, None, "unused", audit_reason, timestamp) for code in codes],
            )
            conn.execute(
                "INSERT INTO batches(batch_id,sku_key,channel,goods_no,batch_label,count,created_at) VALUES(?,?,?,?,?,?,?)",
                (batch_id, sku_key, channel, goods_no, label, len(codes), timestamp),
            )

    def batch(self, sku_key: str, channel: str, count: int, goods_no: Optional[str], label: str) -> dict[str, Any]:
        if count < 1 or count > 500:
            raise ValueError("COUNT_OUT_OF_RANGE")
        sku = self.ledger.get_sku(sku_key)
        if not sku:
            raise ValueError("UNKNOWN_SKU")
        batch_id = "B" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2).upper()
        trimmed_goods = (goods_no or "").strip() or None
        sku_goods = (sku.get("goods_no") or "").strip() or None
        resolved_goods = trimmed_goods or sku_goods
        if channel == "16688" and not resolved_goods:
            raise ValueError("GOODS_NO_REQUIRED")
        codes = self._generate_unique_codes(sku_key, count)
        if channel == "16688" and resolved_goods:
            self._persist_batch(
                batch_id, sku_key, channel, resolved_goods, label, codes,
                remark=f"16688 导入待核对库存(batch={batch_id})",
                audit_reason="issued_16688_import_uncertain",
            )
            content = "\n\n".join(
                f"提货链接：{build_redeem_url(self.settings.public_base_url, code)}\n兑换码：{code}"
                for code in codes
            )
            try:
                imported = self.channel.stock_secret_add(resolved_goods, content)
                if (
                    not isinstance(imported, dict)
                    or imported.get("goods_no") != resolved_goods
                    or self._parse_16688_int(imported.get("import_count")) != count
                    or self._parse_16688_int(imported.get("stock_dup_count")) != 0
                ):
                    raise UpstreamError(502, "16688 未确认完整导入，须核对本批次库存", rejected=False)
            except UpstreamTimeout:
                self._mark_channel16688_batch_import(batch_id, "16688 导入超时待核对库存")
                raise
            except UpstreamError as exc:
                self._mark_channel16688_batch_import(
                    batch_id, "16688 导入结果待核对库存", rejected=exc.rejected and (
                        "仅卡密商品支持导入库存" in exc.message
                        or exc.status_code == 400 and "goods_no invalid" in exc.message
                    ),
                )
                raise
            self._mark_channel16688_batch_import(batch_id, "")
        else:
            self._persist_batch(batch_id, sku_key, channel, resolved_goods, label, codes)
        # Live 16688 batches are delivered exclusively by the platform.
        export_codes = [] if channel == "16688" and self.settings.execution_mode == "live" else codes
        return {
            "batch_id": batch_id,
            "sku_key": sku_key,
            "channel": channel,
            "count": count,
            "codes": export_codes,
            "export_lines_code_only": "\n".join(export_codes),
            "export_lines_with_url": "\n".join(
                f"提货链接：{build_redeem_url(self.settings.public_base_url, code)} | 兑换码：{code}"
                for code in export_codes
            ),
        }

    def _mark_channel16688_batch_import(
        self, batch_id: str, uncertain_remark: str, *, rejected: bool = False,
    ) -> None:
        with self.ledger.transaction(immediate=True) as conn:
            if rejected:
                if conn.execute(
                    """SELECT 1 FROM codes WHERE batch_id=?
                       AND (status!='unused' OR COALESCE(external_order_id,'')!='') LIMIT 1""",
                    (batch_id,),
                ).fetchone():
                    raise UpstreamError(502, "16688 拒绝响应与已发生的兑换或发货状态冲突", rejected=False)
                conn.execute("DELETE FROM audit_events WHERE code IN (SELECT code FROM codes WHERE batch_id=?)", (batch_id,))
                conn.execute("DELETE FROM codes WHERE batch_id=?", (batch_id,))
                conn.execute("DELETE FROM batches WHERE batch_id=?", (batch_id,))
            elif uncertain_remark:
                conn.execute(
                    "UPDATE codes SET remark=? WHERE batch_id=? AND status='unused'",
                    (f"{uncertain_remark}(batch={batch_id})", batch_id),
                )
            else:
                conn.execute(
                    "UPDATE codes SET remark='' WHERE batch_id=? AND status='unused' AND remark=?",
                    (batch_id, f"16688 导入待核对库存(batch={batch_id})"),
                )
                conn.execute(
                    """UPDATE audit_events SET reason='issued' WHERE reason='issued_16688_import_uncertain'
                       AND code IN (SELECT code FROM codes WHERE batch_id=?)""",
                    (batch_id,),
                )

    def redeem(
        self, code: str, buyer_email: Optional[str], confirm: Optional[str], preflight: bool,
    ) -> tuple[dict[str, Any], Optional[str]]:
        row = self.ledger.get_code(code)
        if not row:
            return {}, "CODE_NOT_FOUND"
        if row["status"] in {"completed", "waiting_fulfillment", "delivered_pending_warranty"}:
            return self.public_row(row), None
        if row["status"] == "needs_manual_review":
            return self.public_row(row), None
        if row["status"] == "revoked":
            return {}, "CODE_REVOKED"
        if row["status"] == "purchasing":
            return {}, "CONCURRENT_IN_PROGRESS"
        sku = self.ledger.get_sku(row["sku_key"])
        if not preflight:
            return {}, "PREFLIGHT_REQUIRED"
        payload: dict[str, Any] = {"product_id": sku["product_id"], "quantity": 1}
        normalized: Optional[str] = None
        if sku["product_type"] == "slot":
            normalized = (buyer_email or "").strip().lower()
            if not is_valid_email(normalized):
                return {}, "INVALID_EMAIL"
            if confirm is not None and confirm.strip() != "" and normalized != confirm.strip().lower():
                return {}, "EMAIL_MISMATCH"
            payload["customer_email"] = normalized
        trade_no = ""
        marketplace_unavailable = False
        verification_revision = 0
        if row["channel"] == "16688" and not self._is_confirmed_16688_replacement(row):
            order_goods = row.get("goods_no") or sku.get("goods_no")
            if not order_goods:
                return {}, "ORDER_NOT_ELIGIBLE"
            verification_revision = self.ledger.channel16688_verification_revision(code)
            trade_no, marketplace_unavailable, error = self._verify_16688_redemption(code, order_goods)
            if error:
                return {}, error
        state, claimed = self.ledger.claim_for_purchase(
            code, payload, normalized, mask_email(normalized),
            verified_trade_no=trade_no, marketplace_unavailable=marketplace_unavailable,
            verification_revision=verification_revision,
        )
        if state != "claimed":
            return {}, {
                "busy": "CONCURRENT_IN_PROGRESS",
                "spent": "CODE_REVOKED",
                "ineligible": "ORDER_NOT_ELIGIBLE",
                "unconfirmed_stock": "CHANNEL16688_STOCK_IMPORT_PENDING",
            }.get(state, "CODE_NOT_FOUND")
        if marketplace_unavailable:
            self._alert_16688_paused()
        try:
            preflight_ok, reason, cost = self.upstream_preflight(sku)
            error_message = self._preflight_error_message(reason, sku, cost)
        except (UpstreamTimeout, UpstreamError, ValueError, TypeError) as exc:
            logger.warning("upstream preflight failed code=%s sku=%s error_type=%s detail=%s", code, sku["sku_key"], type(exc).__name__, exc)
            preflight_ok, reason, cost = False, "UPSTREAM_ERROR", 0
            error_message = self._format_upstream_exception("上游预检失败", exc)
        if not preflight_ok:
            self.ledger.transition(code, "needs_manual_review", reason, {"last_error_code": reason, "last_error_message": error_message}, ("purchasing",))
            self.alert(code, reason)
            return self.public_row(self.ledger.get_code(code)), None
        return self._purchase(code, claimed, sku, cost), None

    def _is_confirmed_16688_replacement(self, row: dict[str, Any]) -> bool:
        return bool(row.get("external_order_id")) and self.ledger.has_16688_purchase_evidence(
            row["code"], "16688_confirmed_sale_replacement_issued",
        )

    def _verify_16688_redemption(self, code: str, goods_no: str) -> tuple[str, bool, str]:
        try:
            order = self.channel.verify_order(code, goods_no)
        except (UpstreamError, UpstreamTimeout) as exc:
            if self._channel16688_unavailable(exc):
                return "", True, ""
            return "", False, "CHANNEL16688_VERIFICATION_FAILED"
        if order.get("error") == "ORDER_NOT_ELIGIBLE":
            if order.get("final_rejection"):
                row = self.ledger.get_code(code)
                if row and row["status"] in {"unused", "needs_manual_review"}:
                    self.ledger.transition(
                        code, row["status"], "16688_platform_sale_rejected", allowed=(row["status"],),
                    )
            return "", False, "ORDER_NOT_ELIGIBLE"
        trade_no = str(order.get("trade_no") or "").strip()
        if order.get("ok") and trade_no:
            return trade_no, False, ""
        return "", False, "CHANNEL16688_VERIFICATION_FAILED"

    def upstream_preflight(self, sku: dict[str, Any], override: bool = False) -> tuple[bool, str, float]:
        catalog = self.canboso.list_products()
        item = next((p for p in catalog.get("products", []) if p.get("productId") == sku["product_id"]), None)
        if not item:
            return False, "UPSTREAM_ERROR", 0
        available = (item.get("availability") or {}).get("available")
        if available == 0:
            return False, "UPSTREAM_OUT_OF_STOCK", 0
        price = float((item.get("price") or {}).get("amount", 0))
        if price <= 0 or (not override and price > float(sku["max_cost_usd"])):
            return False, "PRICE_LIMIT_EXCEEDED", price
        balance_data = self.canboso.get_balance()
        balance = float(balance_data.get("balanceUsd", balance_data.get("balance", 0)))
        if balance < price:
            return False, "LOW_BALANCE", price
        return True, "OK", price

    def _purchase(self, code: str, row: dict[str, Any], sku: dict[str, Any], cost: float) -> dict[str, Any]:
        try:
            result = self.canboso.purchase(row["idempotency_key"], json.loads(row["request_payload_json"]))
        except UpstreamTimeout as exc:
            logger.warning("upstream purchase timeout code=%s sku=%s detail=%s", code, sku["sku_key"], exc)
            self.ledger.transition(code, "needs_manual_review", "upstream_timeout", {"last_error_code": "UPSTREAM_TIMEOUT", "last_error_message": self._format_upstream_exception("上游下单超时", exc)}, ("purchasing",))
            self.alert(code, "UPSTREAM_ERROR")
            return self.public_row(self.ledger.get_code(code))
        except UpstreamError as e:
            logger.warning("upstream purchase error code=%s sku=%s status=%s detail=%s", code, sku["sku_key"], e.status_code, e.message)
            self.ledger.transition(code, "needs_manual_review", "upstream_error", {"last_error_code": f"HTTP_{e.status_code}", "last_error_message": e.message}, ("purchasing",))
            self.alert(code, "UPSTREAM_ERROR")
            return self.public_row(self.ledger.get_code(code))
        order = result.get("order") or {}
        if not order.get("orderCode"):
            self.ledger.transition(code, "needs_manual_review", "upstream_purchase_unconfirmed", {"last_error_code": "UPSTREAM_ERROR", "last_error_message": "Canboso 未返回订单号，需核对原请求"}, ("purchasing",))
            self.alert(code, "UPSTREAM_ERROR")
            return self.public_row(self.ledger.get_code(code))
        payment = result.get("payment") or {}
        confirmed_cost = self._canboso_usd_amount(payment.get("amountUsd"))
        if confirmed_cost is None and str(payment.get("currency")).upper() in {"USD", "USDT"}:
            confirmed_cost = self._canboso_usd_amount(payment.get("amount"))
        status = str(order.get("status") or "").strip().lower()
        updates: dict[str, Any] = {
            "upstream_order_code": order["orderCode"],
            "upstream_status": status,
            "cost_usd": cost if confirmed_cost is None else confirmed_cost,
        }
        if status in CANBOSO_CANCELLED_STATUSES:
            target, reason = "needs_manual_review", "canboso_purchase_cancelled"
            updates.update(self._canboso_cancellation(status))
        elif sku["product_type"] == "account":
            delivery = self._safe_delivery(result.get("delivery") or {})
            if status in {"completed", "delivered"} or self._has_delivery_credentials(delivery):
                updates.update({
                    "delivery_json": json.dumps(delivery, ensure_ascii=False),
                    "completed_at": None,
                    "last_error_code": None,
                    "last_error_message": None,
                })
                target, reason = "delivered_pending_warranty", "exclusive_delivered"
            else:
                updates.update({"last_error_code": None, "last_error_message": None})
                target, reason = "waiting_fulfillment", "exclusive_order_accepted"
        else:
            updates.update({"last_error_code": None, "last_error_message": None})
            target, reason = "waiting_fulfillment", "shared_order_accepted"
        if not self.ledger.transition(code, target, reason, updates, ("purchasing",)):
            logger.error("Canboso accepted purchase but order could not be recorded")
        return self.public_row(self.ledger.get_code(code))

    def replay(
        self, code: str, override: bool = False, note: str = "", merchant_verified_order_no: str = "",
        merchant_verified_recipient_email: str = "",
        force_new_order: bool = False,
        new_buyer_email: str = "",
    ) -> tuple[dict[str, Any], Optional[str]]:
        row = self.ledger.get_code(code)
        if not row or row["status"] != "needs_manual_review":
            return (self.admin_detail(row) if row else {}), "RETRY_NOT_AVAILABLE"
        if force_new_order:
            if self.ledger.is_disputed_16688_code(code, row.get("last_error_code")):
                return self.admin_detail(row), "RETRY_NOT_AVAILABLE"
            if row.get("upstream_order_code"):
                upstream_state = str(row.get("upstream_status") or "").strip().lower()
                if upstream_state not in CANBOSO_CANCELLED_STATUSES:
                    return self.admin_detail(row), "UPSTREAM_ORDER_ACTIVE"
        else:
            if row.get("upstream_order_code") or not row.get("idempotency_key") or not row.get("request_payload_json"):
                return self.admin_detail(row), "RETRY_NOT_AVAILABLE"
        manual_trade_no = merchant_verified_order_no.strip()
        if manual_trade_no and (row["channel"] != "16688" or not CHANNEL16688_TRADE_NO_RE.fullmatch(manual_trade_no)):
            return self.admin_detail(row), "ORDER_NOT_ELIGIBLE"
        sku = self.ledger.get_sku(row["sku_key"])
        orig_payload: dict[str, Any] = {}
        if row.get("request_payload_json"):
            try:
                loaded = json.loads(row["request_payload_json"])
                if isinstance(loaded, dict):
                    orig_payload = loaded
            except ValueError:
                orig_payload = {}
        target_email: Optional[str] = None
        if sku["product_type"] == "slot":
            if force_new_order and new_buyer_email.strip():
                target_email = new_buyer_email.strip().lower()
                if not is_valid_email(target_email):
                    return self.admin_detail(row), "INVALID_EMAIL"
            else:
                target_email = str(orig_payload.get("customer_email") or row.get("buyer_email") or "").strip().lower() or None
                if force_new_order and not is_valid_email(target_email or ""):
                    return self.admin_detail(row), "INVALID_EMAIL"
        if row["channel"] == "16688" and sku["product_type"] == "slot":
            recipient = merchant_verified_recipient_email.strip().lower()
            if not recipient or recipient != target_email:
                return self.admin_detail(row), "MERCHANT_RECIPIENT_CONFIRMATION_REQUIRED"
        try:
            ok, reason, cost = self.upstream_preflight(sku, override)
            error_message = self._preflight_error_message(reason, sku, cost)
        except (UpstreamTimeout, UpstreamError, ValueError, TypeError) as exc:
            logger.warning("admin retry preflight failed code=%s sku=%s error_type=%s detail=%s", code, sku["sku_key"], type(exc).__name__, exc)
            ok, reason, cost = False, "UPSTREAM_ERROR", 0
            error_message = self._format_upstream_exception("重试预检失败", exc)
        if not ok:
            self.ledger.transition(code, "needs_manual_review", f"admin_retry_failed:{reason}", {"last_error_code": reason, "last_error_message": error_message}, ("needs_manual_review",))
            self.alert(code, reason)
            return self.admin_detail(self.ledger.get_code(code)), reason
        verified_trade_no = ""
        manually_verified = False
        live_verified = False
        verification_revision = 0
        if row["channel"] == "16688" and not self._is_confirmed_16688_replacement(row):
            order_goods = row.get("goods_no") or sku.get("goods_no")
            if not order_goods:
                self.ledger.transition(
                    code,
                    "needs_manual_review",
                    "admin_retry_failed:ORDER_NOT_ELIGIBLE",
                    {"last_error_code": "ORDER_NOT_ELIGIBLE", "last_error_message": "16688 商品编号未配置"},
                    ("needs_manual_review",),
                )
                self.alert(code, "ORDER_NOT_ELIGIBLE")
                return self.admin_detail(self.ledger.get_code(code)), "ORDER_NOT_ELIGIBLE"
            verification_revision = self.ledger.channel16688_verification_revision(code)
            verified_trade_no, unavailable, verify_err = self._verify_16688_redemption(code, order_goods)
            live_verified = bool(verified_trade_no)
            if unavailable:
                self._alert_16688_paused()
                verified_trade_no = manual_trade_no
                manually_verified = bool(manual_trade_no)
                has_prior_evidence = (
                    self.ledger.has_16688_purchase_evidence(code, "16688_openapi_unavailable_code_purchase")
                    or self.ledger.has_16688_purchase_evidence(code, "channel16688_order_verified")
                )
                if not manually_verified and not has_prior_evidence:
                    verify_err = "CHANNEL16688_OPENAPI_PAUSED"
            if verify_err:
                verify_err = "UPSTREAM_ERROR" if verify_err == "CHANNEL16688_VERIFICATION_FAILED" else verify_err
                verify_msg = {
                    "ORDER_NOT_ELIGIBLE": "16688 订单不符合领货条件",
                    "CHANNEL16688_OPENAPI_PAUSED": "16688 接口暂不可用，请先到商家后台核验旧订单",
                }.get(verify_err, "16688 核验失败")
                self.ledger.transition(
                    code,
                    "needs_manual_review",
                    f"admin_retry_failed:{verify_err}",
                    {"last_error_code": verify_err, "last_error_message": verify_msg},
                    ("needs_manual_review",),
                )
                if verify_err != "CHANNEL16688_OPENAPI_PAUSED":
                    self.alert(code, verify_err)
                return self.admin_detail(self.ledger.get_code(code)), verify_err
            if live_verified and manual_trade_no and manual_trade_no != verified_trade_no:
                return self.admin_detail(row), "ORDER_NOT_ELIGIBLE"
        elif row["channel"] == "16688" and manual_trade_no and manual_trade_no != row["external_order_id"]:
            return self.admin_detail(row), "ORDER_NOT_ELIGIBLE"
        if force_new_order:
            audit_reason = "admin_reorder_16688_merchant_verified" if manually_verified else "admin_reorder"
            prev_order = str(row.get("upstream_order_code") or "").strip()
            if prev_order:
                audit_reason += f"[prev={prev_order}]"
            old_email = str(row.get("buyer_email") or "").strip().lower()
            if target_email and old_email and target_email != old_email:
                audit_reason += f"[email:{old_email}->{target_email}]"
        else:
            audit_reason = "admin_retry_16688_merchant_verified" if manually_verified else "admin_retry"
        if note.strip():
            audit_reason += f":{note.strip()[:200]}"
        if force_new_order:
            new_payload: dict[str, Any] = {"product_id": sku["product_id"], "quantity": 1}
            if sku["product_type"] == "slot" and target_email:
                new_payload["customer_email"] = target_email
            new_idem_key = f"redeem:{code}:reorder:{uuid.uuid4().hex[:12]}"
            resolved_buyer_email = target_email if sku["product_type"] == "slot" else row.get("buyer_email")
            resolved_buyer_masked = mask_email(resolved_buyer_email) if resolved_buyer_email else row.get("buyer_email_masked")
            claim_error = self.ledger.claim_purchase_retry(
                code,
                row.get("idempotency_key") or "",
                audit_reason,
                verified_trade_no,
                live_verified=live_verified,
                verification_revision=verification_revision,
                force_new_order=True,
                new_idempotency_key=new_idem_key,
                new_payload_json=json.dumps(new_payload, ensure_ascii=False),
                new_buyer_email=resolved_buyer_email,
                new_buyer_email_masked=resolved_buyer_masked,
                cancelled_statuses=tuple(CANBOSO_CANCELLED_STATUSES),
            )
        else:
            claim_error = self.ledger.claim_purchase_retry(
                code, row["idempotency_key"], audit_reason, verified_trade_no, live_verified=live_verified,
                verification_revision=verification_revision,
            )
        if claim_error:
            return self.admin_detail(self.ledger.get_code(code)), claim_error
        self._purchase(code, self.ledger.get_code(code), sku, cost)
        updated = self.ledger.get_code(code)
        if updated["status"] == "needs_manual_review" or not updated["upstream_order_code"]:
            error = {
                "UPSTREAM_TIMEOUT": "UPSTREAM_TIMEOUT",
                "CANBOSO_ORDER_CANCELLED": "CANBOSO_ORDER_CANCELLED",
            }.get(updated["last_error_code"], "CANBOSO_PURCHASE_UNCONFIRMED")
            return self.admin_detail(updated), error
        return self.admin_detail(updated), None

    def poll(self, code: str, *, allow_manual_review: bool = False) -> dict[str, Any]:
        row = self.ledger.get_code(code)
        if not row:
            return {}
        is_manual_poll = (
            allow_manual_review
            and row["status"] == "needs_manual_review"
            and bool(row.get("upstream_order_code"))
        )
        if row["status"] != "waiting_fulfillment" and not is_manual_poll:
            return self.public_row(row)
        if row["status"] == "waiting_fulfillment" and row.get("upstream_status") in CANBOSO_CANCELLED_STATUSES:
            return self._record_canboso_cancellation(code, row["upstream_status"])
        is_over_24h = False
        if row["status"] == "waiting_fulfillment" and row.get("submitted_at"):
            try:
                is_over_24h = datetime.fromisoformat(row["submitted_at"]) < datetime.now(timezone.utc).astimezone() - timedelta(hours=24)
            except ValueError:
                is_over_24h = False
        if not row.get("upstream_order_code"):
            if is_over_24h:
                self.ledger.transition(
                    code,
                    "needs_manual_review",
                    "fulfillment_over_24h",
                    {"last_error_code": "FULFILLMENT_TIMEOUT", "last_error_message": "上游履约超过 24 小时且缺少上游单号，请人工核对"},
                    ("waiting_fulfillment",),
                )
                self.alert(code, "FULFILLMENT_TIMEOUT")
                return self.public_row(self.ledger.get_code(code))
            return self.public_row(row)
        try:
            response = self.canboso.get_order(row["upstream_order_code"])
        except (UpstreamTimeout, UpstreamError) as exc:
            err_text = self._format_upstream_exception("查询上游订单失败", exc)
            if is_over_24h:
                self.ledger.transition(
                    code,
                    "needs_manual_review",
                    "fulfillment_over_24h",
                    {"last_error_code": "FULFILLMENT_TIMEOUT", "last_error_message": err_text, "last_polled_at": now_iso()},
                    ("waiting_fulfillment",),
                )
                self.alert(code, "FULFILLMENT_TIMEOUT")
                return self.public_row(self.ledger.get_code(code))
            if is_manual_poll:
                logger.warning("manual upstream poll failed code=%s detail=%s", code, err_text)
                self.ledger.touch_poll(code, {"last_polled_at": now_iso()}, allowed=("needs_manual_review",))
                return self.public_row(self.ledger.get_code(code))
            return self._handle_poll_failure(code, err_text)
        except Exception as exc:
            err_text = f"查询上游订单异常：{type(exc).__name__}"
            if is_over_24h:
                self.ledger.transition(
                    code,
                    "needs_manual_review",
                    "fulfillment_over_24h",
                    {"last_error_code": "FULFILLMENT_TIMEOUT", "last_error_message": err_text, "last_polled_at": now_iso()},
                    ("waiting_fulfillment",),
                )
                self.alert(code, "FULFILLMENT_TIMEOUT")
                return self.public_row(self.ledger.get_code(code))
            if is_manual_poll:
                logger.warning("manual upstream poll failed code=%s detail=%s", code, err_text)
                self.ledger.touch_poll(code, {"last_polled_at": now_iso()}, allowed=("needs_manual_review",))
                return self.public_row(self.ledger.get_code(code))
            return self._handle_poll_failure(code, err_text)
        item = (response.get("orders") or [{}])[0]
        status = str(item.get("status") or "").lower()
        updates: dict[str, Any] = {"upstream_status": status, "last_polled_at": now_iso()}
        confirmed_cost = self._canboso_usd_amount((item.get("payment") or {}).get("amountUsd"))
        if confirmed_cost is not None:
            updates["cost_usd"] = confirmed_cost
        if status in CANBOSO_CANCELLED_STATUSES:
            if is_manual_poll:
                updates.update(self._canboso_cancellation(status))
                self.ledger.transition(code, "needs_manual_review", "admin_poll_cancelled", updates, ("needs_manual_review",))
                return self.public_row(self.ledger.get_code(code))
            return self._record_canboso_cancellation(code, status, updates)
        if status in {"completed", "delivered"} or item.get("deliveredAt"):
            sku = self.ledger.get_sku(row["sku_key"])
            if sku and sku["product_type"] == "account":
                if not row.get("delivery_json"):
                    delivery = self._safe_delivery(item.get("delivery") or item)
                    updates["delivery_json"] = json.dumps(delivery, ensure_ascii=False)
                has_warranty = bool(row.get("warranty_json"))
                target = "completed" if has_warranty else "delivered_pending_warranty"
                updates.update({
                    "completed_at": (row.get("completed_at") or now_iso()) if has_warranty else None,
                    "last_error_code": None,
                    "last_error_message": None,
                })
                reason = "admin_poll_completed" if is_manual_poll else "exclusive_delivered"
                self.ledger.transition(code, target, reason, updates, ("waiting_fulfillment", "needs_manual_review"))
            else:
                updates.update({
                    "completed_at": now_iso(),
                    "delivery_json": None,
                    "last_error_code": None,
                    "last_error_message": None,
                })
                reason = "admin_poll_completed" if is_manual_poll else "shared_fulfillment_completed"
                self.ledger.transition(code, "completed", reason, updates, ("waiting_fulfillment", "needs_manual_review"))
            return self.public_row(self.ledger.get_code(code))
        if is_over_24h:
            updates.update({
                "last_error_code": "FULFILLMENT_TIMEOUT",
                "last_error_message": "上游履约超过 24 小时仍未完成，请核对上游订单",
            })
            self.ledger.transition(code, "needs_manual_review", "fulfillment_over_24h", updates, ("waiting_fulfillment",))
            self.alert(code, "FULFILLMENT_TIMEOUT")
            return self.public_row(self.ledger.get_code(code))
        prev_status = str(row.get("upstream_status") or "").strip().lower()
        state_changed = (status != prev_status) or (confirmed_cost is not None and confirmed_cost != row.get("cost_usd"))
        recovered_from_error = bool(row.get("last_error_code"))
        if is_manual_poll:
            if state_changed:
                self.ledger.transition(code, "needs_manual_review", "admin_poll_sync", updates, ("needs_manual_review",))
            else:
                self.ledger.touch_poll(code, updates, allowed=("needs_manual_review",))
        else:
            if recovered_from_error:
                updates.update({"last_error_code": None, "last_error_message": None})
            if state_changed or recovered_from_error:
                poll_reason = "exclusive_poll" if row["sku_key"] == "ultra_exclusive_admin" else "shared_poll"
                self.ledger.transition(code, "waiting_fulfillment", poll_reason, updates, ("waiting_fulfillment",))
            else:
                self.ledger.touch_poll(code, updates, allowed=("waiting_fulfillment",))
        return self.public_row(self.ledger.get_code(code))

    def recover_stuck_purchasing(self) -> list[str]:
        timeout_s = max(60, int(self.settings.purchasing_stuck_timeout_seconds))
        cutoff_iso = (datetime.now(timezone.utc).astimezone() - timedelta(seconds=timeout_s)).isoformat(timespec="seconds")
        stuck_rows = self.ledger.list_stuck_purchasing(cutoff_iso)
        recovered: list[str] = []
        for row in stuck_rows:
            code = row["code"]
            if self.ledger.transition(
                code,
                "needs_manual_review",
                "purchasing_stuck_recovery",
                {
                    "last_error_code": "PURCHASING_STUCK_RECOVERY",
                    "last_error_message": f"下单超过 {timeout_s} 秒未落账（可能进程重启或上游中断），请先在 Canboso 核对订单后再处理",
                },
                ("purchasing",),
            ):
                self.alert(code, "PURCHASING_STUCK_RECOVERY")
                recovered.append(code)
        return recovered

    @staticmethod
    def _canboso_usd_amount(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            amount = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return amount if math.isfinite(amount) and amount >= 0 else None

    @staticmethod
    def _canboso_cancellation(status: str) -> dict[str, Any]:
        return {
            "upstream_status": status,
            "last_error_code": "CANBOSO_ORDER_CANCELLED",
            "last_error_message": "Canboso 已终止订单，请核对钱包退款和买家交付。",
        }

    def _record_canboso_cancellation(self, code: str, status: str, updates: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self.ledger.transition(code, "needs_manual_review", "canboso_order_cancelled", {
            **(updates or {}), **self._canboso_cancellation(status),
        }, ("waiting_fulfillment",))
        return self.public_row(self.ledger.get_code(code))

    def _handle_poll_failure(self, code: str, error_message: str = "upstream error") -> dict[str, Any]:
        failures = self._consecutive_poll_failures(code) + 1
        if failures < self.POLL_FAILURE_THRESHOLD:
            self.ledger.transition(
                code,
                "waiting_fulfillment",
                "poll_upstream_error",
                {
                    "last_error_code": "UPSTREAM_ERROR",
                    "last_error_message": error_message,
                    "last_polled_at": now_iso(),
                },
                ("waiting_fulfillment",),
            )
            logger.warning("shared fulfillment poll failed code=%s consecutive_failures=%s detail=%s", code, failures, error_message)
        else:
            self.ledger.transition(
                code,
                "needs_manual_review",
                "upstream_error",
                {"last_error_code": "UPSTREAM_ERROR", "last_error_message": error_message, "last_polled_at": now_iso()},
                ("waiting_fulfillment",),
            )
            logger.warning("shared fulfillment poll escalated to manual review code=%s consecutive_failures=%s detail=%s", code, failures, error_message)
            self.alert(code, "UPSTREAM_ERROR")
        return self.public_row(self.ledger.get_code(code))

    @staticmethod
    def _format_upstream_exception(prefix: str, exc: Exception) -> str:
        if isinstance(exc, UpstreamTimeout):
            detail = str(exc).strip()
            return f"{prefix}（连接超时：{detail}）" if detail else f"{prefix}（连接超时）"
        if isinstance(exc, UpstreamError):
            detail = str(exc.message or "").strip()
            return f"{prefix}（HTTP {exc.status_code}：{detail}）" if detail else f"{prefix}（HTTP {exc.status_code}）"
        return f"{prefix}（{type(exc).__name__}）"

    @staticmethod
    def _preflight_error_message(reason: str, sku: dict[str, Any], cost: float) -> str:
        if reason == "PRICE_LIMIT_EXCEEDED":
            if cost > 0:
                return f"上游现价 ${cost:.2f} 超过最高进价上限 ${float(sku['max_cost_usd']):.2f}"
            return "上游商品价格异常，已暂停下单"
        if reason == "LOW_BALANCE":
            return f"Canboso 账户余额不足（需 ${cost:.2f}）"
        if reason == "UPSTREAM_OUT_OF_STOCK":
            return "上游商品当前缺货，请等补货后再重试"
        if reason == "UPSTREAM_ERROR":
            return "上游商品列表中未找到对应商品"
        return reason

    def _consecutive_poll_failures(self, code: str) -> int:
        count = 0
        for event in reversed(self.ledger.audit(code)):
            if event["reason"] == "poll_upstream_error":
                count += 1
                continue
            break
        return count

    def _warranty_lock(self, code: str) -> threading.Lock:
        with self._warranty_locks_guard:
            lock = self._warranty_locks.get(code)
            if lock is None:
                lock = threading.Lock()
                self._warranty_locks[code] = lock
            return lock

    def _forward_warranty_locked(
        self,
        code: str,
        attachment: Optional[tuple[str, bytes, str]] = None,
    ) -> tuple[bool, Optional[str]]:
        current = self.ledger.get_code(code)
        if not current:
            return False, "CODE_NOT_FOUND"
        warranty = json.loads(current["warranty_json"]) if current.get("warranty_json") else {}
        if not isinstance(warranty, dict):
            return False, "WARRANTY_NOT_ALLOWED"
        if warranty.get("status") == "registered_upstream" or warranty.get("tg_message_id") is not None:
            return True, None
        admin_email = str(warranty.get("admin_email") or "").strip()
        upstream_order_code = current.get("upstream_order_code") or ""
        text = format_warranty_forward_template(upstream_order_code, admin_email)
        try:
            sent = self.tg_forwarder.send_text(text)
        except Exception as exc:
            err_msg = sanitize_tg_error(
                exc,
                secrets=(
                    self.settings.tg_api_hash,
                    self.settings.tg_session,
                    getattr(self.tg_forwarder, "api_hash", ""),
                    getattr(self.tg_forwarder, "session", ""),
                ),
            )
            warranty.update(
                {
                    "status": "submitted",
                    "auto_forwarded": False,
                    "tg_forward_status": "failed",
                    "tg_error": err_msg,
                    "forward_template": text,
                }
            )
            self.ledger.transition(
                code,
                "completed",
                "warranty_tg_forward_failed",
                {"warranty_json": json.dumps(warranty, ensure_ascii=False)},
                ("completed",),
            )
            self._safe_send(
                "【需人工处理】独享整组保修 TG 自动报备失败",
                (
                    f"兑换码：{code}\n"
                    f"上游订单号：{upstream_order_code}\n"
                    f"买家主邮箱：{admin_email}\n"
                    f"失败原因：{err_msg}\n"
                    "\n"
                    "请人工复制以下越南语话术发给 Canboso 管理员，或在后台点击「重试 TG 报备」：\n"
                    f"{text}"
                ),
                attachment,
            )
            return False, err_msg

        sent_at = str(sent.get("sent_at") or now_iso())
        msg_id = sent.get("message_id")
        warranty.update(
            {
                "status": "registered_upstream",
                "registered_at": sent_at,
                "auto_forwarded": True,
                "tg_forward_status": "sent",
                "tg_message_id": msg_id,
                "tg_sent_at": sent_at,
                "tg_error": None,
                "forward_template": text,
            }
        )
        self.ledger.transition(
            code,
            "completed",
            "warranty_auto_forwarded",
            {"warranty_json": json.dumps(warranty, ensure_ascii=False)},
            ("completed",),
        )
        self._safe_send(
            "独享整组保修已自动 TG 报备",
            (
                f"兑换码：{code}\n"
                f"上游订单号：{upstream_order_code}\n"
                f"买家主邮箱：{admin_email}\n"
                f"Telegram 消息 ID：{msg_id}\n"
                f"发送时间：{sent_at}\n"
                "\n"
                f"{text}"
            ),
        )
        return True, None

    def warranty(
        self,
        code: str,
        admin_email: str,
        confirm: Optional[str],
        note: str,
        content: Optional[bytes] = None,
        content_type: str = "",
    ) -> tuple[dict[str, Any], Optional[str]]:
        with self._warranty_lock(code):
            row = self.ledger.get_code(code)
            if not row:
                return {}, "CODE_NOT_FOUND"
            if row["sku_key"] != "ultra_exclusive_admin":
                return {}, "WARRANTY_NOT_APPLICABLE"
            if row["status"] not in {"delivered_pending_warranty", "completed"} or self._channel16688_delivery_on_hold(row):
                return {}, "WARRANTY_NOT_ALLOWED"
            old_warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else {}
            if old_warranty.get("status") == "registered_upstream" or bool(old_warranty.get("activated_at")):
                return {}, "WARRANTY_LOCKED"
            email = admin_email.strip().lower()
            if not is_valid_email(email):
                return {}, "INVALID_EMAIL"
            if confirm is not None and confirm.strip() != "" and email != confirm.strip().lower():
                return {}, "EMAIL_MISMATCH"
            delivery = json.loads(row["delivery_json"]) if row.get("delivery_json") else {}
            store_email = str(delivery.get("account_text") or "").strip().lower() if isinstance(delivery, dict) else ""
            if store_email and email == store_email:
                return {}, "WARRANTY_SAME_AS_STORE_EMAIL"

            old_path = row.get("screenshot_path")
            path: Optional[Path] = None
            has_screenshot = False
            screenshot_path_value: Optional[str] = None
            screenshot_ct_value: Optional[str] = None
            screenshot_size_value: Optional[int] = None
            email_attachment: Optional[tuple[str, bytes, str]] = None
            if content is not None:
                if not content:
                    return {}, "SCREENSHOT_EMPTY"
                if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                    return {}, "INVALID_SCREENSHOT_TYPE"
                if len(content) > 5 * 1024 * 1024:
                    return {}, "SCREENSHOT_TOO_LARGE"
                path = Path(self.settings.storage_dir) / (uuid.uuid4().hex + "." + content_type.split("/")[1])
                path.write_bytes(content)
                has_screenshot = True
                screenshot_path_value = str(path)
                screenshot_ct_value = content_type
                screenshot_size_value = len(content)
                email_attachment = (path.name, content, content_type)

            tg_ready = bool(self.tg_forwarder and getattr(self.tg_forwarder, "is_ready", False))
            forward_template = format_warranty_forward_template(row.get("upstream_order_code") or "", email)
            submitted = {
                "status": "submitted",
                "admin_email": email,
                "submitted_at": now_iso(),
                "registered_at": None,
                "note": note,
                "has_screenshot": has_screenshot,
                "screenshot_path": screenshot_path_value,
                "forward_template": forward_template,
                "auto_forwarded": False,
                "tg_forward_status": "pending" if tg_ready else "disabled",
                "tg_message_id": old_warranty.get("tg_message_id"),
                "tg_sent_at": old_warranty.get("tg_sent_at"),
                "tg_error": None,
            }
            changed = self.ledger.transition(
                code,
                "completed",
                "warranty_submitted",
                {
                    "buyer_email": email,
                    "buyer_email_masked": mask_email(email),
                    "warranty_json": json.dumps(submitted, ensure_ascii=False),
                    "screenshot_path": screenshot_path_value,
                    "screenshot_content_type": screenshot_ct_value,
                    "screenshot_size": screenshot_size_value,
                    "completed_at": now_iso(),
                    "last_error_code": None,
                    "last_error_message": None,
                },
                ("delivered_pending_warranty", "completed"),
            )
            if not changed:
                if path is not None:
                    path.unlink(missing_ok=True)
                current = self.ledger.get_code(code)
                if current:
                    current_warranty = json.loads(current.get("warranty_json") or "{}")
                    if current_warranty.get("status") == "registered_upstream" or bool(current_warranty.get("activated_at")):
                        return {}, "WARRANTY_LOCKED"
                return {}, "WARRANTY_NOT_ALLOWED"
            if old_path and old_path != screenshot_path_value:
                old = Path(old_path)
                if old.is_file():
                    old.unlink()
            if tg_ready:
                self._forward_warranty_locked(code, attachment=email_attachment)
            else:
                self._safe_send("独享整组买家提交保修登记", submitted["forward_template"], email_attachment)
            return self.public_row(self.ledger.get_code(code)), None

    def warranty_forward(self, code: str) -> tuple[dict[str, Any], Optional[str], Optional[str]]:
        with self._warranty_lock(code):
            row = self.ledger.get_code(code)
            if not row:
                return {}, "CODE_NOT_FOUND", None
            if row["sku_key"] != "ultra_exclusive_admin":
                return {}, "WARRANTY_NOT_APPLICABLE", None
            warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else {}
            if (
                row["status"] != "completed"
                or not isinstance(warranty, dict)
                or not str(warranty.get("admin_email") or "").strip()
            ):
                return {}, "WARRANTY_NOT_ALLOWED", None
            if warranty.get("status") == "registered_upstream":
                return {}, "WARRANTY_LOCKED", None
            if not (self.tg_forwarder and getattr(self.tg_forwarder, "is_ready", False)):
                return {}, "TG_NOT_CONFIGURED", None
            ok, err_msg = self._forward_warranty_locked(code)
            if not ok:
                return {}, "TG_FORWARD_FAILED", err_msg
            return self.admin_detail(self.ledger.get_code(code)), None, None

    def warranty_ack(self, code: str, note: str) -> tuple[dict[str, Any], Optional[str]]:
        with self._warranty_lock(code):
            row = self.ledger.get_code(code)
            if not row:
                return {}, "CODE_NOT_FOUND"
            if row["sku_key"] != "ultra_exclusive_admin":
                return {}, "WARRANTY_NOT_APPLICABLE"
            warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else {}
            if (
                row["status"] != "completed"
                or not isinstance(warranty, dict)
                or not str(warranty.get("admin_email") or "").strip()
            ):
                return {}, "WARRANTY_NOT_ALLOWED"
            warranty.update({"status": "registered_upstream", "registered_at": now_iso(), "note": note or warranty.get("note", "")})
            self.ledger.transition(
                code,
                "completed",
                "warranty_ack",
                {
                    "warranty_json": json.dumps(warranty, ensure_ascii=False),
                    "last_error_code": None,
                    "last_error_message": None,
                },
                ("completed",),
            )
            return self.admin_detail(self.ledger.get_code(code)), None

    def mark_exclusive_activated(
        self,
        code: str,
        activated: bool = True,
        note: str = "",
    ) -> tuple[dict[str, Any], Optional[str]]:
        with self._warranty_lock(code):
            row = self.ledger.get_code(code)
            if not row:
                return {}, "CODE_NOT_FOUND"
            if row["sku_key"] != "ultra_exclusive_admin":
                return {}, "WARRANTY_NOT_APPLICABLE"
            warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else {}
            if (
                row["status"] != "completed"
                or not isinstance(warranty, dict)
                or not str(warranty.get("admin_email") or "").strip()
            ):
                return {}, "WARRANTY_NOT_ALLOWED"
            clean_note = note.strip()
            if activated:
                warranty["activated_at"] = warranty.get("activated_at") or now_iso()
                reason = f"exclusive_activated:{clean_note}" if clean_note else "exclusive_activated"
            else:
                warranty["activated_at"] = None
                reason = f"exclusive_activation_reverted:{clean_note}" if clean_note else "exclusive_activation_reverted"
            self.ledger.transition(
                code,
                "completed",
                reason,
                {"warranty_json": json.dumps(warranty, ensure_ascii=False)},
                ("completed",),
            )
            return self.admin_detail(self.ledger.get_code(code)), None

    def record_upstream_refund(self, code: str, refund_usd: float, note: str = "") -> tuple[dict[str, Any], Optional[str]]:
        row = self.ledger.get_code(code)
        if not row:
            return {}, "CODE_NOT_FOUND"
        if not row.get("upstream_order_code"):
            return {}, "NO_UPSTREAM_ORDER"
        if not self.ledger.record_upstream_refund(code, refund_usd, note):
            return {}, "NO_UPSTREAM_ORDER"
        return self.admin_detail(self.ledger.get_code(code)), None

    def reset_code_to_unused(self, code: str, note: str = "") -> tuple[dict[str, Any], Optional[str]]:
        outcome = self.ledger.reset_code_to_unused(code, note)
        if outcome != "OK":
            row = self.ledger.get_code(code)
            return (self.admin_detail(row) if row else {}), outcome
        return self.admin_detail(self.ledger.get_code(code)), None

    def alert(self, code: str, reason: str) -> None:
        if self.ledger.claim_notification(f"alert:{code}:{reason}"):
            self._safe_send("Plan 2 异常报警", f"兑换码 {code} 需要人工处理。事件：{reason}。")

    @staticmethod
    def _is_16688_openapi_paused_message(message: str) -> bool:
        text = message.strip()
        if not text or CHANNEL16688_OPENAPI_NEGATED_PAUSE_RE.search(text):
            return False
        return bool(CHANNEL16688_OPENAPI_PAUSED_RE.search(text))

    @classmethod
    def _channel16688_paused(cls, exc: Exception) -> bool:
        return (
            isinstance(exc, UpstreamError)
            and exc.status_code == 502
            and exc.rejected
            and cls._is_16688_openapi_paused_message(exc.message)
        )

    @classmethod
    def _channel16688_unavailable(cls, exc: Exception) -> bool:
        return (
            isinstance(exc, UpstreamTimeout)
            or cls._channel16688_paused(exc)
            or isinstance(exc, UpstreamError) and (
                exc.status_code in {520, 521, 522, 523, 524}
                or exc.status_code in {500, 502, 503, 504} and not exc.rejected
                or exc.status_code == 503 and exc.message.startswith("16688 network error")
            )
        )

    def _alert_16688_paused(self) -> None:
        if self.ledger.claim_notification(CHANNEL16688_OPENAPI_PAUSED_KEY) and not self._safe_send(
            "16688 接口暂不可用，库存同步受影响",
            "16688 接口暂不可用。有效、未使用且入库已确认的兑换码仍可自动提货，金额、库存和余额检查照常执行。",
        ):
            self.ledger.clear_notification(CHANNEL16688_OPENAPI_PAUSED_KEY)

    def _safe_send(self, subject: str, body: str, attachment: Optional[tuple[str, bytes, str]] = None) -> bool:
        try:
            self.mailer.send(subject, body, attachment)
            return True
        except Exception as exc:
            logger.warning("merchant email send failed subject=%s error=%s", subject, type(exc).__name__)
            return False

    def _new_code(self, sku_key: str) -> str:
        prefix = "U20S" if sku_key == "ultra_shared_slot" else "U20E"
        return f"{prefix}-" + "-".join("".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4)) for _ in range(2))

    def _safe_delivery(self, raw: dict[str, Any]) -> dict[str, Any]:
        accounts = raw.get("accounts") or []
        first = accounts[0] if accounts else {}
        if isinstance(first, str):
            email = first.strip()
        elif isinstance(first, dict):
            email = str(first.get("user") or first.get("email") or first.get("otherInfo") or "").strip()
        else:
            email = ""
        if not email and isinstance(raw, dict):
            email = str(raw.get("account") or raw.get("email") or raw.get("text") or "").strip()
        return {
            "account_text": email,
            "credentials": [{"label": "商店套餐邮箱", "value": email}] if email else [],
            "raw_lines": [email] if email else [],
            "delivered_at": now_iso(),
        }

    @staticmethod
    def _has_delivery_credentials(delivery: Optional[dict[str, Any]]) -> bool:
        if not isinstance(delivery, dict):
            return False
        return bool(
            str(delivery.get("account_text") or "").strip()
            or delivery.get("credentials")
            or delivery.get("raw_lines")
        )

    def _code_row(self, code: str) -> dict[str, Any]:
        return self.ledger.get_code(code)

    def _channel16688_delivery_on_hold(self, row: dict[str, Any]) -> bool:
        if row["channel"] != "16688":
            return False
        if row.get("last_error_code") == "CHANNEL16688_RECIPIENT_DISPUTED":
            return True
        with closing(self.ledger.connect()) as conn:
            return conn.execute(
                """SELECT 1 FROM audit_events AS event
                   WHERE code=? AND (
                       reason IN ('16688_recipient_disputed', '16688_platform_resold_redeemed_code',
                                  '16688_consumed_unsold_stock')
                       OR (
                           reason IN ('admin_manual_canboso_purchase_16688_api_paused',
                                      '16688_openapi_paused_auto_purchase')
                           AND NOT EXISTS (
                               SELECT 1 FROM audit_events AS sale
                               WHERE sale.code=event.code AND sale.event_id<event.event_id
                               AND sale.reason IN ('channel16688_sold_sync','channel16688_order_verified')
                           )
                       )
                   ) LIMIT 1""",
                (row["code"],),
            ).fetchone() is not None

    @staticmethod
    def _elapsed_minutes(row: dict[str, Any]) -> Optional[int]:
        return Service._elapsed_minutes_from_iso(row.get("submitted_at"))

    @staticmethod
    def _elapsed_minutes_from_iso(iso_text: Optional[str]) -> Optional[int]:
        raw = str(iso_text or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        now_dt = datetime.now(timezone.utc).astimezone()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now_dt.tzinfo)
        return max(0, int((now_dt - parsed).total_seconds() // 60))

    def public_row(self, row: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not row:
            return {}
        sku = self.ledger.get_sku(row["sku_key"])
        delivery = json.loads(row["delivery_json"]) if row.get("delivery_json") else None
        raw_warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else None
        if raw_warranty:
            warranty_email = (
                normalize_display_email(raw_warranty.get("admin_email"))
                or normalize_display_email(raw_warranty.get("admin_email_masked"))
            )
            warranty = {
                "status": raw_warranty.get("status", "pending"),
                "admin_email_masked": warranty_email,
                "submitted_at": raw_warranty.get("submitted_at"),
                "has_screenshot": bool(raw_warranty.get("has_screenshot")),
                "note": raw_warranty.get("note"),
            }
        elif row["sku_key"] == "ultra_exclusive_admin" and row["status"] in {"delivered_pending_warranty", "completed"}:
            warranty = {
                "status": "pending",
                "admin_email_masked": None,
                "submitted_at": None,
                "has_screenshot": False,
                "note": None,
            }
        else:
            warranty = None
        if sku["product_type"] == "slot":
            delivery = None
        elapsed = self._elapsed_minutes(row)
        notice = "订单正在人工处理，请带兑换码联系客服。" if row["status"] == "needs_manual_review" else None
        display_email = (
            normalize_display_email(row.get("buyer_email"))
            or (warranty.get("admin_email_masked") if warranty else None)
            or normalize_display_email(row.get("buyer_email_masked"))
        )
        status = row["status"]
        delivery_on_hold = self._channel16688_delivery_on_hold(row)
        if delivery_on_hold:
            status = "needs_manual_review"
            display_email = None
            delivery = None
            warranty = None
            elapsed = None
            notice = "这枚卡密的历史履约需要核对。如商家已补发，请使用新兑换码。需要帮助时请带 16688 订单号联系客服。"
        elif row["channel"] == "16688" and sku["product_type"] == "slot" and display_email:
            local, domain = display_email.rsplit("@", 1)
            display_email = f"{local[:1]}***@{domain}"
        warranty_submitted_at = (
            None
            if delivery_on_hold
            else (raw_warranty.get("submitted_at") if isinstance(raw_warranty, dict) else None)
        )
        exclusive_activated_at = (
            None
            if delivery_on_hold
            else (raw_warranty.get("activated_at") if isinstance(raw_warranty, dict) else None)
        )
        warranty_elapsed = (
            None
            if delivery_on_hold
            else self._elapsed_minutes_from_iso(warranty_submitted_at)
        )
        eta_text = (
            self.settings.shared_sla_eta_text
            if sku["product_type"] == "slot"
            else self.settings.exclusive_sla_eta_text
        )
        return {
            "code": row["code"],
            "sku_key": row["sku_key"],
            "sku_title": sku["title"],
            "product_type": sku["product_type"],
            "channel": row["channel"],
            "status": status,
            "buyer_email_masked": display_email,
            "delivery": delivery,
            "warranty": warranty,
            "fulfillment": {
                "eta_text": eta_text,
                "submitted_at": None if delivery_on_hold else row.get("submitted_at"),
                "completed_at": None if delivery_on_hold else row.get("completed_at"),
                "last_polled_at": None if delivery_on_hold else row.get("last_polled_at"),
                "elapsed_minutes": elapsed,
                "warranty_submitted_at": warranty_submitted_at,
                "exclusive_activated_at": exclusive_activated_at,
                "warranty_elapsed_minutes": warranty_elapsed,
            },
            "buyer_notice": notice,
            "support_hint": self.settings.support_contact_hint,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def admin_summary(
        self,
        row: dict[str, Any],
        *,
        sku_map: Optional[dict[str, dict[str, Any]]] = None,
        disputed_codes: Optional[set[str]] = None,
        resettable_codes: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        sku = (sku_map or {}).get(row["sku_key"]) or self.ledger.get_sku(row["sku_key"])
        elapsed = self._elapsed_minutes(row)
        warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else None
        delivery = json.loads(row["delivery_json"]) if row.get("delivery_json") else None
        resolved_email = row.get("buyer_email") or (warranty.get("admin_email") if isinstance(warranty, dict) else None) or None
        store_email = (delivery.get("account_text") if isinstance(delivery, dict) else None) or None
        warranty_status = warranty.get("status") if isinstance(warranty, dict) else None
        is_dispatched = bool((row.get("external_order_id") or "").strip())
        if row["status"] == "unused":
            display_status = "dispatched_unused" if is_dispatched else "in_stock"
        elif row["status"] == "completed" and row["sku_key"] == "ultra_exclusive_admin" and warranty_status == "submitted":
            display_status = "pending_warranty_ack"
        else:
            display_status = row["status"]
        upstream_status_low = str(row.get("upstream_status") or "").strip().lower()
        last_err = row.get("last_error_code")
        can_poll_upstream = bool(row.get("upstream_order_code") and row["status"] in {"waiting_fulfillment", "needs_manual_review"})
        is_disputed = last_err == "CHANNEL16688_RECIPIENT_DISPUTED" or (
            disputed_codes is not None and row["code"] in disputed_codes
        )
        unconfirmed_purchase = not row.get("upstream_order_code") and last_err in {
            "PURCHASING_STUCK_RECOVERY",
            "CANBOSO_PURCHASE_UNCONFIRMED",
        }
        can_reorder = bool(
            row["status"] == "needs_manual_review"
            and not is_disputed
            and not unconfirmed_purchase
            and (not row.get("upstream_order_code") or upstream_status_low in CANBOSO_CANCELLED_STATUSES)
        )
        can_reset_unused = (
            row["code"] in resettable_codes
            if resettable_codes is not None
            else self.ledger.can_reset_to_unused(row)
        )
        return {
            "code": row["code"],
            "sku_key": row["sku_key"],
            "sku_title": sku["title"],
            "product_type": sku["product_type"],
            "channel": row["channel"],
            "goods_no": row.get("goods_no"),
            "external_order_id": row.get("external_order_id"),
            "remark": row.get("remark") or "",
            "status": row["status"],
            "display_status": display_status,
            "is_dispatched": is_dispatched,
            "buyer_email": resolved_email,
            "buyer_email_masked": normalize_display_email(resolved_email) or normalize_display_email(row.get("buyer_email_masked")),
            "store_email": store_email,
            "upstream_order_code": row.get("upstream_order_code"),
            "upstream_status": row.get("upstream_status"),
            "cost_usd": row.get("cost_usd"),
            "upstream_refund_usd": row.get("upstream_refund_usd"),
            "upstream_refund_note": row.get("upstream_refund_note"),
            "warranty_status": warranty_status,
            "last_error_code": row.get("last_error_code"),
            "last_error_message": row.get("last_error_message"),
            "is_overdue_24h": bool(elapsed is not None and elapsed > 24 * 60 and row["status"] == "waiting_fulfillment"),
            "elapsed_minutes": elapsed,
            "can_poll_upstream": can_poll_upstream,
            "can_reorder": can_reorder,
            "can_reset_unused": can_reset_unused,
            "submitted_at": row.get("submitted_at"),
            "completed_at": row.get("completed_at"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def admin_detail(self, row: dict[str, Any]) -> dict[str, Any]:
        result = self.admin_summary(row)
        if result["can_reorder"] and self.ledger.is_disputed_16688_code(row["code"], row.get("last_error_code")):
            result["can_reorder"] = False
        warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else None
        if warranty:
            if warranty.get("admin_email"):
                if "admin_email_masked" in warranty:
                    warranty["admin_email_masked"] = normalize_display_email(warranty["admin_email"])
                warranty["forward_template"] = format_warranty_forward_template(
                    row.get("upstream_order_code") or "",
                    warranty["admin_email"],
                )
            warranty["screenshot_url"] = f"/api/v1/admin/codes/{row['code']}/warranty-screenshot" if row.get("screenshot_path") else None
            warranty.pop("screenshot_path", None)
            warranty.setdefault("auto_forwarded", False)
            warranty.setdefault("tg_forward_status", "disabled")
            warranty.setdefault("tg_message_id", None)
            warranty.setdefault("tg_sent_at", None)
            warranty.setdefault("tg_error", None)
            warranty.setdefault("activated_at", None)
        result.update({
            "redeem_url": f"{self.settings.public_base_url}/r/{row['code']}",
            "batch_id": row.get("batch_id"),
            "buyer_email": result["buyer_email"],
            "idempotency_key": row.get("idempotency_key"),
            "delivery": json.loads(row["delivery_json"]) if row.get("delivery_json") else None,
            "warranty": warranty,
            "audit_events": self.ledger.audit(row["code"]),
        })
        return result

    def overview(self, force_refresh: bool = False) -> dict[str, Any]:
        if force_refresh and hasattr(self.canboso, "invalidate_products_cache"):
            self.canboso.invalidate_products_cache()
        catalog = self.canboso.list_products()
        balance = self.canboso.get_balance()
        balance_usd = float(balance.get("balanceUsd", balance.get("balance", 0)))
        skus = []
        for key in SKUS:
            skus.append(self.sku_status(key, catalog))
        counts = {
            status: self._count("status=?", (status,))
            for status in ("unused", "waiting_fulfillment", "delivered_pending_warranty", "needs_manual_review", "revoked")
        }
        counts.update({
            "completed": self._count(COMPLETED_ADMIN_SQL),
            "pending_warranty_ack": self._count(PENDING_WARRANTY_ACK_SQL),
            "manual_attention": self._count(MANUAL_ATTENTION_SQL),
            "in_stock": self._count(IN_STOCK_SQL),
            "dispatched_unused": self._count(DISPATCHED_UNUSED_SQL),
            "total": self._count(),
            "overdue_24h": self._count("status='waiting_fulfillment' AND submitted_at < ?", ((datetime.now(timezone.utc).astimezone() - timedelta(hours=24)).isoformat(),)),
        })
        if balance_usd < self.settings.canboso_low_balance_threshold_usd and self.ledger.claim_notification("low-balance"):
            self._safe_send("Canboso 低余额报警", f"Canboso 余额低于配置阈值，请及时处理。当前余额为 ${balance_usd:.2f}。")
        return {
            "execution_mode": self.settings.execution_mode,
            "public_base_url": self.settings.public_base_url,
            "canboso": {
                "balance_usd": balance_usd,
                "low_balance_threshold_usd": self.settings.canboso_low_balance_threshold_usd,
                "is_low_balance": balance_usd < self.settings.canboso_low_balance_threshold_usd,
                "checked_at": now_iso(),
                "error": None,
                "today_purchases": self.ledger.canboso_purchase_totals(datetime.now(timezone(timedelta(hours=8))).date().isoformat()),
            },
            "telegram_warranty": {
                "enabled": bool(self.settings.tg_warranty_enabled),
                "ready": bool(self.tg_forwarder and getattr(self.tg_forwarder, "is_ready", False)),
                "target_configured": bool((self.settings.tg_admin_target or "").strip()),
            },
            "skus": skus,
            "counts": counts,
        }

    def sku_status(self, sku_key: str, catalog: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        s = self.ledger.get_sku(sku_key)
        if not s:
            raise ValueError("UNKNOWN_SKU")
        catalog = catalog if catalog is not None else self.canboso.list_products()
        p = next((x for x in catalog.get("products", []) if x.get("productId") == s["product_id"]), {})
        price = float((p.get("price") or {}).get("amount", 0)) if p else None
        available = (p.get("availability") or {}).get("available") if p else None
        return {
            "sku_key": sku_key,
            "title": s["title"],
            "product_type": s["product_type"],
            "canboso_product_id": s["product_id"],
            "goods_no": s.get("goods_no"),
            "current_price_usd": price,
            "max_cost_usd": s["max_cost_usd"],
            "available": available,
            "is_available": available != 0,
            "price_exceeded": price is not None and price > s["max_cost_usd"],
            "unused_codes_taobao": self._count("sku_key=? AND channel='taobao' AND status='unused'", (sku_key,)),
            "unused_codes_16688": self._count("sku_key=? AND channel='16688' AND status='unused'", (sku_key,)),
            "in_stock_codes_taobao": self._count("sku_key=? AND channel='taobao' AND status='unused' AND COALESCE(TRIM(external_order_id), '') = ''", (sku_key,)),
            "dispatched_unused_codes_taobao": self._count("sku_key=? AND channel='taobao' AND status='unused' AND COALESCE(TRIM(external_order_id), '') <> ''", (sku_key,)),
            "in_stock_codes_16688": self._count("sku_key=? AND channel='16688' AND status='unused' AND COALESCE(TRIM(external_order_id), '') = ''", (sku_key,)),
            "dispatched_unused_codes_16688": self._count("sku_key=? AND channel='16688' AND status='unused' AND COALESCE(TRIM(external_order_id), '') <> ''", (sku_key,)),
        }

    def _count(self, where: str = "", params: tuple[Any, ...] = ()) -> int:
        with self.ledger.connect() as c:
            return c.execute("SELECT COUNT(*) FROM codes" + (" WHERE " + where if where else ""), params).fetchone()[0]

    def agiso_sku_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        db_rules = [
            {
                **row,
                "enabled": bool(row["enabled"]),
                "source": "db",
            }
            for row in self.ledger.list_agiso_sku_mappings(enabled_only=enabled_only)
        ]
        env_rules: list[dict[str, Any]] = []
        try:
            env_rules = parse_env_sku_rules(self.settings.agiso_sku_rules_json)
        except ValueError as exc:
            logger.warning("invalid PLAN2_AGISO_SKU_RULES_JSON: %s", exc)
        if enabled_only:
            env_rules = [rule for rule in env_rules if rule.get("enabled")]
        return db_rules + env_rules

    def agiso_restock_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        try:
            rules = parse_env_restock_rules(self.settings.agiso_restock_rules_json)
        except ValueError as exc:
            logger.warning("invalid PLAN2_AGISO_RESTOCK_RULES_JSON: %s", exc)
            return []
        valid: list[dict[str, Any]] = []
        for rule in rules:
            if rule["sku_key"] not in SKUS:
                logger.warning("ignoring agiso restock rule with unknown sku_key=%s", rule["sku_key"])
                continue
            if enabled_only and not rule.get("enabled"):
                continue
            valid.append(rule)
        return valid

    def agiso_status(self) -> dict[str, Any]:
        enabled_rules = self.agiso_sku_rules(enabled_only=True)
        enabled_restock_rules = self.agiso_restock_rules(enabled_only=True)
        return {
            "configured": bool(self.settings.agiso_app_secret.strip()),
            "webhook_ready": bool(self.settings.agiso_app_secret.strip()),
            "api_ready": bool(self.agiso and getattr(self.agiso, "is_ready", False)),
            "app_id": self.settings.agiso_app_id,
            "has_app_secret": bool(self.settings.agiso_app_secret.strip()),
            "has_access_token": bool(self.settings.agiso_access_token.strip()),
            "webhook_url": f"{self.settings.public_base_url.rstrip('/')}/api/v1/webhooks/agiso",
            "api_base_url": self.settings.agiso_api_base_url,
            "execution_mode": self.settings.execution_mode,
            "auto_create_result": self.settings.agiso_auto_create_result,
            "auto_dummy_send": self.settings.agiso_auto_dummy_send,
            "auto_send_tb_card": self.settings.agiso_auto_send_tb_card,
            "auto_send_ww_msg": self.settings.agiso_auto_send_ww_msg,
            "auto_revoke_on_refund": self.settings.agiso_auto_revoke_on_refund,
            "auto_restore_on_refund_close": self.settings.agiso_auto_restore_on_refund_close,
            "poll_enabled": self.settings.agiso_poll_enabled,
            "poll_interval_seconds": self.settings.agiso_poll_interval_seconds,
            "restock_enabled": self.settings.agiso_restock_enabled,
            "restock_interval_seconds": self.settings.agiso_restock_interval_seconds,
            "restock_rules_count": len(enabled_restock_rules),
            "sku_mappings_count": len(enabled_rules),
            "deliveries_summary": self.ledger.agiso_delivery_counts(),
            "events_total": self.ledger.agiso_event_count(),
        }

    def create_agiso_sku_mapping(self, data: dict[str, Any]) -> dict[str, Any]:
        sku_key = str(data.get("sku_key") or "").strip()
        if sku_key not in SKUS:
            raise ValueError("UNKNOWN_SKU")
        normalized = {
            "sku_key": sku_key,
            "num_iid": str(data.get("num_iid") or "").strip(),
            "sku_id": str(data.get("sku_id") or "").strip(),
            "outer_iid": str(data.get("outer_iid") or "").strip(),
            "outer_sku_id": str(data.get("outer_sku_id") or "").strip(),
            "sku_props_contains": str(data.get("sku_props_contains") or "").strip(),
            "enabled": bool(data.get("enabled", True)),
            "note": str(data.get("note") or "").strip()[:200],
        }
        if not rule_has_matcher(normalized):
            raise ValueError("MATCHER_REQUIRED")
        created = self.ledger.create_agiso_sku_mapping(normalized)
        created["enabled"] = bool(created["enabled"])
        created["source"] = "db"
        return created

    def _agiso_alert(self, dedupe_key: str, subject: str, body: str) -> None:
        if self.ledger.claim_notification(f"agiso:{dedupe_key}"):
            self._safe_send(subject, body)

    @staticmethod
    def _summarize_agiso_payload(payload: dict[str, Any]) -> str:
        orders = payload.get("Orders") if isinstance(payload.get("Orders"), list) else []
        num_iids = sorted({str(item.get("NumIid")) for item in orders if isinstance(item, dict) and item.get("NumIid") is not None})
        summary = {
            "Tid": str(payload.get("Tid") or ""),
            "Oid": str(payload.get("Oid") or ""),
            "Status": str(payload.get("Status") or ""),
            "RefundId": str(payload.get("RefundId") or ""),
            "OrdersCount": len(orders),
            "NumIids": num_iids[:10],
        }
        return json.dumps({k: v for k, v in summary.items() if v not in ("", [], 0)}, ensure_ascii=False)

    def handle_agiso_webhook(
        self,
        *,
        from_platform: str,
        aopic: int,
        payload: dict[str, Any],
        timestamp: str,
        sign: str,
    ) -> tuple[int, str, dict[str, Any]]:
        tid = str(payload.get("Tid") or "").strip()
        oid = str(payload.get("Oid") or "").strip()
        summary = self._summarize_agiso_payload(payload)

        if from_platform.lower() == "open":
            self.ledger.record_agiso_event(
                source="webhook",
                from_platform=from_platform,
                aopic=aopic,
                tid=tid,
                oid=oid,
                status="simulated_verified",
                detail="开放平台模拟推送验签通过",
                payload_summary=summary,
            )
            return 200, "", {"status": "simulated_verified", "tid": tid}

        if aopic in (AOPIC_BUYER_PAY, AOPIC_BUYER_PAY_SIMPLE):
            trade_status = str(payload.get("Status") or "").strip()
            if trade_status and trade_status != "WAIT_SELLER_SEND_GOODS":
                self.ledger.record_agiso_event(
                    source="webhook",
                    from_platform=from_platform,
                    aopic=aopic,
                    tid=tid,
                    oid=oid,
                    status="ignored_status",
                    detail=f"订单状态为 {trade_status}，仅处理 WAIT_SELLER_SEND_GOODS",
                    payload_summary=summary,
                )
                return 200, "", {"status": "ignored_status", "tid": tid, "trade_status": trade_status}

            trade = payload
            orders = payload.get("Orders")
            if aopic == AOPIC_BUYER_PAY_SIMPLE or not isinstance(orders, list) or not orders:
                if not tid:
                    self.ledger.record_agiso_event(
                        source="webhook",
                        from_platform=from_platform,
                        aopic=aopic,
                        tid="",
                        oid=oid,
                        status="invalid_payload",
                        detail="推送缺少 Tid",
                        payload_summary=summary,
                    )
                    return 400, "缺少订单号", {"status": "invalid_payload"}
                if not self.agiso or not getattr(self.agiso, "is_ready", False):
                    self.ledger.record_agiso_event(
                        source="webhook",
                        from_platform=from_platform,
                        aopic=aopic,
                        tid=tid,
                        oid=oid,
                        status="missing_access_token",
                        detail="收到简化付款推送，但尚未配置 AccessToken 无法反查订单详情",
                        payload_summary=summary,
                    )
                    self._agiso_alert(
                        f"missing-token:{tid}",
                        "阿奇索自动发货需人工处理（未配置 AccessToken）",
                        f"淘宝订单 {tid} 收到付款推送（aopic={aopic}），但服务尚未配置 AccessToken，无法反查订单明细并自动发码。",
                    )
                    return 503, "未配置AccessToken", {"status": "missing_access_token", "tid": tid}
                try:
                    trade = self.agiso.trade_info(tid)
                except AgisoError as exc:
                    self.ledger.record_agiso_event(
                        source="webhook",
                        from_platform=from_platform,
                        aopic=aopic,
                        tid=tid,
                        oid=oid,
                        status="trade_info_error",
                        detail=exc.message,
                        payload_summary=summary,
                    )
                    should_retry = exc.transient or exc.error_code in {1, 2, 8, 10, 13, 16, 28}
                    if should_retry:
                        self._agiso_alert(
                            f"tradeinfo:{tid}",
                            "阿奇索订单查询失败（需人工核对）",
                            f"淘宝订单 {tid} 反查 TradeInfo 失败：{exc.message}",
                        )
                        return 503, "订单查询失败", {"status": "trade_info_error", "tid": tid}
                    return 200, "", {"status": "trade_info_error", "tid": tid}
                fetched_status = str(trade.get("Status") or "").strip()
                if fetched_status and fetched_status != "WAIT_SELLER_SEND_GOODS":
                    self.ledger.record_agiso_event(
                        source="webhook",
                        from_platform=from_platform,
                        aopic=aopic,
                        tid=tid,
                        oid=oid,
                        status="ignored_status",
                        detail=f"反查订单状态为 {fetched_status}，跳过自动发码",
                        payload_summary=self._summarize_agiso_payload(trade),
                    )
                    return 200, "", {"status": "ignored_status", "tid": tid, "trade_status": fetched_status}

            outcome = self.fulfill_agiso_trade(
                trade,
                source="webhook",
                from_platform=from_platform,
                aopic=aopic,
            )
            if outcome.get("retry_webhook"):
                return 503, outcome.get("detail", "发货暂不可用"), outcome
            return 200, "", outcome

        if aopic in (AOPIC_REFUND_CREATED, AOPIC_REFUND_SUCCESS):
            outcome = self.handle_agiso_refund(payload, aopic=aopic, from_platform=from_platform)
            return 200, "", outcome

        if aopic == AOPIC_REFUND_CLOSED:
            outcome = self.handle_agiso_refund_closed(payload, from_platform=from_platform)
            return 200, "", outcome

        if aopic == AOPIC_AUTO_SEND_SUCCESS:
            outcome = self.handle_agiso_sent_cards(payload, from_platform=from_platform)
            return 200, "", outcome

        self.ledger.record_agiso_event(
            source="webhook",
            from_platform=from_platform,
            aopic=aopic,
            tid=tid,
            oid=oid,
            status="ignored_aopic",
            detail=f"暂不处理的推送类型 aopic={aopic}",
            payload_summary=summary,
        )
        return 200, "", {"status": "ignored_aopic", "aopic": aopic, "tid": tid}

    def fulfill_agiso_trade(
        self,
        trade: dict[str, Any],
        *,
        source: str,
        from_platform: str = "TbAlds",
        aopic: Optional[int] = None,
    ) -> dict[str, Any]:
        tid = str(trade.get("Tid") or "").strip()
        buyer_nick = str(trade.get("BuyerNick") or "").strip()
        orders = trade.get("Orders") if isinstance(trade.get("Orders"), list) else []
        summary = self._summarize_agiso_payload(trade)
        if not tid:
            self.ledger.record_agiso_event(
                source=source,
                from_platform=from_platform,
                aopic=aopic,
                tid="",
                oid="",
                status="invalid_payload",
                detail="订单缺少 Tid",
                payload_summary=summary,
            )
            return {"status": "invalid_payload", "tid": "", "codes": []}

        existing_deliveries = self.ledger.list_agiso_deliveries_by_tid(tid)
        has_native_delivery = any(str(row.get("delivery_key") or "").startswith("native:") for row in existing_deliveries)
        if has_native_delivery:
            existing_codes = [row["code"] for row in existing_deliveries]
            detail = f"订单已存在原生卡密库绑定记录，跳过开放平台重复发码（{len(existing_codes)} 份）"
            self.ledger.record_agiso_event(
                source=source,
                from_platform=from_platform,
                aopic=aopic,
                tid=tid,
                oid="",
                status="already_bound_external",
                detail=detail,
                payload_summary=summary,
            )
            return {
                "status": "already_bound_external",
                "tid": tid,
                "codes": existing_codes,
                "deliveries": existing_deliveries,
                "retry_webhook": False,
            }
        if not existing_deliveries:
            external_codes = self.ledger.find_codes_by_external_order(tid)
            if external_codes:
                existing_codes = [row["code"] for row in external_codes]
                detail = f"订单已存在外部绑定兑换码（可能已由原生卡密库或人工发码），跳过开放平台重复发码（{len(existing_codes)} 份）"
                self.ledger.record_agiso_event(
                    source=source,
                    from_platform=from_platform,
                    aopic=aopic,
                    tid=tid,
                    oid="",
                    status="already_bound_external",
                    detail=detail,
                    payload_summary=summary,
                )
                return {
                    "status": "already_bound_external",
                    "tid": tid,
                    "codes": existing_codes,
                    "deliveries": [],
                    "retry_webhook": False,
                }

        rules = self.agiso_sku_rules(enabled_only=True)
        valid_skus = set(SKUS.keys())
        matched_deliveries: list[dict[str, Any]] = []
        unmapped_num_iids: list[str] = []
        oversized_oids: list[str] = []

        for order in orders:
            if not isinstance(order, dict):
                continue
            oid = str(order.get("Oid") or tid).strip()
            num_iid = str(order.get("NumIid") or "").strip()
            sku_id = str(order.get("SkuId") or "").strip()
            matched_rule = match_agiso_sku(order, rules, valid_sku_keys=valid_skus)
            if not matched_rule:
                unmapped_num_iids.append(num_iid or oid)
                continue
            try:
                qty = int(order.get("Num") or 1)
            except (TypeError, ValueError):
                qty = 1
            if qty < 1:
                qty = 1
            if qty > 50:
                oversized_oids.append(f"{oid}(qty={qty})")
                continue
            sku_key = matched_rule["sku_key"]
            sku = self.ledger.get_sku(sku_key)
            goods_no = sku.get("goods_no") if sku else None
            for seq in range(1, qty + 1):
                delivery_key = f"{tid}:{oid}:{seq}"
                remark = f"阿奇索自动发码(Oid={oid}, 序号={seq}/{qty})" if qty > 1 else f"阿奇索自动发码(Oid={oid})"
                _, delivery = self.ledger.ensure_agiso_delivery(
                    delivery_key=delivery_key,
                    tid=tid,
                    oid=oid,
                    seq=seq,
                    num_iid=num_iid,
                    sku_id=sku_id,
                    sku_key=sku_key,
                    buyer_nick=buyer_nick,
                    remark=remark,
                    goods_no=goods_no,
                    code_factory=lambda key=sku_key: self._new_code(key),
                )
                matched_deliveries.append(delivery)

        if oversized_oids:
            detail = f"子订单购买数量超安全阈值(>50)，已暂停自动发码：{', '.join(oversized_oids)}"
            self.ledger.record_agiso_event(
                source=source,
                from_platform=from_platform,
                aopic=aopic,
                tid=tid,
                oid="",
                status="quantity_limit_exceeded",
                detail=detail,
                payload_summary=summary,
            )
            self._agiso_alert(f"qty-limit:{tid}", "阿奇索大额数量订单需人工审核", f"淘宝订单 {tid}：{detail}")

        if not matched_deliveries:
            detail = f"未命中任何 SKU 映射规则，已安全忽略（num_iids={','.join(sorted(set(unmapped_num_iids))) or '-'}）"
            self.ledger.record_agiso_event(
                source=source,
                from_platform=from_platform,
                aopic=aopic,
                tid=tid,
                oid="",
                status="ignored_unmapped",
                detail=detail,
                payload_summary=summary,
            )
            return {
                "status": "ignored_unmapped",
                "tid": tid,
                "codes": [],
                "unmapped_count": len(unmapped_num_iids),
                "retry_webhook": False,
            }

        return self._dispatch_agiso_trade_delivery(
            tid,
            source=source,
            from_platform=from_platform,
            aopic=aopic,
            payload_summary=summary,
            force_retry=False,
        )

    def _dispatch_agiso_trade_delivery(
        self,
        tid: str,
        *,
        source: str,
        from_platform: str,
        aopic: Optional[int],
        payload_summary: str = "",
        force_retry: bool = False,
    ) -> dict[str, Any]:
        claimed = self.ledger.claim_agiso_trade_delivery(tid, force_retry=force_retry)
        if not claimed:
            existing = self.ledger.list_agiso_deliveries_by_tid(tid)
            codes = [item["code"] for item in existing]
            active = [item for item in existing if item["delivery_status"] != "revoked"]
            if existing and not active:
                self.ledger.record_agiso_event(
                    source=source,
                    from_platform=from_platform,
                    aopic=aopic,
                    tid=tid,
                    oid="",
                    status="already_revoked",
                    detail=f"订单关联兑换码已作废，跳过重复发货（{len(codes)} 份）",
                    payload_summary=payload_summary,
                )
                return {"status": "already_revoked", "tid": tid, "codes": codes, "deliveries": existing, "retry_webhook": False}
            if active and all(item["delivery_status"] == "delivered" for item in active):
                self.ledger.record_agiso_event(
                    source=source,
                    from_platform=from_platform,
                    aopic=aopic,
                    tid=tid,
                    oid="",
                    status="already_delivered",
                    detail=f"订单已发货，幂等跳过（{len(codes)} 份）",
                    payload_summary=payload_summary,
                )
                return {"status": "already_delivered", "tid": tid, "codes": codes, "deliveries": existing, "retry_webhook": False}
            self.ledger.record_agiso_event(
                source=source,
                from_platform=from_platform,
                aopic=aopic,
                tid=tid,
                oid="",
                status="in_flight",
                detail="订单正在发货处理中，并发重复请求已跳过",
                payload_summary=payload_summary,
            )
            return {"status": "in_flight", "tid": tid, "codes": codes, "deliveries": existing, "retry_webhook": False}

        codes = [item["code"] for item in claimed]
        if not self.agiso or not getattr(self.agiso, "is_ready", False):
            error_text = "AGISO_ACCESS_TOKEN_MISSING"
            updated = self.ledger.update_agiso_trade_deliveries(
                tid,
                {"delivery_status": "failed", "last_error": error_text},
            )
            self.ledger.record_agiso_event(
                source=source,
                from_platform=from_platform,
                aopic=aopic,
                tid=tid,
                oid="",
                status="delivery_failed",
                detail="已生成兑换码但未配置 AccessToken，无法调用阿奇索发货接口",
                payload_summary=payload_summary,
            )
            self._agiso_alert(
                f"delivery-token:{tid}",
                "阿奇索自动发货失败（需人工处理）",
                f"淘宝订单 {tid} 已生成兑换码 {', '.join(codes)}，但未配置 AccessToken，无法自动写入发货结果和标记发货。",
            )
            return {
                "status": "failed",
                "tid": tid,
                "codes": codes,
                "deliveries": updated,
                "errors": [error_text],
                "detail": error_text,
                "retry_webhook": False,
            }

        result_created = all(bool(row["result_created"]) for row in claimed)
        dummy_sent = all(bool(row["dummy_sent"]) for row in claimed)
        card_sent = all(bool(row["card_sent"]) for row in claimed)
        ww_sent = all(bool(row["ww_sent"]) for row in claimed)
        errors: list[str] = []
        transient_core_failure = False

        if self.settings.agiso_auto_create_result and not result_created:
            content = build_create_order_result_content(claimed, SKUS, self.settings.public_base_url)
            try:
                self.agiso.create_order_result(tid, content, alds_type=1)
                result_created = True
                self.ledger.update_agiso_trade_deliveries(tid, {"result_created": 1})
            except AgisoError as exc:
                errors.append(f"CreateOrderResult: {exc.message}")
                transient_core_failure = transient_core_failure or exc.transient

        if self.settings.agiso_auto_dummy_send and not dummy_sent and (result_created or not self.settings.agiso_auto_create_result):
            try:
                self.agiso.logistics_dummy_send([tid])
                dummy_sent = True
                self.ledger.update_agiso_trade_deliveries(tid, {"dummy_sent": 1})
            except AgisoError as exc:
                errors.append(f"LogisticsDummySend: {exc.message}")
                transient_core_failure = transient_core_failure or exc.transient

        if self.settings.agiso_auto_send_tb_card and not card_sent and result_created:
            try:
                self.agiso.send_tb_app_card(tid)
                card_sent = True
                self.ledger.update_agiso_trade_deliveries(tid, {"card_sent": 1})
            except AgisoError as exc:
                errors.append(f"SendTbAppCard: {exc.message}")

        if self.settings.agiso_auto_send_ww_msg and not ww_sent:
            msg = format_trade_delivery_message(claimed, SKUS, self.settings.public_base_url)
            try:
                self.agiso.send_ww_msg(tid, msg)
                ww_sent = True
                self.ledger.update_agiso_trade_deliveries(tid, {"ww_sent": 1})
            except AgisoError as exc:
                errors.append(f"WwMsg/Send: {exc.message}")

        need_result = self.settings.agiso_auto_create_result
        need_dummy = self.settings.agiso_auto_dummy_send
        need_card = self.settings.agiso_auto_send_tb_card and need_result
        need_ww = self.settings.agiso_auto_send_ww_msg

        all_done = (
            (not need_result or result_created)
            and (not need_dummy or dummy_sent)
            and (not need_card or card_sent)
            and (not need_ww or ww_sent)
        )
        any_done = result_created or dummy_sent or card_sent or ww_sent
        if all_done:
            final_status = "delivered"
            last_error = None
        elif any_done:
            final_status = "partial"
            last_error = "; ".join(errors)[:450] or "部分发货步骤未完成"
        else:
            final_status = "failed"
            last_error = "; ".join(errors)[:450] or "发货失败"

        updated = self.ledger.update_agiso_trade_deliveries(
            tid,
            {
                "delivery_status": final_status,
                "result_created": int(result_created),
                "dummy_sent": int(dummy_sent),
                "card_sent": int(card_sent),
                "ww_sent": int(ww_sent),
                "last_error": last_error,
            },
        )
        self.ledger.record_agiso_event(
            source=source,
            from_platform=from_platform,
            aopic=aopic,
            tid=tid,
            oid="",
            status=final_status,
            detail=last_error or f"自动发货完成（{len(codes)} 份）",
            payload_summary=payload_summary,
        )
        if final_status != "delivered":
            self._agiso_alert(
                f"delivery:{tid}:{final_status}",
                "阿奇索自动发货异常（需人工处理）",
                (
                    f"淘宝订单 {tid} 自动发货状态：{final_status}\n"
                    f"已生成兑换码：{', '.join(codes)}\n"
                    f"步骤状态：结果={result_created}, 虚拟发货={dummy_sent}, 手淘卡片={card_sent}, 旺旺消息={ww_sent}\n"
                    f"错误详情：{last_error}"
                ),
            )
        return {
            "status": final_status,
            "tid": tid,
            "codes": codes,
            "deliveries": updated,
            "errors": errors,
            "detail": last_error or "",
            "retry_webhook": final_status == "failed" and transient_core_failure,
        }

    def handle_agiso_refund(
        self,
        payload: dict[str, Any],
        *,
        aopic: int,
        from_platform: str = "TbAlds",
    ) -> dict[str, Any]:
        tid = str(payload.get("Tid") or "").strip()
        oid = str(payload.get("Oid") or "").strip()
        refund_id = str(payload.get("RefundId") or "").strip()
        refund_fee = str(payload.get("RefundFee") or "").strip()
        summary = self._summarize_agiso_payload(payload)

        candidate_codes: list[str] = []
        seen: set[str] = set()
        for row in self.ledger.list_agiso_deliveries_for_refund(tid, oid):
            if row["code"] not in seen:
                seen.add(row["code"])
                candidate_codes.append(row["code"])
        if not candidate_codes and tid:
            has_other_suborder_deliveries = bool(oid and oid != tid and self.ledger.list_agiso_deliveries_by_tid(tid))
            if not has_other_suborder_deliveries:
                for row in self.ledger.find_codes_by_external_order(tid):
                    if row["code"] not in seen:
                        seen.add(row["code"])
                        candidate_codes.append(row["code"])

        if not candidate_codes:
            self.ledger.record_agiso_event(
                source="webhook",
                from_platform=from_platform,
                aopic=aopic,
                tid=tid,
                oid=oid,
                status="refund_no_code",
                detail=f"未找到订单 {tid} 关联的兑换码（RefundId={refund_id or '-'}）",
                payload_summary=summary,
            )
            return {"status": "refund_no_code", "tid": tid, "oid": oid, "refund_id": refund_id}

        revoked_codes: list[str] = []
        already_revoked: list[str] = []
        alerted_codes: list[str] = []

        for code in candidate_codes:
            row = self.ledger.get_code(code)
            if not row:
                continue
            if self.settings.agiso_auto_revoke_on_refund and row["status"] == "unused":
                changed = self.ledger.transition(
                    code,
                    "revoked",
                    f"agiso_refund:{aopic}:{refund_id or tid}",
                    {
                        "last_error_code": "REFUNDED",
                        "last_error_message": f"淘宝退款自动作废（RefundId={refund_id or tid}）",
                    },
                    ("unused",),
                )
                if changed:
                    self.ledger.mark_agiso_delivery_revoked(code)
                    revoked_codes.append(code)
                    continue
                row = self.ledger.get_code(code)
            if row["status"] == "revoked":
                self.ledger.mark_agiso_delivery_revoked(code)
                already_revoked.append(code)
                continue
            alerted_codes.append(code)
            is_unused = row["status"] == "unused"
            subject = (
                "淘宝退款需人工处理（未开启自动作废）"
                if is_unused
                else "淘宝退款需人工处理（兑换码已使用）"
            )
            self._agiso_alert(
                f"refund-used:{code}:{refund_id or aopic}",
                subject,
                (
                    f"淘宝订单 {tid}（子订单 {oid or '-'}，退款单 {refund_id or '-'}，退款金额 {refund_fee or '-'}）收到退款通知（aopic={aopic}），\n"
                    f"但对应兑换码 {code} 当前状态为 {row['status']}（买家邮箱：{row.get('buyer_email') or row.get('buyer_email_masked') or '-'}，"
                    f"上游单号：{row.get('upstream_order_code') or '-'}），系统未自动作废，请立即人工核对。"
                ),
            )

        if alerted_codes:
            event_status = "refund_alert_sent"
            detail = f"已核对退款：自动作废 {len(revoked_codes)} 个，需人工处理 {len(alerted_codes)} 个（{','.join(alerted_codes)}）"
        elif revoked_codes:
            event_status = "refund_revoked"
            detail = f"买家未提货，已自动作废兑换码：{','.join(revoked_codes)}"
        else:
            event_status = "refund_already_revoked"
            detail = f"相关兑换码此前已作废：{','.join(already_revoked)}"

        self.ledger.record_agiso_event(
            source="webhook",
            from_platform=from_platform,
            aopic=aopic,
            tid=tid,
            oid=oid,
            status=event_status,
            detail=detail,
            payload_summary=summary,
        )
        return {
            "status": event_status,
            "tid": tid,
            "oid": oid,
            "refund_id": refund_id,
            "revoked_codes": revoked_codes,
            "already_revoked": already_revoked,
            "alerted_codes": alerted_codes,
        }

    def handle_agiso_refund_closed(
        self,
        payload: dict[str, Any],
        *,
        from_platform: str = "TbAlds",
    ) -> dict[str, Any]:
        tid = str(payload.get("Tid") or "").strip()
        oid = str(payload.get("Oid") or "").strip()
        refund_id = str(payload.get("RefundId") or "").strip()
        refund_fee = str(payload.get("RefundFee") or "").strip()
        summary = self._summarize_agiso_payload(payload)

        candidate_codes: list[str] = []
        seen: set[str] = set()
        for row in self.ledger.list_agiso_deliveries_for_refund(tid, oid):
            if row["code"] not in seen:
                seen.add(row["code"])
                candidate_codes.append(row["code"])
        if not candidate_codes and tid:
            has_other_suborder_deliveries = bool(oid and oid != tid and self.ledger.list_agiso_deliveries_by_tid(tid))
            if not has_other_suborder_deliveries:
                for row in self.ledger.find_codes_by_external_order(tid):
                    if row["code"] not in seen:
                        seen.add(row["code"])
                        candidate_codes.append(row["code"])

        if not candidate_codes:
            self.ledger.record_agiso_event(
                source="webhook",
                from_platform=from_platform,
                aopic=AOPIC_REFUND_CLOSED,
                tid=tid,
                oid=oid,
                status="refund_close_no_code",
                detail=f"撤销退款未找到订单 {tid} 关联的兑换码（RefundId={refund_id or '-'}）",
                payload_summary=summary,
            )
            return {"status": "refund_close_no_code", "tid": tid, "oid": oid, "refund_id": refund_id}

        refund_succeeded = self.ledger.has_agiso_refund_success(tid, oid)
        allowed_reasons: set[str] = set()
        if tid:
            allowed_reasons.add(f"agiso_refund:{AOPIC_REFUND_CREATED}:{tid}")
        if refund_id:
            allowed_reasons.add(f"agiso_refund:{AOPIC_REFUND_CREATED}:{refund_id}")

        restored_codes: list[str] = []
        already_active: list[str] = []
        alerted_codes: list[str] = []

        for code in candidate_codes:
            row = self.ledger.get_code(code)
            if not row:
                continue
            if row["status"] != "revoked":
                already_active.append(code)
                continue
            latest_audit = self.ledger.latest_revoke_audit(code)
            audit_reason = (latest_audit.get("reason") or "") if latest_audit else ""
            can_auto_restore = (
                self.settings.agiso_auto_restore_on_refund_close
                and not refund_succeeded
                and (row.get("last_error_code") or "") == "REFUNDED"
                and row.get("submitted_at") is None
                and row.get("upstream_order_code") is None
                and audit_reason in allowed_reasons
            )
            if can_auto_restore:
                changed = self.ledger.transition(
                    code,
                    "unused",
                    f"agiso_refund_closed:{refund_id or tid}",
                    {
                        "last_error_code": None,
                        "last_error_message": None,
                    },
                    ("revoked",),
                )
                if changed:
                    self.ledger.mark_agiso_delivery_restored(code)
                    restored_codes.append(code)
                    continue
                refreshed = self.ledger.get_code(code)
                if refreshed and refreshed["status"] != "revoked":
                    already_active.append(code)
                    continue

            alerted_codes.append(code)
            self._agiso_alert(
                f"refund-close-manual:{code}:{refund_id or tid}",
                "淘宝买家撤销退款需人工核对（兑换码未自动恢复）",
                (
                    f"淘宝订单 {tid}（子订单 {oid or '-'}，退款单 {refund_id or '-'}，退款金额 {refund_fee or '-'}）收到撤销退款通知（aopic=32768），\n"
                    f"但兑换码 {code} 当前为已作废状态（最近作废原因：{audit_reason or '-'}，错误码：{row.get('last_error_code') or '-'}），\n"
                    f"未满足安全自动恢复条件（auto_restore={self.settings.agiso_auto_restore_on_refund_close}，refund_success={refund_succeeded}），请人工核对。"
                ),
            )

        if alerted_codes:
            event_status = "refund_close_manual_review"
            detail = f"撤销退款核对完成：自动恢复 {len(restored_codes)} 个，需人工核对 {len(alerted_codes)} 个（{','.join(alerted_codes)}）"
        elif restored_codes:
            event_status = "refund_close_restored"
            detail = f"买家已撤销退款，自动恢复兑换码：{','.join(restored_codes)}"
        else:
            event_status = "refund_close_noop"
            detail = f"撤销退款关联兑换码已处于可用或处理中状态：{','.join(already_active)}"

        self.ledger.record_agiso_event(
            source="webhook",
            from_platform=from_platform,
            aopic=AOPIC_REFUND_CLOSED,
            tid=tid,
            oid=oid,
            status=event_status,
            detail=detail,
            payload_summary=summary,
        )
        return {
            "status": event_status,
            "tid": tid,
            "oid": oid,
            "refund_id": refund_id,
            "restored_codes": restored_codes,
            "already_active": already_active,
            "alerted_codes": alerted_codes,
        }

    def handle_agiso_sent_cards(self, payload: dict[str, Any], *, from_platform: str = "TbAlds") -> dict[str, Any]:
        tid = str(payload.get("Tid") or "").strip()
        buyer_nick = str(payload.get("BuyerNick") or "").strip()
        summary = self._summarize_agiso_payload(payload)
        extracted = extract_sent_card_entries(payload)
        bound: list[str] = []
        bound_sku_keys: set[str] = set()
        newly_bound_oids: set[str] = set()
        already_bound: list[str] = []
        unmatched: list[str] = []
        conflicts: list[str] = []

        for entry in extracted:
            code = entry["code"]
            entry_oid = entry["oid"] or tid
            if not tid:
                unmatched.append(code)
                continue
            result = self.ledger.bind_code_external_order(code, tid, reason="agiso_card_bound")
            if result in ("bound", "already_bound"):
                code_row = self.ledger.get_code(code)
                if code_row:
                    self.ledger.record_agiso_native_delivery(
                        tid=tid,
                        oid=entry_oid,
                        code=code,
                        sku_key=code_row["sku_key"],
                        buyer_nick=buyer_nick,
                        num_iid=entry["num_iid"],
                        sku_id=entry["sku_id"],
                        delivery_status="revoked" if code_row["status"] == "revoked" else "delivered",
                    )
                if result == "bound":
                    bound.append(code)
                    if code_row:
                        bound_sku_keys.add(code_row["sku_key"])
                    newly_bound_oids.add(entry_oid)
                else:
                    already_bound.append(code)
            elif result == "conflict":
                conflicts.append(code)
            else:
                unmatched.append(code)

        if conflicts:
            self._agiso_alert(
                f"cards-conflict:{tid}:{','.join(sorted(conflicts))}",
                "阿奇索自动发卡发现兑换码冲突（需人工处理）",
                (
                    f"淘宝订单 {tid} 收到自动发货成功推送（aopic=2048），但以下兑换码此前已绑定其他订单号：{', '.join(conflicts)}。\n"
                    "请立即核查阿奇索卡密库存是否存在重复卡密或误导入，避免一码多发。"
                ),
            )

        status = "cards_bound" if bound else ("cards_conflict" if conflicts else "cards_noop")
        detail = f"解析出 {len(extracted)} 个兑换码：新绑定 {len(bound)}，已绑定 {len(already_bound)}，冲突 {len(conflicts)}，未知 {len(unmatched)}"
        self.ledger.record_agiso_event(
            source="webhook",
            from_platform=from_platform,
            aopic=AOPIC_AUTO_SEND_SUCCESS,
            tid=tid,
            oid="",
            status=status,
            detail=detail,
            payload_summary=summary,
        )

        replayed_refunds: list[dict[str, Any]] = []
        if tid and newly_bound_oids:
            seen_refund_events: set[int] = set()
            seen_refund_keys: set[tuple[int, str, str]] = set()
            for entry_oid in sorted(newly_bound_oids):
                for pending in self.ledger.find_pending_unmatched_refund(tid, entry_oid):
                    ev_id = int(pending["event_id"])
                    if ev_id in seen_refund_events:
                        continue
                    seen_refund_events.add(ev_id)
                    pending_summary: dict[str, Any] = {}
                    try:
                        pending_summary = json.loads(pending.get("payload_summary") or "{}")
                    except ValueError:
                        pending_summary = {}
                    pending_aopic = int(pending["aopic"])
                    replay_oid = str(pending.get("oid") or pending_summary.get("Oid") or entry_oid).strip()
                    replay_refund_id = str(pending_summary.get("RefundId") or "").strip()
                    dedupe_key = (pending_aopic, replay_oid, replay_refund_id)
                    if dedupe_key in seen_refund_keys:
                        continue
                    seen_refund_keys.add(dedupe_key)
                    replay_payload = {
                        "Tid": tid,
                        "Oid": replay_oid,
                        "RefundId": replay_refund_id,
                    }
                    replayed_refunds.append(
                        self.handle_agiso_refund(
                            replay_payload,
                            aopic=pending_aopic,
                            from_platform=pending.get("from_platform") or from_platform,
                        )
                    )

        return {
            "status": status,
            "tid": tid,
            "bound_codes": bound,
            "bound_sku_keys": sorted(bound_sku_keys),
            "already_bound": already_bound,
            "conflicts": conflicts,
            "unmatched": unmatched,
            "replayed_refunds": replayed_refunds,
        }

    def sync_agiso_orders(self, *, lookback_minutes: int = 60, page_size: int = 50, max_pages: int = 5) -> dict[str, Any]:
        if not self.agiso or not getattr(self.agiso, "is_ready", False):
            raise AgisoError("Agiso API credentials are not configured", status_code=503)
        tz_cn = timezone(timedelta(hours=8))
        start_str = (datetime.now(tz_cn) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        fetched = 0
        fulfilled = 0
        skipped = 0
        failed = 0
        items: list[dict[str, Any]] = []

        for page_no in range(1, max_pages + 1):
            trades = self.agiso.trades_buyer_pay(start_str, page_no=page_no, page_size=page_size)
            fetched += len(trades)
            for trade in trades:
                status_str = str(trade.get("Status") or "").strip()
                if status_str and status_str != "WAIT_SELLER_SEND_GOODS":
                    skipped += 1
                    continue
                outcome = self.fulfill_agiso_trade(trade, source="poll", from_platform="TbAlds", aopic=None)
                items.append({"tid": outcome.get("tid"), "status": outcome.get("status"), "codes": outcome.get("codes", [])})
                if outcome.get("status") == "delivered":
                    fulfilled += 1
                elif outcome.get("status") in {"failed", "partial"}:
                    failed += 1
                else:
                    skipped += 1
            if len(trades) < page_size:
                break
        return {
            "modify_time_start": start_str,
            "fetched": fetched,
            "fulfilled": fulfilled,
            "skipped": skipped,
            "failed": failed,
            "items": items,
        }

    def retry_agiso_order(self, tid: str, *, fetch_from_agiso: bool = True) -> Optional[dict[str, Any]]:
        clean_tid = tid.strip()
        existing = self.ledger.list_agiso_deliveries_by_tid(clean_tid)
        if not existing:
            if not fetch_from_agiso:
                return None
            if not self.agiso or not getattr(self.agiso, "is_ready", False):
                raise AgisoError("Agiso API credentials are not configured", status_code=503)
            trade = self.agiso.trade_info(clean_tid)
            fetched_status = str(trade.get("Status") or "").strip()
            if fetched_status and fetched_status != "WAIT_SELLER_SEND_GOODS":
                self.ledger.record_agiso_event(
                    source="admin_retry",
                    from_platform="TbAlds",
                    aopic=None,
                    tid=clean_tid,
                    oid="",
                    status="ignored_status",
                    detail=f"反查订单状态为 {fetched_status}，仅处理 WAIT_SELLER_SEND_GOODS",
                    payload_summary=self._summarize_agiso_payload(trade),
                )
                return {
                    "status": "ignored_status",
                    "tid": clean_tid,
                    "trade_status": fetched_status,
                    "codes": [],
                }
            return self.fulfill_agiso_trade(trade, source="admin_retry", from_platform="TbAlds", aopic=None)
        if all(str(row.get("delivery_key") or "").startswith("native:") for row in existing):
            return {
                "status": "already_bound_external",
                "tid": clean_tid,
                "codes": [row["code"] for row in existing],
                "deliveries": existing,
            }
        return self._dispatch_agiso_trade_delivery(
            clean_tid,
            source="admin_retry",
            from_platform="TbAlds",
            aopic=None,
            force_retry=True,
        )

    def get_agiso_remote_skus(self, num_iids: list[str]) -> list[dict[str, Any]]:
        if not self.agiso or not getattr(self.agiso, "is_ready", False):
            raise AgisoError("Agiso API credentials are not configured", status_code=503)
        cleaned = [item.strip() for item in num_iids if item and item.strip()]
        if not cleaned:
            raise ValueError("NUM_IIDS_REQUIRED")
        return self.agiso.item_skus_get(cleaned)

    def push_agiso_cards(
        self,
        *,
        sku_key: str,
        cpc_id: int,
        count: int,
        batch_label: str = "",
        is_use_first: bool = False,
        allow_replace: bool = False,
        source: str = "admin_push_cards",
    ) -> dict[str, Any]:
        if count < 1 or count > 500:
            raise ValueError("COUNT_OUT_OF_RANGE")
        if sku_key not in SKUS:
            raise ValueError("UNKNOWN_SKU")
        if not self.agiso or not getattr(self.agiso, "is_ready", False):
            raise AgisoError("Agiso API credentials are not configured", status_code=503)

        batch_id = "AG" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2).upper()
        sku = self.ledger.get_sku(sku_key)
        goods_no = sku.get("goods_no") if sku else None
        codes = self._generate_unique_codes(sku_key, count)
        self._persist_batch(
            batch_id=batch_id,
            sku_key=sku_key,
            channel="taobao",
            goods_no=goods_no,
            label=batch_label or f"agiso-cpc-{cpc_id}",
            codes=codes,
            remark=f"阿奇索卡密库推送(cpcId={cpc_id}, batch={batch_id})",
            audit_reason="agiso_push_cards_issued",
        )
        lines = [f"{build_redeem_url(self.settings.public_base_url, code)},{code}" for code in codes]
        try:
            agiso_result = self.agiso.card_pwd_add(
                cpc_id,
                lines,
                is_use_first=is_use_first,
                allow_replace=allow_replace,
                summary=batch_label or batch_id,
            )
        except AgisoError as exc:
            if not exc.transient and exc.status_code == 200:
                for code in codes:
                    self.ledger.transition(
                        code,
                        "revoked",
                        "agiso_push_cards_failed",
                        {"last_error_code": "AGISO_PUSH_FAILED", "last_error_message": exc.message},
                        ("unused",),
                    )
            self.ledger.record_agiso_event(
                source=source,
                from_platform="TbAlds",
                aopic=None,
                tid="",
                oid="",
                status="push_cards_failed",
                detail=f"batch={batch_id}, cpcId={cpc_id}: {exc.message}",
            )
            raise

        def _to_int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        add_success = _to_int(agiso_result.get("AddSuccessCount"))
        add_fail = _to_int(agiso_result.get("AddFailCount"))
        replace_success = _to_int(agiso_result.get("ReplaceUnSoldSuccessCount")) + _to_int(
            agiso_result.get("ReplaceSoldSuccessCount")
        )
        replace_fail = _to_int(agiso_result.get("ReplaceFailCount"))
        total_success = add_success + replace_success
        total_fail = add_fail + replace_fail
        result_msg = str(agiso_result.get("ResultMsg") or "").strip()

        if total_success == 0 and (total_fail > 0 or ("AddSuccessCount" in agiso_result and count > 0)):
            error_detail = (
                f"Agiso CardPwd/Add 全部失败 (cpcId={cpc_id}, fail={total_fail}, msg={result_msg or 'AddSuccessCount=0'})"
            )
            for code in codes:
                self.ledger.transition(
                    code,
                    "revoked",
                    "agiso_push_cards_failed",
                    {"last_error_code": "AGISO_PUSH_FAILED", "last_error_message": error_detail},
                    ("unused",),
                )
            self.ledger.record_agiso_event(
                source=source,
                from_platform="TbAlds",
                aopic=None,
                tid="",
                oid="",
                status="push_cards_failed",
                detail=f"batch={batch_id}, {error_detail}",
            )
            raise AgisoError(error_detail, status_code=200, transient=False, data=agiso_result)

        if total_fail > 0:
            partial_detail = (
                f"batch={batch_id}, cpcId={cpc_id}, success={total_success}, fail={total_fail}, msg={result_msg or '-'}"
            )
            self.ledger.record_agiso_event(
                source=source,
                from_platform="TbAlds",
                aopic=None,
                tid="",
                oid="",
                status="push_cards_partial",
                detail=partial_detail,
            )
            self._agiso_alert(
                f"push-partial:{batch_id}",
                "阿奇索卡密库推卡部分失败（需人工核对）",
                (
                    f"SKU {sku_key}（卡种 cpcId={cpc_id}，批次 {batch_id}）共生成 {count} 个码，"
                    f"阿奇索入库成功 {total_success} 个、失败 {total_fail} 个（ResultMsg={result_msg or '-'}）。\n"
                    "为避免误作废已入库的码，本次未自动回滚，请去阿奇索卡密库核对缺失卡密。"
                ),
            )
            return {
                "batch_id": batch_id,
                "sku_key": sku_key,
                "cpc_id": cpc_id,
                "count": count,
                "codes": codes,
                "partial_failure": True,
                "add_success_count": add_success,
                "add_fail_count": add_fail,
                "replace_fail_count": replace_fail,
                "agiso_result": agiso_result,
            }

        self.ledger.record_agiso_event(
            source=source,
            from_platform="TbAlds",
            aopic=None,
            tid="",
            oid="",
            status="push_cards_ok",
            detail=f"batch={batch_id}, cpcId={cpc_id}, count={count}, added={total_success}",
        )
        return {
            "batch_id": batch_id,
            "sku_key": sku_key,
            "cpc_id": cpc_id,
            "count": count,
            "codes": codes,
            "partial_failure": False,
            "add_success_count": add_success,
            "add_fail_count": add_fail,
            "replace_fail_count": replace_fail,
            "agiso_result": agiso_result,
        }

    @staticmethod
    def _parse_storage_map(storage_rows: list[dict[str, Any]]) -> dict[int, int]:
        qty_by_cpc: dict[int, int] = {}
        for entry in storage_rows:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("IdNo") if "IdNo" in entry else entry.get("idNo")
            raw_qty = entry.get("Qty") if "Qty" in entry else entry.get("qty")
            if raw_id is None:
                continue
            try:
                qty_by_cpc[int(str(raw_id).strip())] = int(raw_qty or 0)
            except (TypeError, ValueError):
                continue
        return qty_by_cpc

    def get_agiso_card_stock(self, sku_keys: Optional[list[str]] = None) -> dict[str, Any]:
        if not self.agiso or not getattr(self.agiso, "is_ready", False):
            raise AgisoError("Agiso API credentials are not configured", status_code=503)
        rules = self.agiso_restock_rules(enabled_only=False)
        if sku_keys:
            wanted = {item.strip() for item in sku_keys if item and item.strip()}
            rules = [rule for rule in rules if rule["sku_key"] in wanted]
        if not rules:
            return {"restock_enabled": self.settings.agiso_restock_enabled, "items": []}
        cpc_ids = list(dict.fromkeys(rule["cpc_id"] for rule in rules))
        storage_rows = self.agiso.card_pwd_category_get_storage(cpc_ids)
        qty_by_cpc = self._parse_storage_map(storage_rows)
        items: list[dict[str, Any]] = []
        for rule in rules:
            cpc_id = rule["cpc_id"]
            found = cpc_id in qty_by_cpc
            current_qty = qty_by_cpc.get(cpc_id) if found else None
            planned_push = 0
            if found and current_qty is not None and rule["enabled"] and current_qty <= rule["min_stock"]:
                planned_push = min(max(0, rule["target_stock"] - current_qty), rule["max_push"])
            sku_key = rule["sku_key"]
            items.append(
                {
                    "sku_key": sku_key,
                    "sku_title": SKUS[sku_key]["title"],
                    "cpc_id": cpc_id,
                    "enabled": rule["enabled"],
                    "min_stock": rule["min_stock"],
                    "target_stock": rule["target_stock"],
                    "max_push": rule["max_push"],
                    "current_qty": current_qty,
                    "storage_found": found,
                    "needs_restock": planned_push > 0,
                    "planned_push": planned_push,
                    "local_unused_taobao": self._count(
                        "sku_key=? AND channel='taobao' AND status='unused'",
                        (sku_key,),
                    ),
                    "local_in_stock_taobao": self._count(
                        "sku_key=? AND channel='taobao' AND status='unused' AND COALESCE(TRIM(external_order_id), '') = ''",
                        (sku_key,),
                    ),
                    "local_dispatched_unused_taobao": self._count(
                        "sku_key=? AND channel='taobao' AND status='unused' AND COALESCE(TRIM(external_order_id), '') <> ''",
                        (sku_key,),
                    ),
                }
            )
        return {"restock_enabled": self.settings.agiso_restock_enabled, "items": items}

    def check_and_restock_agiso_cards(
        self,
        *,
        sku_keys: Optional[list[str]] = None,
        dry_check: bool = False,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        with self._restock_lock:
            if not self.agiso or not getattr(self.agiso, "is_ready", False):
                raise AgisoError("Agiso API credentials are not configured", status_code=503)
            rules = self.agiso_restock_rules(enabled_only=True)
            if sku_keys:
                wanted = {item.strip() for item in sku_keys if item and item.strip()}
                rules = [rule for rule in rules if rule["sku_key"] in wanted]
            if not rules:
                return {
                    "trigger": trigger,
                    "dry_check": dry_check,
                    "checked": 0,
                    "restocked": 0,
                    "total_pushed": 0,
                    "errors": [],
                    "items": [],
                }

            hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
            cpc_ids = list(dict.fromkeys(rule["cpc_id"] for rule in rules))
            try:
                storage_rows = self.agiso.card_pwd_category_get_storage(cpc_ids)
            except AgisoError as exc:
                logger.warning(
                    "agiso restock storage query failed trigger=%s cpc_ids=%s detail=%s",
                    trigger,
                    cpc_ids,
                    exc.message,
                )
                self.ledger.record_agiso_event(
                    source=f"restock:{trigger}",
                    from_platform="TbAlds",
                    aopic=None,
                    tid="",
                    oid="",
                    status="restock_storage_error",
                    detail=exc.message,
                )
                raise

            qty_by_cpc = self._parse_storage_map(storage_rows)
            restocked = 0
            total_pushed = 0
            errors: list[str] = []
            items: list[dict[str, Any]] = []

            for rule in rules:
                sku_key = rule["sku_key"]
                cpc_id = rule["cpc_id"]
                if cpc_id not in qty_by_cpc:
                    msg = f"{sku_key}(cpcId={cpc_id}) 未出现在 GetStorage 返回结果中"
                    errors.append(msg)
                    self._agiso_alert(
                        f"restock-missing-cpc:{cpc_id}:{hour_bucket}",
                        "阿奇索自动补卡未找到对应卡种（需人工排查）",
                        f"自动补卡规则配置了 {sku_key} -> cpcId={cpc_id}，但阿奇索库存查询未返回该卡种，请核对卡种 ID 是否正确。",
                    )
                    items.append(
                        {
                            "sku_key": sku_key,
                            "cpc_id": cpc_id,
                            "current_qty": None,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "missing_cpc",
                            "pushed_count": 0,
                            "error": msg,
                        }
                    )
                    continue

                current_qty = qty_by_cpc[cpc_id]
                if current_qty > rule["min_stock"]:
                    items.append(
                        {
                            "sku_key": sku_key,
                            "cpc_id": cpc_id,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "sufficient",
                            "pushed_count": 0,
                        }
                    )
                    continue

                needed = min(max(0, rule["target_stock"] - current_qty), rule["max_push"])
                if needed <= 0:
                    items.append(
                        {
                            "sku_key": sku_key,
                            "cpc_id": cpc_id,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "noop",
                            "pushed_count": 0,
                        }
                    )
                    continue

                if dry_check:
                    items.append(
                        {
                            "sku_key": sku_key,
                            "cpc_id": cpc_id,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "would_restock",
                            "planned_push": needed,
                            "pushed_count": 0,
                        }
                    )
                    continue

                try:
                    push_res = self.push_agiso_cards(
                        sku_key=sku_key,
                        cpc_id=cpc_id,
                        count=needed,
                        batch_label=f"auto-restock-{trigger}-{sku_key}",
                        is_use_first=rule["is_use_first"],
                        allow_replace=rule["allow_replace"],
                        source=f"restock:{trigger}",
                    )
                except AgisoError as exc:
                    err_msg = f"{sku_key}(cpcId={cpc_id}): {exc.message}"
                    errors.append(err_msg)
                    self._agiso_alert(
                        f"restock-push:{sku_key}:{hour_bucket}",
                        "阿奇索自动补卡推送失败（需人工处理）",
                        f"触发来源 {trigger} 为 {sku_key}（cpcId={cpc_id}，当前库存 {current_qty}）自动补发 {needed} 个卡密失败：{exc.message}",
                    )
                    items.append(
                        {
                            "sku_key": sku_key,
                            "cpc_id": cpc_id,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "failed",
                            "pushed_count": 0,
                            "error": exc.message,
                        }
                    )
                    continue

                restocked += 1
                pushed_ok = int(push_res.get("add_success_count") or 0)
                if pushed_ok == 0 and not push_res.get("partial_failure"):
                    pushed_ok = needed
                total_pushed += pushed_ok
                qty_by_cpc[cpc_id] = current_qty + pushed_ok
                action = "partial" if push_res.get("partial_failure") else "restocked"
                if push_res.get("partial_failure"):
                    errors.append(
                        f"{sku_key}(cpcId={cpc_id}): 部分入库失败 (success={pushed_ok}, fail={push_res.get('add_fail_count', 0)})"
                    )
                items.append(
                    {
                        "sku_key": sku_key,
                        "cpc_id": cpc_id,
                        "current_qty": current_qty,
                        "after_qty": qty_by_cpc[cpc_id],
                        "min_stock": rule["min_stock"],
                        "target_stock": rule["target_stock"],
                        "action": action,
                        "batch_id": push_res["batch_id"],
                        "pushed_count": pushed_ok,
                        "agiso_result": push_res["agiso_result"],
                    }
                )

            return {
                "trigger": trigger,
                "dry_check": dry_check,
                "checked": len(rules),
                "restocked": restocked,
                "total_pushed": total_pushed,
                "errors": errors,
                "items": items,
            }

    def _channel16688_ready(self) -> bool:
        return bool(self.channel is not None and getattr(self.channel, "is_ready", True))

    @staticmethod
    def _parse_16688_int(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _extract_16688_available_qty(cls, stock: dict[str, Any]) -> Optional[int]:
        raw_avail = stock.get("available_quantity")
        avail = cls._parse_16688_int(raw_avail)
        if avail is not None:
            return avail
        raw_total = stock.get("total")
        total = cls._parse_16688_int(raw_total)
        if total is not None:
            return total
        avail_blank = raw_avail is None or (isinstance(raw_avail, str) and not raw_avail.strip())
        total_blank = raw_total is None or (isinstance(raw_total, str) and not raw_total.strip())
        return 0 if (avail_blank and total_blank) else None

    def _channel16688_alert(self, dedupe_key: str, subject: str, body: str) -> None:
        if self.ledger.claim_notification(f"channel16688:{dedupe_key}"):
            self._safe_send(subject, body)

    def channel16688_restock_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        try:
            rules = parse_env_channel16688_restock_rules(self.settings.channel16688_restock_rules_json)
        except ValueError as exc:
            logger.warning("invalid PLAN2_CHANNEL16688_RESTOCK_RULES_JSON: %s", exc)
            return []
        valid: list[dict[str, Any]] = []
        seen_resolved_goods: set[str] = set()
        for rule in rules:
            sku_key = rule["sku_key"]
            if sku_key not in SKUS:
                logger.warning("ignoring 16688 restock rule with unknown sku_key=%s", sku_key)
                continue
            if enabled_only and not rule.get("enabled"):
                continue
            sku_row = self.ledger.get_sku(sku_key) or {}
            resolved_goods = (rule.get("goods_no") or "").strip() or (sku_row.get("goods_no") or "").strip()
            if resolved_goods:
                if resolved_goods in seen_resolved_goods:
                    logger.warning(
                        "ignoring duplicate resolved 16688 restock rule sku_key=%s goods_no=%s",
                        sku_key,
                        resolved_goods,
                    )
                    continue
                seen_resolved_goods.add(resolved_goods)
            valid.append({**rule, "goods_no": resolved_goods})
        return valid

    def sync_channel16688_sold_codes(
        self,
        *,
        sku_keys: Optional[list[str]] = None,
        max_pages: int = 5,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if not self._channel16688_ready():
            raise UpstreamError(503, "16688 API credentials are not configured", rejected=True)
        wanted = {item.strip() for item in (sku_keys or []) if item and item.strip()}
        targets: list[tuple[str, str]] = []
        seen_goods: set[str] = set()
        rules = self.channel16688_restock_rules(enabled_only=False)
        for rule in rules:
            sku_key = rule["sku_key"]
            goods_no = (rule.get("goods_no") or "").strip()
            if wanted and sku_key not in wanted:
                continue
            if goods_no and goods_no not in seen_goods:
                seen_goods.add(goods_no)
                targets.append((sku_key, goods_no))
        if not targets:
            for sku_key in SKUS:
                if wanted and sku_key not in wanted:
                    continue
                sku_row = self.ledger.get_sku(sku_key) or {}
                goods_no = (sku_row.get("goods_no") or "").strip()
                if goods_no and goods_no not in seen_goods:
                    seen_goods.add(goods_no)
                    targets.append((sku_key, goods_no))

        scanned = 0
        bound = 0
        already_bound = 0
        missing = 0
        conflicts = 0
        errors: list[str] = []
        per_sku: list[dict[str, Any]] = []
        any_truncated = False
        page_limit = max(1, int(max_pages))

        for sku_key, goods_no in targets:
            sku_scanned = 0
            sku_bound = 0
            sku_already = 0
            sku_error = ""
            sku_truncated = False
            try:
                for page_no in range(1, page_limit + 1):
                    resp = self.channel.stock_secret_list(
                        goods_no,
                        status=1,
                        page_no=page_no,
                        page_size=page_size,
                    )
                    rows = resp.get("list") or []
                    total = self._parse_16688_int(resp.get("total")) or 0
                    for entry in rows:
                        if not isinstance(entry, dict):
                            continue
                        try:
                            entry_status = int(entry.get("status", -1))
                        except (TypeError, ValueError):
                            entry_status = -1
                        trade_no = str(entry.get("trade_no") or "").strip()
                        if entry_status != 2 or not trade_no:
                            continue
                        secret_text = str(entry.get("secret_code") or "")
                        extracted_codes = list(dict.fromkeys(PLAN2_CODE_RE.findall(secret_text)))
                        if len(extracted_codes) > 1:
                            conflicts += 1
                            continue
                        for found_code in extracted_codes:
                            sku_scanned += 1
                            scanned += 1
                            code_row = self.ledger.get_code(found_code)
                            if not code_row or code_row.get("channel") != "16688":
                                missing += 1
                                continue
                            code_goods = code_row.get("goods_no") or self.ledger.get_sku(code_row["sku_key"])["goods_no"]
                            if code_row["sku_key"] != sku_key or code_goods != goods_no:
                                conflicts += 1
                                continue
                            result = self.ledger.bind_code_external_order(
                                found_code,
                                trade_no,
                                reason="channel16688_sold_sync",
                            )
                            if result == "bound":
                                bound += 1
                                sku_bound += 1
                            elif result == "already_bound":
                                already_bound += 1
                                sku_already += 1
                            elif result == "conflict":
                                conflicts += 1
                            else:
                                missing += 1
                    if not rows or len(rows) < page_size or (total > 0 and page_no * page_size >= total):
                        break
                    if page_no == page_limit:
                        sku_truncated = True
                        any_truncated = True
            except (UpstreamTimeout, UpstreamError) as exc:
                sku_error = str(exc) or type(exc).__name__
                errors.append(f"{sku_key}({goods_no}): {sku_error}")
            per_sku.append(
                {
                    "sku_key": sku_key,
                    "goods_no": goods_no,
                    "scanned": sku_scanned,
                    "bound": sku_bound,
                    "already_bound": sku_already,
                    "truncated": sku_truncated,
                    "error": sku_error or None,
                }
            )

        return {
            "scanned": scanned,
            "bound": bound,
            "already_bound": already_bound,
            "missing": missing,
            "conflicts": conflicts,
            "truncated": any_truncated,
            "errors": errors,
            "items": per_sku,
        }

    def get_channel16688_stock(self, sku_keys: Optional[list[str]] = None) -> dict[str, Any]:
        if not self._channel16688_ready():
            raise UpstreamError(503, "16688 API credentials are not configured", rejected=True)
        rules = self.channel16688_restock_rules(enabled_only=False)
        if sku_keys:
            wanted = {item.strip() for item in sku_keys if item and item.strip()}
            rules = [rule for rule in rules if rule["sku_key"] in wanted]
        if not rules:
            return {
                "restock_enabled": self.settings.channel16688_restock_enabled,
                "restock_interval_seconds": self.settings.channel16688_restock_interval_seconds,
                "items": [],
            }
        items: list[dict[str, Any]] = []
        for rule in rules:
            sku_key = rule["sku_key"]
            goods_no = (rule.get("goods_no") or "").strip()
            current_qty: Optional[int] = None
            locked_qty: Optional[int] = None
            sold_qty: Optional[int] = None
            stock_found = False
            item_error: Optional[str] = None
            if not goods_no:
                item_error = "GOODS_NO_REQUIRED"
            else:
                stock = self.channel.stock_secret_list(goods_no, status=0, page_no=1, page_size=1)
                current_qty = self._extract_16688_available_qty(stock)
                locked_qty = self._parse_16688_int(stock.get("locked_quantity")) or 0
                sold_qty = self._parse_16688_int(stock.get("sold_quantity")) or 0
                if current_qty is None:
                    item_error = "INVALID_STOCK_RESPONSE"
                    stock_found = False
                else:
                    stock_found = True
            planned_push = 0
            if stock_found and current_qty is not None and rule["enabled"] and current_qty <= rule["min_stock"]:
                planned_push = min(max(0, rule["target_stock"] - current_qty), rule["max_push"])
            goods_clause = " AND goods_no=?" if goods_no else ""
            count_params: tuple[Any, ...] = (sku_key, goods_no) if goods_no else (sku_key,)
            items.append(
                {
                    "sku_key": sku_key,
                    "sku_title": SKUS[sku_key]["title"],
                    "goods_no": goods_no,
                    "enabled": rule["enabled"],
                    "min_stock": rule["min_stock"],
                    "target_stock": rule["target_stock"],
                    "max_push": rule["max_push"],
                    "current_qty": current_qty,
                    "locked_qty": locked_qty,
                    "sold_qty": sold_qty,
                    "stock_found": stock_found,
                    "needs_restock": planned_push > 0,
                    "planned_push": planned_push,
                    "error": item_error,
                    "local_unused_16688": self._count(
                        f"sku_key=?{goods_clause} AND channel='16688' AND status='unused'",
                        count_params,
                    ),
                    "local_in_stock_16688": self._count(
                        f"sku_key=?{goods_clause} AND channel='16688' AND {IN_STOCK_SQL}",
                        count_params,
                    ),
                    "local_dispatched_unused_16688": self._count(
                        f"sku_key=?{goods_clause} AND channel='16688' AND {DISPATCHED_UNUSED_SQL}",
                        count_params,
                    ),
                }
            )
        return {
            "restock_enabled": self.settings.channel16688_restock_enabled,
            "restock_interval_seconds": self.settings.channel16688_restock_interval_seconds,
            "items": items,
        }

    def _check_channel16688_available_stock(
        self, goods_no: str, sku_key: str,
    ) -> tuple[dict[str, Any], int]:
        page_size = 50
        page_no = 1
        stock = self.channel.stock_secret_list(goods_no, status=0, page_no=page_no, page_size=page_size)
        page = stock
        conflicts = 0
        seen_ids: set[int] = set()
        seen_codes: set[str] = set()
        while items := page.get("list"):
            entries: dict[int, set[str]] = {}
            page_ids: set[int] = set()
            for item in items:
                if not isinstance(item, dict) or self._parse_16688_int(item.get("status")) != 0 or item.get("trade_no"):
                    continue
                secret_id = self._parse_16688_int(item.get("id"))
                if secret_id is None or secret_id <= 0:
                    continue
                page_ids.add(secret_id)
                codes = set(PLAN2_CODE_RE.findall(str(item.get("secret_code") or "")))
                if codes:
                    entries[secret_id] = codes
            if len(items) >= page_size and page_ids <= seen_ids:
                raise UpstreamError(502, "16688 待售卡密分页重复", rejected=False)
            with closing(self.ledger.connect()) as conn:
                for secret_id, codes in entries.items():
                    if secret_id in seen_ids:
                        continue
                    placeholders = ",".join("?" for _ in codes)
                    known = conn.execute(
                        f"""SELECT code, status, goods_no, sku_key, external_order_id, upstream_order_code, last_error_code
                            FROM codes WHERE channel='16688' AND code IN ({placeholders})""",
                        tuple(codes),
                    ).fetchall()
                    if (
                        len(codes) != 1 or len(known) != 1
                        or known[0]["status"] != "unused" or (known[0]["external_order_id"] or "").strip()
                        or known[0]["goods_no"] != goods_no or known[0]["sku_key"] != sku_key or known[0]["code"] in seen_codes
                        or known[0]["upstream_order_code"]
                        or self.ledger._was_consumed(conn, known[0]["code"], known[0]["last_error_code"])
                    ):
                        conflicts += 1
                    else:
                        seen_codes.add(known[0]["code"])
            seen_ids.update(page_ids)
            if len(items) < page_size:
                break
            page_no += 1
            page = self.channel.stock_secret_list(goods_no, status=0, page_no=page_no, page_size=page_size)

        return stock, conflicts

    def check_and_restock_channel16688_cards(
        self,
        *,
        sku_keys: Optional[list[str]] = None,
        dry_check: bool = False,
        sync_sold: bool = True,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        with self._channel16688_restock_lock:
            if not self._channel16688_ready():
                raise UpstreamError(503, "16688 API credentials are not configured", rejected=True)
            rules = self.channel16688_restock_rules(enabled_only=True)
            if sku_keys:
                wanted = {item.strip() for item in sku_keys if item and item.strip()}
                rules = [rule for rule in rules if rule["sku_key"] in wanted]
            if not rules:
                return {
                    "trigger": trigger,
                    "dry_check": dry_check,
                    "checked": 0,
                    "restocked": 0,
                    "total_pushed": 0,
                    "sync_sold": {"skipped": True, "reason": "no_rules"},
                    "stock_conflicts": 0,
                    "errors": [],
                    "items": [],
                }

            hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
            target_skus = list(dict.fromkeys(rule["sku_key"] for rule in rules))
            if not sync_sold:
                sync_result: dict[str, Any] = {"skipped": True, "reason": "disabled"}
            elif dry_check:
                sync_result = {"skipped": True, "reason": "dry_check"}
            else:
                sync_result = self.sync_channel16688_sold_codes(sku_keys=target_skus)

            restocked = 0
            total_pushed = 0
            errors: list[str] = list(sync_result.get("errors") or [])
            items: list[dict[str, Any]] = []
            openapi_paused = False
            checked_stock = False
            stock_conflicts = 0

            for rule in rules:
                sku_key = rule["sku_key"]
                goods_no = (rule.get("goods_no") or "").strip()
                if not goods_no:
                    msg = f"{sku_key} 缺少 16688 商品编号 goods_no"
                    errors.append(msg)
                    if not dry_check:
                        self._channel16688_alert(
                            f"restock-missing-goods:{sku_key}:{hour_bucket}",
                            "16688 自动补卡缺少商品编号（需人工排查）",
                            f"触发来源 {trigger} 检查 {sku_key} 时未找到 16688 商品编号，请在管理台 SKU 配置或规则中补全 goods_no。",
                        )
                    items.append(
                        {
                            "sku_key": sku_key,
                            "goods_no": "",
                            "current_qty": None,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "missing_goods_no",
                            "pushed_count": 0,
                            "error": msg,
                        }
                    )
                    continue

                try:
                    conflicts = 0
                    if dry_check:
                        stock = self.channel.stock_secret_list(goods_no, status=0, page_no=1, page_size=1)
                    else:
                        stock, conflicts = self._check_channel16688_available_stock(goods_no, sku_key)
                        stock_conflicts += conflicts
                except (UpstreamTimeout, UpstreamError) as exc:
                    detail = str(exc) or type(exc).__name__
                    err_msg = f"{sku_key}({goods_no}) 查询库存失败: {detail}"
                    errors.append(err_msg)
                    if self._channel16688_unavailable(exc) or (
                        isinstance(exc, UpstreamError) and self._is_16688_openapi_paused_message(detail)
                    ):
                        openapi_paused = True
                    elif not dry_check:
                        logger.warning(
                            "16688 restock stock query failed trigger=%s sku=%s goods_no=%s detail=%s",
                            trigger,
                            sku_key,
                            goods_no,
                            detail,
                        )
                    items.append(
                        {
                            "sku_key": sku_key,
                            "goods_no": goods_no,
                            "current_qty": None,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "stock_error",
                            "pushed_count": 0,
                            "error": detail,
                        }
                    )
                    continue

                current_qty = self._extract_16688_available_qty(stock)
                if current_qty is None:
                    err_msg = f"{sku_key}({goods_no}) 返回的库存数据格式异常"
                    errors.append(err_msg)
                    if not dry_check:
                        logger.warning(
                            "16688 restock stock response invalid trigger=%s sku=%s goods_no=%s",
                            trigger,
                            sku_key,
                            goods_no,
                        )
                    items.append(
                        {
                            "sku_key": sku_key,
                            "goods_no": goods_no,
                            "current_qty": None,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "stock_error",
                            "pushed_count": 0,
                            "error": "INVALID_STOCK_RESPONSE",
                        }
                    )
                    continue
                checked_stock = True
                if conflicts:
                    err_msg = f"{sku_key}({goods_no}) 有 {conflicts} 条 16688 待售库存与本地账本冲突，已暂停该商品补货"
                    errors.append(err_msg)
                    logger.warning("%s", err_msg)
                    items.append(
                        {
                            "sku_key": sku_key,
                            "goods_no": goods_no,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "stock_conflict",
                            "pushed_count": 0,
                            "error": err_msg,
                        }
                    )
                    continue
                if current_qty > rule["min_stock"]:
                    items.append(
                        {
                            "sku_key": sku_key,
                            "goods_no": goods_no,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "sufficient",
                            "pushed_count": 0,
                        }
                    )
                    continue

                needed = min(max(0, rule["target_stock"] - current_qty), rule["max_push"])
                if needed <= 0:
                    items.append(
                        {
                            "sku_key": sku_key,
                            "goods_no": goods_no,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "noop",
                            "pushed_count": 0,
                        }
                    )
                    continue

                if dry_check:
                    items.append(
                        {
                            "sku_key": sku_key,
                            "goods_no": goods_no,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "would_restock",
                            "planned_push": needed,
                            "pushed_count": 0,
                        }
                    )
                    continue

                try:
                    batch_res = self.batch(
                        sku_key,
                        "16688",
                        needed,
                        goods_no,
                        f"auto-restock-{trigger}-{sku_key}-{goods_no}",
                    )
                except (UpstreamTimeout, UpstreamError, ValueError) as exc:
                    detail = str(exc) or type(exc).__name__
                    err_msg = f"{sku_key}({goods_no}) 补卡失败: {detail}"
                    errors.append(err_msg)
                    self._channel16688_alert(
                        f"restock-push:{sku_key}:{goods_no}:{hour_bucket}",
                        "16688 自动补卡导入失败（需人工处理）",
                        f"触发来源 {trigger} 为 {sku_key}（goods_no={goods_no}，当前库存 {current_qty}）自动补发 {needed} 张卡密失败：{detail}",
                    )
                    items.append(
                        {
                            "sku_key": sku_key,
                            "goods_no": goods_no,
                            "current_qty": current_qty,
                            "min_stock": rule["min_stock"],
                            "target_stock": rule["target_stock"],
                            "action": "failed",
                            "pushed_count": 0,
                            "error": detail,
                        }
                    )
                    continue

                pushed_count = int(batch_res.get("count") or needed)
                restocked += 1
                total_pushed += pushed_count
                items.append(
                    {
                        "sku_key": sku_key,
                        "goods_no": goods_no,
                        "current_qty": current_qty,
                        "after_qty": current_qty + pushed_count,
                        "min_stock": rule["min_stock"],
                        "target_stock": rule["target_stock"],
                        "action": "restocked",
                        "batch_id": batch_res["batch_id"],
                        "pushed_count": pushed_count,
                    }
                )

            if not dry_check and openapi_paused:
                logger.warning("16688 OpenAPI is paused (restock trigger=%s)", trigger)
            elif not dry_check and checked_stock:
                self.ledger.clear_notification(CHANNEL16688_OPENAPI_PAUSED_KEY)

            return {
                "trigger": trigger,
                "dry_check": dry_check,
                "checked": len(rules),
                "restocked": restocked,
                "total_pushed": total_pushed,
                "sync_sold": sync_result,
                "stock_conflicts": stock_conflicts,
                "errors": errors,
                "items": items,
            }

