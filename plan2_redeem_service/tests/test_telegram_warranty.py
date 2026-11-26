from __future__ import annotations

import asyncio
import concurrent.futures
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.gateways import DryRunCanboso
from backend.service import Mailer, format_warranty_forward_template
from backend.telegram_forwarder import (
    DryRunTelegramForwarder,
    PlaywrightTelegramWebForwarder,
    TelegramForwardError,
    TelegramWarrantyForwarder,
    _build_playwright_env,
    build_telegram_forwarder,
    normalize_web_timeout_seconds,
    resolve_tg_transport,
    sanitize_tg_error,
)
from tests.conftest import Fake16688


ADMIN = {"Authorization": "Bearer test-admin"}


def _issue_and_redeem_exclusive(c: TestClient) -> tuple[str, str]:
    issued = c.post(
        "/api/v1/admin/codes/issue",
        headers=ADMIN,
        json={"sku_key": "ultra_exclusive_admin", "channel": "taobao"},
    )
    assert issued.status_code == 201, issued.text
    code = issued.json()["code"]
    redeemed = c.post(
        f"/api/v1/public/codes/{code}/redeem",
        json={"preflight_confirmed": True},
    )
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["status"] == "delivered_pending_warranty"
    store_email = redeemed.json()["delivery"]["account_text"]
    return code, store_email


def _build_app(
    tmp_path: Path,
    *,
    tg_enabled: bool = False,
    tg_target: str = "",
    tg_api_id: int = 0,
    tg_api_hash: str = "",
    tg_session: str = "",
    tg_forwarder: Any = None,
):
    settings = Settings(
        db_path=str(tmp_path / "redeem.sqlite3"),
        storage_dir=str(tmp_path / "private"),
        admin_token="test-admin",
        public_base_url="http://testserver",
        execution_mode="dry_run",
        merchant_email="merchant@example.com",
        tg_warranty_enabled=tg_enabled,
        tg_api_id=tg_api_id,
        tg_api_hash=tg_api_hash,
        tg_session=tg_session,
        tg_admin_target=tg_target,
    )
    canboso = DryRunCanboso(initial_balance=1000)
    channel = Fake16688()
    mailer = Mailer(settings)
    app = create_app(
        settings,
        canboso=canboso,
        channel=channel,
        mailer=mailer,
        tg_forwarder=tg_forwarder,
    )
    return app, settings, mailer


def test_settings_from_env_handles_empty_tg_numeric_fields_and_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PLAN2_CONFIG_FILE", raising=False)
    monkeypatch.setenv("PLAN2_TG_WARRANTY_ENABLED", "")
    monkeypatch.setenv("PLAN2_TG_API_ID", "")
    monkeypatch.setenv("PLAN2_TG_TIMEOUT_SECONDS", "")
    monkeypatch.setenv("PLAN2_TG_ADMIN_TARGET", "")
    settings = Settings.from_env()
    assert settings.tg_warranty_enabled is False
    assert settings.tg_api_id == 0
    assert settings.tg_timeout_seconds == 20.0
    assert settings.tg_admin_target == ""

    monkeypatch.setenv("PLAN2_TG_API_ID", "   ")
    monkeypatch.setenv("PLAN2_TG_TIMEOUT_SECONDS", "   ")
    settings_spaces = Settings.from_env()
    assert settings_spaces.tg_api_id == 0
    assert settings_spaces.tg_timeout_seconds == 20.0

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "tg_warranty_enabled: true\n"
        "tg_api_id: ''\n"
        "tg_admin_target: 123456789\n"
        "tg_timeout_seconds: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PLAN2_CONFIG_FILE", str(cfg))
    for key in (
        "PLAN2_TG_WARRANTY_ENABLED",
        "PLAN2_TG_API_ID",
        "PLAN2_TG_TIMEOUT_SECONDS",
        "PLAN2_TG_ADMIN_TARGET",
    ):
        monkeypatch.delenv(key, raising=False)
    from_yaml = Settings.from_env()
    assert from_yaml.tg_warranty_enabled is True
    assert from_yaml.tg_api_id == 0
    assert from_yaml.tg_admin_target == "123456789"
    assert from_yaml.tg_timeout_seconds == 20.0
    assert "tg_api_hash" not in repr(from_yaml)
    assert "tg_session" not in repr(from_yaml)


def test_warranty_no_screenshot_json_and_multipart_submissions(tmp_path: Path):
    app, _, mailer = _build_app(tmp_path, tg_enabled=False)
    with TestClient(app) as c:
        code_json, _ = _issue_and_redeem_exclusive(c)
        resp_json = c.post(
            f"/api/v1/public/codes/{code_json}/warranty",
            json={"admin_email": "buyer.json@gmail.com", "note": "JSON无图提交"},
        )
        assert resp_json.status_code == 200, resp_json.text
        body_json = resp_json.json()
        assert body_json["status"] == "completed"
        assert set(body_json["warranty"]) == {"status", "admin_email_masked", "submitted_at", "has_screenshot", "note"}
        assert body_json["warranty"]["status"] == "submitted"
        assert body_json["warranty"]["has_screenshot"] is False
        assert body_json["warranty"]["admin_email_masked"] == "buyer.json@gmail.com"
        assert body_json["buyer_email_masked"] == "buyer.json@gmail.com"
        assert "tg_forward_status" not in resp_json.text

        detail_json = c.get(f"/api/v1/admin/codes/{code_json}", headers=ADMIN).json()
        assert detail_json["warranty"]["has_screenshot"] is False
        assert detail_json["warranty"]["screenshot_url"] is None
        assert "screenshot_path" not in detail_json["warranty"]
        assert detail_json["warranty"]["auto_forwarded"] is False
        assert detail_json["warranty"]["tg_forward_status"] == "disabled"
        assert detail_json["warranty"]["tg_message_id"] is None
        assert detail_json["warranty"]["tg_sent_at"] is None
        assert detail_json["warranty"]["tg_error"] is None

        code_mp, _ = _issue_and_redeem_exclusive(c)
        resp_mp = c.post(
            f"/api/v1/public/codes/{code_mp}/warranty",
            files={
                "admin_email": (None, "buyer.multipart@gmail.com"),
                "note": (None, "multipart无图提交"),
            },
        )
        assert resp_mp.status_code == 200, resp_mp.text
        body_mp = resp_mp.json()
        assert body_mp["status"] == "completed"
        assert body_mp["warranty"]["status"] == "submitted"
        assert body_mp["warranty"]["has_screenshot"] is False
        assert body_mp["warranty"]["admin_email_masked"] == "buyer.multipart@gmail.com"
        assert len(mailer.sent) == 2
        assert all(m["attachment"] is False for m in mailer.sent)


def test_warranty_rejects_store_email_case_and_whitespace_insensitive(tmp_path: Path):
    app, _, _ = _build_app(tmp_path, tg_enabled=True, tg_target="100001")
    with TestClient(app) as c:
        code, store_email = _issue_and_redeem_exclusive(c)
        resp = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            json={"admin_email": f"  {store_email.upper()}  "},
        )
        assert resp.status_code == 400
        payload = resp.json()
        assert payload["error_code"] == "WARRANTY_SAME_AS_STORE_EMAIL"
        assert "商店会员邮箱" in payload["message"]
        assert app.state.tg_forwarder.sent == []
        assert app.state.ledger.get_code(code)["status"] == "delivered_pending_warranty"


def test_warranty_legacy_screenshot_validation_and_authenticated_download(tmp_path: Path):
    app, _, _ = _build_app(tmp_path, tg_enabled=False)
    with TestClient(app) as c:
        code, _ = _issue_and_redeem_exclusive(c)
        empty = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner@gmail.com"},
            files={"screenshot": ("empty.png", b"", "image/png")},
        )
        assert empty.status_code == 422 and empty.json()["error_code"] == "SCREENSHOT_EMPTY"

        too_large = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner@gmail.com"},
            files={"screenshot": ("big.png", b"a" * (5 * 1024 * 1024 + 1), "image/png")},
        )
        assert too_large.status_code == 422 and too_large.json()["error_code"] == "SCREENSHOT_TOO_LARGE"

        bad_type = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner@gmail.com"},
            files={"screenshot": ("doc.txt", b"hello", "text/plain")},
        )
        assert bad_type.status_code == 422 and bad_type.json()["error_code"] == "INVALID_SCREENSHOT_TYPE"

        ok = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner@gmail.com", "note": "带图兼容"},
            files={"screenshot": ("proof.png", b"valid-png", "image/png")},
        )
        assert ok.status_code == 200
        assert ok.json()["warranty"]["has_screenshot"] is True
        assert c.get(f"/api/v1/admin/codes/{code}/warranty-screenshot").status_code == 401
        dl = c.get(f"/api/v1/admin/codes/{code}/warranty-screenshot", headers=ADMIN)
        assert dl.status_code == 200 and dl.content == b"valid-png"


def test_tg_auto_forward_success_transitions_and_is_idempotent(tmp_path: Path):
    tg = DryRunTelegramForwarder(enabled=True, target="900001")
    app, _, mailer = _build_app(tmp_path, tg_enabled=True, tg_target="900001", tg_forwarder=tg)
    with TestClient(app) as c:
        code, _ = _issue_and_redeem_exclusive(c)
        upstream_order = app.state.ledger.get_code(code)["upstream_order_code"]

        resp = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            json={"admin_email": "buyer.auto@gmail.com", "note": "已拉商店号"},
        )
        assert resp.status_code == 200, resp.text
        pub = resp.json()
        assert pub["status"] == "completed"
        assert set(pub["warranty"]) == {"status", "admin_email_masked", "submitted_at", "has_screenshot", "note"}
        assert pub["warranty"]["status"] == "registered_upstream"
        assert pub["warranty"]["has_screenshot"] is False
        assert pub["warranty"]["admin_email_masked"] == "buyer.auto@gmail.com"
        assert pub["buyer_email_masked"] == "buyer.auto@gmail.com"
        assert "tg_message_id" not in resp.text
        assert "auto_forwarded" not in resp.text

        expected_template = format_warranty_forward_template(upstream_order, "buyer.auto@gmail.com")
        assert len(tg.sent) == 1
        assert tg.sent[0]["text"] == expected_template
        assert tg.sent[0]["target"] == "900001"
        assert "gửi kèm ảnh" not in tg.sent[0]["text"]

        detail = c.get(f"/api/v1/admin/codes/{code}", headers=ADMIN).json()
        assert detail["warranty_status"] == "registered_upstream"
        assert detail["display_status"] == "completed"
        assert detail["warranty"]["status"] == "registered_upstream"
        assert detail["warranty"]["auto_forwarded"] is True
        assert detail["warranty"]["tg_forward_status"] == "sent"
        assert detail["warranty"]["tg_message_id"] == tg.sent[0]["message_id"]
        assert detail["warranty"]["tg_sent_at"] == tg.sent[0]["sent_at"]
        assert detail["warranty"]["tg_error"] is None
        assert detail["warranty"]["forward_template"] == expected_template
        audit_reasons = [event["reason"] for event in detail["audit_events"]]
        assert "warranty_submitted" in audit_reasons
        assert "warranty_auto_forwarded" in audit_reasons
        assert any("自动 TG 报备" in mail["subject"] for mail in mailer.sent)

        overview = c.get("/api/v1/admin/overview", headers=ADMIN).json()
        assert overview["counts"]["pending_warranty_ack"] == 0
        assert overview["counts"]["manual_attention"] == 0
        assert overview["counts"]["completed"] == 1
        manual = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "manual_attention"}).json()
        assert manual["items"] == []

        repeat_buyer = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            json={"admin_email": "another@gmail.com"},
        )
        assert repeat_buyer.status_code == 409
        assert repeat_buyer.json()["error_code"] == "WARRANTY_LOCKED"

        repeat_admin = c.post(f"/api/v1/admin/codes/{code}/warranty-forward", headers=ADMIN)
        assert repeat_admin.status_code == 409
        assert repeat_admin.json()["error_code"] == "WARRANTY_LOCKED"
        assert len(tg.sent) == 1


def test_tg_forward_concurrent_submissions_send_only_once(tmp_path: Path):
    class SlowDryRunTG(DryRunTelegramForwarder):
        def send_text(self, text: str) -> dict[str, Any]:
            time.sleep(0.08)
            return super().send_text(text)

    tg = SlowDryRunTG(enabled=True, target="900001")
    app, _, _ = _build_app(tmp_path, tg_enabled=True, tg_target="900001", tg_forwarder=tg)
    with TestClient(app) as c:
        code, _ = _issue_and_redeem_exclusive(c)

    svc = app.state.service
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(svc.warranty, code, "race.owner@gmail.com", None, "first", None, "")
        fut2 = pool.submit(svc.warranty, code, "race.owner@gmail.com", None, "second", None, "")
        res1, err1 = fut1.result()
        res2, err2 = fut2.result()

    assert sorted([err1 or "OK", err2 or "OK"]) == ["OK", "WARRANTY_LOCKED"]
    assert len(tg.sent) == 1


def test_tg_forward_failure_fallback_sanitizes_secrets_and_admin_retry_recovers(tmp_path: Path):
    fake_hash = "test_secret_api_hash_value_999"
    fake_session = "1AAABBBCCC_TEST_SESSION_TOKEN_XYZ"

    class FlakyTG:
        enabled = True
        target_configured = True
        is_ready = True
        api_hash = fake_hash
        session = fake_session

        def __init__(self) -> None:
            self.should_fail = True
            self.sent: list[str] = []

        def send_text(self, text: str) -> dict[str, Any]:
            if self.should_fail:
                raise RuntimeError(
                    f"Network failure\nusing api_hash={fake_hash} session={fake_session} details"
                )
            self.sent.append(text)
            return {"message_id": 7788, "sent_at": "2026-11-26T00:10:00+08:00", "target": "900001"}

    flaky_tg = FlakyTG()
    app, _, mailer = _build_app(
        tmp_path,
        tg_enabled=True,
        tg_target="900001",
        tg_api_id=123456,
        tg_api_hash=fake_hash,
        tg_session=fake_session,
        tg_forwarder=flaky_tg,
    )
    with TestClient(app) as c:
        code, _ = _issue_and_redeem_exclusive(c)
        upstream_order = app.state.ledger.get_code(code)["upstream_order_code"]

        resp = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            json={"admin_email": "fallback.buyer@gmail.com", "note": "请保修"},
        )
        assert resp.status_code == 200, resp.text
        pub = resp.json()
        assert pub["status"] == "completed"
        assert pub["warranty"]["status"] == "submitted"
        assert fake_hash not in resp.text
        assert fake_session not in resp.text

        detail = c.get(f"/api/v1/admin/codes/{code}", headers=ADMIN).json()
        assert detail["warranty"]["status"] == "submitted"
        assert detail["warranty"]["auto_forwarded"] is False
        assert detail["warranty"]["tg_forward_status"] == "failed"
        assert detail["warranty"]["tg_message_id"] is None
        tg_error = detail["warranty"]["tg_error"]
        assert tg_error and "RuntimeError:" in tg_error
        assert fake_hash not in tg_error
        assert fake_session not in tg_error
        assert "[REDACTED]" in tg_error
        assert "\n" not in tg_error
        assert "warranty_tg_forward_failed" in [e["reason"] for e in detail["audit_events"]]

        assert len(mailer.sent) == 1
        alert_mail = mailer.sent[0]
        assert "失败" in alert_mail["subject"]
        assert code in alert_mail["body"]
        assert upstream_order in alert_mail["body"]
        assert "fallback.buyer@gmail.com" in alert_mail["body"]
        assert format_warranty_forward_template(upstream_order, "fallback.buyer@gmail.com") in alert_mail["body"]
        assert fake_hash not in alert_mail["body"]
        assert fake_session not in alert_mail["body"]

        overview = c.get("/api/v1/admin/overview", headers=ADMIN).json()
        assert overview["counts"]["pending_warranty_ack"] == 1
        assert overview["counts"]["manual_attention"] == 1
        assert overview["counts"]["completed"] == 0

        retry_fail = c.post(f"/api/v1/admin/codes/{code}/warranty-forward", headers=ADMIN)
        assert retry_fail.status_code == 503
        assert retry_fail.json()["error_code"] == "TG_FORWARD_FAILED"
        assert fake_hash not in retry_fail.text
        assert fake_session not in retry_fail.text

        flaky_tg.should_fail = False
        retry_ok = c.post(f"/api/v1/admin/codes/{code}/warranty-forward", headers=ADMIN)
        assert retry_ok.status_code == 200, retry_ok.text
        recovered = retry_ok.json()
        assert recovered["warranty"]["status"] == "registered_upstream"
        assert recovered["warranty"]["auto_forwarded"] is True
        assert recovered["warranty"]["tg_forward_status"] == "sent"
        assert recovered["warranty"]["tg_message_id"] == 7788
        assert recovered["warranty"]["tg_error"] is None
        assert recovered["display_status"] == "completed"
        assert len(flaky_tg.sent) == 1

        overview_after = c.get("/api/v1/admin/overview", headers=ADMIN).json()
        assert overview_after["counts"]["pending_warranty_ack"] == 0
        assert overview_after["counts"]["manual_attention"] == 0
        assert overview_after["counts"]["completed"] == 1


def test_admin_warranty_forward_errors_and_overview_redaction(tmp_path: Path):
    fake_hash = "super_secret_hash_do_not_leak"
    fake_session = "super_secret_session_do_not_leak"
    app, _, _ = _build_app(
        tmp_path,
        tg_enabled=False,
        tg_target="900002",
        tg_api_id=654321,
        tg_api_hash=fake_hash,
        tg_session=fake_session,
    )
    with TestClient(app) as c:
        overview_resp = c.get("/api/v1/admin/overview", headers=ADMIN)
        assert overview_resp.status_code == 200
        overview = overview_resp.json()
        assert overview["telegram_warranty"] == {
            "enabled": False,
            "ready": False,
            "target_configured": True,
        }
        assert fake_hash not in overview_resp.text
        assert fake_session not in overview_resp.text
        assert "900002" not in overview_resp.text

        assert c.post("/api/v1/admin/codes/U20E-NONE/warranty-forward").status_code == 401
        not_found = c.post("/api/v1/admin/codes/U20E-NONE/warranty-forward", headers=ADMIN)
        assert not_found.status_code == 404 and not_found.json()["error_code"] == "CODE_NOT_FOUND"

        shared_code = c.post(
            "/api/v1/admin/codes/issue",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "taobao"},
        ).json()["code"]
        not_applicable = c.post(f"/api/v1/admin/codes/{shared_code}/warranty-forward", headers=ADMIN)
        assert not_applicable.status_code == 409 and not_applicable.json()["error_code"] == "WARRANTY_NOT_APPLICABLE"

        excl_code, _ = _issue_and_redeem_exclusive(c)
        not_submitted = c.post(f"/api/v1/admin/codes/{excl_code}/warranty-forward", headers=ADMIN)
        assert not_submitted.status_code == 409 and not_submitted.json()["error_code"] == "WARRANTY_NOT_ALLOWED"

        sub = c.post(
            f"/api/v1/public/codes/{excl_code}/warranty",
            json={"admin_email": "manual.retry@gmail.com"},
        )
        assert sub.status_code == 200
        not_configured = c.post(f"/api/v1/admin/codes/{excl_code}/warranty-forward", headers=ADMIN)
        assert not_configured.status_code == 503
        assert not_configured.json()["error_code"] == "TG_NOT_CONFIGURED"


def test_sanitize_tg_error_and_forwarder_entity_warmup_and_verify():
    long_msg = "x" * 300 + "\nsecret_hash\tsecret_session"
    sanitized = sanitize_tg_error(
        ValueError(long_msg),
        secrets=("secret_hash", "secret_session"),
    )
    assert sanitized.startswith("ValueError: ")
    assert "secret_hash" not in sanitized
    assert "secret_session" not in sanitized
    assert "\n" not in sanitized
    assert len(sanitized) <= 240

    class FakeTelethonClient:
        def __init__(self, *, authorized: bool = True, fail_first_entity: bool = False) -> None:
            self.authorized = authorized
            self.fail_first_entity = fail_first_entity
            self.entity_calls: list[Any] = []
            self.dialogs_calls: list[int] = []
            self.sent_messages: list[tuple[Any, str]] = []
            self.disconnected = False

        async def connect(self) -> None:
            return None

        async def is_user_authorized(self) -> bool:
            return self.authorized

        async def get_me(self) -> Any:
            return SimpleNamespace(id=111, username="shop_sender")

        async def get_entity(self, target: Any) -> Any:
            self.entity_calls.append(target)
            if self.fail_first_entity and len(self.entity_calls) == 1:
                raise ValueError("Could not find the input entity for PeerUser")
            return SimpleNamespace(id=999888, username="canboso_admin")

        async def get_dialogs(self, limit: int = 200) -> list[Any]:
            self.dialogs_calls.append(limit)
            return []

        async def send_message(self, entity: Any, text: str) -> Any:
            self.sent_messages.append((entity, text))
            return SimpleNamespace(id=4242)

        async def disconnect(self) -> None:
            self.disconnected = True

    created_clients: list[FakeTelethonClient] = []

    def factory_warmup() -> FakeTelethonClient:
        client = FakeTelethonClient(authorized=True, fail_first_entity=True)
        created_clients.append(client)
        return client

    fwd = TelegramWarrantyForwarder(
        enabled=True,
        api_id=12345,
        api_hash="hash_secret_1",
        session="session_secret_1",
        target="999888",
        timeout_seconds=10.0,
        min_interval_seconds=0.05,
        client_factory=factory_warmup,
    )
    assert fwd.is_ready is True
    res1 = fwd.send_text("hello 1")
    assert res1["message_id"] == 4242
    assert created_clients[0].entity_calls == [999888, 999888]
    assert created_clients[0].dialogs_calls == [200]
    assert created_clients[0].disconnected is True

    # Verify read-only check never sends messages
    verify_summary = fwd.verify_connection()
    assert verify_summary["ok"] is True
    assert verify_summary["me_id"] == 111
    assert verify_summary["target_id"] == 999888
    assert created_clients[-1].sent_messages == []
    assert created_clients[-1].disconnected is True

    # Verify running inside an active asyncio event loop works without nested-loop error
    async def _call_inside_loop() -> dict[str, Any]:
        return fwd.send_text("hello from running loop")

    res2 = asyncio.run(_call_inside_loop())
    assert res2["message_id"] == 4242

    # Verify min_interval_seconds pacing on consecutive sends
    t0 = time.monotonic()
    res3 = fwd.send_text("hello paced")
    elapsed = time.monotonic() - t0
    assert res3["message_id"] == 4242
    assert elapsed >= 0.04

    # Unauthorized session raises TelegramForwardError and still disconnects
    unauth_client = FakeTelethonClient(authorized=False)
    fwd_unauth = TelegramWarrantyForwarder(
        enabled=True,
        api_id=12345,
        api_hash="hash_secret_2",
        session="session_secret_2",
        target="@shop_admin",
        min_interval_seconds=0.0,
        client_factory=lambda: unauth_client,
    )
    with pytest.raises(TelegramForwardError, match="not authorized"):
        fwd_unauth.send_text("should fail")
    assert unauth_client.disconnected is True

    # Secret leakage in exception from client is stripped by forwarder
    class FailingClient(FakeTelethonClient):
        async def send_message(self, entity: Any, text: str) -> Any:
            raise RuntimeError("boom hash_secret_3 session_secret_3")

    fail_client = FailingClient()
    fwd_fail = TelegramWarrantyForwarder(
        enabled=True,
        api_id=12345,
        api_hash="hash_secret_3",
        session="session_secret_3",
        target="@shop_admin",
        min_interval_seconds=0.0,
        client_factory=lambda: fail_client,
    )
    with pytest.raises(TelegramForwardError) as exc_info:
        fwd_fail.send_text("trigger failure")
    assert "hash_secret_3" not in str(exc_info.value)
    assert "session_secret_3" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    assert fail_client.disconnected is True


def test_tg_login_verify_only_cli(tmp_path: Path, monkeypatch, capsys):
    from backend import tg_login

    tg_keys = (
        "PLAN2_CONFIG_FILE",
        "PLAN2_TG_WARRANTY_ENABLED",
        "PLAN2_TG_API_ID",
        "PLAN2_TG_API_HASH",
        "PLAN2_TG_SESSION",
        "PLAN2_TG_ADMIN_TARGET",
        "PLAN2_TG_TIMEOUT_SECONDS",
    )
    for key in tg_keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tg_login.os, "environ", tg_login.os.environ.copy())

    empty_env = tmp_path / "empty.env"
    empty_env.write_text("PLAN2_TG_API_ID=\n", encoding="utf-8")
    exit_missing = tg_login.main(["--verify-only", "--env-file", str(empty_env)])
    assert exit_missing == 1
    captured_missing = capsys.readouterr()
    assert "PLAN2_TG_API_ID" in captured_missing.err

    valid_env = tmp_path / "valid.env"
    valid_env.write_text(
        "PLAN2_TG_API_ID=123456\n"
        "PLAN2_TG_API_HASH=dummy_hash_for_cli_test\n"
        "PLAN2_TG_SESSION=dummy_session_for_cli_test\n"
        "PLAN2_TG_ADMIN_TARGET=998877\n",
        encoding="utf-8",
    )

    def fake_verify(self):
        return {
            "ok": True,
            "mode": "live",
            "target": self.target,
            "me_id": 100,
            "me_username": "sender_bot",
            "target_id": 998877,
            "target_type": "User",
            "target_username": "admin_user",
        }

    monkeypatch.setattr(TelegramWarrantyForwarder, "verify_connection", fake_verify)
    exit_ok = tg_login.main(["--verify-only", "--env-file", str(valid_env)])
    assert exit_ok == 0
    captured_ok = capsys.readouterr()
    assert '"ok": true' in captured_ok.out
    assert "dummy_hash_for_cli_test" not in captured_ok.out
    assert "dummy_session_for_cli_test" not in captured_ok.out


class _FakeKeyboard:
    def __init__(self, page: "_FakeWebPage") -> None:
        self.page = page
        self.events: list[tuple[str, str]] = []

    async def press(self, key: str) -> None:
        self.events.append(("press", key))
        if key == "Backspace":
            self.page.composer_text = ""
        elif key == "Shift+Enter":
            self.page.composer_text += "\n"

    async def insert_text(self, text: str) -> None:
        self.events.append(("insert_text", text))
        self.page.composer_text += text


class _FakeLocator:
    def __init__(self, page: "_FakeWebPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def count(self) -> int:
        return 1

    async def click(self, **kwargs: Any) -> None:
        self.page.clicks.append(self.selector)
        if "btn-send" in self.selector:
            self.page.sent_payloads.append(self.page.composer_text)
            self.page.composer_text = ""
            self.page.send_clicked = True


class _FakeWebPage:
    def __init__(
        self,
        *,
        logged_in: bool = True,
        target_peer: str = "998877",
        active_peer: str = "998877",
        before_mid: int = 11550,
        confirmed_mid: int = 11565,
        delivery_error: bool = False,
        mismatch_before_click: bool = False,
        stuck_draft: bool = False,
        corrupt_text_before_click: bool = False,
    ) -> None:
        self.logged_in = logged_in
        self.target_peer = target_peer
        self.active_peer = active_peer
        self.before_mid = before_mid
        self.confirmed_mid = confirmed_mid
        self.delivery_error = delivery_error
        self.mismatch_before_click = mismatch_before_click
        self.stuck_draft = stuck_draft
        self.corrupt_text_before_click = corrupt_text_before_click
        self.composer_text = ""
        self.clicks: list[str] = []
        self.sent_payloads: list[str] = []
        self.send_clicked = False
        self.poll_after_send = 0
        self.keyboard = _FakeKeyboard(self)

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.last_url = url

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "hasLoginPrompt" in script:
            return {
                "hasAuth": self.logged_in,
                "inChats": self.logged_in,
                "authVisible": not self.logged_in,
                "hasLoginPrompt": not self.logged_in,
                "meId": 100 if self.logged_in else None,
            }
        if ".chatlist a.chatlist-chat')" in script:
            return True
        if "el.click()" in script:
            return True
        if "window.location.hash" in script:
            return None
        if "composerVisible" in script:
            return {
                "hash": f"#{self.active_peer}",
                "peerId": self.active_peer,
                "peerTitle": "Admin Peer",
                "composerVisible": True,
            }
        if "btnReady:" in script:
            assert isinstance(arg, dict)
            expected_peer = str(arg.get("expectedPeer") or "")
            expected_norm = str(arg.get("expectedNorm") or "")
            actual_norm = " ".join(self.composer_text.split())
            if self.corrupt_text_before_click:
                actual_norm = "stale draft " + actual_norm
            return {
                "peerMatch": (not self.mismatch_before_click) and (self.active_peer == expected_peer),
                "textLen": len(actual_norm),
                "textMatch": bool(expected_norm) and actual_norm == expected_norm,
                "btnReady": bool(actual_norm),
                "maxMid": self.before_mid,
                "errorMids": [],
            }
        if "allCount" in script:
            return {"maxMid": self.before_mid, "allCount": 3, "errorMids": []}
        if "innerText || '').trim().length)" in script:
            return 5 if self.stuck_draft else len(self.composer_text.strip())
        if "let maxMid = 0;" in script:
            return self.before_mid
        if "peer-title')?.getAttribute('data-peer-id')" in script:
            return self.active_peer
        if "confirmedMid" in script:
            self.poll_after_send += 1
            assert isinstance(arg, dict)
            assert int(arg.get("minMid", -1)) == self.before_mid
            assert isinstance(arg.get("requiredTokens"), list) and len(arg["requiredTokens"]) >= 1
            if self.delivery_error:
                return {"confirmedMid": 0, "hasError": True, "remainingDraftLen": 0}
            if self.poll_after_send == 1:
                return {"confirmedMid": 0, "hasError": False, "remainingDraftLen": 0}
            return {
                "confirmedMid": self.confirmed_mid if self.send_clicked else 0,
                "hasError": False,
                "remainingDraftLen": 0,
            }
        raise AssertionError(f"Unexpected evaluate script: {script[:80]}")


def _page_ctx_factory(page: _FakeWebPage):
    class _Ctx:
        async def __aenter__(self) -> _FakeWebPage:
            return page

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    return _Ctx


def test_build_telegram_forwarder_transport_selection(tmp_path: Path):
    profile_dir = tmp_path / "tg_web_profile"
    profile_dir.mkdir()

    dry_settings = Settings(
        execution_mode="dry_run",
        tg_warranty_enabled=True,
        tg_transport="web",
        tg_web_profile_dir=str(profile_dir),
        tg_admin_target="998877",
    )
    assert isinstance(build_telegram_forwarder(dry_settings), DryRunTelegramForwarder)

    auto_web_settings = Settings(
        execution_mode="live",
        tg_warranty_enabled=True,
        tg_transport="auto",
        tg_web_profile_dir=str(profile_dir),
        tg_admin_target="998877",
        tg_timeout_seconds=45.0,
    )
    assert resolve_tg_transport(auto_web_settings) == "web"
    web_fwd = build_telegram_forwarder(auto_web_settings)
    assert isinstance(web_fwd, PlaywrightTelegramWebForwarder)
    assert web_fwd.is_ready is True
    assert web_fwd.timeout_seconds == 45.0

    low_timeout_web_settings = Settings(
        execution_mode="live",
        tg_warranty_enabled=True,
        tg_transport="web",
        tg_web_profile_dir=str(profile_dir),
        tg_admin_target="998877",
        tg_timeout_seconds=15.0,
    )
    low_timeout_web_fwd = build_telegram_forwarder(low_timeout_web_settings)
    assert isinstance(low_timeout_web_fwd, PlaywrightTelegramWebForwarder)
    assert low_timeout_web_fwd.timeout_seconds == 30.0
    assert normalize_web_timeout_seconds(None) == 45.0
    assert normalize_web_timeout_seconds(0) == 45.0
    assert normalize_web_timeout_seconds(15) == 30.0
    assert normalize_web_timeout_seconds(60) == 60.0

    auto_telethon_settings = Settings(
        execution_mode="live",
        tg_warranty_enabled=True,
        tg_transport="auto",
        tg_api_id=123456,
        tg_api_hash="hash_value",
        tg_session="session_value",
        tg_web_profile_dir=str(profile_dir),
        tg_admin_target="998877",
    )
    assert resolve_tg_transport(auto_telethon_settings) == "telethon"
    assert isinstance(build_telegram_forwarder(auto_telethon_settings), TelegramWarrantyForwarder)

    missing_profile_fwd = PlaywrightTelegramWebForwarder(
        enabled=True,
        profile_dir=str(tmp_path / "does_not_exist"),
        target="998877",
    )
    assert missing_profile_fwd.is_ready is False

    username_target_fwd = PlaywrightTelegramWebForwarder(
        enabled=True,
        profile_dir=str(profile_dir),
        target="@admin_handle",
    )
    assert username_target_fwd.is_ready is False
    with pytest.raises(TelegramForwardError, match="numeric peerId"):
        username_target_fwd.send_text("hello")


def test_playwright_web_forwarder_send_verify_and_guards(tmp_path: Path):
    profile_dir = tmp_path / "tg_web_profile"
    profile_dir.mkdir()

    ok_page = _FakeWebPage(before_mid=11550, confirmed_mid=11588)
    fwd = PlaywrightTelegramWebForwarder(
        enabled=True,
        profile_dir=str(profile_dir),
        target="998877",
        timeout_seconds=10.0,
        min_interval_seconds=0.0,
        page_factory=_page_ctx_factory(ok_page),
    )

    verified = fwd.verify_connection()
    assert verified["ok"] is True
    assert verified["transport"] == "web"
    assert verified["target_id"] == "998877"
    assert ok_page.last_url == "https://web.telegram.org/k/#998877"
    assert ok_page.sent_payloads == []

    vn_text = format_warranty_forward_template("CAN-9901", "buyer.demo@gmail.com")
    sent = fwd.send_text(vn_text)
    assert sent["message_id"] == 11588
    assert sent["target"] == "998877"
    assert ok_page.sent_payloads == [vn_text]
    assert ("press", "Shift+Enter") in ok_page.keyboard.events

    logged_out_fwd = PlaywrightTelegramWebForwarder(
        enabled=True,
        profile_dir=str(profile_dir),
        target="998877",
        timeout_seconds=5.0,
        min_interval_seconds=0.0,
        page_factory=_page_ctx_factory(_FakeWebPage(logged_in=False)),
    )
    with pytest.raises(TelegramForwardError, match="not logged in"):
        logged_out_fwd.verify_connection()

    mismatch_page = _FakeWebPage(mismatch_before_click=True)
    mismatch_fwd = PlaywrightTelegramWebForwarder(
        enabled=True,
        profile_dir=str(profile_dir),
        target="998877",
        timeout_seconds=5.0,
        min_interval_seconds=0.0,
        page_factory=_page_ctx_factory(mismatch_page),
    )
    with pytest.raises(TelegramForwardError, match="peer mismatch"):
        mismatch_fwd.send_text("test")
    assert mismatch_page.sent_payloads == []

    err_fwd = PlaywrightTelegramWebForwarder(
        enabled=True,
        profile_dir=str(profile_dir),
        target="998877",
        timeout_seconds=5.0,
        min_interval_seconds=0.0,
        page_factory=_page_ctx_factory(_FakeWebPage(delivery_error=True)),
    )
    with pytest.raises(TelegramForwardError, match="failed to deliver"):
        err_fwd.send_text("test")

    stuck_page = _FakeWebPage(stuck_draft=True)
    stuck_fwd = PlaywrightTelegramWebForwarder(
        enabled=True,
        profile_dir=str(profile_dir),
        target="998877",
        timeout_seconds=5.0,
        min_interval_seconds=0.0,
        page_factory=_page_ctx_factory(stuck_page),
    )
    with pytest.raises(TelegramForwardError, match="could not be cleared"):
        stuck_fwd.send_text("test")
    assert stuck_page.sent_payloads == []

    corrupt_page = _FakeWebPage(corrupt_text_before_click=True)
    corrupt_fwd = PlaywrightTelegramWebForwarder(
        enabled=True,
        profile_dir=str(profile_dir),
        target="998877",
        timeout_seconds=5.0,
        min_interval_seconds=0.0,
        page_factory=_page_ctx_factory(corrupt_page),
    )
    with pytest.raises(TelegramForwardError, match="text mismatch"):
        corrupt_fwd.send_text("test")
    assert corrupt_page.sent_payloads == []


def test_tg_login_verify_only_web_transport(tmp_path: Path, monkeypatch, capsys):
    from backend import tg_login

    tg_keys = (
        "PLAN2_CONFIG_FILE",
        "PLAN2_TG_WARRANTY_ENABLED",
        "PLAN2_TG_TRANSPORT",
        "PLAN2_TG_API_ID",
        "PLAN2_TG_API_HASH",
        "PLAN2_TG_SESSION",
        "PLAN2_TG_WEB_PROFILE_DIR",
        "PLAN2_TG_WEB_URL",
        "PLAN2_TG_ADMIN_TARGET",
        "PLAN2_TG_TIMEOUT_SECONDS",
    )
    for key in tg_keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tg_login.os, "environ", tg_login.os.environ.copy())

    profile_dir = tmp_path / "tg_web_profile"
    profile_dir.mkdir()

    missing_env = tmp_path / "web_missing.env"
    missing_env.write_text(
        "PLAN2_TG_TRANSPORT=web\n"
        f"PLAN2_TG_WEB_PROFILE_DIR={tmp_path / 'absent'}\n"
        "PLAN2_TG_ADMIN_TARGET=998877\n",
        encoding="utf-8",
    )
    assert tg_login.main(["--verify-only", "--env-file", str(missing_env)]) == 1
    assert "不存在" in capsys.readouterr().err

    tg_login.os.environ.pop("PLAN2_TG_WEB_PROFILE_DIR", None)

    valid_env = tmp_path / "web_valid.env"
    valid_env.write_text(
        "PLAN2_TG_TRANSPORT=web\n"
        f"PLAN2_TG_WEB_PROFILE_DIR={profile_dir}\n"
        "PLAN2_TG_ADMIN_TARGET=998877\n",
        encoding="utf-8",
    )

    def fake_web_verify(self):
        return {
            "ok": True,
            "mode": "live",
            "transport": "web",
            "target": self.target,
            "me_id": 100,
            "target_id": "998877",
            "target_title": "Admin",
            "composer_ready": True,
        }

    monkeypatch.setattr(PlaywrightTelegramWebForwarder, "verify_connection", fake_web_verify)
    assert tg_login.main(["--verify-only", "--env-file", str(valid_env)]) == 0
    out = capsys.readouterr().out
    assert '"transport": "web"' in out
    assert '"ok": true' in out

    assert tg_login.main(["--env-file", str(valid_env)]) == 1
    assert "--verify-only" in capsys.readouterr().err


def test_tg_login_verify_only_web_enforces_timeout_floor(tmp_path: Path, monkeypatch, capsys):
    from backend import tg_login

    tg_keys = (
        "PLAN2_CONFIG_FILE",
        "PLAN2_TG_WARRANTY_ENABLED",
        "PLAN2_TG_TRANSPORT",
        "PLAN2_TG_API_ID",
        "PLAN2_TG_API_HASH",
        "PLAN2_TG_SESSION",
        "PLAN2_TG_WEB_PROFILE_DIR",
        "PLAN2_TG_WEB_URL",
        "PLAN2_TG_ADMIN_TARGET",
        "PLAN2_TG_TIMEOUT_SECONDS",
    )
    for key in tg_keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tg_login.os, "environ", tg_login.os.environ.copy())

    profile_dir = tmp_path / "tg_web_profile"
    profile_dir.mkdir()

    recorded_timeouts: list[float] = []

    def fake_web_verify(self):
        recorded_timeouts.append(self.timeout_seconds)
        return {
            "ok": True,
            "mode": "live",
            "transport": "web",
            "target": self.target,
            "me_id": 100,
            "target_id": "998877",
            "target_title": "Admin",
            "composer_ready": True,
        }

    monkeypatch.setattr(PlaywrightTelegramWebForwarder, "verify_connection", fake_web_verify)

    low_timeout_env = tmp_path / "web_low_timeout.env"
    low_timeout_env.write_text(
        "PLAN2_TG_TRANSPORT=web\n"
        f"PLAN2_TG_WEB_PROFILE_DIR={profile_dir}\n"
        "PLAN2_TG_ADMIN_TARGET=998877\n"
        "PLAN2_TG_TIMEOUT_SECONDS=15\n",
        encoding="utf-8",
    )
    assert tg_login.main(["--verify-only", "--env-file", str(low_timeout_env)]) == 0
    capsys.readouterr()
    assert recorded_timeouts == [30.0]

    tg_login.os.environ.pop("PLAN2_TG_TIMEOUT_SECONDS", None)
    high_timeout_env = tmp_path / "web_high_timeout.env"
    high_timeout_env.write_text(
        "PLAN2_TG_TRANSPORT=web\n"
        f"PLAN2_TG_WEB_PROFILE_DIR={profile_dir}\n"
        "PLAN2_TG_ADMIN_TARGET=998877\n"
        "PLAN2_TG_TIMEOUT_SECONDS=60\n",
        encoding="utf-8",
    )
    assert tg_login.main(["--verify-only", "--env-file", str(high_timeout_env)]) == 0
    capsys.readouterr()
    assert recorded_timeouts == [30.0, 60.0]


def test_build_playwright_env_isolates_fallback_dirs_by_user_or_uid(tmp_path: Path, monkeypatch):
    from backend import telegram_forwarder

    env_keys = (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "USERNAME",
        "USER",
        "LOGNAME",
        "LNAME",
    )
    for key in env_keys:
        monkeypatch.delenv(key, raising=False)

    posix_tmp = tmp_path / "posix_tmp"
    posix_tmp.mkdir()
    unwritable_dir = tmp_path / "missing_home"
    chmod_calls: list[tuple[str, int]] = []

    def fake_chmod(path: str, mode: int) -> None:
        chmod_calls.append((path, mode))

    with monkeypatch.context() as mp_ctx:
        mp_ctx.setenv("HOME", str(unwritable_dir))
        mp_ctx.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing_cfg"))
        mp_ctx.setattr(telegram_forwarder.tempfile, "gettempdir", lambda: str(posix_tmp))
        mp_ctx.setattr(telegram_forwarder.os, "getuid", lambda: 1042, raising=False)
        mp_ctx.setattr(telegram_forwarder.os, "name", "posix")
        mp_ctx.setattr(telegram_forwarder.os, "chmod", fake_chmod)
        posix_env = _build_playwright_env()

    expected_posix_root = posix_tmp / "plan2-pw-1042"
    assert Path(posix_env["HOME"]) == expected_posix_root
    assert expected_posix_root.is_dir()
    expected_subdirs = {
        "XDG_CONFIG_HOME": ".config",
        "XDG_CACHE_HOME": ".cache",
        "XDG_DATA_HOME": ".local-share",
        "XDG_RUNTIME_DIR": ".runtime",
    }
    for key, subdir in expected_subdirs.items():
        target_dir = expected_posix_root / subdir
        assert Path(posix_env[key]) == target_dir
        assert target_dir.is_dir()
    assert (str(expected_posix_root), 0o700) in chmod_calls
    assert (str(expected_posix_root / ".runtime"), 0o700) in chmod_calls

    def failing_chmod(path: str, mode: int) -> None:
        raise PermissionError("chmod denied")

    with monkeypatch.context() as mp_ctx:
        mp_ctx.setattr(telegram_forwarder.tempfile, "gettempdir", lambda: str(posix_tmp))
        mp_ctx.setattr(telegram_forwarder.os, "getuid", lambda: 1042, raising=False)
        mp_ctx.setattr(telegram_forwarder.os, "name", "posix")
        mp_ctx.setattr(telegram_forwarder.os, "chmod", failing_chmod)
        tolerated_env = _build_playwright_env()
    assert Path(tolerated_env["HOME"]) == expected_posix_root

    win_tmp = tmp_path / "win_tmp"
    win_tmp.mkdir()
    with monkeypatch.context() as mp_ctx:
        mp_ctx.delattr(telegram_forwarder.os, "getuid", raising=False)
        mp_ctx.setattr(telegram_forwarder.tempfile, "gettempdir", lambda: str(win_tmp))
        mp_ctx.setattr(telegram_forwarder.getpass, "getuser", lambda: "CORP\\Alice Smith")
        win_env = _build_playwright_env()

    expected_win_root = win_tmp / "plan2-pw-CORP_Alice_Smith"
    assert Path(win_env["HOME"]) == expected_win_root
    assert expected_win_root.is_dir()
    for key, subdir in expected_subdirs.items():
        target_dir = expected_win_root / subdir
        assert Path(win_env[key]) == target_dir
        assert target_dir.is_dir()

    def raise_getuser() -> str:
        raise RuntimeError("no user")

    with monkeypatch.context() as mp_ctx:
        mp_ctx.delattr(telegram_forwarder.os, "getuid", raising=False)
        mp_ctx.setattr(telegram_forwarder.tempfile, "gettempdir", lambda: str(win_tmp))
        mp_ctx.setattr(telegram_forwarder.getpass, "getuser", raise_getuser)
        mp_ctx.setenv("USERNAME", "ops.bot@host")
        env_user_fallback = _build_playwright_env()
    assert Path(env_user_fallback["HOME"]) == win_tmp / "plan2-pw-ops_bot_host"

    with monkeypatch.context() as mp_ctx:
        mp_ctx.delattr(telegram_forwarder.os, "getuid", raising=False)
        mp_ctx.setattr(telegram_forwarder.tempfile, "gettempdir", lambda: str(win_tmp))
        mp_ctx.setattr(telegram_forwarder.getpass, "getuser", raise_getuser)
        default_env = _build_playwright_env()
    expected_default_root = win_tmp / "plan2-pw-default"
    assert Path(default_env["HOME"]) == expected_default_root
    assert expected_default_root.is_dir()
    assert (expected_default_root / ".runtime").is_dir()

    writable_tmp = tmp_path / "writable_tmp"
    writable_tmp.mkdir()
    existing_dirs = {}
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        dir_path = tmp_path / f"existing_{key.lower()}"
        dir_path.mkdir()
        existing_dirs[key] = str(dir_path)
    with monkeypatch.context() as mp_ctx:
        mp_ctx.setattr(telegram_forwarder.tempfile, "gettempdir", lambda: str(writable_tmp))
        for key, dir_value in existing_dirs.items():
            mp_ctx.setenv(key, dir_value)
        preserved_env = _build_playwright_env()
    for key, dir_value in existing_dirs.items():
        assert preserved_env[key] == dir_value
    assert list(writable_tmp.iterdir()) == []
