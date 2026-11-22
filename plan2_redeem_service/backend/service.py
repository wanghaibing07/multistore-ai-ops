from __future__ import annotations

import json
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

EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+-]*@(gmail\.com|googlemail\.com)$", re.IGNORECASE)
SKUS = {
    "ultra_shared_slot": {"title": "VEO3 Ultra x20 共享车位（1个月·0信用全保）", "product_type": "slot", "product_id": "6ac26aaeec73a42d6430a837", "reference_price_usd": 13.47, "max_cost_usd": 16.0},
    "ultra_exclusive_admin": {"title": "VEO3 Ultra x20 独享整组·管理员版（1个月全保）", "product_type": "account", "product_id": "6ae03419c0080f9e15f695a8", "reference_price_usd": 67.50, "max_cost_usd": 72.0},
}


def mask_email(value: Optional[str]) -> Optional[str]:
    if not value or "@" not in value:
        return None
    local, domain = value.split("@", 1)
    return f"{local[:2] if len(local) > 1 else local[:1]}***@{domain}"


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
        msg = EmailMessage()
        msg["From"] = self.settings.smtp_from or self.settings.smtp_username
        msg["To"] = self.settings.merchant_email
        msg["Subject"] = subject
        msg.set_content(body)
        if attachment:
            filename, data, content_type = attachment
            maintype, subtype = content_type.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if self.settings.smtp_username:
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
            smtp.send_message(msg)


class Service:
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
            if normalized != (confirm or "").strip().lower():
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
        except (UpstreamTimeout, UpstreamError, ValueError, TypeError):
            preflight_ok, reason, cost = False, "UPSTREAM_ERROR", 0
        if not preflight_ok:
            self.ledger.transition(code, "needs_manual_review", reason, {"last_error_code": reason, "last_error_message": reason}, ("purchasing",))
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
        except UpstreamTimeout:
            self.ledger.transition(code, "needs_manual_review", "upstream_timeout", {"last_error_code": "UPSTREAM_TIMEOUT", "last_error_message": "upstream timeout"}, ("purchasing",))
            self.alert(code, "UPSTREAM_ERROR")
            return self.public_row(self.ledger.get_code(code))
        except UpstreamError as e:
            self.ledger.transition(code, "needs_manual_review", "upstream_error", {"last_error_code": f"HTTP_{e.status_code}", "last_error_message": e.message}, ("purchasing",))
            self.alert(code, "UPSTREAM_ERROR")
            return self.public_row(self.ledger.get_code(code))
        order = result.get("order") or {}
        updates: dict[str, Any] = {"upstream_order_code": order.get("orderCode"), "upstream_status": order.get("status"), "cost_usd": cost, "request_payload_json": None}
        if sku["product_type"] == "account":
            delivery = self._safe_delivery(result.get("delivery") or {})
            updates.update({"status": "delivered_pending_warranty", "delivery_json": json.dumps(delivery, ensure_ascii=False), "completed_at": None})
            self.ledger.transition(code, "delivered_pending_warranty", "exclusive_delivered", updates, ("purchasing",))
            self.mailer.send("独享整组出货成功", f"兑换码 {code} 已出货，订单号 {order.get('orderCode') or 'unknown'}。")
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
        except (UpstreamTimeout, UpstreamError, ValueError, TypeError):
            ok, reason, cost = False, "UPSTREAM_ERROR", 0
        if not ok:
            self.ledger.transition(code, "needs_manual_review", f"admin_retry_failed:{reason}", {"last_error_code": reason, "last_error_message": reason}, ("needs_manual_review",))
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
        except (UpstreamTimeout, UpstreamError):
            self.ledger.transition(code, "needs_manual_review", "upstream_error", {"last_error_code": "UPSTREAM_ERROR", "last_error_message": "upstream error"}, ("waiting_fulfillment",))
            self.alert(code, "UPSTREAM_ERROR")
            return self.public_row(self.ledger.get_code(code))
        except Exception:
            self.ledger.transition(code, "needs_manual_review", "upstream_error", {"last_error_code": "UPSTREAM_ERROR", "last_error_message": "upstream error"}, ("waiting_fulfillment",))
            self.alert(code, "UPSTREAM_ERROR")
            return self.public_row(self.ledger.get_code(code))
        item = (response.get("orders") or [{}])[0]
        status = str(item.get("status") or "").lower()
        updates = {"upstream_status": status, "last_polled_at": now_iso()}
        if status in {"completed", "delivered"} or item.get("deliveredAt"):
            updates.update({"completed_at": now_iso(), "delivery_json": None})
            self.ledger.transition(code, "completed", "shared_fulfillment_completed", updates, ("waiting_fulfillment",))
            self.mailer.send("共享车位拉人完成", f"兑换码 {code} 已完成，订单号 {row.get('upstream_order_code') or 'unknown'}。")
        else:
            self.ledger.transition(code, "waiting_fulfillment", "shared_poll", updates, ("waiting_fulfillment",))
        return self.public_row(self.ledger.get_code(code))

    def warranty(self, code: str, admin_email: str, confirm: str, note: str, content: bytes, content_type: str) -> tuple[dict[str, Any], Optional[str]]:
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
        if email != confirm.strip().lower():
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
        submitted = {"status": "submitted", "admin_email": email, "submitted_at": now_iso(), "registered_at": None, "note": note, "has_screenshot": True, "screenshot_path": str(path), "forward_template": f"Canboso 管理员报备\n订单号：{row.get('upstream_order_code') or ''}\n主 Gmail：{email}"}
        changed = self.ledger.transition(code, "completed", "warranty_submitted", {"warranty_json": json.dumps(submitted, ensure_ascii=False), "screenshot_path": str(path), "screenshot_content_type": content_type, "screenshot_size": len(content), "completed_at": now_iso()}, ("delivered_pending_warranty", "completed"))
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
        self.mailer.send("独享整组买家提交保修登记", submitted["forward_template"], (path.name, content, content_type))
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
            self.mailer.send("Plan 2 异常报警", f"兑换码 {code} 需要人工处理。事件：{reason}。")

    def _new_code(self, sku_key: str) -> str:
        prefix = "U20S" if sku_key == "ultra_shared_slot" else "U20E"
        return f"{prefix}-" + "-".join("".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4)) for _ in range(2))

    def _safe_delivery(self, raw: dict[str, Any]) -> dict[str, Any]:
        accounts = raw.get("accounts") or []
        first = accounts[0] if accounts else {}
        email = first.get("user") or first.get("email") or ""
        return {"account_text": email, "credentials": [{"label": "商店套餐邮箱", "value": email}], "raw_lines": [email], "delivered_at": now_iso()}

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
        notice = "订单正在人工处理中，请联系店铺客服。" if row["status"] == "needs_manual_review" else None
        return {"code": row["code"], "sku_key": row["sku_key"], "sku_title": sku["title"], "product_type": sku["product_type"], "channel": row["channel"], "status": row["status"], "buyer_email_masked": row.get("buyer_email_masked"), "delivery": delivery, "warranty": warranty, "fulfillment": {"eta_text": "预计 1~6 小时内发出家庭组邀请，最晚不超过 24 小时" if sku["product_type"] == "slot" else None, "submitted_at": row.get("submitted_at"), "completed_at": row.get("completed_at"), "last_polled_at": row.get("last_polled_at"), "elapsed_minutes": elapsed}, "buyer_notice": notice, "support_hint": "如有疑问，请携带订单号与兑换码联系店铺客服处理。", "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def admin_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        sku = self.ledger.get_sku(row["sku_key"])
        elapsed = self.public_row(row)["fulfillment"]["elapsed_minutes"]
        return {"code": row["code"], "sku_key": row["sku_key"], "sku_title": sku["title"], "product_type": sku["product_type"], "channel": row["channel"], "goods_no": row.get("goods_no"), "external_order_id": row.get("external_order_id"), "remark": row.get("remark") or "", "status": row["status"], "buyer_email_masked": row.get("buyer_email_masked"), "upstream_order_code": row.get("upstream_order_code"), "upstream_status": row.get("upstream_status"), "cost_usd": row.get("cost_usd"), "warranty_status": (json.loads(row["warranty_json"]).get("status") if row.get("warranty_json") else None), "last_error_code": row.get("last_error_code"), "last_error_message": row.get("last_error_message"), "is_overdue_24h": bool(elapsed is not None and elapsed > 24 * 60 and row["status"] == "waiting_fulfillment"), "elapsed_minutes": elapsed, "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def admin_detail(self, row: dict[str, Any]) -> dict[str, Any]:
        result = self.admin_summary(row)
        warranty = json.loads(row["warranty_json"]) if row.get("warranty_json") else None
        if warranty:
            warranty["screenshot_url"] = f"/api/v1/admin/codes/{row['code']}/warranty-screenshot" if row.get("screenshot_path") else None
            warranty.pop("screenshot_path", None)
        result.update({"redeem_url": f"{self.settings.public_base_url}/r/{row['code']}", "batch_id": row.get("batch_id"), "buyer_email": row.get("buyer_email"), "idempotency_key": row.get("idempotency_key"), "delivery": json.loads(row["delivery_json"]) if row.get("delivery_json") else None, "warranty": warranty, "audit_events": self.ledger.audit(row["code"])})
        return result

    def overview(self) -> dict[str, Any]:
        catalog = self.canboso.list_products()
        balance = self.canboso.get_balance()
        balance_usd = float(balance.get("balanceUsd", balance.get("balance", 0)))
        skus = []
        for key in SKUS:
            skus.append(self.sku_status(key, catalog))
        counts = {status: self._count("status=?", (status,)) for status in ("unused", "waiting_fulfillment", "delivered_pending_warranty", "completed", "needs_manual_review", "revoked")}
        counts.update({"total": self._count(), "overdue_24h": self._count("status='waiting_fulfillment' AND submitted_at < ?", ((datetime.now(timezone.utc).astimezone() - timedelta(hours=24)).isoformat(),))})
        if balance_usd < self.settings.canboso_low_balance_threshold_usd and self.ledger.claim_notification("low-balance"):
            self.mailer.send("Canboso 低余额报警", f"Canboso 余额低于配置阈值，请及时处理。当前余额为 ${balance_usd:.2f}。")
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

