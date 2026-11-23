from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
import json
import logging
import smtplib
import ssl
from typing import Any, Callable

import httpx

from .storage import DualDatabaseStore, now_shanghai_str, sanitize_order_meta

LOGGER = logging.getLogger("plan1_auto_restock.alerts")

LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


@dataclass(frozen=True)
class EmailAlertSettings:
    enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    use_ssl: bool = True
    username: str = ""
    password: str = ""
    to_addrs: tuple[str, ...] = ()
    min_level: str = "WARNING"
    cooldown_seconds: int = 1800
    subject_prefix: str = "[16688补库告警]"
    timeout_seconds: float = 15.0
    exclude_event_types: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return bool(
            self.enabled
            and self.smtp_host.strip()
            and self.username.strip()
            and self.password.strip()
            and self.to_addrs
        )


def parse_email_recipients(raw_recipients: str, fallback_addr: str = "") -> tuple[str, ...]:
    parts = [item.strip() for item in raw_recipients.replace(";", ",").split(",") if item.strip()]
    if parts:
        return tuple(dict.fromkeys(parts))
    fallback = fallback_addr.strip()
    return (fallback,) if fallback else ()


class AlertDispatcher:
    def __init__(
        self,
        *,
        store: DualDatabaseStore,
        webhook_url: str = "",
        email_settings: EmailAlertSettings | None = None,
        http_client: httpx.Client | None = None,
        smtp_sender: Callable[[EmailMessage, EmailAlertSettings], None] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.webhook_url = webhook_url.strip()
        self.email_settings = email_settings or EmailAlertSettings()
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=5.0)
        self._smtp_sender = smtp_sender or self._send_via_smtp
        self._clock = clock

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _level_rank(level: str) -> int:
        return LEVEL_ORDER.get(level.strip().upper(), LEVEL_ORDER["INFO"])

    @staticmethod
    def _build_email_throttle_key(event_type: str, sku_id: str, safe_detail: dict[str, Any]) -> str:
        complaint_no = str(safe_detail.get("complaint_no") or "").strip()
        if complaint_no:
            status_val = safe_detail.get("status", "")
            return f"email:{event_type}:{sku_id}:{complaint_no}:{status_val}"
        return f"email:{event_type}:{sku_id or '-'}"

    @staticmethod
    def _build_email_message(
        *,
        settings: EmailAlertSettings,
        timestamp: str,
        level: str,
        event_type: str,
        sku_id: str,
        message: str,
        safe_detail: dict[str, Any],
    ) -> EmailMessage:
        sku_tag = f" ({sku_id})" if sku_id else ""
        subject = f"{settings.subject_prefix} [{level}] {event_type}{sku_tag}"
        lines = [
            f"时间: {timestamp}",
            f"级别: {level}",
            f"事件: {event_type}",
            f"SKU: {sku_id or '-'}",
            f"详情: {message}",
        ]
        if safe_detail:
            lines.append("")
            lines.append("附加信息:")
            lines.append(json.dumps(safe_detail, ensure_ascii=False, indent=2))

        email_msg = EmailMessage()
        email_msg["From"] = formataddr(("16688自动补库告警", settings.username))
        email_msg["To"] = ", ".join(settings.to_addrs)
        email_msg["Subject"] = subject
        email_msg.set_content("\n".join(lines), subtype="plain", charset="utf-8")
        return email_msg

    @staticmethod
    def _send_via_smtp(email_msg: EmailMessage, settings: EmailAlertSettings) -> None:
        if settings.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                host=settings.smtp_host,
                port=settings.smtp_port,
                timeout=settings.timeout_seconds,
                context=context,
            ) as server:
                server.login(settings.username, settings.password)
                server.send_message(email_msg, from_addr=settings.username, to_addrs=list(settings.to_addrs))
            return

        with smtplib.SMTP(
            host=settings.smtp_host,
            port=settings.smtp_port,
            timeout=settings.timeout_seconds,
        ) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(settings.username, settings.password)
            server.send_message(email_msg, from_addr=settings.username, to_addrs=list(settings.to_addrs))

    def _dispatch_email(
        self,
        *,
        timestamp: str,
        level: str,
        event_type: str,
        sku_id: str,
        message: str,
        safe_detail: dict[str, Any],
        bypass_cooldown: bool = False,
        bypass_level: bool = False,
        raise_on_error: bool = False,
    ) -> str:
        settings = self.email_settings
        if not settings.enabled:
            return "disabled"
        if not settings.is_ready:
            err_msg = "邮件告警已启用但缺少 SMTP 账号/授权码或收件人，跳过邮件发送"
            LOGGER.warning(err_msg)
            if raise_on_error:
                raise RuntimeError(err_msg)
            return "misconfigured"
        if not bypass_level and self._level_rank(level) < self._level_rank(settings.min_level):
            return "filtered_by_level"
        excluded = {item.strip().upper() for item in settings.exclude_event_types if item.strip()}
        if not bypass_level and event_type.strip().upper() in excluded:
            LOGGER.debug("邮件告警已按配置跳过事件类型：%s", event_type)
            return "excluded_event_type"

        now_ts = self._clock() if self._clock is not None else None
        throttle_key = self._build_email_throttle_key(event_type, sku_id, safe_detail)
        if not bypass_cooldown and not self.store.should_send_throttled_alert(
            throttle_key,
            cooldown_seconds=settings.cooldown_seconds,
            now_ts=now_ts,
        ):
            LOGGER.debug("邮件告警冷却中，已抑制重复推送：%s", throttle_key)
            return "throttled"

        email_msg = self._build_email_message(
            settings=settings,
            timestamp=timestamp,
            level=level,
            event_type=event_type,
            sku_id=sku_id,
            message=message,
            safe_detail=safe_detail,
        )
        try:
            self._smtp_sender(email_msg, settings)
        except Exception as exc:
            LOGGER.warning("邮件告警发送失败（已降级写本地事件表）: %s", exc)
            if raise_on_error:
                raise
            return f"failed:{exc}"

        if not bypass_cooldown:
            self.store.mark_throttled_alert_sent(
                throttle_key,
                level=level,
                message=message,
                now_ts=now_ts,
            )
        return "sent"

    def emit(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        sku_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._emit_internal(
            level=level,
            event_type=event_type,
            message=message,
            sku_id=sku_id,
            detail=detail,
            bypass_cooldown=False,
            bypass_level=False,
            raise_on_error=False,
        )

    def send_test_alert(
        self,
        *,
        message: str,
        level: str = "WARNING",
        sku_id: str = "system-test",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._emit_internal(
            level=level,
            event_type="TEST_ALERT",
            message=message,
            sku_id=sku_id,
            detail=detail,
            bypass_cooldown=True,
            bypass_level=True,
            raise_on_error=True,
        )

    def _emit_internal(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        sku_id: str = "",
        detail: dict[str, Any] | None = None,
        bypass_cooldown: bool = False,
        bypass_level: bool = False,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        normalized_level = level.strip().upper() or "INFO"
        timestamp = now_shanghai_str()
        safe_detail = sanitize_order_meta(detail or {})
        self.store.record_event(
            level=normalized_level,
            event_type=event_type,
            sku_id=sku_id,
            message=message,
            detail=safe_detail,
        )
        log_level = getattr(logging, normalized_level, logging.INFO)
        LOGGER.log(log_level, "[%s] sku=%s %s", event_type, sku_id or "-", message)

        webhook_status = "disabled"
        if self.webhook_url:
            payload = {
                "timestamp": timestamp,
                "level": normalized_level,
                "event_type": event_type,
                "sku_id": sku_id,
                "message": message,
                "detail": safe_detail,
                "msg_type": "text",
                "content": {"text": f"[Plan1 {normalized_level}] {event_type}: {message}"},
            }
            try:
                resp = self._client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
                webhook_status = "sent"
            except httpx.HTTPError as exc:
                LOGGER.warning("Webhook 告警推送失败（已降级写本地事件表）: %s", exc)
                webhook_status = f"failed:{exc}"
                if raise_on_error:
                    raise

        email_status = self._dispatch_email(
            timestamp=timestamp,
            level=normalized_level,
            event_type=event_type,
            sku_id=sku_id,
            message=message,
            safe_detail=safe_detail,
            bypass_cooldown=bypass_cooldown,
            bypass_level=bypass_level,
            raise_on_error=raise_on_error,
        )
        return {
            "timestamp": timestamp,
            "level": normalized_level,
            "event_type": event_type,
            "webhook": webhook_status,
            "email": email_status,
            "email_to": list(self.email_settings.to_addrs),
            "smtp_host": self.email_settings.smtp_host if self.email_settings.enabled else "",
            "smtp_port": self.email_settings.smtp_port if self.email_settings.enabled else 0,
        }
