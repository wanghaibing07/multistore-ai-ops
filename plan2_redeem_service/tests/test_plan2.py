from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db import now_iso
from backend.gateways import DryRun16688, DryRunCanboso, UpstreamError, UpstreamTimeout
from backend.gateways import CanbosoClient, Channel16688Client
from backend.service import Mailer, format_warranty_forward_template


ADMIN = {"Authorization": "Bearer test-admin"}


def client(app):
    return TestClient(app)


def issue(c, sku="ultra_shared_slot", channel="taobao", **extra):
    response = c.post("/api/v1/admin/codes/issue", headers=ADMIN, json={"sku_key": sku, "channel": channel, **extra})
    assert response.status_code == 201, response.text
    return response.json()["code"]


def test_poll_interval_defaults_to_twenty_minutes_and_supports_env_override(monkeypatch):
    monkeypatch.delenv("PLAN2_CONFIG_FILE", raising=False)
    monkeypatch.delenv("PLAN2_POLL_INTERVAL_SECONDS", raising=False)
    assert Settings.from_env().poll_interval_seconds == 1200

    monkeypatch.setenv("PLAN2_POLL_INTERVAL_SECONDS", "45")
    assert Settings.from_env().poll_interval_seconds == 45


def test_health_auth_and_batch_issue(components):
    app, _, _, channel, _ = components
    with client(app) as c:
        assert c.get("/api/v1/health").json() == {"status": "ok", "execution_mode": "dry_run"}
        assert c.get("/api/v1/admin/overview").status_code == 401
        result = c.post("/api/v1/admin/codes/batch", headers=ADMIN, json={"sku_key": "ultra_shared_slot", "channel": "16688", "count": 3, "goods_no": "G100", "batch_label": "首批"})
        assert result.status_code == 201
        assert len(result.json()["codes"]) == 3
        assert channel.imported


def test_shared_redeem_and_poll_completion(components):
    app, _, canboso, _, mailer = components
    with client(app) as c:
        code = issue(c)
        bad = c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "a@gmail.com", "buyer_email_confirm": "b@gmail.com", "preflight_confirmed": True})
        assert bad.status_code == 422 and bad.json()["error_code"] == "EMAIL_MISMATCH"
        redeemed = c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "alice@gmail.com", "buyer_email_confirm": "alice@gmail.com", "preflight_confirmed": True})
        assert redeemed.status_code == 200
        assert redeemed.json()["status"] == "waiting_fulfillment"
        assert redeemed.json()["delivery"] is None
        row = app.state.ledger.get_code(code)
        canboso.orders[row["upstream_order_code"]]["status"] = "completed"
        canboso.orders[row["upstream_order_code"]]["deliveredAt"] = now_iso()
        done = c.post(f"/api/v1/admin/codes/{code}/poll", headers=ADMIN)
        assert done.json()["status"] == "completed"
        assert done.json()["delivery"] is None
        assert any("共享车位" in mail["subject"] for mail in mailer.sent)


def test_gmail_domain_is_required(components):
    app, _, _, _, _ = components
    with client(app) as c:
        code = issue(c)
        response = c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "buyer@example.com", "buyer_email_confirm": "buyer@example.com", "preflight_confirmed": True})
        assert response.status_code == 422
        assert response.json()["error_code"] == "INVALID_EMAIL"


def test_googlemail_domain_is_accepted(components):
    app, _, _, _, _ = components
    with client(app) as c:
        code = issue(c)
        response = c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "buyer@googlemail.com", "buyer_email_confirm": "buyer@googlemail.com", "preflight_confirmed": True})
        assert response.status_code == 200


def test_concurrent_double_spend_only_one_charge(components):
    app, _, canboso, _, _ = components
    code = issue(client(app))
    svc = app.state.service
    def redeem():
        return svc.redeem(code, "a@gmail.com", "a@gmail.com", True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: redeem(), range(8)))
    assert canboso.charge_count == 1
    assert sum(1 for result, err in results if not err and result.get("status") == "waiting_fulfillment") == 1


def test_timeout_after_charge_replay_reuses_idempotency_key(tmp_path):
    settings = Settings(db_path=str(tmp_path / "db.sqlite3"), storage_dir=str(tmp_path / "private"), admin_token="test-admin", execution_mode="dry_run")
    canboso = __import__("tests.conftest", fromlist=["TimeoutAfterCharge"]).TimeoutAfterCharge()
    app = create_app(settings, canboso=canboso, channel=__import__("tests.conftest", fromlist=["Fake16688"]).Fake16688(), mailer=Mailer(settings))
    c = client(app)
    code = issue(c, sku="ultra_exclusive_admin")
    first = c.post(f"/api/v1/public/codes/{code}/redeem", json={"preflight_confirmed": True})
    assert first.json()["status"] == "needs_manual_review"
    original_key = app.state.ledger.get_code(code)["idempotency_key"]
    retry = c.post(f"/api/v1/admin/codes/{code}/retry", headers=ADMIN, json={})
    assert retry.json()["status"] == "delivered_pending_warranty"
    assert app.state.ledger.get_code(code)["idempotency_key"] == original_key
    assert canboso.charge_count == 1


def test_price_guard_uses_usd_amount_and_ignores_vnd_promotion(components):
    app, _, canboso, _, mailer = components
    canboso.products["6ac26aaeec73a42d6430a837"]["promotions"] = [{"unitPrice": 999999}]
    code = issue(client(app))
    result, err = app.state.service.redeem(code, "a@gmail.com", "a@gmail.com", True)
    assert err is None and result["status"] == "waiting_fulfillment"
    canboso.products["6ac26aaeec73a42d6430a837"]["price"]["amount"] = 99
    code2 = issue(client(app))
    result2, err2 = app.state.service.redeem(code2, "a@gmail.com", "a@gmail.com", True)
    assert err2 is None and result2["status"] == "needs_manual_review"
    assert app.state.ledger.get_code(code2)["idempotency_key"]
    assert any("异常" in mail["subject"] for mail in mailer.sent)


def test_16688_refund_or_complaint_is_blocked(components):
    app, _, _, channel, _ = components
    channel.allowed = False
    c = client(app)
    code = issue(c, channel="16688")
    response = c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "a@gmail.com", "buyer_email_confirm": "a@gmail.com", "preflight_confirmed": True})
    assert response.status_code == 409 and response.json()["error_code"] == "ORDER_NOT_ELIGIBLE"
    assert app.state.ledger.get_code(code)["status"] == "unused"


def test_over_24h_alert_and_no_6h_alert(components):
    app, _, canboso, _, mailer = components
    c = client(app)
    code = issue(c)
    c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "a@gmail.com", "buyer_email_confirm": "a@gmail.com", "preflight_confirmed": True})
    old = (datetime.now(timezone.utc).astimezone() - timedelta(hours=6)).isoformat()
    with app.state.ledger.transaction(immediate=True) as conn:
        conn.execute("UPDATE codes SET submitted_at=?, updated_at=? WHERE code=?", (old, old, code))
    app.state.service.poll(code)
    assert app.state.ledger.get_code(code)["status"] == "waiting_fulfillment"
    assert not any("异常" in mail["subject"] for mail in mailer.sent)
    very_old = (datetime.now(timezone.utc).astimezone() - timedelta(hours=25)).isoformat()
    with app.state.ledger.transaction(immediate=True) as conn:
        conn.execute("UPDATE codes SET submitted_at=? WHERE code=?", (very_old, code))
    app.state.service.poll(code)
    assert app.state.ledger.get_code(code)["status"] == "needs_manual_review"
    assert any("异常" in mail["subject"] for mail in mailer.sent)


def test_poll_transient_upstream_failures_wait_then_manual_review(components):
    app, _, canboso, _, mailer = components
    with client(app) as c:
        code = issue(c)
        redeemed = c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "a@gmail.com", "buyer_email_confirm": "a@gmail.com", "preflight_confirmed": True})
        assert redeemed.json()["status"] == "waiting_fulfillment"

        original_get_order = canboso.get_order
        remaining_failures = 3

        def flaky_get_order(order_code):
            nonlocal remaining_failures
            if remaining_failures:
                remaining_failures -= 1
                raise UpstreamTimeout("temporary network failure")
            return original_get_order(order_code)

        canboso.get_order = flaky_get_order
        for _ in range(2):
            result = app.state.service.poll(code)
            assert result["status"] == "waiting_fulfillment"
        assert not any("异常" in mail["subject"] for mail in mailer.sent)

        result = app.state.service.poll(code)
        assert result["status"] == "needs_manual_review"
        assert sum("异常" in mail["subject"] for mail in mailer.sent) == 1


def test_successful_poll_resets_transient_failure_count(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        code = issue(c)
        c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "a@gmail.com", "buyer_email_confirm": "a@gmail.com", "preflight_confirmed": True})
        original_get_order = canboso.get_order
        remaining_failures = 2

        def flaky_get_order(order_code):
            nonlocal remaining_failures
            if remaining_failures:
                remaining_failures -= 1
                raise UpstreamTimeout("temporary network failure")
            return original_get_order(order_code)

        canboso.get_order = flaky_get_order
        assert app.state.service.poll(code)["status"] == "waiting_fulfillment"
        assert app.state.service.poll(code)["status"] == "waiting_fulfillment"
        assert app.state.service.poll(code)["status"] == "waiting_fulfillment"

        remaining_failures = 1
        assert app.state.service.poll(code)["status"] == "waiting_fulfillment"


def test_exclusive_delivery_warranty_and_screenshot_auth(components):
    app, _, _, _, mailer = components
    c = client(app)
    code = issue(c, sku="ultra_exclusive_admin")
    delivered = c.post(f"/api/v1/public/codes/{code}/redeem", json={"preflight_confirmed": True})
    assert delivered.json()["status"] == "delivered_pending_warranty"
    warranty = c.post(f"/api/v1/public/codes/{code}/warranty", data={"admin_email": "owner@gmail.com", "admin_email_confirm": "owner@gmail.com", "note": "已建组"}, files={"screenshot": ("invite.png", b"png-bytes", "image/png")})
    assert warranty.status_code == 200 and warranty.json()["status"] == "completed"
    assert set(warranty.json()["warranty"]) == {"status", "admin_email_masked", "submitted_at", "has_screenshot", "note"}
    assert "owner@gmail.com" not in warranty.text
    assert "forward_template" not in warranty.text
    detail = c.get(f"/api/v1/admin/codes/{code}", headers=ADMIN).json()
    assert "owner@gmail.com" in detail["warranty"]["forward_template"]
    assert c.get(f"/api/v1/admin/codes/{code}/warranty-screenshot").status_code == 401
    screenshot = c.get(f"/api/v1/admin/codes/{code}/warranty-screenshot", headers=ADMIN)
    assert screenshot.status_code == 200 and screenshot.content == b"png-bytes"
    assert any("保修" in mail["subject"] for mail in mailer.sent)


def test_warranty_rejects_oversized_upload(components):
    app, _, _, _, _ = components
    with client(app) as c:
        code = issue(c, sku="ultra_exclusive_admin")
        c.post(f"/api/v1/public/codes/{code}/redeem", json={"preflight_confirmed": True})
        response = c.post(f"/api/v1/public/codes/{code}/warranty", data={"admin_email": "owner@gmail.com", "admin_email_confirm": "owner@gmail.com"}, files={"screenshot": ("invite.png", b"x" * (5 * 1024 * 1024 + 1), "image/png")})
        assert response.status_code == 422
        assert response.json()["error_code"] == "SCREENSHOT_TOO_LARGE"


def test_empty_screenshot_and_unknown_api_are_rejected(components):
    app, _, _, _, _ = components
    with client(app) as c:
        code = issue(c, sku="ultra_exclusive_admin")
        c.post(f"/api/v1/public/codes/{code}/redeem", json={"preflight_confirmed": True})
        empty = c.post(f"/api/v1/public/codes/{code}/warranty", data={"admin_email": "owner@gmail.com", "admin_email_confirm": "owner@gmail.com"}, files={"screenshot": ("empty.png", b"", "image/png")})
        assert empty.status_code == 422 and empty.json()["error_code"] == "SCREENSHOT_EMPTY"
        missing = c.get("/api/v1/does-not-exist")
        assert missing.status_code == 404 and missing.json()["error_code"] == "NOT_FOUND"


def test_sku_patch_persists_cap_and_can_clear_goods_no(components):
    app, _, _, _, _ = components
    with client(app) as c:
        first = c.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 18.5, "goods_no": "G200"})
        assert first.status_code == 200
        assert first.json()["max_cost_usd"] == 18.5
        assert first.json()["canboso_product_id"]
        assert "current_price_usd" in first.json()
        restarted = create_app(app.state.settings, canboso=app.state.service.canboso, channel=app.state.service.channel, mailer=app.state.mailer)
        assert restarted.state.ledger.get_sku("ultra_shared_slot")["max_cost_usd"] == 18.5
        cleared = c.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 18.5, "goods_no": None})
        assert cleared.status_code == 200 and cleared.json()["goods_no"] is None


def test_16688_verification_error_keeps_replay_key_and_retry_reports_failure(components):
    app, _, canboso, _, _ = components

    class Broken16688:
        def verify_order(self, code, goods_no):
            raise RuntimeError("channel unavailable")

        def stock_secret_add(self, goods_no, content):
            return {"import_count": 1}

    app.state.service.channel = Broken16688()
    with client(app) as c:
        configured = c.patch(
            "/api/v1/admin/settings/skus/ultra_shared_slot",
            headers=ADMIN,
            json={"max_cost_usd": 16.0, "goods_no": "G100"},
        )
        assert configured.status_code == 200
        code = issue(c, channel="16688")
        first = c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "a@gmail.com", "buyer_email_confirm": "a@gmail.com", "preflight_confirmed": True})
        assert first.status_code == 200 and first.json()["status"] == "needs_manual_review"
        row = app.state.ledger.get_code(code)
        assert row["idempotency_key"] and row["request_payload_json"]
        canboso.products["6ac26aaeec73a42d6430a837"]["price"]["amount"] = 99
        retry = c.post(f"/api/v1/admin/codes/{code}/retry", headers=ADMIN, json={"note": "再次检查"})
        assert retry.status_code == 503 and retry.json()["error_code"] == "PRICE_LIMIT_EXCEEDED"
        assert "admin_retry_failed:PRICE_LIMIT_EXCEEDED" in [event["reason"] for event in app.state.ledger.audit(code)]


def test_revoke_completed_is_blocked(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        code = issue(c)
        c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "a@gmail.com", "buyer_email_confirm": "a@gmail.com", "preflight_confirmed": True})
        row = app.state.ledger.get_code(code)
        canboso.orders[row["upstream_order_code"]]["status"] = "completed"
        c.post(f"/api/v1/admin/codes/{code}/poll", headers=ADMIN)
        response = c.post(f"/api/v1/admin/codes/{code}/revoke", headers=ADMIN, json={})
        assert response.status_code == 409 and response.json()["error_code"] == "CANNOT_REVOKE_COMPLETED"


def test_canboso_client_uses_buyer_key_and_purchase_idempotency(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"success": True}

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("httpx.request", fake_request)
    client_ = CanbosoClient("https://canboso.com", "buyer-secret")
    client_.list_products()
    client_.purchase("plan2-key-123456", {"product_id": "sku", "quantity": 1})
    assert calls[0][2]["params"] == {"key": "buyer-secret"}
    assert calls[1][2]["headers"] == {"Idempotency-Key": "plan2-key-123456"}
    assert calls[1][2]["json"]["key"] == "buyer-secret"


def test_canboso_429_honors_retry_after_without_tight_loop(monkeypatch):
    responses = []
    sleeps = []

    class Response:
        def __init__(self, status_code, retry_after=None):
            self.status_code = status_code
            self.headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}

        def json(self):
            return {"success": True}

    responses.extend([Response(429, 2), Response(200)])
    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr("time.sleep", lambda value: sleeps.append(value))
    assert CanbosoClient("https://canboso.com", "buyer-secret").get_balance() == {"success": True}
    assert sleeps == [2]


def test_16688_signature_matches_protocol(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"code": 1, "data": {"ok": True}}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr("backend.gateways.secrets.choice", lambda alphabet: "A")
    monkeypatch.setattr("backend.gateways.time.time", lambda: 1700000000)
    result = Channel16688Client("https://16688.example", "app-id", "secret")._call("openApi/order/info", {"trade_no": "T1"})
    assert result == {"ok": True}
    body = captured["json"]
    unsigned = {key: value for key, value in body.items() if key != "sign"}
    canonical = "&".join(f"{key}={unsigned[key]}" for key in sorted(unsigned))
    plain = f"POST\norder\ninfo\n{canonical}"
    assert body["app_id"] == "app-id"
    assert len(body["nonce"]) == 24 and body["nonce"].isalnum()
    assert body["sign"] == hmac.new(b"secret", plain.encode(), hashlib.sha256).hexdigest()


def test_16688_client_keeps_order_and_stock_methods_on_class():
    client_ = Channel16688Client("https://16688.example", "app-id", "secret")
    assert all(callable(getattr(client_, name, None)) for name in ("verify_order", "stock_secret_add", "invalidate"))

    calls = []
    responses = [
        {"list": [{"identity": "merchant", "trade_no": "T1"}]},
        {"goods_no": "G1", "cards": [{"code": "C1"}], "status": 4},
        {"list": []},
        {"list": []},
        {"list": []},
    ]

    def fake_call(path, params):
        calls.append((path, params))
        return responses.pop(0)

    client_._call = fake_call
    assert client_.verify_order("C1", "G1") == {"ok": True, "trade_no": "T1"}
    assert client_.stock_secret_add("G1", "C1") == {"list": []}
    assert client_.invalidate("G1", [7]) == {"list": []}
    assert calls == [
        ("openApi/order/list", {"card_no": "C1", "page_no": 1, "page_size": 20}),
        ("openApi/order/info", {"trade_no": "T1"}),
        ("openApi/complaint/list", {"keywords": "T1", "page_no": 1, "page_size": 20}),
        ("openApi/stockSecretBatch/add", {"goods_no": "G1", "content": "C1", "split_type": 2, "remove_repeat": 1}),
        ("openApi/stockSecret/invalidate", {"goods_no": "G1", "ids": [7]}),
    ]


def test_mailer_uses_smtp_ssl_on_port_465(monkeypatch):
    calls = []

    class FakeSMTPSSL:
        def __init__(self, host, port, timeout=None, context=None):
            calls.append(("init", host, port, timeout, context is not None))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            calls.append(("login", user, len(password)))

        def send_message(self, msg, from_addr=None, to_addrs=None):
            calls.append(("send", from_addr, to_addrs, msg["Subject"]))

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    settings = Settings(
        execution_mode="live",
        smtp_host="smtp.qq.com",
        smtp_port=465,
        smtp_username="sender@qq.com",
        smtp_password="secret-password",
        smtp_from="sender@qq.com",
        merchant_email="merchant@qq.com",
    )
    Mailer(settings).send("主题", "正文")
    assert calls == [
        ("init", "smtp.qq.com", 465, 20, True),
        ("login", "sender@qq.com", 15),
        ("send", "sender@qq.com", ["merchant@qq.com"], "主题"),
    ]


def test_smtp_failure_does_not_break_purchase_warranty_or_overview(components, monkeypatch):
    app, _, canboso, _, mailer = components
    canboso.balance = 70.0

    def broken_send(*args, **kwargs):
        raise OSError("smtp blocked")

    monkeypatch.setattr(mailer, "send", broken_send)
    with client(app) as c:
        ov = c.get("/api/v1/admin/overview", headers=ADMIN)
        assert ov.status_code == 200 and ov.json()["canboso"]["is_low_balance"] is True

        code = issue(c, sku_key="ultra_exclusive_admin")
        red = c.post(f"/api/v1/public/codes/{code}/redeem", json={"preflight_confirmed": True})
        assert red.status_code == 200 and red.json()["status"] == "delivered_pending_warranty"
        assert red.json()["delivery"]["account_text"] == "store-ultra@example.com"

        war = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner@gmail.com", "admin_email_confirm": "owner@gmail.com", "note": "ok"},
            files={"screenshot": ("proof.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        )
        assert war.status_code == 200 and war.json()["status"] == "completed"

        test_mail = c.post("/api/v1/admin/notifications/test-email", headers=ADMIN)
        assert test_mail.status_code == 503 and test_mail.json()["error_code"] == "SMTP_ERROR"


def test_canboso_client_rejects_http_200_success_false(monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"success": False, "message": "Thieu key API"}

    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: Response())
    with pytest.raises(UpstreamError):
        CanbosoClient("https://canboso.com", "bad").get_balance()


def test_single_email_redeem_and_optional_confirm_backward_compat(components):
    app, _, _, _, _ = components
    with client(app) as c:
        code_omit = issue(c)
        resp_omit = c.post(
            f"/api/v1/public/codes/{code_omit}/redeem",
            json={"buyer_email": "SingleUser@gmail.com", "preflight_confirmed": True},
        )
        assert resp_omit.status_code == 200
        body_omit = resp_omit.json()
        assert body_omit["status"] == "waiting_fulfillment"
        assert body_omit["buyer_email_masked"] == "si***@gmail.com"
        assert "buyer_email" not in body_omit
        assert "upstream_order_code" not in body_omit
        assert "singleuser@gmail.com" not in resp_omit.text.lower()

        code_blank = issue(c)
        resp_blank = c.post(
            f"/api/v1/public/codes/{code_blank}/redeem",
            json={"buyer_email": "blank.confirm@gmail.com", "buyer_email_confirm": "   ", "preflight_confirmed": True},
        )
        assert resp_blank.status_code == 200
        assert resp_blank.json()["status"] == "waiting_fulfillment"

        code_mismatch = issue(c)
        resp_mismatch = c.post(
            f"/api/v1/public/codes/{code_mismatch}/redeem",
            json={"buyer_email": "one@gmail.com", "buyer_email_confirm": "two@gmail.com", "preflight_confirmed": True},
        )
        assert resp_mismatch.status_code == 422
        assert resp_mismatch.json()["error_code"] == "EMAIL_MISMATCH"


def test_single_email_warranty_optional_confirm_and_privacy(components):
    app, _, _, _, _ = components
    with client(app) as c:
        code = issue(c, sku="ultra_exclusive_admin")
        delivered = c.post(f"/api/v1/public/codes/{code}/redeem", json={"preflight_confirmed": True})
        assert delivered.status_code == 200
        upstream_order = app.state.ledger.get_code(code)["upstream_order_code"]

        mismatch = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner@gmail.com", "admin_email_confirm": "wrong@gmail.com"},
            files={"screenshot": ("invite.png", b"png-bytes", "image/png")},
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["error_code"] == "EMAIL_MISMATCH"

        submitted = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "OwnerOne@gmail.com", "note": "已邀请"},
            files={"screenshot": ("invite.png", b"png-bytes", "image/png")},
        )
        assert submitted.status_code == 200
        pub = submitted.json()
        assert pub["status"] == "completed"
        assert pub["buyer_email_masked"] == "ow***@gmail.com"
        assert pub["warranty"]["admin_email_masked"] == "ow***@gmail.com"
        assert "buyer_email" not in pub
        assert "admin_email" not in pub["warranty"]
        assert "forward_template" not in pub["warranty"]
        assert "upstream_order_code" not in pub
        assert "ownerone@gmail.com" not in submitted.text.lower()
        assert upstream_order not in submitted.text

        row = app.state.ledger.get_code(code)
        assert row["buyer_email"] == "ownerone@gmail.com"
        assert row["buyer_email_masked"] == "ow***@gmail.com"

        updated = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner.two@gmail.com", "admin_email_confirm": "", "note": "更新邮箱"},
            files={"screenshot": ("invite2.png", b"png-bytes-2", "image/png")},
        )
        assert updated.status_code == 200
        row_after = app.state.ledger.get_code(code)
        assert row_after["buyer_email"] == "owner.two@gmail.com"
        assert row_after["buyer_email_masked"] == "ow***@gmail.com"

        fetched_pub = c.get(f"/api/v1/public/codes/{code}")
        assert fetched_pub.status_code == 200
        assert "buyer_email" not in fetched_pub.json()
        assert "owner.two@gmail.com" not in fetched_pub.text.lower()
        assert "forward_template" not in fetched_pub.text
        assert upstream_order not in fetched_pub.text


def test_vietnamese_forward_template_and_legacy_exclusive_compat_and_search(components):
    app, _, _, _, mailer = components
    with client(app) as c:
        new_code = issue(c, sku="ultra_exclusive_admin")
        c.post(f"/api/v1/public/codes/{new_code}/redeem", json={"preflight_confirmed": True})
        new_order = app.state.ledger.get_code(new_code)["upstream_order_code"]
        war = c.post(
            f"/api/v1/public/codes/{new_code}/warranty",
            data={"admin_email": "new.owner@gmail.com", "note": "已建组"},
            files={"screenshot": ("invite.png", b"png-bytes", "image/png")},
        )
        assert war.status_code == 200
        expected_new = format_warranty_forward_template(new_order, "new.owner@gmail.com")
        assert expected_new == (
            "Chào shop ạ, em gửi thông tin đăng ký bảo hành gói Family:\n"
            f"- Mã đơn: {new_order}\n"
            "- Gmail chủ nhóm: new.owner@gmail.com\n"
            "Em đã mời tài khoản shop vào nhóm và gửi kèm ảnh xác nhận, nhờ shop ghi nhận bảo hành giúp em ạ. Cảm ơn shop!"
        )
        new_detail = c.get(f"/api/v1/admin/codes/{new_code}", headers=ADMIN).json()
        assert new_detail["warranty"]["forward_template"] == expected_new
        assert new_detail["buyer_email"] == "new.owner@gmail.com"
        assert new_detail["buyer_email_masked"] == "ne***@gmail.com"
        assert new_detail["store_email"] == "store-ultra@example.com"
        assert any(mail["body"] == expected_new for mail in mailer.sent)

        legacy_code = issue(c, sku="ultra_exclusive_admin")
        legacy_warranty = {
            "status": "submitted",
            "admin_email": "legacy.owner@gmail.com",
            "submitted_at": now_iso(),
            "registered_at": None,
            "note": "历史单",
            "has_screenshot": False,
            "forward_template": "Canboso 管理员报备\n订单号：ORD-LEGACY-92HH\n主 Gmail：legacy.owner@gmail.com",
        }
        legacy_delivery = {
            "account_text": "store-legacy@gmail.com",
            "credentials": [{"label": "商店套餐邮箱", "value": "store-legacy@gmail.com"}],
            "raw_lines": ["store-legacy@gmail.com"],
            "delivered_at": now_iso(),
        }
        with app.state.ledger.transaction(immediate=True) as conn:
            conn.execute(
                """UPDATE codes
                   SET status='completed', buyer_email=NULL, buyer_email_masked=NULL,
                       upstream_order_code=?, delivery_json=?, warranty_json=?
                   WHERE code=?""",
                (
                    "ORD-LEGACY-92HH",
                    json.dumps(legacy_delivery, ensure_ascii=False),
                    json.dumps(legacy_warranty, ensure_ascii=False),
                    legacy_code,
                ),
            )

        legacy_detail = c.get(f"/api/v1/admin/codes/{legacy_code}", headers=ADMIN).json()
        expected_legacy = format_warranty_forward_template("ORD-LEGACY-92HH", "legacy.owner@gmail.com")
        assert legacy_detail["warranty"]["forward_template"] == expected_legacy
        assert "Canboso 管理员报备" not in legacy_detail["warranty"]["forward_template"]
        assert legacy_detail["buyer_email"] == "legacy.owner@gmail.com"
        assert legacy_detail["buyer_email_masked"] == "le***@gmail.com"
        assert legacy_detail["store_email"] == "store-legacy@gmail.com"

        legacy_pub = c.get(f"/api/v1/public/codes/{legacy_code}")
        assert legacy_pub.status_code == 200
        assert "buyer_email" not in legacy_pub.json()
        assert "legacy.owner@gmail.com" not in legacy_pub.text.lower()
        assert "forward_template" not in legacy_pub.text
        assert "ORD-LEGACY-92HH" not in legacy_pub.text

        slot_code = issue(c, sku="ultra_shared_slot")
        c.post(
            f"/api/v1/public/codes/{slot_code}/redeem",
            json={"buyer_email": "slot.buyer@gmail.com", "preflight_confirmed": True},
        )

        listed = c.get("/api/v1/admin/codes", headers=ADMIN).json()
        by_code = {item["code"]: item for item in listed["items"]}
        assert by_code[new_code]["buyer_email"] == "new.owner@gmail.com"
        assert by_code[new_code]["buyer_email_masked"] == "ne***@gmail.com"
        assert by_code[new_code]["store_email"] == "store-ultra@example.com"
        assert by_code[legacy_code]["buyer_email"] == "legacy.owner@gmail.com"
        assert by_code[legacy_code]["buyer_email_masked"] == "le***@gmail.com"
        assert by_code[legacy_code]["store_email"] == "store-legacy@gmail.com"
        assert by_code[slot_code]["buyer_email"] == "slot.buyer@gmail.com"
        assert by_code[slot_code]["buyer_email_masked"] == "sl***@gmail.com"
        assert by_code[slot_code]["store_email"] is None

        search_new = c.get("/api/v1/admin/codes", headers=ADMIN, params={"q": "new.owner@gmail.com"}).json()
        assert [item["code"] for item in search_new["items"]] == [new_code]

        search_legacy = c.get("/api/v1/admin/codes", headers=ADMIN, params={"q": "legacy.owner@gmail.com"}).json()
        assert [item["code"] for item in search_legacy["items"]] == [legacy_code]

        search_store_email = c.get("/api/v1/admin/codes", headers=ADMIN, params={"q": "store-legacy@gmail.com"}).json()
        assert [item["code"] for item in search_store_email["items"]] == [legacy_code]

        search_new_store_email = c.get("/api/v1/admin/codes", headers=ADMIN, params={"q": "store-ultra@example.com"}).json()
        assert [item["code"] for item in search_new_store_email["items"]] == [new_code]


def test_issue_wangwang_message_contains_link_and_code_for_both_skus(components):
    app, _, _, _, _ = components
    with client(app) as c:
        slot_resp = c.post("/api/v1/admin/codes/issue", headers=ADMIN, json={"sku_key": "ultra_shared_slot", "channel": "taobao"}).json()
        assert slot_resp["code"] in slot_resp["wangwang_message"]
        assert slot_resp["redeem_url"] in slot_resp["wangwang_message"]
        assert slot_resp["item"]["sku_title"] == "Google Ultra x20 共享车位"
        assert slot_resp["wangwang_message"] == (
            "感谢下单 Google Ultra x20 共享车位！\n"
            f"点链接填您的 Gmail 提货：{slot_resp['redeem_url']}\n"
            f"兑换码：{slot_resp['code']}\n"
            "提交后一般 1~6 小时内发家庭组邀请（最晚 24 小时），随时能在链接里看进度。"
        )
        assert "Gmail" in slot_resp["wangwang_message"]

        excl_resp = c.post("/api/v1/admin/codes/issue", headers=ADMIN, json={"sku_key": "ultra_exclusive_admin", "channel": "taobao"}).json()
        assert excl_resp["code"] in excl_resp["wangwang_message"]
        assert excl_resp["redeem_url"] in excl_resp["wangwang_message"]
        assert "保修" in excl_resp["wangwang_message"]


def test_canboso_products_cache_and_get_transient_retries(monkeypatch):
    calls = []
    sleeps = []

    class Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.headers = {}
            self._payload = payload or {"products": [{"productId": "sku-1"}]}

        def json(self):
            return self._payload

    events = [
        httpx.ReadTimeout("timed out"),
        Response(503, {"message": "temporary upstream overload"}),
        Response(200, {"products": [{"productId": "sku-1"}]}),
        Response(200, {"products": [{"productId": "sku-2"}]}),
    ]

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        item = events.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("httpx.request", fake_request)
    monkeypatch.setattr("time.sleep", lambda value: sleeps.append(value))
    client_ = CanbosoClient("https://canboso.com", "buyer-secret", timeout=25, products_cache_ttl_seconds=60, max_get_retries=2)

    first = client_.list_products()
    second = client_.list_products()
    assert first == {"products": [{"productId": "sku-1"}]}
    assert second == first
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]

    client_.invalidate_products_cache()
    refreshed = client_.list_products()
    assert refreshed == {"products": [{"productId": "sku-2"}]}
    assert len(calls) == 4


def test_canboso_purchase_does_not_retry_timeout_and_preflight_records_detail(tmp_path, monkeypatch):
    post_calls = []

    def fake_request(method, url, **kwargs):
        post_calls.append(method)
        raise httpx.ReadTimeout("purchase timed out with key=demo-secret-token")

    monkeypatch.setattr("httpx.request", fake_request)
    client_ = CanbosoClient("https://canboso.com", "demo-secret-token", timeout=25, max_get_retries=2)
    with pytest.raises(UpstreamTimeout):
        client_.purchase("plan2-idemp", {"product_id": "sku-1", "quantity": 1})
    assert post_calls == ["POST"]

    class FailingPreflightCanboso(DryRunCanboso):
        def list_products(self, force_refresh: bool = False):
            raise UpstreamTimeout("GET /api/v2/telegram-buyer/products timed out after 25s")

    settings = Settings(db_path=str(tmp_path / "db.sqlite3"), storage_dir=str(tmp_path / "private"), admin_token="test-admin", execution_mode="dry_run")
    app = create_app(settings, canboso=FailingPreflightCanboso(), channel=DryRun16688(), mailer=Mailer(settings))
    with client(app) as c:
        code = issue(c)
        resp = c.post(f"/api/v1/public/codes/{code}/redeem", json={"buyer_email": "buyer@gmail.com", "preflight_confirmed": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "needs_manual_review"
        detail = c.get(f"/api/v1/admin/codes/{code}", headers=ADMIN).json()
        assert detail["last_error_code"] == "UPSTREAM_ERROR"
        assert "上游预检失败" in detail["last_error_message"]
        assert "timed out after 25s" in detail["last_error_message"]

