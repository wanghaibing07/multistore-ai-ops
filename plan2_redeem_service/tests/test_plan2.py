from __future__ import annotations

import concurrent.futures
from contextlib import closing
import csv
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.agiso import format_trade_delivery_message
from backend.config import Settings
from backend.db import now_iso
from backend.gateways import DryRun16688, DryRunCanboso, UpstreamError, UpstreamTimeout
from backend.gateways import CanbosoClient, Channel16688Client
from backend.service import Mailer, SKUS, Service, format_warranty_forward_template


ADMIN = {"Authorization": "Bearer test-admin"}


def client(app):
    return TestClient(app)


def issue(c, sku="ultra_shared_slot", channel="taobao", **extra):
    response = c.post("/api/v1/admin/codes/issue", headers=ADMIN, json={"sku_key": sku, "channel": channel, **extra})
    assert response.status_code == 201, response.text
    return response.json()["code"]


def issue_16688_batch(c, count=1):
    response = c.post("/api/v1/admin/codes/batch", headers=ADMIN, json={
        "sku_key": "ultra_shared_slot", "channel": "16688", "goods_no": "G100", "count": count,
    })
    assert response.status_code == 201, response.text
    return response.json()["codes"]


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
        assert mailer.sent == []


def test_canboso_cancelled_purchase_enters_manual_review_and_stays_in_gross(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        code = issue(c)
        assert c.post(f"/api/v1/public/codes/{code}/redeem", json={
            "buyer_email": "buyer@example.com", "preflight_confirmed": True,
        }).json()["status"] == "waiting_fulfillment"
        ledger = app.state.ledger
        row = ledger.get_code(code)
        canboso.orders[row["upstream_order_code"]]["status"] = "cancelled"
        cancelled = c.post(f"/api/v1/admin/codes/{code}/poll", headers=ADMIN).json()
        assert cancelled["status"] == "needs_manual_review"
        assert cancelled["last_error_code"] == "CANBOSO_ORDER_CANCELLED"
        assert cancelled["upstream_order_code"] == row["upstream_order_code"]
        retry = c.post(f"/api/v1/admin/codes/{code}/retry", headers=ADMIN, json={})
        assert retry.status_code == 409 and retry.json()["error_code"] == "RETRY_NOT_AVAILABLE"
        assert len(canboso.orders) == 1
        today = c.get("/api/v1/admin/overview", headers=ADMIN).json()["canboso"]["today_purchases"]
        assert today == {"order_count": 1, "gross_usd": row["cost_usd"], "cancelled_count": 1, "cancelled_gross_usd": row["cost_usd"]}


def test_admin_retry_preserves_cancelled_purchase_during_concurrent_revoke(components, monkeypatch):
    app, _, canboso, _, _ = components
    started, release = Event(), Event()
    def purchase(key, payload):
        started.set()
        assert release.wait(8)
        return {"order": {"orderCode": "test-cancelled", "status": "CANCELLED"}, "payment": {"currency": "USD", "amount": 62.25}}

    with client(app) as c:
        code = issue(c, sku="ultra_exclusive_admin")
        canboso.balance = 0
        assert c.post(f"/api/v1/public/codes/{code}/redeem", json={"preflight_confirmed": True}).json()["status"] == "needs_manual_review"
        canboso.balance = 1000
        monkeypatch.setattr(canboso, "purchase", purchase)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            retry = pool.submit(c.post, f"/api/v1/admin/codes/{code}/retry", headers=ADMIN, json={})
            try:
                assert started.wait(8)
                revoke = c.post(f"/api/v1/admin/codes/{code}/revoke", headers=ADMIN, json={})
                assert revoke.status_code == 409 and revoke.json()["error_code"] == "CONCURRENT_IN_PROGRESS"
            finally:
                release.set()
            result = retry.result()
        assert result.status_code == 409 and result.json()["error_code"] == "CANBOSO_ORDER_CANCELLED"
        row = app.state.ledger.get_code(code)
        assert row["status"] == "needs_manual_review" and row["upstream_order_code"] == "test-cancelled"
        assert row["cost_usd"] == 62.25 and row["delivery_json"] is None
        assert c.get("/api/v1/admin/overview", headers=ADMIN).json()["canboso"]["today_purchases"]["cancelled_gross_usd"] == 62.25


def test_any_valid_email_domain_is_accepted_for_redeem_and_warranty(components):
    app, _, _, _, _ = components
    with client(app) as c:
        valid_emails = [
            ("buyer@example.com", "buyer@example.com"),
            ("123456789@qq.com", "123456789@qq.com"),
            ("User.Name+tag@Outlook.com", "user.name+tag@outlook.com"),
            ("admin@sub.company.co.uk", "admin@sub.company.co.uk"),
            ("buyer@googlemail.com", "buyer@googlemail.com"),
        ]
        for raw_email, expected_email in valid_emails:
            code = issue(c)
            response = c.post(
                f"/api/v1/public/codes/{code}/redeem",
                json={"buyer_email": raw_email, "buyer_email_confirm": raw_email, "preflight_confirmed": True},
            )
            assert response.status_code == 200, raw_email
            body = response.json()
            assert body["buyer_email_masked"] == expected_email
            assert "buyer_email" not in body
            assert app.state.ledger.get_code(code)["buyer_email"] == raw_email.lower()

        exclusive_code = issue(c, sku="ultra_exclusive_admin")
        delivered = c.post(f"/api/v1/public/codes/{exclusive_code}/redeem", json={"preflight_confirmed": True})
        assert delivered.status_code == 200
        warranty_resp = c.post(
            f"/api/v1/public/codes/{exclusive_code}/warranty",
            data={"admin_email": "Owner@163.com", "admin_email_confirm": "owner@163.com", "note": "non-gmail"},
            files={"screenshot": ("proof.png", b"png-bytes", "image/png")},
        )
        assert warranty_resp.status_code == 200
        assert warranty_resp.json()["warranty"]["admin_email_masked"] == "owner@163.com"
        assert app.state.ledger.get_code(exclusive_code)["buyer_email"] == "owner@163.com"


def test_invalid_email_syntax_is_rejected_for_redeem_and_warranty(components):
    app, _, _, _, _ = components
    invalid_emails = [
        "   ",
        "not-an-email",
        "buyer@localhost",
        "a..b@qq.com",
        ".a@qq.com",
        "a.@qq.com",
        "buyer@-bad.com",
        "buyer@bad-.com",
        "buyer@bad.1",
        "buyer @qq.com",
        "buyer@xn--bad-",
    ]
    with client(app) as c:
        slot_code = issue(c)
        for bad_email in ["", *invalid_emails]:
            response = c.post(
                f"/api/v1/public/codes/{slot_code}/redeem",
                json={"buyer_email": bad_email, "buyer_email_confirm": bad_email, "preflight_confirmed": True},
            )
            assert response.status_code == 422, bad_email
            assert response.json()["error_code"] == "INVALID_EMAIL"

        exclusive_code = issue(c, sku="ultra_exclusive_admin")
        assert c.post(f"/api/v1/public/codes/{exclusive_code}/redeem", json={"preflight_confirmed": True}).status_code == 200
        for bad_email in invalid_emails:
            response = c.post(
                f"/api/v1/public/codes/{exclusive_code}/warranty",
                data={"admin_email": bad_email, "note": "invalid"},
                files={"screenshot": ("proof.png", b"png-bytes", "image/png")},
            )
            assert response.status_code == 422, bad_email
            assert response.json()["error_code"] == "INVALID_EMAIL"


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
    app, _, canboso, channel, _ = components
    channel.allowed = False
    c = client(app)
    code, = issue_16688_batch(c)
    response = c.post(f"/api/v1/public/codes/{code}/redeem", json={
        "buyer_email": "a@gmail.com", "preflight_confirmed": True, "channel_order_no": "TB-001",
    })
    assert response.status_code == 409 and response.json()["error_code"] == "ORDER_NOT_ELIGIBLE"
    assert app.state.ledger.get_code(code)["status"] == "unused"
    assert app.state.ledger.audit(code)[-1]["reason"] == "16688_platform_sale_rejected"
    assert canboso.charge_count == 0


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
    assert mailer.sent == []
    warranty = c.post(f"/api/v1/public/codes/{code}/warranty", data={"admin_email": "owner@gmail.com", "admin_email_confirm": "owner@gmail.com", "note": "已建组"}, files={"screenshot": ("invite.png", b"png-bytes", "image/png")})
    assert warranty.status_code == 200 and warranty.json()["status"] == "completed"
    assert set(warranty.json()["warranty"]) == {"status", "admin_email_masked", "submitted_at", "has_screenshot", "note"}
    assert warranty.json()["warranty"]["admin_email_masked"] == "owner@gmail.com"
    assert warranty.json()["buyer_email_masked"] == "owner@gmail.com"
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


def test_16688_admin_retry_reports_price_guard_after_verified_redemption(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        code, = issue_16688_batch(c)
        canboso.products["6ac26aaeec73a42d6430a837"]["price"]["amount"] = 99
        first = c.post(f"/api/v1/public/codes/{code}/redeem", json={
            "buyer_email": "a@gmail.com", "preflight_confirmed": True, "channel_order_no": "TB-001",
        })
        assert first.status_code == 200 and first.json()["status"] == "needs_manual_review"
        row = app.state.ledger.get_code(code)
        assert row["idempotency_key"] and row["request_payload_json"]
        retry = c.post(f"/api/v1/admin/codes/{code}/retry", headers=ADMIN, json={
            "note": "再次检查", "merchant_verified_recipient_email": "a@gmail.com",
        })
        assert retry.status_code == 503 and retry.json()["error_code"] == "PRICE_LIMIT_EXCEEDED"
        assert "admin_retry_failed:PRICE_LIMIT_EXCEEDED" in [event["reason"] for event in app.state.ledger.audit(code)]


def raise_16688_paused(*_):
    raise UpstreamError(502, "系统长时间超负载，已影响平台稳定性，暂停OpenAPI", rejected=True)


def test_16688_openapi_pause_message_matching_and_fallback(components, monkeypatch):
    app, _, canboso, channel, _ = components
    for msg in (
        "系统长时间超负载，已影响平台稳定性，暂停OpenAPI",
        "系统长时间因爬虫、AI自动化操作OpenApi超负载，已影响平台稳定性，短时间OpenAPI不再恢复",
        "短时间OpenAPI不再恢复",
        "OpenAPI 维护中，请稍后重试",
    ):
        exc = UpstreamError(502, msg, rejected=True)
        assert Service._channel16688_paused(exc) is True
        assert Service._channel16688_unavailable(exc) is True

    for msg in (
        "未暂停 OpenAPI",
        "没有暂停OpenAPI",
        "OpenAPI 已恢复",
        "仅卡密商品支持查询库存",
        "订单不存在",
    ):
        exc = UpstreamError(502, msg, rejected=True)
        assert Service._channel16688_paused(exc) is False
        assert Service._channel16688_unavailable(exc) is False

    def raise_new_16688_paused(*_):
        raise UpstreamError(
            502,
            "系统长时间因爬虫、AI自动化操作OpenApi超负载，已影响平台稳定性，短时间OpenAPI不再恢复",
            rejected=True,
        )

    with client(app) as c:
        code, = issue_16688_batch(c)
        monkeypatch.setattr(channel, "verify_order", raise_new_16688_paused)
        resp = c.post(
            f"/api/v1/public/codes/{code}/redeem",
            json={"buyer_email": "buyer@example.com", "preflight_confirmed": True},
        )
        assert resp.status_code == 200 and resp.json()["status"] == "waiting_fulfillment"
        assert canboso.charge_count == 1


def test_16688_pause_blocks_uncertain_inventory_and_previously_consumed_codes(components, monkeypatch):
    app, _, canboso, channel, _ = components
    monkeypatch.setattr(channel, "verify_order", raise_16688_paused)
    with client(app) as c:
        uncertain, spent = issue_16688_batch(c, count=2)
        ledger = app.state.ledger
        ledger.transition(uncertain, "unused", "issued_16688_import_uncertain")
        ledger.transition(spent, "waiting_fulfillment", "shared_order_accepted")
        ledger.transition(spent, "unused", "legacy_reset")
        for code, expected in ((uncertain, "CHANNEL16688_STOCK_IMPORT_PENDING"), (spent, "CODE_REVOKED")):
            result = c.post(f"/api/v1/public/codes/{code}/redeem", json={
                "buyer_email": "other@example.com", "preflight_confirmed": True,
            })
            assert result.json()["error_code"] == expected
            assert c.get(f"/api/v1/public/codes/{code}").json()["status"] == "unused"
            row = ledger.get_code(code)
            assert not row["buyer_email"] and not row["idempotency_key"]
        assert canboso.charge_count == 0


def test_16688_paused_public_redeem_purchases_once_per_code(components, monkeypatch):
    app, _, canboso, channel, mailer = components
    with client(app) as c:
        codes = issue_16688_batch(c, count=2)
        monkeypatch.setattr(channel, "verify_order", raise_16688_paused)
        ledger = app.state.ledger
        ledger.transition(codes[0], "unused", "16688_order_not_eligible")
        ledger.bind_code_external_order(codes[1], "T-SYNCED", reason="channel16688_sold_sync")
        for code, expected_trade in zip(codes, ("", "T-SYNCED")):
            payload = {"buyer_email": "buyer@example.com", "preflight_confirmed": True}
            endpoint = f"/api/v1/public/codes/{code}/redeem"
            first = c.post(endpoint, json=payload)
            assert first.status_code == 200 and first.json()["status"] == "waiting_fulfillment"
            assert c.post(endpoint, json=payload).json() == first.json()
            row = ledger.get_code(code)
            assert row["upstream_order_code"] in canboso.orders
            assert row["idempotency_key"] in canboso.idempotent
            assert row["external_order_id"] == expected_trade
            assert first.json()["buyer_email_masked"] == "b***@example.com"
            assert "16688_openapi_unavailable_code_purchase" in [event["reason"] for event in ledger.audit(code)]
        assert canboso.charge_count == 2
        assert len(mailer.sent) == 1


def test_16688_live_rejection_overrides_cached_sale_during_pause(components, monkeypatch):
    app, _, canboso, channel, _ = components
    with client(app) as c:
        code, = issue_16688_batch(c)
        ledger = app.state.ledger
        ledger.bind_code_external_order(code, "TB-001", reason="channel16688_sold_sync")
        endpoint = f"/api/v1/public/codes/{code}/redeem"
        payload = {"buyer_email": "buyer@example.com", "preflight_confirmed": True}
        channel.allowed = False
        assert c.post(endpoint, json=payload).json()["error_code"] == "ORDER_NOT_ELIGIBLE"
        monkeypatch.setattr(channel, "verify_order", raise_16688_paused)
        assert c.post(endpoint, json=payload).json()["error_code"] == "ORDER_NOT_ELIGIBLE"
        assert ledger.get_code(code)["status"] == "unused"
        assert canboso.charge_count == 0


def test_16688_concurrent_code_only_redemption_after_timeout_buys_once(components, monkeypatch):
    app, _, canboso, channel, _ = components
    with client(app) as c:
        code, = issue_16688_batch(c)
    service = app.state.service
    checked = Barrier(2)

    def timed_out(*_):
        checked.wait(timeout=5)
        raise UpstreamTimeout("16688 request timed out")

    monkeypatch.setattr(channel, "verify_order", timed_out)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        requests = [
            pool.submit(service.redeem, code, f"buyer{i}@example.com", None, True)
            for i in range(2)
        ]
        results = [request.result(timeout=5) for request in requests]

    assert sum(error is None for _, error in results) == 1
    assert any(error == "CONCURRENT_IN_PROGRESS" for _, error in results)
    assert canboso.charge_count == 1
    assert len([event for event in app.state.ledger.audit(code) if event["reason"] == "redeem_claimed"]) == 1


def test_16688_newer_refund_or_complaint_rejection_wins_over_in_flight_verification(components, monkeypatch):
    app, _, canboso, channel, _ = components
    with client(app) as c:
        code, = issue_16688_batch(c)
        ledger = app.state.ledger
        live_verify = channel.verify_order

        def verified_before_new_rejection(*args):
            success = live_verify(*args)
            assert ledger.transition(code, "unused", "16688_platform_sale_rejected", allowed=("unused",))
            return success

        monkeypatch.setattr(channel, "verify_order", verified_before_new_rejection)
        endpoint = f"/api/v1/public/codes/{code}/redeem"
        payload = {"buyer_email": "new@example.com", "preflight_confirmed": True}
        assert c.post(endpoint, json=payload).json()["error_code"] == "ORDER_NOT_ELIGIBLE"
        assert canboso.charge_count == 0
        assert ledger.get_code(code)["buyer_email"] is None
        monkeypatch.setattr(channel, "verify_order", live_verify)
        assert c.post(endpoint, json=payload).json()["status"] == "waiting_fulfillment"
        assert canboso.charge_count == 1


def test_16688_confirmed_replacement_is_not_checked_as_platform_stock(components, monkeypatch):
    app, _, canboso, channel, _ = components
    service = app.state.service
    row = service.issue("ultra_shared_slot", "16688", "TEST-REPLACEMENT", goods_no="G10000001")
    code = row["code"]
    service.ledger.transition(code, "unused", "16688_confirmed_sale_replacement_issued")
    monkeypatch.setattr(channel, "verify_order", lambda *_: pytest.fail("replacement is not a platform stock card"))
    with client(app) as c:
        response = c.post(f"/api/v1/public/codes/{code}/redeem", json={
            "buyer_email": "new@example.com", "preflight_confirmed": True,
        })
    assert response.status_code == 200 and response.json()["status"] == "waiting_fulfillment"
    assert service.ledger.get_code(code)["external_order_id"] == "TEST-REPLACEMENT"
    assert canboso.charge_count == 1


def test_live_admin_stock_list_shows_full_16688_codes_and_opens_detail(components):
    app, settings, _, _, _ = components
    with client(app) as c:
        code, = issue_16688_batch(c)
        settings.execution_mode = "live"
        inventory = c.get("/api/v1/admin/codes?status=in_stock", headers=ADMIN).json()
        assert inventory["items"][0]["code"] == code
        assert "stock_code_hidden" not in inventory["items"][0]
        detail = c.get(f"/api/v1/admin/codes/{code}", headers=ADMIN)
        assert detail.status_code == 200
        assert detail.json()["code"] == code
        app.state.ledger.bind_code_external_order(code, "TEST-SOLD", reason="channel16688_sold_sync")
        dispatched = c.get("/api/v1/admin/codes?status=dispatched_unused", headers=ADMIN).json()
        assert dispatched["items"][0]["code"] == code


def test_16688_recipient_dispute_hides_invitation_without_reopening_purchase(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        code, = issue_16688_batch(c)
        ledger = app.state.ledger
        endpoint = f"/api/v1/public/codes/{code}"
        payload = {"buyer_email": "buyer@example.com", "preflight_confirmed": True, "channel_order_no": "TB-001"}
        assert c.post(endpoint + "/redeem", json=payload).status_code == 200
        stored = ledger.get_code(code)
        assert ledger.transition(code, "completed", "shared_fulfillment_completed", allowed=("waiting_fulfillment",))
        assert ledger.transition(code, "completed", "16688_recipient_disputed", {
            "last_error_code": "CHANNEL16688_RECIPIENT_DISPUTED",
        }, ("completed",))
        view = c.get(endpoint).json()
        assert view["status"] == "needs_manual_review"
        assert view["buyer_email_masked"] is None
        assert "新兑换码" in view["buyer_notice"]
        assert c.post(endpoint + "/redeem", json=payload).json() == view
        admin = c.get(f"/api/v1/admin/codes/{code}", headers=ADMIN).json()
        assert admin["status"] == "completed" and admin["buyer_email"] == payload["buyer_email"]
        assert admin["upstream_order_code"] == stored["upstream_order_code"] and canboso.charge_count == 1


@pytest.mark.parametrize("guard", ["PRICE_LIMIT_EXCEEDED", "LOW_BALANCE", "UPSTREAM_OUT_OF_STOCK"])
def test_16688_paused_redeem_keeps_canboso_preflight_guards(components, monkeypatch, guard):
    app, _, canboso, channel, _ = components
    monkeypatch.setattr(channel, "verify_order", raise_16688_paused)
    with client(app) as c:
        code, = issue_16688_batch(c)
        ledger = app.state.ledger
        ledger.bind_code_external_order(code, "T-TEST", reason="channel16688_sold_sync")
        product = canboso.products[ledger.get_sku("ultra_shared_slot")["product_id"]]
        if guard == "PRICE_LIMIT_EXCEEDED":
            product["price"]["amount"] = 999
        elif guard == "LOW_BALANCE":
            canboso.balance = 0
        else:
            product["availability"]["available"] = 0
        response = c.post(f"/api/v1/public/codes/{code}/redeem", json={
            "buyer_email": "buyer@example.com", "preflight_confirmed": True, "channel_order_no": "T-TEST",
        })
        assert response.status_code == 200 and response.json()["status"] == "needs_manual_review"
        assert app.state.ledger.get_code(code)["last_error_code"] == guard
        assert canboso.charge_count == 0


def test_16688_paused_admin_retry_can_bind_merchant_verified_order_and_reuse_key(components, monkeypatch):
    app, _, canboso, channel, mailer = components

    def not_paused(*_):
        raise UpstreamError(502, "未暂停 OpenAPI", rejected=True)

    with client(app) as c:
        ledger = app.state.ledger
        code, = issue_16688_batch(c)
        # Requests moved to review before the public ownership check was added.
        email = "buyer@example.com"
        product_id = ledger.get_sku("ultra_shared_slot")["product_id"]
        state, _ = ledger.claim_for_purchase(code, {"product_id": product_id, "quantity": 1, "customer_email": email}, email, email)
        assert state == "claimed"
        assert ledger.transition(code, "needs_manual_review", "16688_unavailable", allowed=("purchasing",))
        monkeypatch.setattr(channel, "verify_order", raise_16688_paused)
        endpoint = f"/api/v1/admin/codes/{code}/retry"
        recipient = {"merchant_verified_recipient_email": email}
        assert c.post(endpoint, headers=ADMIN, json={}).json()["error_code"] == "MERCHANT_RECIPIENT_CONFIRMATION_REQUIRED"
        assert c.post(endpoint, headers=ADMIN, json={"merchant_verified_recipient_email": "other@example.com"}).status_code == 422
        assert c.post(endpoint, headers=ADMIN, json=recipient).json()["error_code"] == "CHANNEL16688_OPENAPI_PAUSED"
        assert len(mailer.sent) == 1
        assert not canboso.orders
        original = ledger.get_code(code)
        assert ledger.bind_code_external_order(code, "merchant-123") == "bound"
        assert c.post(endpoint, headers=ADMIN, json={**recipient, "merchant_verified_order_no": "wrong-123"}).status_code == 409

        monkeypatch.setattr(channel, "verify_order", lambda *_: {"ok": False, "error": "ORDER_NOT_ELIGIBLE"})
        payload = {**recipient, "merchant_verified_order_no": "merchant-123"}
        assert c.post(endpoint, headers=ADMIN, json=payload).status_code == 409
        assert not canboso.orders

        monkeypatch.setattr(channel, "verify_order", not_paused)
        assert c.post(endpoint, headers=ADMIN, json=payload).json()["error_code"] == "UPSTREAM_ERROR"
        assert not canboso.orders

        monkeypatch.setattr(channel, "verify_order", raise_16688_paused)
        retry = c.post(endpoint, headers=ADMIN, json=payload)
        assert retry.status_code == 200 and retry.json()["status"] == "waiting_fulfillment"
        assert retry.json()["external_order_id"] == "merchant-123"
        assert original["idempotency_key"] in canboso.idempotent
        assert "admin_retry_16688_merchant_verified" in [event["reason"] for event in ledger.audit(code)]
        c.post(endpoint, headers=ADMIN, json=payload)
        assert len(canboso.orders) == 1


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
    assert all(callable(getattr(client_, name, None)) for name in ("verify_order", "stock_secret_list", "stock_secret_add", "invalidate"))

    calls = []
    responses = [
        {"list": [{"id": 10, "secret_code": "提货链接：https://redeem.example/r/C1\n兑换码：C1", "status": 2, "trade_no": "T1"}]},
        {"identity": "merchant", "goods_no": "G1", "cards": [{"code": "提货链接：https://redeem.example/r/C1\n兑换码：C1"}], "status": 4},
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
        ("openApi/stockSecret/list", {"goods_no": "G1", "page_no": 1, "page_size": 20, "keywords": "C1"}),
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
        assert body_omit["buyer_email_masked"] == "singleuser@gmail.com"
        assert "buyer_email" not in body_omit
        assert "upstream_order_code" not in body_omit

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
        assert pub["buyer_email_masked"] == "ownerone@gmail.com"
        assert pub["warranty"]["admin_email_masked"] == "ownerone@gmail.com"
        assert "buyer_email" not in pub
        assert "admin_email" not in pub["warranty"]
        assert "forward_template" not in pub["warranty"]
        assert "upstream_order_code" not in pub
        assert upstream_order not in submitted.text

        row = app.state.ledger.get_code(code)
        assert row["buyer_email"] == "ownerone@gmail.com"
        assert row["buyer_email_masked"] == "ownerone@gmail.com"

        updated = c.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner.two@gmail.com", "admin_email_confirm": "", "note": "更新邮箱"},
            files={"screenshot": ("invite2.png", b"png-bytes-2", "image/png")},
        )
        assert updated.status_code == 200
        row_after = app.state.ledger.get_code(code)
        assert row_after["buyer_email"] == "owner.two@gmail.com"
        assert row_after["buyer_email_masked"] == "owner.two@gmail.com"

        fetched_pub = c.get(f"/api/v1/public/codes/{code}")
        assert fetched_pub.status_code == 200
        assert "buyer_email" not in fetched_pub.json()
        assert fetched_pub.json()["buyer_email_masked"] == "owner.two@gmail.com"
        assert fetched_pub.json()["warranty"]["admin_email_masked"] == "owner.two@gmail.com"
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
            "Chào shop ạ,\n"
            "Em gửi thông tin đăng ký bảo hành gói Family:\n"
            "\n"
            f"• Mã đơn: {new_order}\n"
            "• Gmail chủ nhóm: new.owner@gmail.com\n"
            "\n"
            "Em đã gửi lời mời vào nhóm Family cho tài khoản shop.\n"
            "Nhờ shop kiểm tra và ghi nhận bảo hành giúp em nhé.\n"
            "Cảm ơn shop nhiều ạ!"
        )
        new_detail = c.get(f"/api/v1/admin/codes/{new_code}", headers=ADMIN).json()
        assert new_detail["warranty"]["forward_template"] == expected_new
        assert new_detail["buyer_email"] == "new.owner@gmail.com"
        assert new_detail["buyer_email_masked"] == "new.owner@gmail.com"
        assert new_detail["store_email"] == "store-ultra@example.com"
        assert any(mail["body"] == expected_new for mail in mailer.sent)

        legacy_code = issue(c, sku="ultra_exclusive_admin")
        legacy_warranty = {
            "status": "submitted",
            "admin_email": "legacy.owner@gmail.com",
            "admin_email_masked": "le***@gmail.com",
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
        assert legacy_detail["buyer_email_masked"] == "legacy.owner@gmail.com"
        assert legacy_detail["warranty"]["admin_email_masked"] == "legacy.owner@gmail.com"
        assert legacy_detail["store_email"] == "store-legacy@gmail.com"

        legacy_pub = c.get(f"/api/v1/public/codes/{legacy_code}")
        assert legacy_pub.status_code == 200
        assert "buyer_email" not in legacy_pub.json()
        assert legacy_pub.json()["buyer_email_masked"] == "legacy.owner@gmail.com"
        assert legacy_pub.json()["warranty"]["admin_email_masked"] == "legacy.owner@gmail.com"
        assert "forward_template" not in legacy_pub.text
        assert "ORD-LEGACY-92HH" not in legacy_pub.text

        slot_code = issue(c, sku="ultra_shared_slot")
        c.post(
            f"/api/v1/public/codes/{slot_code}/redeem",
            json={"buyer_email": "slot.buyer@gmail.com", "preflight_confirmed": True},
        )
        with app.state.ledger.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE codes SET buyer_email_masked=? WHERE code=?",
                ("sl***@gmail.com", slot_code),
            )
        slot_pub = c.get(f"/api/v1/public/codes/{slot_code}")
        assert slot_pub.status_code == 200
        assert slot_pub.json()["buyer_email_masked"] == "slot.buyer@gmail.com"

        listed = c.get("/api/v1/admin/codes", headers=ADMIN).json()
        by_code = {item["code"]: item for item in listed["items"]}
        assert by_code[new_code]["buyer_email"] == "new.owner@gmail.com"
        assert by_code[new_code]["buyer_email_masked"] == "new.owner@gmail.com"
        assert by_code[new_code]["store_email"] == "store-ultra@example.com"
        assert by_code[legacy_code]["buyer_email"] == "legacy.owner@gmail.com"
        assert by_code[legacy_code]["buyer_email_masked"] == "legacy.owner@gmail.com"
        assert by_code[legacy_code]["store_email"] == "store-legacy@gmail.com"
        assert by_code[slot_code]["buyer_email"] == "slot.buyer@gmail.com"
        assert by_code[slot_code]["buyer_email_masked"] == "slot.buyer@gmail.com"
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
            f"点链接自助填 Gmail 提货（不用在旺旺发邮箱）：{slot_resp['redeem_url']}\n"
            f"兑换码：{slot_resp['code']}\n"
            "\n"
            "【先看这里】\n"
            "淘宝显示已发货，就是兑换码和提货链接已经发给你了，直接点上面的链接去填 Gmail 就行。\n"
            "\n"
            "【填邮箱前先查这 3 件事（都要满足）】\n"
            "1. 先关掉 Google 付款资料：https://payments.google.com/gp/w/home/settings\n"
            "2. 先退出原来的 Google 家庭组：https://g.co/yourfamily\n"
            "3. 这个 Gmail 最近 12 个月里没加过、没退过、也没被重拉过任何 Google 家庭组（Google 规定每 12 个月只能换 1 次家庭组，哪怕只进出过 1 次也会被限制加不进新组）。\n"
            "\n"
            "【提交后等多久】\n"
            "提交 Gmail 后就会马上给你占好名额并开始排队，一般 3-7 小时内发家庭组邀请，最晚 24 小时，没超过 24 小时不用催；随时点提货链接都能看进度，收到 Google 邮件点“接受邀请”就开通了。\n"
            "\n"
            "【质保和退换说明】\n"
            "1. 质保 30 天会员，不保你的 Google 账号本身；自己违规使用或反代导致封号没法保修。\n"
            "2. 本款是 0 积分/点数车位，请不要使用积分或点数，用了会被系统自动踢出家庭组且不退款。\n"
            "3. 提交邮箱后就占用了名额，如果不是商品本身问题（比如 12 个月内换过组加不进、没退旧家庭组、没关付款资料、填错邮箱等），不支持全额退款；可以补 49.9 元手续费换新 Gmail 重新拉人，或扣除 49.9 元名额成本后退还余款。"
        )
        assert "Gmail" in slot_resp["wangwang_message"]
        slot_public = c.get(f"/api/v1/public/codes/{slot_resp['code']}").json()
        assert slot_public["fulfillment"]["eta_text"] == "一般 3-7 小时内发家庭组邀请，最晚 24 小时"

        excl_resp = c.post("/api/v1/admin/codes/issue", headers=ADMIN, json={"sku_key": "ultra_exclusive_admin", "channel": "taobao"}).json()
        assert excl_resp["code"] in excl_resp["wangwang_message"]
        assert excl_resp["redeem_url"] in excl_resp["wangwang_message"]
        assert excl_resp["item"]["sku_title"] == "Google Ultra x20 独享整组（管理员版）"
        assert "VEO3" not in excl_resp["item"]["sku_title"]
        assert excl_resp["wangwang_message"] == (
            "感谢下单 Google Ultra x20 独享整组（管理员版）！\n"
            f"点链接自助秒领会员邮箱：{excl_resp['redeem_url']}\n"
            f"兑换码：{excl_resp['code']}\n"
            "\n"
            "【先看这里】\n"
            "淘宝显示已发货，就是兑换码和提货链接已经发给你了，直接点上面的链接就能秒领会员邮箱。\n"
            "\n"
            "【开通前先查这 3 件事】\n"
            "1. 先关掉 Google 付款资料：https://payments.google.com/gp/w/home/settings\n"
            "2. 先退出原来的 Google 家庭组：https://g.co/yourfamily\n"
            "3. 准备一个你自己常用的稳定 Gmail 当组长，且这个号最近 12 个月里没加过、没退过、也没被重拉过其他家庭组（Google 规定每 12 个月只能换 1 次家庭组）。\n"
            "\n"
            "【3 步开通（很简单）】\n"
            "1. 点上面的提货链接，立刻领到我们给的会员邮箱。\n"
            "2. 用你自己的常用 Gmail 建一个 Google 家庭组，把这个会员邮箱拉进去；对方同意后你就开通 Ultra 会员了，自己还能再拉 4 个人一起用。\n"
            "3. 发完邀请后，记得回到这个提货页填写你当组长的常用邮箱，系统会自动为你登记 30 天质保（免传截图；如果遇延迟一般 3-7 小时内处理，最晚 24 小时）。\n"
            "\n"
            "【质保和退换说明】\n"
            "1. 千万别踢掉我们给的会员邮箱！踢掉就会立刻掉会员，而且没法保修。\n"
            "2. 质保 30 天（从领到邮箱那天开始算），如果不是你自己的原因掉会员，免费给你保修；领完邮箱后，不是商品本身问题不支持全额退款。\n"
            "3. 如果是你自己的 Gmail 被封或登不上，没法保修。\n"
            "4. 如果因为自己的号被封、登不上、12 个月换组限制，或不小心踢掉了我们给的会员邮箱导致用不了/掉会员，补救或换号要收 79.9 元手续费，而且不保证一定能恢复。"
        )
        for forbidden in ("虚拟服务", "非空包裹", "非实物", "虚拟发货", "无需物流"):
            assert forbidden not in slot_resp["wangwang_message"]
            assert forbidden not in excl_resp["wangwang_message"]
        excl_public = c.get(f"/api/v1/public/codes/{excl_resp['code']}").json()
        assert excl_public["sku_title"] == "Google Ultra x20 独享整组（管理员版）"

        merged_msg = format_trade_delivery_message(
            [
                {"sku_key": "ultra_shared_slot", "code": slot_resp["code"]},
                {"sku_key": "ultra_exclusive_admin", "code": excl_resp["code"]},
            ],
            SKUS,
            "https://redeem.example.com",
        )
        assert "Google Ultra x20 独享整组（管理员版）" in merged_msg
        assert "点链接自助填 Gmail 提货（不用在旺旺发邮箱）：" in merged_msg
        assert "点链接自助秒领会员邮箱：" in merged_msg
        assert "https://payments.google.com/gp/w/home/settings" in merged_msg
        assert "https://g.co/yourfamily" in merged_msg
        assert "49.9 元手续费" in merged_msg
        assert "79.9 元手续费" in merged_msg
        assert "千万别踢掉我们给的会员邮箱" in merged_msg
        assert "3-7 小时" in merged_msg
        assert "2-6" not in merged_msg
        assert "1~6" not in merged_msg
        assert "独享账号" not in merged_msg
        assert "VEO3" not in merged_msg

        multi_slot_msg = format_trade_delivery_message(
            [
                {"sku_key": "ultra_shared_slot", "code": slot_resp["code"]},
                {"sku_key": "ultra_shared_slot", "code": "U20S-TEST-0002"},
            ],
            SKUS,
            "https://redeem.example.com",
        )
        assert "https://payments.google.com/gp/w/home/settings" in multi_slot_msg
        assert "https://g.co/yourfamily" in multi_slot_msg
        assert "49.9 元手续费" in multi_slot_msg
        assert "【共享车位·提货与使用须知】" not in multi_slot_msg
        assert "79.9 元手续费" not in multi_slot_msg


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


def test_16688_client_preserves_sanitized_business_error_and_normalizes_openapi_base_url(monkeypatch):
    captured_urls = []

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    responses = [
        FakeResponse(200, {"code": 0, "msg": "仅卡密商品支持导入库存 secret-token app-id-val"}),
        FakeResponse(400, {"msg": "参数非法 key=raw-secret"}),
        FakeResponse(502, text="bad gateway secret-token"),
    ]

    def fake_post(url, **kwargs):
        captured_urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr("httpx.post", fake_post)
    client_ = Channel16688Client("https://16688.example/openApi/", "app-id-val", "secret-token")

    with pytest.raises(UpstreamError) as business_exc:
        client_.stock_secret_add("G66428225", "content")
    assert captured_urls[-1] == "https://16688.example/openApi/stockSecretBatch/add"
    assert business_exc.value.rejected is True
    assert "仅卡密商品支持导入库存" in business_exc.value.message
    assert "secret-token" not in business_exc.value.message
    assert "app-id-val" not in business_exc.value.message

    with pytest.raises(UpstreamError) as client_err:
        client_.stock_secret_add("G66428225", "content")
    assert client_err.value.status_code == 400
    assert client_err.value.rejected is True
    assert "参数非法" in client_err.value.message
    assert "raw-secret" not in client_err.value.message

    with pytest.raises(UpstreamError) as server_err:
        client_.stock_secret_add("G66428225", "content")
    assert server_err.value.status_code == 502
    assert server_err.value.rejected is False
    assert "secret-token" not in server_err.value.message

    def fake_connect_error(url, **kwargs):
        raise httpx.ConnectError("connect failed")

    monkeypatch.setattr("httpx.post", fake_connect_error)
    with pytest.raises(UpstreamError) as conn_err:
        client_.stock_secret_add("G66428225", "content")
    assert conn_err.value.rejected is True

    def fake_read_error(url, **kwargs):
        raise httpx.ReadError("read broken")

    monkeypatch.setattr("httpx.post", fake_read_error)
    with pytest.raises(UpstreamError) as read_err:
        client_.stock_secret_add("G66428225", "content")
    assert read_err.value.rejected is False


def test_admin_batch_16688_rejection_returns_structured_error_without_orphan_codes(components):
    app, _, _, _, _ = components

    class Rejecting16688:
        def __init__(self, exc):
            self.exc = exc

        def stock_secret_add(self, goods_no, content):
            raise self.exc

    app.state.service.channel = Rejecting16688(UpstreamError(502, "仅卡密商品支持导入库存", rejected=True))
    with client(app) as c:
        resp = c.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "16688", "count": 5, "goods_no": "G66428225", "batch_label": "测试批次"},
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["error_code"] == "CHANNEL_16688_REJECTED"
        assert "仅卡密商品支持导入库存" in body["message"]
        assert "卡密/自动发货" in body["message"]

        with closing(app.state.ledger.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0

        app.state.service.channel = Rejecting16688(UpstreamError(400, "16688 HTTP 400: goods_no invalid", rejected=True))
        resp_4xx = c.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "16688", "count": 2, "goods_no": "G_BAD", "batch_label": "测试4xx"},
        )
        assert resp_4xx.status_code == 422
        assert resp_4xx.json()["error_code"] == "CHANNEL_16688_REJECTED"
        with closing(app.state.ledger.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_admin_batch_16688_timeout_and_ambiguous_error_persist_reconciliation_codes(components):
    app, _, _, _, _ = components

    class Timeout16688:
        def stock_secret_add(self, goods_no, content):
            raise UpstreamTimeout("16688 timed out")

    app.state.service.channel = Timeout16688()
    with client(app) as c:
        resp = c.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "16688", "count": 3, "goods_no": "G100", "batch_label": "超时批次"},
        )
        assert resp.status_code == 504
        assert resp.json()["error_code"] == "CHANNEL_16688_TIMEOUT"
        with closing(app.state.ledger.connect()) as conn:
            rows = conn.execute("SELECT code, remark, status FROM codes").fetchall()
            assert len(rows) == 3
            assert all(row["status"] == "unused" and "16688 导入超时待核对库存" in row["remark"] for row in rows)
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
            audits = conn.execute("SELECT reason FROM audit_events").fetchall()
            assert len(audits) == 3
            assert all(item["reason"] == "issued_16688_import_uncertain" for item in audits)

    class Ambiguous16688:
        def stock_secret_add(self, goods_no, content):
            raise UpstreamError(503, "16688 network error (ReadError)", rejected=False)

    app.state.service.channel = Ambiguous16688()
    with client(app) as c:
        resp = c.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "16688", "count": 2, "goods_no": "G100", "batch_label": "断流批次"},
        )
        assert resp.status_code == 502
        assert resp.json()["error_code"] == "CHANNEL_16688_ERROR"
        with closing(app.state.ledger.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 5
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 2


def test_admin_batch_taobao_persists_atomically_without_calling_16688(components):
    app, settings, _, channel, _ = components
    settings.public_base_url = "https://redeem.example.com/"
    with client(app) as c:
        resp = c.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "taobao", "count": 4, "batch_label": "淘宝备码"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["count"] == 4
        assert len(body["codes"]) == 4
        assert "https://redeem.example.com//r/" not in body["export_lines_with_url"]
        assert "https://redeem.example.com/r/" in body["export_lines_with_url"]
        assert channel.imported == []
        with closing(app.state.ledger.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM codes WHERE batch_id=?", (body["batch_id"],)).fetchone()[0] == 4
            assert conn.execute("SELECT COUNT(*) FROM batches WHERE batch_id=?", (body["batch_id"],)).fetchone()[0] == 1


def test_admin_distinguishes_in_stock_and_dispatched_unused_codes(components):
    app, _, _, _, _ = components
    with client(app) as c:
        stock_shared = c.post(
            "/api/v1/admin/codes/issue",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "taobao", "external_order_id": "", "remark": "stock-pool"},
        ).json()["code"]
        dispatched_shared = c.post(
            "/api/v1/admin/codes/issue",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "taobao", "external_order_id": "TB-ORDER-1001", "remark": "sent"},
        ).json()["code"]
        stock_exclusive = c.post(
            "/api/v1/admin/codes/issue",
            headers=ADMIN,
            json={"sku_key": "ultra_exclusive_admin", "channel": "16688", "external_order_id": "   ", "remark": "stock-16688"},
        ).json()["code"]

        overview = c.get("/api/v1/admin/overview", headers=ADMIN).json()
        assert overview["counts"]["unused"] == 3
        assert overview["counts"]["in_stock"] == 2
        assert overview["counts"]["dispatched_unused"] == 1
        sku_map = {sku["sku_key"]: sku for sku in overview["skus"]}
        assert sku_map["ultra_shared_slot"]["in_stock_codes_taobao"] == 1
        assert sku_map["ultra_shared_slot"]["dispatched_unused_codes_taobao"] == 1
        assert sku_map["ultra_exclusive_admin"]["in_stock_codes_16688"] == 1
        assert sku_map["ultra_exclusive_admin"]["dispatched_unused_codes_16688"] == 0

        in_stock_resp = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "in_stock"}).json()
        assert in_stock_resp["total"] == 2
        assert {item["code"] for item in in_stock_resp["items"]} == {stock_shared, stock_exclusive}
        assert all(item["display_status"] == "in_stock" and item["is_dispatched"] is False for item in in_stock_resp["items"])

        dispatched_resp = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "dispatched_unused"}).json()
        assert dispatched_resp["total"] == 1
        assert dispatched_resp["items"][0]["code"] == dispatched_shared
        assert dispatched_resp["items"][0]["display_status"] == "dispatched_unused"
        assert dispatched_resp["items"][0]["is_dispatched"] is True

        legacy_unused = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "unused"}).json()
        assert legacy_unused["total"] == 3

        assert app.state.ledger.bind_code_external_order(stock_shared, "TB-ORDER-1002") == "bound"
        after_bind = c.get("/api/v1/admin/overview", headers=ADMIN).json()
        assert after_bind["counts"]["in_stock"] == 1
        assert after_bind["counts"]["dispatched_unused"] == 2


def test_admin_manual_attention_unifies_exception_and_warranty_states(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        # 1. Raw exception order -> needs_manual_review
        failed_code = issue(c, sku="ultra_shared_slot")
        slot_product_id = SKUS["ultra_shared_slot"]["product_id"]
        original_price = canboso.products[slot_product_id]["price"]["amount"]
        canboso.products[slot_product_id]["price"]["amount"] = 99.0
        failed_resp = c.post(
            f"/api/v1/public/codes/{failed_code}/redeem",
            json={"buyer_email": "fail.user@gmail.com", "buyer_email_confirm": "fail.user@gmail.com", "preflight_confirmed": True},
        )
        assert failed_resp.status_code == 200 and failed_resp.json()["status"] == "needs_manual_review"
        canboso.products[slot_product_id]["price"]["amount"] = original_price

        # 2. Exclusive order delivered, waiting for buyer warranty -> delivered_pending_warranty
        pending_warranty_code = issue(c, sku="ultra_exclusive_admin")
        pending_resp = c.post(
            f"/api/v1/public/codes/{pending_warranty_code}/redeem",
            json={"preflight_confirmed": True},
        )
        assert pending_resp.status_code == 200 and pending_resp.json()["status"] == "delivered_pending_warranty"

        # 3. Exclusive order with buyer warranty submitted, waiting for merchant ack -> pending_warranty_ack
        pending_ack_code = issue(c, sku="ultra_exclusive_admin")
        ack_redeem = c.post(
            f"/api/v1/public/codes/{pending_ack_code}/redeem",
            json={"preflight_confirmed": True},
        )
        assert ack_redeem.status_code == 200 and ack_redeem.json()["status"] == "delivered_pending_warranty"
        ack_war = c.post(
            f"/api/v1/public/codes/{pending_ack_code}/warranty",
            data={"admin_email": "exclusive.owner@gmail.com", "admin_email_confirm": "exclusive.owner@gmail.com", "note": "invited"},
            files={"screenshot": ("proof.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        )
        assert ack_war.status_code == 200 and ack_war.json()["status"] == "completed"

        # 4. Shared order fully completed -> completed
        shared_done_code = issue(c, sku="ultra_shared_slot")
        shared_redeem = c.post(
            f"/api/v1/public/codes/{shared_done_code}/redeem",
            json={"buyer_email": "done.user@gmail.com", "buyer_email_confirm": "done.user@gmail.com", "preflight_confirmed": True},
        )
        assert shared_redeem.status_code == 200 and shared_redeem.json()["status"] == "waiting_fulfillment"
        shared_row = app.state.ledger.get_code(shared_done_code)
        canboso.orders[shared_row["upstream_order_code"]]["status"] = "completed"
        canboso.orders[shared_row["upstream_order_code"]]["deliveredAt"] = now_iso()
        poll_done = c.post(f"/api/v1/admin/codes/{shared_done_code}/poll", headers=ADMIN)
        assert poll_done.status_code == 200 and poll_done.json()["status"] == "completed"

        overview = c.get("/api/v1/admin/overview", headers=ADMIN).json()
        counts = overview["counts"]
        assert counts["needs_manual_review"] == 1
        assert counts["delivered_pending_warranty"] == 1
        assert counts["pending_warranty_ack"] == 1
        assert counts["manual_attention"] == 3
        assert counts["completed"] == 1

        manual_list = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "manual_attention"}).json()
        assert manual_list["total"] == 3
        manual_by_code = {item["code"]: item for item in manual_list["items"]}
        assert set(manual_by_code) == {failed_code, pending_warranty_code, pending_ack_code}
        assert manual_by_code[failed_code]["status"] == "needs_manual_review"
        assert manual_by_code[failed_code]["display_status"] == "needs_manual_review"
        assert manual_by_code[pending_warranty_code]["status"] == "delivered_pending_warranty"
        assert manual_by_code[pending_warranty_code]["display_status"] == "delivered_pending_warranty"
        assert manual_by_code[pending_ack_code]["status"] == "completed"
        assert manual_by_code[pending_ack_code]["warranty_status"] == "submitted"
        assert manual_by_code[pending_ack_code]["display_status"] == "pending_warranty_ack"

        completed_list = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "completed"}).json()
        assert completed_list["total"] == 1
        assert [item["code"] for item in completed_list["items"]] == [shared_done_code]

        # Legacy raw filters still work cleanly
        raw_manual = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "needs_manual_review"}).json()
        assert [item["code"] for item in raw_manual["items"]] == [failed_code]
        raw_pending_warranty = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "delivered_pending_warranty"}).json()
        assert [item["code"] for item in raw_pending_warranty["items"]] == [pending_warranty_code]
        pending_ack_only = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "pending_warranty_ack"}).json()
        assert [item["code"] for item in pending_ack_only["items"]] == [pending_ack_code]

        # Retry must stay strictly gated on raw needs_manual_review
        charges_before = canboso.charge_count
        retry_pending = c.post(f"/api/v1/admin/codes/{pending_warranty_code}/retry", headers=ADMIN, json={"note": "noop"})
        retry_ack = c.post(f"/api/v1/admin/codes/{pending_ack_code}/retry", headers=ADMIN, json={"note": "noop"})
        assert retry_pending.status_code == 200 and retry_pending.json()["status"] == "delivered_pending_warranty"
        assert retry_ack.status_code == 200 and retry_ack.json()["status"] == "completed"
        assert canboso.charge_count == charges_before

        # After merchant marks warranty registered upstream, order leaves manual_attention and enters completed
        acked = c.post(f"/api/v1/admin/codes/{pending_ack_code}/warranty-ack", headers=ADMIN, json={"note": "已转发上游"})
        assert acked.status_code == 200
        assert acked.json()["warranty_status"] == "registered_upstream"
        assert acked.json()["display_status"] == "completed"

        after_counts = c.get("/api/v1/admin/overview", headers=ADMIN).json()["counts"]
        assert after_counts["needs_manual_review"] == 1
        assert after_counts["delivered_pending_warranty"] == 1
        assert after_counts["pending_warranty_ack"] == 0
        assert after_counts["manual_attention"] == 2
        assert after_counts["completed"] == 2

        after_manual = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "manual_attention"}).json()
        assert {item["code"] for item in after_manual["items"]} == {failed_code, pending_warranty_code}
        after_completed = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "completed"}).json()
        assert {item["code"] for item in after_completed["items"]} == {shared_done_code, pending_ack_code}


def test_admin_codes_sorting_and_all_excludes_in_stock(components):
    app, _, canboso, _, _ = components
    ledger = app.state.ledger

    def set_timestamps(
        code: str,
        *,
        created_at: str,
        submitted_at: str | None = None,
        updated_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        with ledger.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE codes SET created_at=?, submitted_at=?, updated_at=?, completed_at=? WHERE code=?",
                (created_at, submitted_at, updated_at or created_at, completed_at, code),
            )

    with client(app) as c:
        # 1. Two in-stock codes with distinct created_at
        stock_older = c.post(
            "/api/v1/admin/codes/issue",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "taobao", "external_order_id": "", "remark": "stock-old"},
        ).json()["code"]
        stock_newer = c.post(
            "/api/v1/admin/codes/issue",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "taobao", "external_order_id": "", "remark": "stock-new"},
        ).json()["code"]
        set_timestamps(stock_older, created_at="2026-11-25T08:00:00+08:00")
        set_timestamps(stock_newer, created_at="2026-11-25T23:50:00+08:00")

        # 2. Two waiting_fulfillment codes: older-created used MORE recently, and older-used polled MORE recently
        wait_created_early_used_latest = issue(c, sku="ultra_shared_slot", external_order_id="TB-WAIT-1")
        wait_created_late_used_earlier = issue(c, sku="ultra_shared_slot", external_order_id="TB-WAIT-2")
        assert c.post(
            f"/api/v1/public/codes/{wait_created_early_used_latest}/redeem",
            json={"buyer_email": "wait.latest@example.com", "preflight_confirmed": True},
        ).status_code == 200
        assert c.post(
            f"/api/v1/public/codes/{wait_created_late_used_earlier}/redeem",
            json={"buyer_email": "wait.earlier@example.com", "preflight_confirmed": True},
        ).status_code == 200
        set_timestamps(
            wait_created_early_used_latest,
            created_at="2026-11-25T09:00:00+08:00",
            submitted_at="2026-11-25T22:30:00+08:00",
            updated_at="2026-11-25T22:31:00+08:00",
        )
        set_timestamps(
            wait_created_late_used_earlier,
            created_at="2026-11-25T20:00:00+08:00",
            submitted_at="2026-11-25T20:10:00+08:00",
            updated_at="2026-11-25T23:40:00+08:00",
        )

        # 3. Completed code used between the two waiting orders
        completed_mid = issue(c, sku="ultra_shared_slot", external_order_id="TB-DONE-1")
        assert c.post(
            f"/api/v1/public/codes/{completed_mid}/redeem",
            json={"buyer_email": "done.mid@example.com", "preflight_confirmed": True},
        ).status_code == 200
        upstream_done = ledger.get_code(completed_mid)["upstream_order_code"]
        canboso.orders[upstream_done]["status"] = "completed"
        canboso.orders[upstream_done]["deliveredAt"] = "2026-11-25T22:00:00+08:00"
        assert c.post(f"/api/v1/admin/codes/{completed_mid}/poll", headers=ADMIN).status_code == 200
        set_timestamps(
            completed_mid,
            created_at="2026-11-25T10:00:00+08:00",
            submitted_at="2026-11-25T21:15:00+08:00",
            updated_at="2026-11-25T22:00:00+08:00",
            completed_at="2026-11-25T22:00:00+08:00",
        )

        # 4. Two dispatched_unused codes (no submitted_at; sorted by dispatch/updated_at DESC)
        dispatched_older = issue(c, sku="ultra_shared_slot", external_order_id="TB-DISP-OLD")
        dispatched_newer = issue(c, sku="ultra_shared_slot", external_order_id="TB-DISP-NEW")
        set_timestamps(
            dispatched_older,
            created_at="2026-11-25T18:00:00+08:00",
            updated_at="2026-11-25T19:00:00+08:00",
        )
        set_timestamps(
            dispatched_newer,
            created_at="2026-11-25T11:00:00+08:00",
            updated_at="2026-11-25T23:45:00+08:00",
        )

        # 5. Unused-then-revoked stock code revoked very recently (must NOT jump ahead of buyer-used orders in "全部")
        revoked_unused = c.post(
            "/api/v1/admin/codes/issue",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "taobao", "external_order_id": "", "remark": "bulk-revoke"},
        ).json()["code"]
        assert c.post(f"/api/v1/admin/codes/{revoked_unused}/revoke", headers=ADMIN, json={"reason": "批量置换作废"}).status_code == 200
        set_timestamps(
            revoked_unused,
            created_at="2026-11-25T12:00:00+08:00",
            updated_at="2026-11-25T23:55:00+08:00",
        )

        # Verify in_stock sorts by created_at DESC
        in_stock_resp = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "in_stock"}).json()
        assert [item["code"] for item in in_stock_resp["items"]] == [stock_newer, stock_older]
        assert all(item["submitted_at"] is None for item in in_stock_resp["items"])

        # Verify "全部" (omitted or blank status) excludes in_stock and sorts used orders by submitted_at DESC first
        for all_params in ({}, {"status": ""}, {"status": "   "}):
            all_resp = c.get("/api/v1/admin/codes", headers=ADMIN, params=all_params).json()
            all_codes = [item["code"] for item in all_resp["items"]]
            assert stock_older not in all_codes
            assert stock_newer not in all_codes
            assert all_codes == [
                wait_created_early_used_latest,
                completed_mid,
                wait_created_late_used_earlier,
                revoked_unused,
                dispatched_newer,
                dispatched_older,
            ]
            assert all_resp["total"] == 6

        # Searching for an in-stock code inside "全部" must still exclude it; "in_stock" finds it
        all_search_stock = c.get("/api/v1/admin/codes", headers=ADMIN, params={"q": stock_newer}).json()
        assert all_search_stock["total"] == 0
        in_stock_search = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "in_stock", "q": stock_newer}).json()
        assert [item["code"] for item in in_stock_search["items"]] == [stock_newer]

        # Verify waiting_fulfillment sorts by submitted_at DESC even when poll refreshed updated_at on the earlier-used order
        waiting_resp = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "waiting_fulfillment"}).json()
        assert [item["code"] for item in waiting_resp["items"]] == [
            wait_created_early_used_latest,
            wait_created_late_used_earlier,
        ]
        assert waiting_resp["items"][0]["submitted_at"] == "2026-11-25T22:30:00+08:00"
        assert waiting_resp["items"][0]["completed_at"] is None

        # Verify completed exposes submitted_at & completed_at
        completed_resp = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "completed"}).json()
        assert [item["code"] for item in completed_resp["items"]] == [completed_mid]
        assert completed_resp["items"][0]["submitted_at"] == "2026-11-25T21:15:00+08:00"
        assert completed_resp["items"][0]["completed_at"] == "2026-11-25T22:00:00+08:00"

        # Verify dispatched_unused sorts by dispatch/updated_at DESC
        dispatched_resp = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "dispatched_unused"}).json()
        assert [item["code"] for item in dispatched_resp["items"]] == [dispatched_newer, dispatched_older]

        # Verify revoked tab returns revoked codes sorted by usage/revoke recency
        revoked_resp = c.get("/api/v1/admin/codes", headers=ADMIN, params={"status": "revoked"}).json()
        assert [item["code"] for item in revoked_resp["items"]] == [revoked_unused]


def test_admin_reconciliation_export_auth_filters_sanitization_and_csv(components):
    app, _, _, _, _ = components
    ledger = app.state.ledger

    with client(app) as c:
        assert c.get("/api/v1/admin/reconciliation-export").status_code == 401
        assert c.get("/api/v1/admin/reconciliation-export", params={"format": "csv"}).status_code == 401

        assert c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"start_date": "2026-11-3"}).status_code == 400
        assert c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"end_date": "2026-02-30"}).status_code == 400
        assert c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"format": "xml"}).status_code == 400

        code_tb = issue(c, sku="ultra_shared_slot", channel="taobao", external_order_id="")
        code_16688 = issue(c, sku="ultra_exclusive_admin", channel="16688", external_order_id="EXT-16688-01", goods_no="G200")
        code_unused = issue(c, sku="ultra_shared_slot", channel="taobao", external_order_id="EXT-UNUSED-01")
        code_fallback = issue(c, sku="ultra_shared_slot", channel="taobao", external_order_id="EXT-TB-FALLBACK-01")

        with ledger.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE codes
                SET status='completed',
                    buyer_email='secret.buyer@gmail.com',
                    buyer_email_masked='se***@gmail.com',
                    request_payload_json='{"buyer_email":"secret.buyer@gmail.com"}',
                    upstream_order_code='CB-PLAN2-TB-01',
                    upstream_status='completed',
                    cost_usd=12.5,
                    delivery_json='{"email":"delivered_acct@example.com","password":"TopSecretPwd_999"}',
                    warranty_json='{"note":"private_warranty_secret"}',
                    submitted_at='2026-11-24T10:30:00+08:00',
                    completed_at='2026-11-24T10:35:00+08:00',
                    created_at='2026-11-20T09:00:00+08:00',
                    updated_at='2026-11-24T10:35:00+08:00'
                WHERE code=?
                """,
                (code_tb,),
            )
            conn.execute(
                """
                INSERT INTO agiso_deliveries(
                    delivery_key, tid, oid, seq, num_iid, sku_id, sku_key, code,
                    buyer_nick, delivery_status, result_created, card_sent, ww_sent, dummy_sent,
                    last_error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "TB-TID-9001:TB-OID-9001:1",
                    "TB-TID-9001",
                    "TB-OID-9001",
                    1,
                    "1001",
                    "2001",
                    "ultra_shared_slot",
                    code_tb,
                    "tb_secret_buyer_nick_888",
                    "delivered",
                    1,
                    1,
                    1,
                    1,
                    None,
                    "2026-11-24T10:00:00+08:00",
                    "2026-11-24T10:05:00+08:00",
                ),
            )
            conn.execute(
                """
                UPDATE codes
                SET status='waiting_fulfillment',
                    buyer_email='other.secret@gmail.com',
                    buyer_email_masked='ot***@gmail.com',
                    upstream_order_code='CB-PLAN2-16688-02',
                    upstream_status='processing',
                    cost_usd=62.25,
                    delivery_json='{"password":"AnotherSecretPassword_777"}',
                    submitted_at='2026-11-25T15:00:00+08:00',
                    completed_at=NULL,
                    created_at='2026-11-25T14:00:00+08:00',
                    updated_at='2026-11-25T15:05:00+08:00'
                WHERE code=?
                """,
                (code_16688,),
            )
            conn.execute(
                """
                UPDATE codes
                SET sku_key='orphan_sku_key',
                    status='needs_manual_review',
                    upstream_order_code='CB-PLAN2-MAN-03',
                    upstream_status='cancelled',
                    cost_usd=NULL,
                    submitted_at='',
                    completed_at='',
                    created_at='2026-11-22T08:15:00+08:00',
                    updated_at='2026-11-22T08:20:00+08:00'
                WHERE code=?
                """,
                (code_fallback,),
            )

        json_resp = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN)
        assert json_resp.status_code == 200
        raw_json_text = json_resp.text
        payload = json_resp.json()
        assert payload["source"] == "tg_fatbunny"
        assert payload["count"] == 3
        assert [item["code"] for item in payload["items"]] == [code_16688, code_tb, code_fallback]
        assert code_unused not in [item["code"] for item in payload["items"]]

        tb_item = payload["items"][1]
        assert tb_item == {
            "code": code_tb,
            "sku_key": "ultra_shared_slot",
            "sku_title": SKUS["ultra_shared_slot"]["title"],
            "product_type": SKUS["ultra_shared_slot"]["product_type"],
            "channel": "taobao",
            "external_order_id": "TB-TID-9001",
            "agiso_tid": "TB-TID-9001",
            "agiso_oid": "TB-OID-9001",
            "goods_no": "",
            "upstream_order_code": "CB-PLAN2-TB-01",
            "upstream_status": "completed",
            "status": "completed",
            "cost_usd": 12.5,
            "quantity": 1,
            "source": "tg_fatbunny",
            "seller": "FatBunny Hub (方案二)",
            "submitted_at": "2026-11-24T10:30:00+08:00",
            "completed_at": "2026-11-24T10:35:00+08:00",
            "created_at": "2026-11-20T09:00:00+08:00",
            "updated_at": "2026-11-24T10:35:00+08:00",
        }
        fallback_item = payload["items"][2]
        assert fallback_item["sku_title"] == "orphan_sku_key"
        assert fallback_item["product_type"] == ""
        assert fallback_item["cost_usd"] is None

        forbidden_tokens = [
            "buyer_email",
            "buyer_email_masked",
            "delivery_json",
            "request_payload_json",
            "warranty_json",
            "buyer_nick",
            "secret.buyer@gmail.com",
            "se***@gmail.com",
            "other.secret@gmail.com",
            "ot***@gmail.com",
            "delivered_acct@example.com",
            "TopSecretPwd_999",
            "AnotherSecretPassword_777",
            "private_warranty_secret",
            "tb_secret_buyer_nick_888",
        ]
        for token in forbidden_tokens:
            assert token not in raw_json_text

        channel_resp = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"channel": "16688"}).json()
        assert [item["code"] for item in channel_resp["items"]] == [code_16688]
        assert channel_resp["count"] == 1
        taobao_resp = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"channel": "taobao"}).json()
        assert [item["code"] for item in taobao_resp["items"]] == [code_tb, code_fallback]
        assert taobao_resp["count"] == 2
        qianniu_resp = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"channel": "qianniu"}).json()
        assert [item["code"] for item in qianniu_resp["items"]] == [code_tb, code_fallback]
        assert qianniu_resp["count"] == 2
        zh_qianniu_resp = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"channel": "千牛"}).json()
        assert [item["code"] for item in zh_qianniu_resp["items"]] == [code_tb, code_fallback]
        store_16688_resp = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"channel": "16688_store"}).json()
        assert [item["code"] for item in store_16688_resp["items"]] == [code_16688]

        date_resp = c.get(
            "/api/v1/admin/reconciliation-export",
            headers=ADMIN,
            params={"start_date": "2026-11-22", "end_date": "2026-11-24"},
        ).json()
        assert [item["code"] for item in date_resp["items"]] == [code_tb, code_fallback]
        assert date_resp["count"] == 2

        csv_resp = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"format": "csv"})
        assert csv_resp.status_code == 200
        assert "text/csv" in csv_resp.headers["content-type"]
        assert "charset=utf-8" in csv_resp.headers["content-type"].lower()
        assert csv_resp.headers["content-disposition"] == 'attachment; filename="plan2-reconciliation.csv"'
        assert csv_resp.content.startswith(b"\xef\xbb\xbf")

        csv_text = csv_resp.content.decode("utf-8-sig")
        for token in forbidden_tokens:
            assert token not in csv_text

        rows = list(csv.DictReader(csv_text.splitlines()))
        assert list(rows[0].keys()) == [
            "采购单号",
            "来源",
            "商品名称",
            "商家名称",
            "数量",
            "原币币种",
            "原币金额",
            "付款时间",
            "状态",
            "关联渠道",
            "关联销售单号",
            "关联子单号",
            "兑换码",
            "SKU",
        ]
        assert len(rows) == 3
        assert rows[0]["采购单号"] == "CB-PLAN2-16688-02"
        assert rows[0]["原币币种"] == "USD"
        assert rows[0]["原币金额"] == "62.2500"
        assert rows[0]["付款时间"] == "2026-11-25T15:00:00+08:00"
        assert rows[0]["关联销售单号"] == "EXT-16688-01"
        assert rows[0]["关联子单号"] == ""

        assert rows[1]["采购单号"] == "CB-PLAN2-TB-01"
        assert rows[1]["来源"] == "tg_fatbunny"
        assert rows[1]["商品名称"] == SKUS["ultra_shared_slot"]["title"]
        assert rows[1]["商家名称"] == "FatBunny Hub (方案二)"
        assert rows[1]["数量"] == "1"
        assert rows[1]["原币币种"] == "USD"
        assert rows[1]["原币金额"] == "12.5000"
        assert rows[1]["付款时间"] == "2026-11-24T10:30:00+08:00"
        assert rows[1]["状态"] == "completed"
        assert rows[1]["关联渠道"] == "taobao"
        assert rows[1]["关联销售单号"] == "TB-TID-9001"
        assert rows[1]["关联子单号"] == "TB-OID-9001"
        assert rows[1]["兑换码"] == code_tb
        assert rows[1]["SKU"] == "ultra_shared_slot"

        assert rows[2]["采购单号"] == "CB-PLAN2-MAN-03"
        assert rows[2]["商品名称"] == "orphan_sku_key"
        assert rows[2]["原币金额"] == ""
        assert rows[2]["付款时间"] == "2026-11-22T08:15:00+08:00"

        # Formula injection escaping in CSV export
        code_formula = issue(c, sku="ultra_shared_slot", channel="taobao", external_order_id="=SUM(A1:A2)")
        with ledger.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE codes
                SET status = 'completed',
                    upstream_order_code = '+CMD|calc!A0',
                    cost_usd = 9.50,
                    submitted_at = '2026-11-26T10:00:00+08:00'
                WHERE code = ?
                """,
                (code_formula,),
            )
        csv_formula = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"format": "csv"}).content.decode("utf-8-sig")
        formula_rows = list(csv.DictReader(csv_formula.splitlines()))
        assert formula_rows[0]["采购单号"] == "'+CMD|calc!A0"
        assert formula_rows[0]["关联销售单号"] == "'=SUM(A1:A2)"
        assert formula_rows[0]["原币金额"] == "9.5000"
        assert formula_rows[0]["数量"] == "1"


def test_admin_poll_manual_review_completes_or_records_cancellation(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        code_done = issue(c)
        c.post(
            f"/api/v1/public/codes/{code_done}/redeem",
            json={"buyer_email": "done@gmail.com", "buyer_email_confirm": "done@gmail.com", "preflight_confirmed": True},
        )
        row_done = app.state.ledger.get_code(code_done)
        assert app.state.ledger.transition(
            code_done,
            "needs_manual_review",
            "manual_flag",
            {"last_error_code": "FULFILLMENT_TIMEOUT", "last_error_message": "超过 24 小时"},
            ("waiting_fulfillment",),
        )
        canboso.orders[row_done["upstream_order_code"]]["status"] = "completed"
        polled = c.post(f"/api/v1/admin/codes/{code_done}/poll", headers=ADMIN)
        assert polled.status_code == 200
        body = polled.json()
        assert body["status"] == "completed"
        assert body["last_error_code"] is None
        assert body["last_error_message"] is None
        assert any(ev["reason"] == "admin_poll_completed" for ev in body["audit_events"])

        code_cancel = issue(c)
        c.post(
            f"/api/v1/public/codes/{code_cancel}/redeem",
            json={"buyer_email": "cancel@gmail.com", "buyer_email_confirm": "cancel@gmail.com", "preflight_confirmed": True},
        )
        row_cancel = app.state.ledger.get_code(code_cancel)
        assert app.state.ledger.transition(
            code_cancel,
            "needs_manual_review",
            "manual_flag",
            {"last_error_code": "FULFILLMENT_TIMEOUT", "last_error_message": "超过 24 小时"},
            ("waiting_fulfillment",),
        )
        canboso.orders[row_cancel["upstream_order_code"]]["status"] = "cancelled"
        polled_cancel = c.post(f"/api/v1/admin/codes/{code_cancel}/poll", headers=ADMIN)
        assert polled_cancel.status_code == 200
        cancel_body = polled_cancel.json()
        assert cancel_body["status"] == "needs_manual_review"
        assert cancel_body["upstream_status"] == "cancelled"
        assert cancel_body["can_reorder"] is True
        assert any(ev["reason"] == "admin_poll_cancelled" for ev in cancel_body["audit_events"])


def test_force_new_order_reorders_same_or_new_email_and_blocks_active_upstream(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        code = issue(c)
        c.post(
            f"/api/v1/public/codes/{code}/redeem",
            json={"buyer_email": "old@gmail.com", "buyer_email_confirm": "old@gmail.com", "preflight_confirmed": True},
        )
        first_order = app.state.ledger.get_code(code)["upstream_order_code"]
        assert app.state.ledger.transition(
            code,
            "needs_manual_review",
            "manual_check",
            {"last_error_code": "FULFILLMENT_TIMEOUT"},
            ("waiting_fulfillment",),
        )
        blocked = c.post(f"/api/v1/admin/codes/{code}/retry", headers=ADMIN, json={"force_new_order": True})
        assert blocked.status_code == 409
        assert blocked.json()["error_code"] == "UPSTREAM_ORDER_ACTIVE"

        canboso.orders[first_order]["status"] = "cancelled"
        c.post(f"/api/v1/admin/codes/{code}/poll", headers=ADMIN)

        bad_email = c.post(
            f"/api/v1/admin/codes/{code}/retry",
            headers=ADMIN,
            json={"force_new_order": True, "new_buyer_email": "not-an-email"},
        )
        assert bad_email.status_code == 422
        assert bad_email.json()["error_code"] == "INVALID_EMAIL"

        reordered = c.post(
            f"/api/v1/admin/codes/{code}/retry",
            headers=ADMIN,
            json={"force_new_order": True, "note": "原邮箱重下"},
        )
        assert reordered.status_code == 200
        re_body = reordered.json()
        second_order = re_body["upstream_order_code"]
        assert second_order and second_order != first_order
        assert re_body["status"] == "waiting_fulfillment"
        assert re_body["buyer_email"] == "old@gmail.com"
        assert any(f"[prev={first_order}]" in ev["reason"] for ev in re_body["audit_events"])

        canboso.orders[second_order]["status"] = "cancelled"
        c.post(f"/api/v1/admin/codes/{code}/poll", headers=ADMIN)
        reordered_email = c.post(
            f"/api/v1/admin/codes/{code}/retry",
            headers=ADMIN,
            json={"force_new_order": True, "new_buyer_email": "fresh@gmail.com", "note": "买家换新号"},
        )
        assert reordered_email.status_code == 200
        re2 = reordered_email.json()
        assert re2["upstream_order_code"] != second_order
        assert re2["buyer_email"] == "fresh@gmail.com"
        assert any("[email:old@gmail.com->fresh@gmail.com]" in ev["reason"] for ev in re2["audit_events"])


def test_poll_over_24h_queries_upstream_before_timing_out(components):
    app, _, canboso, _, _ = components
    old_ts = (datetime.now(timezone.utc).astimezone() - timedelta(hours=25)).isoformat(timespec="seconds")
    with client(app) as c:
        code_finished = issue(c)
        c.post(
            f"/api/v1/public/codes/{code_finished}/redeem",
            json={"buyer_email": "ok24@gmail.com", "buyer_email_confirm": "ok24@gmail.com", "preflight_confirmed": True},
        )
        row1 = app.state.ledger.get_code(code_finished)
        with app.state.ledger.transaction(immediate=True) as conn:
            conn.execute("UPDATE codes SET submitted_at=? WHERE code=?", (old_ts, code_finished))
        canboso.orders[row1["upstream_order_code"]]["status"] = "completed"
        res1 = app.state.service.poll(code_finished)
        assert res1["status"] == "completed"

        code_still_pending = issue(c)
        c.post(
            f"/api/v1/public/codes/{code_still_pending}/redeem",
            json={"buyer_email": "slow24@gmail.com", "buyer_email_confirm": "slow24@gmail.com", "preflight_confirmed": True},
        )
        with app.state.ledger.transaction(immediate=True) as conn:
            conn.execute("UPDATE codes SET submitted_at=? WHERE code=?", (old_ts, code_still_pending))
        res2 = app.state.service.poll(code_still_pending)
        assert res2["status"] == "needs_manual_review"
        assert app.state.ledger.get_code(code_still_pending)["last_error_code"] == "FULFILLMENT_TIMEOUT"


def test_recover_stuck_purchasing_transitions_only_stale_rows(components):
    app, _, _, _, mailer = components
    stale_ts = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
    fresh_ts = now_iso()
    with client(app) as c:
        stale_code = issue(c)
        fresh_code = issue(c)
        with app.state.ledger.transaction(immediate=True) as conn:
            conn.execute("UPDATE codes SET status='purchasing', updated_at=? WHERE code=?", (stale_ts, stale_code))
            conn.execute("UPDATE codes SET status='purchasing', updated_at=? WHERE code=?", (fresh_ts, fresh_code))
        recovered = app.state.service.recover_stuck_purchasing()
        assert recovered == [stale_code]
        stale_row = app.state.ledger.get_code(stale_code)
        fresh_row = app.state.ledger.get_code(fresh_code)
        assert stale_row["status"] == "needs_manual_review"
        assert stale_row["last_error_code"] == "PURCHASING_STUCK_RECOVERY"
        assert fresh_row["status"] == "purchasing"
        assert any("PURCHASING_STUCK_RECOVERY" in msg["body"] for msg in mailer.sent)


def test_revoke_blocks_active_upstream_unless_forced(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        code_active = issue(c)
        c.post(
            f"/api/v1/public/codes/{code_active}/redeem",
            json={"buyer_email": "rev1@gmail.com", "buyer_email_confirm": "rev1@gmail.com", "preflight_confirmed": True},
        )
        blocked = c.post(f"/api/v1/admin/codes/{code_active}/revoke", headers=ADMIN, json={"reason": "误操作"})
        assert blocked.status_code == 409
        assert blocked.json()["error_code"] == "UPSTREAM_ORDER_ACTIVE"

        forced = c.post(
            f"/api/v1/admin/codes/{code_active}/revoke",
            headers=ADMIN,
            json={"reason": "核对后强制取消", "force": True},
        )
        assert forced.status_code == 200
        assert forced.json()["status"] == "revoked"
        assert any("[force]" in ev["reason"] for ev in forced.json()["audit_events"])

        code_cancelled = issue(c)
        c.post(
            f"/api/v1/public/codes/{code_cancelled}/redeem",
            json={"buyer_email": "rev2@gmail.com", "buyer_email_confirm": "rev2@gmail.com", "preflight_confirmed": True},
        )
        up_code = app.state.ledger.get_code(code_cancelled)["upstream_order_code"]
        canboso.orders[up_code]["status"] = "cancelled"
        c.post(f"/api/v1/admin/codes/{code_cancelled}/poll", headers=ADMIN)
        allowed = c.post(f"/api/v1/admin/codes/{code_cancelled}/revoke", headers=ADMIN, json={"reason": "上游已退"})
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "revoked"


def test_warranty_ack_validates_sku_and_submitted_warranty(components):
    app, _, _, _, _ = components
    with client(app) as c:
        shared_code = issue(c, sku="ultra_shared_slot")
        resp_shared = c.post(f"/api/v1/admin/codes/{shared_code}/warranty-ack", headers=ADMIN, json={"note": "x"})
        assert resp_shared.status_code == 409
        assert resp_shared.json()["error_code"] == "WARRANTY_NOT_APPLICABLE"

        excl_code = issue(c, sku="ultra_exclusive_admin")
        c.post(f"/api/v1/public/codes/{excl_code}/redeem", json={"preflight_confirmed": True})
        resp_no_email = c.post(f"/api/v1/admin/codes/{excl_code}/warranty-ack", headers=ADMIN, json={"note": "x"})
        assert resp_no_email.status_code == 409
        assert resp_no_email.json()["error_code"] == "WARRANTY_NOT_ALLOWED"


def test_steady_poll_uses_touch_poll_without_bloating_audit_events(components):
    app, _, _, _, _ = components
    with client(app) as c:
        code = issue(c)
        c.post(
            f"/api/v1/public/codes/{code}/redeem",
            json={"buyer_email": "steady@gmail.com", "buyer_email_confirm": "steady@gmail.com", "preflight_confirmed": True},
        )
        app.state.service.poll(code)
        count_after_first = len(app.state.ledger.audit(code))
        for _ in range(5):
            app.state.service.poll(code)
        assert len(app.state.ledger.audit(code)) == count_after_first
        assert app.state.ledger.get_code(code)["last_polled_at"] is not None


def test_upstream_refund_and_reconciliation_exclude_inactive_and_list_filters(components):
    app, _, canboso, _, _ = components
    with client(app) as c:
        unused_code = issue(c)
        no_up = c.post(
            f"/api/v1/admin/codes/{unused_code}/upstream-refund",
            headers=ADMIN,
            json={"upstream_refund_usd": 10.0, "note": "无上游单"},
        )
        assert no_up.status_code == 409
        assert no_up.json()["error_code"] == "NO_UPSTREAM_ORDER"

        active_code = issue(c)
        c.post(
            f"/api/v1/public/codes/{active_code}/redeem",
            json={"buyer_email": "act@gmail.com", "buyer_email_confirm": "act@gmail.com", "preflight_confirmed": True},
        )
        active_up = app.state.ledger.get_code(active_code)["upstream_order_code"]
        canboso.orders[active_up]["status"] = "completed"
        c.post(f"/api/v1/admin/codes/{active_code}/poll", headers=ADMIN)

        cancelled_code = issue(c)
        c.post(
            f"/api/v1/public/codes/{cancelled_code}/redeem",
            json={"buyer_email": "can@gmail.com", "buyer_email_confirm": "can@gmail.com", "preflight_confirmed": True},
        )
        cancel_up = app.state.ledger.get_code(cancelled_code)["upstream_order_code"]
        canboso.orders[cancel_up]["status"] = "cancelled"
        c.post(f"/api/v1/admin/codes/{cancelled_code}/poll", headers=ADMIN)
        ref_resp = c.post(
            f"/api/v1/admin/codes/{cancelled_code}/upstream-refund",
            headers=ADMIN,
            json={"upstream_refund_usd": 11.2, "note": "越南源头实退扣1.3刀"},
        )
        assert ref_resp.status_code == 200
        assert ref_resp.json()["upstream_refund_usd"] == 11.2
        assert ref_resp.json()["upstream_refund_note"] == "越南源头实退扣1.3刀"

        inf_resp = c.post(
            f"/api/v1/admin/codes/{cancelled_code}/upstream-refund",
            headers={**ADMIN, "Content-Type": "application/json"},
            content=b'{"upstream_refund_usd": Infinity, "note": "bad"}',
        )
        assert inf_resp.status_code == 422

        all_export = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN).json()
        assert all_export["count"] == 2
        by_code = {item["code"]: item for item in all_export["items"]}
        assert by_code[cancelled_code]["upstream_refund_usd"] == 11.2
        assert "upstream_refund_usd" not in by_code[active_code]

        active_only = c.get("/api/v1/admin/reconciliation-export", headers=ADMIN, params={"exclude_inactive": "true"}).json()
        assert active_only["count"] == 1
        assert active_only["items"][0]["code"] == active_code

        filtered_err = c.get(
            "/api/v1/admin/codes",
            headers=ADMIN,
            params={"error_code": "CANBOSO_ORDER_CANCELLED"},
        ).json()
        assert [item["code"] for item in filtered_err["items"]] == [cancelled_code]

        bad_date = c.get("/api/v1/admin/codes", headers=ADMIN, params={"start_date": "2026/11/01"})
        assert bad_date.status_code == 400
        assert bad_date.json()["error_code"] == "INVALID_DATE"

        today = now_iso()[:10]
        by_date = c.get("/api/v1/admin/codes", headers=ADMIN, params={"start_date": today, "end_date": today}).json()
        assert by_date["total"] >= 2


def test_api_wrong_method_returns_405_with_allow_header(components):
    app, _, _, _, _ = components
    with client(app) as c:
        resp_405 = c.get("/api/v1/public/codes/U20S-TEST-CODE/refresh")
        assert resp_405.status_code == 405
        assert resp_405.json()["error_code"] == "METHOD_NOT_ALLOWED"
        assert "POST" in (resp_405.headers.get("allow") or "")

        resp_404 = c.get("/api/v1/nonexistent-endpoint")
        assert resp_404.status_code == 404
        assert resp_404.json()["error_code"] == "NOT_FOUND"

        post_404 = c.post("/api/v1/nonexistent-endpoint", json={})
        assert post_404.status_code == 404
        assert post_404.json()["error_code"] == "NOT_FOUND"

        delete_404 = c.delete("/api/v1/nonexistent-endpoint")
        assert delete_404.status_code == 404
        assert delete_404.json()["error_code"] == "NOT_FOUND"


def test_reset_unused_allows_buyer_resubmit_after_low_balance_and_preserves_order_binding(components):
    app, _, canboso, _, mailer = components
    with client(app) as http_client:
        shared_code = issue(
            http_client,
            sku="ultra_shared_slot",
            channel="taobao",
            external_order_id="TB-LOW-001",
            remark="余额不足回归测试",
        )
        canboso.balance = 5.0
        first_redeem = http_client.post(
            f"/api/v1/public/codes/{shared_code}/redeem",
            json={
                "buyer_email": "first@gmail.com",
                "buyer_email_confirm": "first@gmail.com",
                "preflight_confirmed": True,
            },
        )
        assert first_redeem.status_code == 200
        assert first_redeem.json()["status"] == "needs_manual_review"
        assert len(canboso.orders) == 0
        assert len(mailer.sent) == 1

        detail_before = http_client.get(f"/api/v1/admin/codes/{shared_code}", headers=ADMIN).json()
        assert detail_before["last_error_code"] == "LOW_BALANCE"
        assert detail_before["can_reset_unused"] is True

        list_before = http_client.get(
            "/api/v1/admin/codes",
            headers=ADMIN,
            params={"error_code": "LOW_BALANCE"},
        ).json()
        listed_map = {item["code"]: item for item in list_before["items"]}
        assert listed_map[shared_code]["can_reset_unused"] is True

        retry_still_low = http_client.post(
            f"/api/v1/admin/codes/{shared_code}/retry",
            headers=ADMIN,
            json={},
        )
        assert retry_still_low.status_code == 503
        assert retry_still_low.json()["error_code"] == "LOW_BALANCE"
        detail_after_retry_fail = http_client.get(f"/api/v1/admin/codes/{shared_code}", headers=ADMIN).json()
        assert detail_after_retry_fail["can_reset_unused"] is True

        reset_resp = http_client.post(
            f"/api/v1/admin/codes/{shared_code}/reset-unused",
            headers=ADMIN,
            json={"note": "钱包已充值，请买家重交"},
        )
        assert reset_resp.status_code == 200
        reset_data = reset_resp.json()
        assert reset_data["status"] == "unused"
        assert reset_data["display_status"] == "dispatched_unused"
        assert reset_data["can_reset_unused"] is False
        assert reset_data["external_order_id"] == "TB-LOW-001"
        assert reset_data["remark"] == "余额不足回归测试"
        assert reset_data["buyer_email"] is None
        assert reset_data["buyer_email_masked"] is None
        assert reset_data["idempotency_key"] is None
        assert reset_data["submitted_at"] is None
        assert reset_data["last_error_code"] is None
        assert reset_data["last_error_message"] is None
        assert any(
            event["from_status"] == "needs_manual_review"
            and event["to_status"] == "unused"
            and event["reason"] == "admin_reset_unused:钱包已充值，请买家重交"
            for event in reset_data["audit_events"]
        )

        public_reset = http_client.get(f"/api/v1/public/codes/{shared_code}").json()
        assert public_reset["status"] == "unused"
        assert public_reset["buyer_email_masked"] is None

        second_low = http_client.post(
            f"/api/v1/public/codes/{shared_code}/redeem",
            json={
                "buyer_email": "second@gmail.com",
                "buyer_email_confirm": "second@gmail.com",
                "preflight_confirmed": True,
            },
        )
        assert second_low.status_code == 200
        assert second_low.json()["status"] == "needs_manual_review"
        assert len(mailer.sent) == 2

        second_reset = http_client.post(
            f"/api/v1/admin/codes/{shared_code}/reset-unused",
            headers=ADMIN,
            json={},
        )
        assert second_reset.status_code == 200
        assert second_reset.json()["status"] == "unused"

        canboso.balance = 500.0
        resubmitted = http_client.post(
            f"/api/v1/public/codes/{shared_code}/redeem",
            json={
                "buyer_email": "finalbuyer@gmail.com",
                "buyer_email_confirm": "finalbuyer@gmail.com",
                "preflight_confirmed": True,
            },
        )
        assert resubmitted.status_code == 200
        assert resubmitted.json()["status"] == "waiting_fulfillment"
        final_detail = http_client.get(f"/api/v1/admin/codes/{shared_code}", headers=ADMIN).json()
        assert final_detail["external_order_id"] == "TB-LOW-001"
        assert final_detail["buyer_email"] == "finalbuyer@gmail.com"
        assert final_detail["upstream_order_code"] is not None
        assert final_detail["can_reset_unused"] is False

        exc_batch = http_client.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={
                "sku_key": "ultra_exclusive_admin",
                "channel": "16688",
                "goods_no": "G100",
                "count": 1,
            },
        )
        assert exc_batch.status_code == 201
        exclusive_code = exc_batch.json()["codes"][0]
        canboso.balance = 10.0
        exc_first = http_client.post(
            f"/api/v1/public/codes/{exclusive_code}/redeem",
            json={"preflight_confirmed": True},
        )
        assert exc_first.status_code == 200
        assert exc_first.json()["status"] == "needs_manual_review"
        exc_detail = http_client.get(f"/api/v1/admin/codes/{exclusive_code}", headers=ADMIN).json()
        assert exc_detail["external_order_id"] == "TB-001"
        assert exc_detail["can_reset_unused"] is True

        exc_reset = http_client.post(
            f"/api/v1/admin/codes/{exclusive_code}/reset-unused",
            headers=ADMIN,
            json={"note": "独享单退回重交"},
        )
        assert exc_reset.status_code == 200
        assert exc_reset.json()["status"] == "unused"
        assert exc_reset.json()["external_order_id"] == "TB-001"

        canboso.balance = 500.0
        exc_resubmit = http_client.post(
            f"/api/v1/public/codes/{exclusive_code}/redeem",
            json={"preflight_confirmed": True},
        )
        assert exc_resubmit.status_code == 200
        assert exc_resubmit.json()["status"] == "delivered_pending_warranty"
        assert exc_resubmit.json()["delivery"]["account_text"]
        exc_final = http_client.get(f"/api/v1/admin/codes/{exclusive_code}", headers=ADMIN).json()
        assert exc_final["external_order_id"] == "TB-001"
        assert exc_final["upstream_order_code"] is not None


def test_reset_unused_blocks_purchase_attempts_active_orders_and_disputes(components, monkeypatch):
    app, _, canboso, channel, _ = components
    with client(app) as http_client:
        missing_resp = http_client.post("/api/v1/admin/codes/NO-SUCH-CODE/reset-unused", headers=ADMIN, json={})
        assert missing_resp.status_code == 404
        assert missing_resp.json()["error_code"] == "CODE_NOT_FOUND"

        unused_code = issue(http_client)
        unused_resp = http_client.post(f"/api/v1/admin/codes/{unused_code}/reset-unused", headers=ADMIN, json={})
        assert unused_resp.status_code == 409
        assert unused_resp.json()["error_code"] == "RESET_NOT_AVAILABLE"

        cancelled_code = issue(http_client)
        http_client.post(
            f"/api/v1/public/codes/{cancelled_code}/redeem",
            json={"buyer_email": "cancel@gmail.com", "buyer_email_confirm": "cancel@gmail.com", "preflight_confirmed": True},
        )
        cancelled_up = app.state.ledger.get_code(cancelled_code)["upstream_order_code"]
        canboso.orders[cancelled_up]["status"] = "cancelled"
        http_client.post(f"/api/v1/admin/codes/{cancelled_code}/poll", headers=ADMIN)
        cancelled_detail = http_client.get(f"/api/v1/admin/codes/{cancelled_code}", headers=ADMIN).json()
        assert cancelled_detail["can_reset_unused"] is False
        cancelled_reset = http_client.post(f"/api/v1/admin/codes/{cancelled_code}/reset-unused", headers=ADMIN, json={})
        assert cancelled_reset.status_code == 409
        assert cancelled_reset.json()["error_code"] == "UPSTREAM_ORDER_EXISTS"

        timeout_code = issue(http_client)
        original_purchase = canboso.purchase

        def raise_timeout(idempotency_key, payload):
            raise UpstreamTimeout("purchase timeout")

        monkeypatch.setattr(canboso, "purchase", raise_timeout)
        http_client.post(
            f"/api/v1/public/codes/{timeout_code}/redeem",
            json={"buyer_email": "timeout@gmail.com", "buyer_email_confirm": "timeout@gmail.com", "preflight_confirmed": True},
        )
        assert http_client.get(f"/api/v1/admin/codes/{timeout_code}", headers=ADMIN).json()["can_reset_unused"] is False
        timeout_reset = http_client.post(f"/api/v1/admin/codes/{timeout_code}/reset-unused", headers=ADMIN, json={})
        assert timeout_reset.status_code == 409
        assert timeout_reset.json()["error_code"] == "RESET_NOT_AVAILABLE"

        canboso.balance = 1.0
        http_client.post(f"/api/v1/admin/codes/{timeout_code}/retry", headers=ADMIN, json={})
        timeout_after_low = http_client.get(f"/api/v1/admin/codes/{timeout_code}", headers=ADMIN).json()
        assert timeout_after_low["last_error_code"] == "LOW_BALANCE"
        assert timeout_after_low["can_reset_unused"] is False
        timeout_still_blocked = http_client.post(
            f"/api/v1/admin/codes/{timeout_code}/reset-unused",
            headers=ADMIN,
            json={},
        )
        assert timeout_still_blocked.status_code == 409
        assert timeout_still_blocked.json()["error_code"] == "RESET_NOT_AVAILABLE"

        canboso.balance = 500.0

        def raise_http_error(idempotency_key, payload):
            raise UpstreamError(500, "internal failure")

        monkeypatch.setattr(canboso, "purchase", raise_http_error)
        http_err_code = issue(http_client)
        http_client.post(
            f"/api/v1/public/codes/{http_err_code}/redeem",
            json={"buyer_email": "httperr@gmail.com", "buyer_email_confirm": "httperr@gmail.com", "preflight_confirmed": True},
        )
        assert http_client.get(f"/api/v1/admin/codes/{http_err_code}", headers=ADMIN).json()["can_reset_unused"] is False
        assert http_client.post(
            f"/api/v1/admin/codes/{http_err_code}/reset-unused",
            headers=ADMIN,
            json={},
        ).status_code == 409

        def return_empty_order(idempotency_key, payload):
            return {"order": {}, "payment": {"currency": "USD", "amount": 13.47}}

        monkeypatch.setattr(canboso, "purchase", return_empty_order)
        unconfirmed_code = issue(http_client)
        http_client.post(
            f"/api/v1/public/codes/{unconfirmed_code}/redeem",
            json={"buyer_email": "unconf@gmail.com", "buyer_email_confirm": "unconf@gmail.com", "preflight_confirmed": True},
        )
        unconf_detail = http_client.get(f"/api/v1/admin/codes/{unconfirmed_code}", headers=ADMIN).json()
        assert unconf_detail["last_error_code"] == "UPSTREAM_ERROR"
        assert unconf_detail["can_reset_unused"] is False
        assert http_client.post(
            f"/api/v1/admin/codes/{unconfirmed_code}/reset-unused",
            headers=ADMIN,
            json={},
        ).status_code == 409

        monkeypatch.setattr(canboso, "purchase", original_purchase)
        stuck_code = issue(http_client)
        app.state.ledger.claim_for_purchase(
            stuck_code,
            {"product_id": SKUS["ultra_shared_slot"]["product_id"], "quantity": 1, "customer_email": "stuck@gmail.com"},
            "stuck@gmail.com",
            "stuck@gmail.com",
        )
        old_ts = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=30)).isoformat(timespec="seconds")
        with app.state.ledger.transaction(immediate=True) as conn:
            conn.execute("UPDATE codes SET updated_at=? WHERE code=?", (old_ts, stuck_code))
        app.state.service.recover_stuck_purchasing()
        assert http_client.get(f"/api/v1/admin/codes/{stuck_code}", headers=ADMIN).json()["can_reset_unused"] is False
        assert http_client.post(
            f"/api/v1/admin/codes/{stuck_code}/reset-unused",
            headers=ADMIN,
            json={},
        ).status_code == 409

        rej_batch = http_client.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={
                "sku_key": "ultra_exclusive_admin",
                "channel": "16688",
                "goods_no": "G100",
                "count": 1,
            },
        )
        assert rej_batch.status_code == 201
        rejected_16688_code = rej_batch.json()["codes"][0]
        canboso.balance = 5.0
        rej_first = http_client.post(
            f"/api/v1/public/codes/{rejected_16688_code}/redeem",
            json={"preflight_confirmed": True},
        )
        assert rej_first.status_code == 200
        assert rej_first.json()["status"] == "needs_manual_review"
        assert http_client.get(
            f"/api/v1/admin/codes/{rejected_16688_code}",
            headers=ADMIN,
        ).json()["can_reset_unused"] is True
        canboso.balance = 500.0
        channel.allowed = False
        rej_retry = http_client.post(f"/api/v1/admin/codes/{rejected_16688_code}/retry", headers=ADMIN, json={})
        assert rej_retry.status_code == 409
        assert rej_retry.json()["error_code"] == "ORDER_NOT_ELIGIBLE"
        rejected_detail = http_client.get(f"/api/v1/admin/codes/{rejected_16688_code}", headers=ADMIN).json()
        assert rejected_detail["can_reset_unused"] is False
        assert http_client.post(
            f"/api/v1/admin/codes/{rejected_16688_code}/reset-unused",
            headers=ADMIN,
            json={},
        ).status_code == 409

        channel.allowed = True
        retry_purchase_code = issue(http_client)
        canboso.balance = 5.0
        http_client.post(
            f"/api/v1/public/codes/{retry_purchase_code}/redeem",
            json={"buyer_email": "retrytimeout@gmail.com", "buyer_email_confirm": "retrytimeout@gmail.com", "preflight_confirmed": True},
        )
        canboso.balance = 500.0
        monkeypatch.setattr(canboso, "purchase", raise_timeout)
        http_client.post(f"/api/v1/admin/codes/{retry_purchase_code}/retry", headers=ADMIN, json={})
        canboso.balance = 5.0
        http_client.post(f"/api/v1/admin/codes/{retry_purchase_code}/retry", headers=ADMIN, json={})
        retry_detail = http_client.get(f"/api/v1/admin/codes/{retry_purchase_code}", headers=ADMIN).json()
        assert retry_detail["last_error_code"] == "LOW_BALANCE"
        assert retry_detail["can_reset_unused"] is False
        assert http_client.post(
            f"/api/v1/admin/codes/{retry_purchase_code}/reset-unused",
            headers=ADMIN,
            json={},
        ).status_code == 409


def test_exclusive_waiting_fulfillment_metadata_and_activation_toggle(components):
    app, _, _, _, _ = components
    with client(app) as http_client:
        shared_code = issue(http_client, sku="ultra_shared_slot")
        assert http_client.post(
            "/api/v1/admin/codes/NO-SUCH-CODE/exclusive-activated",
            headers=ADMIN,
            json={"activated": True},
        ).status_code == 404
        bad_sku = http_client.post(
            f"/api/v1/admin/codes/{shared_code}/exclusive-activated",
            headers=ADMIN,
            json={"activated": True},
        )
        assert bad_sku.status_code == 409
        assert bad_sku.json()["error_code"] == "WARRANTY_NOT_APPLICABLE"

        code = issue(http_client, sku="ultra_exclusive_admin")
        redeemed = http_client.post(
            f"/api/v1/public/codes/{code}/redeem",
            json={"preflight_confirmed": True},
        )
        assert redeemed.status_code == 200
        body = redeemed.json()
        assert body["status"] == "delivered_pending_warranty"
        assert body["fulfillment"]["eta_text"] == "一般 3-7 小时内完成交付，最晚 24 小时"
        assert body["fulfillment"]["warranty_submitted_at"] is None
        assert body["fulfillment"]["exclusive_activated_at"] is None
        assert body["fulfillment"]["warranty_elapsed_minutes"] is None
        assert set(body["warranty"]) == {"status", "admin_email_masked", "submitted_at", "has_screenshot", "note"}

        before_warranty = http_client.post(
            f"/api/v1/admin/codes/{code}/exclusive-activated",
            headers=ADMIN,
            json={"activated": True},
        )
        assert before_warranty.status_code == 409
        assert before_warranty.json()["error_code"] == "WARRANTY_NOT_ALLOWED"

        submitted = http_client.post(
            f"/api/v1/public/codes/{code}/warranty",
            data={"admin_email": "owner@gmail.com", "admin_email_confirm": "owner@gmail.com", "note": "主号"},
        )
        assert submitted.status_code == 200
        sub_body = submitted.json()
        assert sub_body["status"] == "completed"
        assert set(sub_body["warranty"]) == {"status", "admin_email_masked", "submitted_at", "has_screenshot", "note"}
        assert sub_body["fulfillment"]["warranty_submitted_at"] == sub_body["warranty"]["submitted_at"]
        assert sub_body["fulfillment"]["exclusive_activated_at"] is None
        assert isinstance(sub_body["fulfillment"]["warranty_elapsed_minutes"], int)
        assert sub_body["fulfillment"]["warranty_elapsed_minutes"] >= 0

        activated = http_client.post(
            f"/api/v1/admin/codes/{code}/exclusive-activated",
            headers=ADMIN,
            json={"activated": True, "note": "已同意进组"},
        )
        assert activated.status_code == 200
        act_detail = activated.json()
        activated_at = act_detail["warranty"]["activated_at"]
        assert activated_at
        assert any(ev["reason"] == "exclusive_activated:已同意进组" for ev in act_detail["audit_events"])
        pub_after_act = http_client.get(f"/api/v1/public/codes/{code}").json()
        assert pub_after_act["fulfillment"]["exclusive_activated_at"] == activated_at
        assert set(pub_after_act["warranty"]) == {"status", "admin_email_masked", "submitted_at", "has_screenshot", "note"}

        reverted = http_client.post(
            f"/api/v1/admin/codes/{code}/exclusive-activated",
            headers=ADMIN,
            json={"activated": False, "note": "误点撤销"},
        )
        assert reverted.status_code == 200
        rev_detail = reverted.json()
        assert rev_detail["warranty"]["activated_at"] is None
        assert any(ev["reason"] == "exclusive_activation_reverted:误点撤销" for ev in rev_detail["audit_events"])
        pub_after_rev = http_client.get(f"/api/v1/public/codes/{code}").json()
        assert pub_after_rev["fulfillment"]["exclusive_activated_at"] is None


def test_exclusive_pending_upstream_enters_waiting_fulfillment_and_polls_to_delivery(components, monkeypatch):
    app, _, canboso, _, _ = components
    with client(app) as http_client:
        code = issue(http_client, sku="ultra_exclusive_admin")

        def pending_purchase(idempotency_key, payload):
            order_code = "ORD-EXCL-WAIT"
            canboso.orders[order_code] = {
                "orderCode": order_code,
                "status": "pending",
                "payment": {"amountUsd": 12.0},
            }
            return {
                "order": {"orderCode": order_code, "status": "pending"},
                "payment": {"amountUsd": 12.0},
                "delivery": {},
            }

        monkeypatch.setattr(canboso, "purchase", pending_purchase)
        redeemed = http_client.post(
            f"/api/v1/public/codes/{code}/redeem",
            json={"preflight_confirmed": True},
        )
        assert redeemed.status_code == 200
        body = redeemed.json()
        assert body["status"] == "waiting_fulfillment"
        assert body["delivery"] is None
        assert body["fulfillment"]["eta_text"] == "一般 3-7 小时内完成交付，最晚 24 小时"

        detail = http_client.get(f"/api/v1/admin/codes/{code}", headers=ADMIN).json()
        assert detail["can_reset_unused"] is False
        shanghai_day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        totals = app.state.ledger.canboso_purchase_totals(shanghai_day)
        assert totals["order_count"] == 1
        assert totals["gross_usd"] == 12.0

        canboso.orders["ORD-EXCL-WAIT"]["status"] = "completed"
        canboso.orders["ORD-EXCL-WAIT"]["deliveredAt"] = now_iso()
        canboso.orders["ORD-EXCL-WAIT"]["delivery"] = {"accounts": [{"email": "store-ultra@gmail.com"}]}
        polled = http_client.post(f"/api/v1/admin/codes/{code}/poll", headers=ADMIN)
        assert polled.status_code == 200
        polled_body = polled.json()
        assert polled_body["status"] == "delivered_pending_warranty"
        assert polled_body["delivery"]["account_text"] == "store-ultra@gmail.com"

