from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from typing import Any, Callable, Coroutine, Iterable, Optional

from .db import now_iso


NUMERIC_PEER_RE = re.compile(r"^-?\d+\Z")
ERROR_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning):")


class TelegramForwardError(RuntimeError):
    """Raised when forwarding a warranty message via Telegram fails."""


def sanitize_tg_error(exc_or_msg: Any, secrets: Iterable[str] = ()) -> str:
    if isinstance(exc_or_msg, BaseException):
        cls_name = type(exc_or_msg).__name__
        detail = str(exc_or_msg)
        stripped_detail = detail.strip()
        if stripped_detail:
            if isinstance(exc_or_msg, TelegramForwardError) and ERROR_PREFIX_RE.match(stripped_detail):
                text = stripped_detail
            elif stripped_detail.startswith(f"{cls_name}:"):
                text = stripped_detail
            else:
                text = f"{cls_name}: {detail}"
        else:
            text = cls_name
    else:
        text = str(exc_or_msg or "")

    unique_secrets: list[str] = []
    seen: set[str] = set()
    for item in secrets or ():
        if not item:
            continue
        raw_secret = str(item)
        for candidate in (raw_secret, raw_secret.strip()):
            if candidate and candidate not in seen:
                seen.add(candidate)
                unique_secrets.append(candidate)
    unique_secrets.sort(key=len, reverse=True)
    for secret in unique_secrets:
        text = text.replace(secret, "[REDACTED]")

    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Telegram forward failed"
    if len(text) > 240:
        return text[:237].rstrip() + "..."
    return text


def _run_async(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result_box["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001
            error_box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")


async def _safe_disconnect(client: Any) -> None:
    disconnect = getattr(client, "disconnect", None)
    if disconnect is None:
        return
    try:
        result = disconnect()
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=5.0)
    except Exception:
        pass


async def _resolve_entity(client: Any, target_text: str) -> Any:
    cleaned = (target_text or "").strip()
    if not cleaned:
        raise TelegramForwardError("Telegram admin target is empty")
    if NUMERIC_PEER_RE.match(cleaned):
        peer_id = int(cleaned)
        try:
            return await client.get_entity(peer_id)
        except ValueError:
            await client.get_dialogs(limit=200)
            return await client.get_entity(peer_id)
    return await client.get_entity(cleaned)


class DryRunTelegramForwarder:
    def __init__(self, enabled: bool = False, target: str = ""):
        self.enabled = bool(enabled)
        self.target = str(target or "")
        self.sent: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seq = 0

    @property
    def target_configured(self) -> bool:
        return bool(self.target.strip())

    @property
    def is_ready(self) -> bool:
        return bool(self.enabled and self.target.strip())

    def send_text(self, text: str) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            msg_id = self._seq
            sent_at = now_iso()
            target = self.target.strip()
            self.sent.append(
                {
                    "message_id": msg_id,
                    "sent_at": sent_at,
                    "target": target,
                    "text": text,
                }
            )
        return {"message_id": msg_id, "sent_at": sent_at, "target": target}

    def verify_connection(self) -> dict[str, Any]:
        return {"ok": self.is_ready, "mode": "dry_run", "target": self.target.strip()}


class TelegramWarrantyForwarder:
    def __init__(
        self,
        enabled: bool,
        api_id: int,
        api_hash: str,
        session: str,
        target: str,
        timeout_seconds: float = 20.0,
        min_interval_seconds: float = 1.5,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.enabled = bool(enabled)
        self.api_id = int(api_id or 0)
        self.api_hash = str(api_hash or "")
        self.session = str(session or "")
        self.target = str(target or "")
        self.timeout_seconds = max(1.0, float(timeout_seconds or 20.0))
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._client_factory = client_factory
        self._send_lock = threading.Lock()
        self._last_send_monotonic: Optional[float] = None

    @property
    def target_configured(self) -> bool:
        return bool(self.target.strip())

    @property
    def is_ready(self) -> bool:
        return bool(
            self.enabled
            and self.api_id > 0
            and self.api_hash.strip()
            and self.session.strip()
            and self.target.strip()
        )

    def _secrets(self) -> tuple[str, ...]:
        return (self.api_hash, self.session)

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        return TelegramClient(
            StringSession(self.session.strip()),
            self.api_id,
            self.api_hash.strip(),
            receive_updates=False,
            timeout=max(5, int(self.timeout_seconds)),
            connection_retries=2,
            request_retries=2,
        )

    def send_text(self, text: str) -> dict[str, Any]:
        if not self.is_ready:
            raise TelegramForwardError("Telegram warranty forwarder is not configured")

        async def _do_send() -> dict[str, Any]:
            client = self._build_client()
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise TelegramForwardError("Telegram session is not authorized")
                entity = await _resolve_entity(client, self.target)
                message = await client.send_message(entity, text)
                return {
                    "message_id": int(message.id),
                    "sent_at": now_iso(),
                    "target": self.target.strip(),
                }
            finally:
                await _safe_disconnect(client)

        async def _with_timeout() -> dict[str, Any]:
            return await asyncio.wait_for(_do_send(), timeout=self.timeout_seconds)

        with self._send_lock:
            if self._last_send_monotonic is not None and self.min_interval_seconds > 0:
                elapsed = time.monotonic() - self._last_send_monotonic
                wait_seconds = self.min_interval_seconds - elapsed
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
            try:
                return _run_async(_with_timeout)
            except TelegramForwardError as exc:
                raise TelegramForwardError(sanitize_tg_error(exc, self._secrets())) from None
            except Exception as exc:
                raise TelegramForwardError(sanitize_tg_error(exc, self._secrets())) from None
            finally:
                self._last_send_monotonic = time.monotonic()

    def verify_connection(self) -> dict[str, Any]:
        if not self.is_ready:
            return {
                "ok": False,
                "mode": "live",
                "target": self.target.strip(),
                "error": "Telegram warranty forwarder is not configured",
            }

        async def _do_verify() -> dict[str, Any]:
            client = self._build_client()
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise TelegramForwardError("Telegram session is not authorized")
                me = await client.get_me()
                entity = await _resolve_entity(client, self.target)
                return {
                    "ok": True,
                    "mode": "live",
                    "target": self.target.strip(),
                    "me_id": getattr(me, "id", None),
                    "me_username": getattr(me, "username", None),
                    "target_id": getattr(entity, "id", None),
                    "target_type": type(entity).__name__,
                    "target_username": getattr(entity, "username", None),
                }
            finally:
                await _safe_disconnect(client)

        async def _with_timeout() -> dict[str, Any]:
            return await asyncio.wait_for(_do_verify(), timeout=self.timeout_seconds)

        try:
            return _run_async(_with_timeout)
        except TelegramForwardError as exc:
            raise TelegramForwardError(sanitize_tg_error(exc, self._secrets())) from None
        except Exception as exc:
            raise TelegramForwardError(sanitize_tg_error(exc, self._secrets())) from None
