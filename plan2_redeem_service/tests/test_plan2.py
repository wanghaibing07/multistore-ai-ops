from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db import now_iso
from backend.gateways import DryRunCanboso
from backend.gateways import CanbosoClient, Channel16688Client
from backend.service import Mailer


ADMIN = {"Authorization": "Bearer test-admin"}


def client(app):
    return TestClient(app)


def issue(c, sku="ultra_shared_slot", channel="taobao", **extra):
    response = c.post("/api/v1/admin/codes/issue", headers=ADMIN, json={"sku_key": sku, "channel": channel, **extra})
    assert response.status_code == 201, response.text
    return response.json()["code"]


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

