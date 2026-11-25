from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import inspect
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, AsyncIterator, Callable, Coroutine, Iterable, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

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
    transport = "dry_run"

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
    transport = "telethon"

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


@contextmanager
def _profile_file_lock(profile_dir: Path, timeout_seconds: float = 30.0) -> Iterator[None]:
    if fcntl is None or not profile_dir.is_dir():
        yield
        return
    lock_path = profile_dir / ".plan2_tg_web.lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    acquired = False
    try:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TelegramForwardError("Timed out waiting for Telegram Web profile lock") from None
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            handle.close()
        except Exception:
            pass


def _cleanup_stale_singleton_files(profile_dir: Path) -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        candidate = profile_dir / name
        try:
            if os.path.lexists(candidate):
                candidate.unlink()
        except OSError:
            pass


def _build_playwright_env() -> dict[str, str]:
    env = os.environ.copy()
    tmp_dir = tempfile.gettempdir()
    home = env.get("HOME", "")
    if not home or not os.path.isdir(home) or not os.access(home, os.W_OK):
        env["HOME"] = tmp_dir
    for key, subdir in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_DATA_HOME", ".local-share"),
        ("XDG_RUNTIME_DIR", ".runtime"),
    ):
        current = env.get(key, "")
        if not current or not os.path.isdir(current) or not os.access(current, os.W_OK):
            fallback = os.path.join(tmp_dir, subdir)
            os.makedirs(fallback, exist_ok=True)
            env[key] = fallback
    return env


class PlaywrightTelegramWebForwarder:
    transport = "web"

    def __init__(
        self,
        enabled: bool,
        profile_dir: str,
        target: str,
        web_url: str = "https://web.telegram.org/k/",
        timeout_seconds: float = 45.0,
        min_interval_seconds: float = 1.5,
        *,
        page_factory: Optional[Callable[[], Any]] = None,
    ):
        self.enabled = bool(enabled)
        self.profile_dir = str(profile_dir or "").strip()
        self.target = str(target or "")
        self.web_url = (str(web_url or "").strip() or "https://web.telegram.org/k/").rstrip("/") + "/"
        self.timeout_seconds = max(5.0, float(timeout_seconds or 45.0))
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._page_factory = page_factory
        self._send_lock = threading.Lock()
        self._last_send_monotonic: Optional[float] = None

    @property
    def target_configured(self) -> bool:
        return bool(self.target.strip())

    @property
    def profile_path(self) -> Optional[Path]:
        if not self.profile_dir:
            return None
        return Path(self.profile_dir).expanduser()

    @property
    def is_ready(self) -> bool:
        profile = self.profile_path
        cleaned_target = self.target.strip()
        return bool(
            self.enabled
            and cleaned_target
            and NUMERIC_PEER_RE.match(cleaned_target)
            and profile is not None
            and profile.is_dir()
        )

    @asynccontextmanager
    async def _open_page(self) -> AsyncIterator[Any]:
        if self._page_factory is not None:
            async with self._page_factory() as page:
                yield page
            return
        profile = self.profile_path
        if profile is None or not profile.is_dir():
            raise TelegramForwardError("Telegram Web profile directory does not exist")
        from playwright.async_api import async_playwright

        _cleanup_stale_singleton_files(profile)
        env = _build_playwright_env()
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(profile),
                headless=True,
                viewport={"width": 1280, "height": 900},
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                env=env,
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                yield page
            finally:
                try:
                    await asyncio.wait_for(context.close(), timeout=5.0)
                except Exception:
                    pass

    async def _open_target_chat(self, page: Any, deadline: float) -> dict[str, Any]:
        cleaned = self.target.strip()
        if not cleaned:
            raise TelegramForwardError("Telegram admin target is empty")
        if not NUMERIC_PEER_RE.match(cleaned):
            raise TelegramForwardError("Telegram Web transport requires a numeric peerId target")

        target_fragment = f"#{cleaned}"
        target_url = f"{self.web_url}{target_fragment}"

        nav_timeout_ms = max(5000, int(min(30.0, max(5.0, deadline - time.monotonic())) * 1000))
        await page.goto(target_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)

        logged_in = False
        me_id: Any = None
        while time.monotonic() < deadline:
            auth_state = await page.evaluate(
                """() => {
                    const pageChats = document.querySelector('#page-chats');
                    const inChats = !!pageChats && getComputedStyle(pageChats).display !== 'none';
                    const authPages = document.querySelector('#auth-pages');
                    const authVisible = !!authPages && getComputedStyle(authPages).display !== 'none';
                    const hasLoginPrompt = authVisible && (
                        !!document.querySelector('.page-signQR, .page-sign, .input-field-phone') ||
                        Array.from(document.querySelectorAll('button')).some(b => /LOG IN BY PHONE NUMBER/i.test(b.innerText || ''))
                    );
                    let parsedMeId = null;
                    for (const key of ['user_auth', 'account1']) {
                        const raw = localStorage.getItem(key);
                        if (!raw) continue;
                        try {
                            const data = JSON.parse(raw);
                            if (data && (data.id || data.userId)) {
                                parsedMeId = data.id || data.userId;
                                break;
                            }
                        } catch {}
                    }
                    const hasAuth = parsedMeId !== null || Boolean(localStorage.getItem('user_auth') || localStorage.getItem('account1'));
                    return { hasAuth, inChats, authVisible, hasLoginPrompt, meId: parsedMeId };
                }"""
            )
            if auth_state.get("hasLoginPrompt") and not auth_state.get("hasAuth"):
                raise TelegramForwardError("Telegram Web session is not logged in")
            if auth_state.get("inChats") and auth_state.get("hasAuth") and not auth_state.get("authVisible"):
                logged_in = True
                me_id = auth_state.get("meId")
                break
            await page.wait_for_timeout(400)

        if not logged_in:
            raise TelegramForwardError("Telegram Web session did not reach chat list before timeout")

        click_fallback_at = time.monotonic() + 3.0
        hash_retry_at = time.monotonic() + 6.0
        fallback_clicked = False
        hash_retried = False
        while time.monotonic() < deadline:
            chat_state = await page.evaluate(
                """() => {
                    const topPeer = document.querySelector('.chat.active .topbar .peer-title, .chat.active .chat-info .peer-title');
                    const composer = document.querySelector('.chat.active .input-message-input[contenteditable="true"]');
                    const rect = composer ? composer.getBoundingClientRect() : null;
                    return {
                        hash: (location.hash || '').trim(),
                        peerId: topPeer ? (topPeer.getAttribute('data-peer-id') || '').trim() : '',
                        peerTitle: topPeer ? (topPeer.innerText || '').trim().slice(0, 80) : '',
                        composerVisible: Boolean(rect && rect.width > 0 && rect.height > 0),
                    };
                }"""
            )
            active_peer_id = str(chat_state.get("peerId") or "")
            composer_visible = bool(chat_state.get("composerVisible"))

            if active_peer_id == cleaned and composer_visible:
                return {
                    "me_id": me_id,
                    "target_id": active_peer_id,
                    "target_title": str(chat_state.get("peerTitle") or ""),
                    "composer_ready": True,
                }
            if not fallback_clicked and time.monotonic() >= click_fallback_at:
                fallback_clicked = True
                chat_item = page.locator(
                    f'.chatlist a.chatlist-chat[data-peer-id="{cleaned}"], '
                    f'.chatlist [data-peer-id="{cleaned}"]'
                ).first
                try:
                    if await chat_item.count() > 0:
                        await chat_item.click(timeout=5000)
                except Exception:
                    pass
            elif not hash_retried and time.monotonic() >= hash_retry_at:
                hash_retried = True
                await page.evaluate(
                    "(frag) => { window.location.hash = frag; window.dispatchEvent(new HashChangeEvent('hashchange')); }",
                    target_fragment,
                )

            await page.wait_for_timeout(300)

        raise TelegramForwardError("Telegram Web target chat could not be opened or peer mismatch")

    async def _send_on_page(self, page: Any, text: str, deadline: float) -> dict[str, Any]:
        chat_info = await self._open_target_chat(page, deadline)
        expected_peer_id = str(chat_info["target_id"])

        history_wait_until = min(deadline, time.monotonic() + 1.5)
        before_max_mid = 0
        before_error_mids: set[str] = set()
        while True:
            snap = await page.evaluate(
                """() => {
                    let maxMid = 0;
                    const errorMids = [];
                    const allCount = document.querySelectorAll('.chat.active .bubble[data-mid]').length;
                    for (const el of document.querySelectorAll('.chat.active .bubble.is-out[data-mid]')) {
                        const raw = (el.getAttribute('data-mid') || '').trim();
                        if (el.classList.contains('is-error') && raw) {
                            errorMids.push(raw);
                        }
                        if (/^\\d+$/.test(raw)) {
                            const num = Number(raw);
                            if (Number.isSafeInteger(num) && num > maxMid) maxMid = num;
                        }
                    }
                    return { maxMid, allCount, errorMids };
                }"""
            )
            before_max_mid = max(before_max_mid, int((snap or {}).get("maxMid") or 0))
            for item in (snap or {}).get("errorMids") or ():
                before_error_mids.add(str(item))
            if int((snap or {}).get("allCount") or 0) > 0 or time.monotonic() >= history_wait_until:
                break
            await page.wait_for_timeout(200)

        current_peer_id = str(
            await page.evaluate(
                "() => (document.querySelector('.chat.active .topbar .peer-title, .chat.active .chat-info .peer-title')?.getAttribute('data-peer-id') || '').trim()"
            )
            or ""
        )
        if current_peer_id != expected_peer_id:
            raise TelegramForwardError("Active Telegram Web chat peer mismatch before send")

        composer = page.locator('.chat.active .input-message-input[contenteditable="true"]').first
        for _attempt in range(2):
            await composer.click(timeout=5000)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            draft_len = int(
                await page.evaluate(
                    "() => ((document.querySelector('.chat.active .input-message-input[contenteditable=\"true\"]')?.innerText || '').trim().length)"
                )
                or 0
            )
            if draft_len == 0:
                break
        else:
            raise TelegramForwardError("Telegram Web composer could not be cleared")

        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized_text.split("\n")
        expected_norm = re.sub(r"\s+", " ", normalized_text).strip()
        if not expected_norm:
            raise TelegramForwardError("Telegram message text is empty")
        for idx, line in enumerate(lines):
            if idx > 0:
                await page.keyboard.press("Shift+Enter")
            if line:
                await page.keyboard.insert_text(line)
        await page.wait_for_timeout(250)

        send_ready = await page.evaluate(
            """(payload) => {
                const expectedPeer = String(payload?.expectedPeer || '');
                const expectedNorm = String(payload?.expectedNorm || '');
                const topPeer = document.querySelector('.chat.active .topbar .peer-title, .chat.active .chat-info .peer-title');
                const box = document.querySelector('.chat.active .input-message-input[contenteditable="true"]');
                const btn = document.querySelector('.chat.active button.btn-send');
                let maxMid = 0;
                const errorMids = [];
                for (const el of document.querySelectorAll('.chat.active .bubble.is-out[data-mid]')) {
                    const raw = (el.getAttribute('data-mid') || '').trim();
                    if (el.classList.contains('is-error') && raw) {
                        errorMids.push(raw);
                    }
                    if (/^\\d+$/.test(raw)) {
                        const num = Number(raw);
                        if (Number.isSafeInteger(num) && num > maxMid) maxMid = num;
                    }
                }
                const boxNorm = box ? (box.innerText || '').replace(/\\s+/g, ' ').trim() : '';
                return {
                    peerMatch: (topPeer?.getAttribute('data-peer-id') || '').trim() === expectedPeer,
                    textLen: boxNorm.length,
                    textMatch: Boolean(expectedNorm) && boxNorm === expectedNorm,
                    btnReady: Boolean(btn && !btn.disabled && btn.classList.contains('send')),
                    maxMid,
                    errorMids,
                };
            }""",
            {"expectedPeer": expected_peer_id, "expectedNorm": expected_norm},
        )
        if not send_ready.get("peerMatch"):
            raise TelegramForwardError("Active Telegram Web chat peer mismatch before click")
        if not send_ready.get("textLen") or not send_ready.get("btnReady"):
            raise TelegramForwardError("Telegram Web composer or send button is not ready")
        if not send_ready.get("textMatch"):
            raise TelegramForwardError("Telegram Web composer text mismatch before send")
        before_max_mid = max(before_max_mid, int(send_ready.get("maxMid") or 0))
        for item in send_ready.get("errorMids") or ():
            before_error_mids.add(str(item))
        required_tokens = [re.sub(r"\s+", " ", line).strip() for line in lines if line.strip()]

        await page.locator(".chat.active button.btn-send").first.click(timeout=5000)

        confirm_deadline = max(deadline, time.monotonic() + 12.0)
        while time.monotonic() < confirm_deadline:
            status = await page.evaluate(
                """(payload) => {
                    const minMid = Number(payload?.minMid || 0);
                    const requiredTokens = Array.isArray(payload?.requiredTokens) ? payload.requiredTokens : [];
                    const ignoredErrorMids = new Set(Array.isArray(payload?.ignoredErrorMids) ? payload.ignoredErrorMids : []);
                    const bubbles = Array.from(document.querySelectorAll('.chat.active .bubble.is-out[data-mid]'));
                    let confirmedMid = 0;
                    let hasError = false;
                    for (const el of bubbles) {
                        const raw = (el.getAttribute('data-mid') || '').trim();
                        const isSending = el.classList.contains('is-sending');
                        const isError = el.classList.contains('is-error');
                        if (isError) {
                            const num = Number(raw);
                            if (!ignoredErrorMids.has(raw) && (!Number.isFinite(num) || num > minMid)) hasError = true;
                        }
                        if (/^\\d+$/.test(raw) && !isSending && !isError) {
                            const num = Number(raw);
                            if (Number.isSafeInteger(num) && num > minMid && num > confirmedMid) {
                                const bodyText = ((el.querySelector('.message')?.innerText || el.innerText || '').replace(/\\s+/g, ' ')).trim();
                                const tokensOk = requiredTokens.every((token) => bodyText.includes(token));
                                if (tokensOk) {
                                    confirmedMid = num;
                                }
                            }
                        }
                    }
                    const box = document.querySelector('.chat.active .input-message-input[contenteditable="true"]');
                    return {
                        confirmedMid,
                        hasError,
                        remainingDraftLen: box ? (box.innerText || '').trim().length : 0,
                    };
                }""",
                {
                    "minMid": before_max_mid,
                    "requiredTokens": required_tokens,
                    "ignoredErrorMids": sorted(before_error_mids),
                },
            )
            if status.get("hasError"):
                raise TelegramForwardError("Telegram Web failed to deliver message")
            confirmed_mid = int(status.get("confirmedMid") or 0)
            if confirmed_mid > before_max_mid:
                await page.wait_for_timeout(800)
                return {
                    "message_id": confirmed_mid,
                    "sent_at": now_iso(),
                    "target": self.target.strip(),
                }
            await page.wait_for_timeout(300)

        raise TelegramForwardError("Timed out waiting for Telegram Web message confirmation")

    def send_text(self, text: str) -> dict[str, Any]:
        if not self.is_ready:
            if self.enabled and self.target.strip() and not NUMERIC_PEER_RE.match(self.target.strip()):
                raise TelegramForwardError("Telegram Web transport requires a numeric peerId target")
            raise TelegramForwardError("Telegram Web warranty forwarder is not configured")

        async def _do_send() -> dict[str, Any]:
            async with self._open_page() as page:
                deadline = time.monotonic() + self.timeout_seconds
                return await self._send_on_page(page, text, deadline)

        async def _with_timeout() -> dict[str, Any]:
            return await asyncio.wait_for(_do_send(), timeout=self.timeout_seconds + 15.0)

        profile = self.profile_path
        lock_timeout = min(self.timeout_seconds, 30.0)
        with self._send_lock:
            try:
                with _profile_file_lock(profile, timeout_seconds=lock_timeout) if profile is not None else contextmanager(lambda: (yield))():
                    if self._last_send_monotonic is not None and self.min_interval_seconds > 0:
                        elapsed = time.monotonic() - self._last_send_monotonic
                        wait_seconds = self.min_interval_seconds - elapsed
                        if wait_seconds > 0:
                            time.sleep(wait_seconds)
                    return _run_async(_with_timeout)
            except TelegramForwardError as exc:
                raise TelegramForwardError(sanitize_tg_error(exc)) from None
            except Exception as exc:
                raise TelegramForwardError(sanitize_tg_error(exc)) from None
            finally:
                self._last_send_monotonic = time.monotonic()

    def verify_connection(self) -> dict[str, Any]:
        if not self.is_ready:
            error_msg = (
                "Telegram Web transport requires a numeric peerId target"
                if self.enabled and self.target.strip() and not NUMERIC_PEER_RE.match(self.target.strip())
                else "Telegram Web warranty forwarder is not configured"
            )
            return {
                "ok": False,
                "mode": "live",
                "transport": "web",
                "target": self.target.strip(),
                "error": error_msg,
            }

        async def _do_verify() -> dict[str, Any]:
            async with self._open_page() as page:
                deadline = time.monotonic() + self.timeout_seconds
                chat_info = await self._open_target_chat(page, deadline)
                return {
                    "ok": True,
                    "mode": "live",
                    "transport": "web",
                    "target": self.target.strip(),
                    "me_id": chat_info.get("me_id"),
                    "target_id": chat_info.get("target_id"),
                    "target_title": chat_info.get("target_title"),
                    "composer_ready": bool(chat_info.get("composer_ready")),
                }

        async def _with_timeout() -> dict[str, Any]:
            return await asyncio.wait_for(_do_verify(), timeout=self.timeout_seconds + 10.0)

        profile = self.profile_path
        lock_timeout = min(self.timeout_seconds, 30.0)
        with self._send_lock:
            try:
                with _profile_file_lock(profile, timeout_seconds=lock_timeout) if profile is not None else contextmanager(lambda: (yield))():
                    return _run_async(_with_timeout)
            except TelegramForwardError as exc:
                raise TelegramForwardError(sanitize_tg_error(exc)) from None
            except Exception as exc:
                raise TelegramForwardError(sanitize_tg_error(exc)) from None


def resolve_tg_transport(settings: Any) -> str:
    configured = str(getattr(settings, "tg_transport", "auto") or "auto").strip().lower()
    if configured in {"telethon", "web"}:
        return configured
    if configured != "auto":
        raise ValueError("tg_transport must be auto, telethon, or web")
    has_telethon = bool(
        int(getattr(settings, "tg_api_id", 0) or 0) > 0
        and str(getattr(settings, "tg_api_hash", "") or "").strip()
        and str(getattr(settings, "tg_session", "") or "").strip()
    )
    if has_telethon:
        return "telethon"
    if str(getattr(settings, "tg_web_profile_dir", "") or "").strip():
        return "web"
    return "telethon"


def build_telegram_forwarder(settings: Any) -> Any:
    if getattr(settings, "execution_mode", "dry_run") == "dry_run":
        return DryRunTelegramForwarder(
            enabled=settings.tg_warranty_enabled,
            target=settings.tg_admin_target,
        )
    transport = resolve_tg_transport(settings)
    if transport == "web":
        return PlaywrightTelegramWebForwarder(
            enabled=settings.tg_warranty_enabled,
            profile_dir=getattr(settings, "tg_web_profile_dir", ""),
            target=settings.tg_admin_target,
            web_url=getattr(settings, "tg_web_url", "https://web.telegram.org/k/"),
            timeout_seconds=max(30.0, float(settings.tg_timeout_seconds or 45.0)),
        )
    return TelegramWarrantyForwarder(
        enabled=settings.tg_warranty_enabled,
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        session=settings.tg_session,
        target=settings.tg_admin_target,
        timeout_seconds=settings.tg_timeout_seconds,
    )
