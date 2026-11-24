from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Optional

from .config import Settings
from .db import Ledger, now_iso
from .gateways import UpstreamError, UpstreamTimeout


logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+-]*@(gmail\.com|googlemail\.com)$", re.IGNORECASE)
SKUS = {
    "ultra_shared_slot": {"title": "Google Ultra x20 共享车位", "product_type": "slot", "product_id": "6ac26aaeec73a42d6430a837", "reference_price_usd": 13.47, "max_cost_usd": 16.0},
    "ultra_exclusive_admin": {"title": "VEO3 Ultra x20 独享整组·管理员版（1个月全保）", "product_type": "account", "product_id": "6ae03419c0080f9e15f695a8", "reference_price_usd": 67.50, "max_cost_usd": 72.0},
}


def mask_email(value: Optional[str]) -> Optional[str]:
    if not value or "@" not in value:
        return None
    local, domain = value.split("@", 1)
    return f"{local[:2] if len(local) > 1 else local[:1]}***@{domain}"


def format_warranty_forward_template(order_code: str, admin_email: str) -> str:
    return (
        "Chào shop ạ, em gửi thông tin đăng ký bảo hành gói Family:\n"
        f"- Mã đơn: {order_code or ''}\n"
        f"- Gmail chủ nhóm: {admin_email}\n"
        "Em đã mời tài khoản shop vào nhóm và gửi kèm ảnh xác nhận, nhờ shop ghi nhận bảo hành giúp em ạ. Cảm ơn shop!"
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

    def __init__(self, ledger: Ledger, settings: Settings, canboso: Any, channel: Any, mailer: Mailer):
        self.ledger, self.settings, self.canboso, self.channel, self.mailer = ledger, settings, canboso, channel, mailer
        ledger.seed_skus(SKUS)

    def issue(self, sku_key: str, channel: str, external_order_id: str = "", remark: str = "", batch_id: Optional[str] = None, goods_no: Optional[str] = None) -> dict[str, Any]:
        sku = self.ledger.get_sku(sku_key)
        if not sku:
            raise ValueError("UNKNOWN_SKU")
        code = self._new_code(sku_key)
        t = now_iso()
        self.ledger.insert_code({"code": code, "sku_key": sku_key, "channel": channel, "batch_id": batch_id, "goods_no": goods_no or sku.get("goods_no"), "external_order_id": external_order_id, "remark": remark, "status": "unused", "created_at": t, "updated_at": t})
        return self._code_row(code)

    def batch(self, sku_key: str, channel: str, count: int, goods_no: Optional[str], label: str) -> dict[str, Any]:
        if count < 1 or count > 500:
            raise ValueError("COUNT_OUT_OF_RANGE")
        sku = self.ledger.get_sku(sku_key)
        if not sku:
            raise ValueError("UNKNOWN_SKU")
        batch_id = "B" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2).upper()
        resolved_goods = goods_no or sku.get("goods_no")
        if channel == "16688" and not resolved_goods:
            raise ValueError("GOODS_NO_REQUIRED")
        codes = [self.issue(sku_key, channel, batch_id=batch_id, goods_no=resolved_goods)["code"] for _ in range(count)]
        with self.ledger.transaction(immediate=True) as c:
            c.execute("INSERT INTO batches(batch_id,sku_key,channel,goods_no,batch_label,count,created_at) VALUES(?,?,?,?,?,?,?)", (batch_id, sku_key, channel, resolved_goods, label, count, now_iso()))
        if channel == "16688" and resolved_goods:
            content = "\n\n".join(f"提货链接：{self.settings.public_base_url}/r/{code}\n兑换码：{code}" for code in codes)
            # In dry_run this is the injected safe adapter. The production adapter is
            # only selected after the operator explicitly switches execution_mode=live.
            self.channel.stock_secret_add(resolved_goods, content)
        return {"batch_id": batch_id, "sku_key": sku_key, "channel": channel, "count": count, "codes": codes,
                "export_lines_code_only": "\n".join(codes),
                "export_lines_with_url": "\n".join(f"提货链接：{self.settings.public_base_url}/r/{c} | 兑换码：{c}" for c in codes)}

    def redeem(self, code: str, buyer_email: Optional[str], confirm: Optional[str], preflight: bool) -> tuple[dict[str, Any], Optional[str]]:
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
            if not EMAIL_RE.match(normalized):
                return {}, "INVALID_EMAIL"
            if confirm is not None and confirm.strip() != "" and normalized != confirm.strip().lower():
                return {}, "EMAIL_MISMATCH"
            payload["customer_email"] = normalized
        state, claimed = self.ledger.claim_for_purchase(code, payload, normalized, mask_email(normalized))
        if state != "claimed":
            return {}, "CONCURRENT_IN_PROGRESS" if state == "busy" else "CODE_NOT_FOUND"
        if row["channel"] == "16688":
            order_goods = row.get("goods_no") or sku.get("goods_no")
            if not order_goods:
                self._release_claim(code, "16688_goods_missing")
                return {}, "ORDER_NOT_ELIGIBLE"
            try:
                order = self.channel.verify_order(code, order_goods)
            except Exception:
                self.ledger.transition(code, "needs_manual_review", "16688_unavailable", {"last_error_code": "UPSTREAM_ERROR", "last_error_message": "16688 verification unavailable"}, ("purchasing",))
                self.alert(code, "UPSTREAM_ERROR")
                return self.public_row(self.ledger.get_code(code)), None
            if not order.get("ok"):
                if order.get("error") == "ORDER_NOT_ELIGIBLE":
                    self._release_claim(code, "16688_order_not_eligible")
                    return {}, "ORDER_NOT_ELIGIBLE"
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

    def _release_claim(self, code: str, reason: str) -> None:
        self.ledger.transition(code, "unused", reason, {
            "buyer_email": None,
            "buyer_email_masked": None,
            "idempotency_key": None,
            "request_payload_json": None,
            "submitted_at": None,
        }, ("purchasing",))

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
        updates: dict[str, Any] = {"upstream_order_code": order.get("orderCode"), "upstream_status": order.get("status"), "cost_usd": cost, "request_payload_json": None}
        if sku["product_type"] == "account":
            delivery = self._safe_delivery(result.get("delivery") or {})
            updates.update({"status": "delivered_pending_warranty", "delivery_json": json.dumps(delivery, ensure_ascii=False), "completed_at": None})
            self.ledger.transition(code, "delivered_pending_warranty", "exclusive_delivered", updates, ("purchasing",))
            self._safe_send("独享整组出货成功", f"兑换码 {code} 已出货，订单号 {order.get('orderCode') or 'unknown'}。")
        else:
            self.ledger.transition(code, "waiting_fulfillment", "shared_order_accepted", updates, ("purchasing",))
        return self.public_row(self.ledger.get_code(code))

    def replay(self, code: str, override: bool = False, note: str = "") -> tuple[dict[str, Any], Optional[str]]:
        row = self.ledger.get_code(code)
        if not row or row["status"] != "needs_manual_review" or not row.get("idempotency_key") or not row.get("request_payload_json"):
            return (self.admin_detail(row) if row else {}), "RETRY_NOT_AVAILABLE"
        sku = self.ledger.get_sku(row["sku_key"])
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
        audit_reason = "admin_retry" if not note.strip() else f"admin_retry:{note.strip()[:200]}"
        self.ledger.transition(code, "purchasing", audit_reason, {"last_error_code": None, "last_error_message": None}, ("needs_manual_review",))
        self._purchase(code, self.ledger.get_code(code), sku, cost)
        return self.admin_detail(self.ledger.get_code(code)), None

    def poll(self, code: str) -> dict[str, Any]:
        row = self.ledger.get_code(code)
        if not row or row["status"] != "waiting_fulfillment":
            return self.public_row(row) if row else {}
        if row.get("submitted_at") and datetime.fromisoformat(row["submitted_at"]) < datetime.now(timezone.utc).astimezone() - timedelta(hours=24):
            self.ledger.transition(code, "needs_manual_review", "fulfillment_over_24h", {"last_error_code": "FULFILLMENT_TIMEOUT"}, ("waiting_fulfillment",))
            self.alert(code, "FULFILLMENT_TIMEOUT")
            return self.public_row(self.ledger.get_code(code))
        try:
            response = self.canboso.get_order(row["upstream_order_code"])
        except (UpstreamTimeout, UpstreamError) as exc:
            return self._handle_poll_failure(code, self._format_upstream_exception("查询上游订单失败", exc))
        except Exception as exc:
            return self._handle_poll_failure(code, f"查询上游订单异常：{type(exc).__name__}")
        item = (response.get("orders") or [{}])[0]
        status = str(item.get("status") or "").lower()
        updates = {"upstream_status": status, "last_polled_at": now_iso()}
        if status in {"completed", "delivered"} or item.get("deliveredAt"):
            updates.update({"completed_at": now_iso(), "delivery_json": None})
            self.ledger.transition(code, "completed", "shared_fulfillment_completed", updates, ("waiting_fulfillment",))
            self._safe_send("共享车位拉人完成", f"兑换码 {code} 已完成，订单号 {row.get('upstream_order_code') or 'unknown'}。")
        else:
            self.ledger.transition(code, "waiting_fulfillment", "shared_poll", updates, ("waiting_fulfillment",))
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

    def warranty(self, code: str, admin_email: str, confirm: Optional[str], note: str, content: bytes, content_type: str) -> tuple[dict[str, Any], Optional[str]]:
        row = self.ledger.get_code(code)
        if not row:
            return {}, "CODE_NOT_FOUND"
        if row["sku_key"] != "ultra_exclusive_admin":
            return {}, "WARRANTY_NOT_APPLICABLE"
        if row["status"] not in {"delivered_pending_warranty", "completed"}:
            return {}, "WARRANTY_NOT_ALLOWED"
        old_warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else {}
        if old_warranty.get("status") == "registered_upstream":
            return {}, "WARRANTY_LOCKED"
        email = admin_email.strip().lower()
        if not EMAIL_RE.match(email):
            return {}, "INVALID_EMAIL"
        if confirm is not None and confirm.strip() != "" and email != confirm.strip().lower():
            return {}, "EMAIL_MISMATCH"
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            return {}, "INVALID_SCREENSHOT_TYPE"
        if not content:
            return {}, "SCREENSHOT_EMPTY"
        if len(content) > 5 * 1024 * 1024:
            return {}, "SCREENSHOT_TOO_LARGE"
        path = Path(self.settings.storage_dir) / (uuid.uuid4().hex + "." + content_type.split("/")[1])
        old_path = row.get("screenshot_path")
        path.write_bytes(content)
        submitted = {
            "status": "submitted",
            "admin_email": email,
            "submitted_at": now_iso(),
            "registered_at": None,
            "note": note,
            "has_screenshot": True,
            "screenshot_path": str(path),
            "forward_template": format_warranty_forward_template(row.get("upstream_order_code") or "", email),
        }
        changed = self.ledger.transition(
            code,
            "completed",
            "warranty_submitted",
            {
                "buyer_email": email,
                "buyer_email_masked": mask_email(email),
                "warranty_json": json.dumps(submitted, ensure_ascii=False),
                "screenshot_path": str(path),
                "screenshot_content_type": content_type,
                "screenshot_size": len(content),
                "completed_at": now_iso(),
            },
            ("delivered_pending_warranty", "completed"),
        )
        if not changed:
            path.unlink(missing_ok=True)
            current = self.ledger.get_code(code)
            if current and json.loads(current.get("warranty_json") or "{}").get("status") == "registered_upstream":
                return {}, "WARRANTY_LOCKED"
            return {}, "WARRANTY_NOT_ALLOWED"
        if old_path and old_path != str(path):
            old = Path(old_path)
            if old.is_file():
                old.unlink()
        self._safe_send("独享整组买家提交保修登记", submitted["forward_template"], (path.name, content, content_type))
        return self.public_row(self.ledger.get_code(code)), None

    def warranty_ack(self, code: str, note: str) -> dict[str, Any]:
        row = self.ledger.get_code(code)
        if not row:
            return {}
        warranty = json.loads(row["warranty_json"] or "{}")
        warranty.update({"status": "registered_upstream", "registered_at": now_iso(), "note": note or warranty.get("note", "")})
        self.ledger.transition(code, "completed", "warranty_ack", {"warranty_json": json.dumps(warranty, ensure_ascii=False)}, ("completed",))
        return self.admin_detail(self.ledger.get_code(code))

    def alert(self, code: str, reason: str) -> None:
        if self.ledger.claim_notification(f"alert:{code}:{reason}"):
            self._safe_send("Plan 2 异常报警", f"兑换码 {code} 需要人工处理。事件：{reason}。")

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

    def _code_row(self, code: str) -> dict[str, Any]:
        return self.ledger.get_code(code)

    def public_row(self, row: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not row:
            return {}
        sku = self.ledger.get_sku(row["sku_key"])
        delivery = json.loads(row["delivery_json"]) if row.get("delivery_json") else None
        warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else ( {"status": "pending", "admin_email_masked": None, "submitted_at": None, "has_screenshot": False, "note": None} if row["sku_key"] == "ultra_exclusive_admin" and row["status"] in {"delivered_pending_warranty", "completed"} else None)
        if warranty:
            warranty = {
                "status": warranty.get("status", "pending"),
                "admin_email_masked": warranty.get("admin_email_masked") or mask_email(warranty.get("admin_email")),
                "submitted_at": warranty.get("submitted_at"),
                "has_screenshot": bool(warranty.get("has_screenshot")),
                "note": warranty.get("note"),
            }
        if sku["product_type"] == "slot":
            delivery = None
        elapsed = None
        if row.get("submitted_at"):
            try:
                elapsed = max(0, int((datetime.now(timezone.utc).astimezone() - datetime.fromisoformat(row["submitted_at"])).total_seconds() // 60))
            except ValueError:
                pass
        notice = "订单正在人工处理，请带兑换码联系客服。" if row["status"] == "needs_manual_review" else None
        masked_email = row.get("buyer_email_masked") or (warranty.get("admin_email_masked") if warranty else None)
        return {"code": row["code"], "sku_key": row["sku_key"], "sku_title": sku["title"], "product_type": sku["product_type"], "channel": row["channel"], "status": row["status"], "buyer_email_masked": masked_email, "delivery": delivery, "warranty": warranty, "fulfillment": {"eta_text": "一般 1~6 小时内发家庭组邀请，最晚 24 小时" if sku["product_type"] == "slot" else None, "submitted_at": row.get("submitted_at"), "completed_at": row.get("completed_at"), "last_polled_at": row.get("last_polled_at"), "elapsed_minutes": elapsed}, "buyer_notice": notice, "support_hint": "有问题请把订单号和兑换码发给客服。", "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def admin_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        sku = self.ledger.get_sku(row["sku_key"])
        elapsed = self.public_row(row)["fulfillment"]["elapsed_minutes"]
        warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else None
        delivery = json.loads(row["delivery_json"]) if row.get("delivery_json") else None
        resolved_email = row.get("buyer_email") or (warranty.get("admin_email") if isinstance(warranty, dict) else None) or None
        store_email = (delivery.get("account_text") if isinstance(delivery, dict) else None) or None
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
            "buyer_email": resolved_email,
            "buyer_email_masked": row.get("buyer_email_masked") or mask_email(resolved_email),
            "store_email": store_email,
            "upstream_order_code": row.get("upstream_order_code"),
            "upstream_status": row.get("upstream_status"),
            "cost_usd": row.get("cost_usd"),
            "warranty_status": warranty.get("status") if isinstance(warranty, dict) else None,
            "last_error_code": row.get("last_error_code"),
            "last_error_message": row.get("last_error_message"),
            "is_overdue_24h": bool(elapsed is not None and elapsed > 24 * 60 and row["status"] == "waiting_fulfillment"),
            "elapsed_minutes": elapsed,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def admin_detail(self, row: dict[str, Any]) -> dict[str, Any]:
        result = self.admin_summary(row)
        warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else None
        if warranty:
            if warranty.get("admin_email"):
                warranty["forward_template"] = format_warranty_forward_template(
                    row.get("upstream_order_code") or "",
                    warranty["admin_email"],
                )
            warranty["screenshot_url"] = f"/api/v1/admin/codes/{row['code']}/warranty-screenshot" if row.get("screenshot_path") else None
            warranty.pop("screenshot_path", None)
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
        counts = {status: self._count("status=?", (status,)) for status in ("unused", "waiting_fulfillment", "delivered_pending_warranty", "completed", "needs_manual_review", "revoked")}
        counts.update({"total": self._count(), "overdue_24h": self._count("status='waiting_fulfillment' AND submitted_at < ?", ((datetime.now(timezone.utc).astimezone() - timedelta(hours=24)).isoformat(),))})
        if balance_usd < self.settings.canboso_low_balance_threshold_usd and self.ledger.claim_notification("low-balance"):
            self._safe_send("Canboso 低余额报警", f"Canboso 余额低于配置阈值，请及时处理。当前余额为 ${balance_usd:.2f}。")
        return {"execution_mode": self.settings.execution_mode, "public_base_url": self.settings.public_base_url, "canboso": {"balance_usd": balance_usd, "low_balance_threshold_usd": self.settings.canboso_low_balance_threshold_usd, "is_low_balance": balance_usd < self.settings.canboso_low_balance_threshold_usd, "checked_at": now_iso(), "error": None}, "skus": skus, "counts": counts}

    def sku_status(self, sku_key: str, catalog: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        s = self.ledger.get_sku(sku_key)
        if not s:
            raise ValueError("UNKNOWN_SKU")
        catalog = catalog if catalog is not None else self.canboso.list_products()
        p = next((x for x in catalog.get("products", []) if x.get("productId") == s["product_id"]), {})
        price = float((p.get("price") or {}).get("amount", 0)) if p else None
        available = (p.get("availability") or {}).get("available") if p else None
        return {"sku_key": sku_key, "title": s["title"], "product_type": s["product_type"], "canboso_product_id": s["product_id"], "goods_no": s.get("goods_no"), "current_price_usd": price, "max_cost_usd": s["max_cost_usd"], "available": available, "is_available": available != 0, "price_exceeded": price is not None and price > s["max_cost_usd"], "unused_codes_taobao": self._count("sku_key=? AND channel='taobao' AND status='unused'", (sku_key,)), "unused_codes_16688": self._count("sku_key=? AND channel='16688' AND status='unused'", (sku_key,))}

    def _count(self, where: str = "", params: tuple[Any, ...] = ()) -> int:
        with self.ledger.connect() as c:
            return c.execute("SELECT COUNT(*) FROM codes" + (" WHERE " + where if where else ""), params).fetchone()[0]

