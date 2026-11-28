from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.gateways import (
    Channel16688Client,
    DryRun16688,
    DryRunCanboso,
    UpstreamError,
    parse_env_channel16688_restock_rules,
)
from backend.service import Mailer


ADMIN = {"Authorization": "Bearer test-admin"}


def _make_app(tmp_path: Path, *, rules: list[dict] | None = None, enabled: bool = True, channel=None):
    settings = Settings(
        db_path=str(tmp_path / "redeem.sqlite3"),
        storage_dir=str(tmp_path / "private"),
        admin_token="test-admin",
        public_base_url="https://redeem.example",
        execution_mode="dry_run",
        merchant_email="merchant@example.com",
        channel16688_restock_enabled=enabled,
        channel16688_restock_interval_seconds=900,
        channel16688_restock_rules_json=json.dumps(rules or [], ensure_ascii=False),
    )
    canboso = DryRunCanboso(initial_balance=1000)
    chan = channel if channel is not None else DryRun16688()
    mailer = Mailer(settings)
    app = create_app(settings, canboso=canboso, channel=chan, mailer=mailer)
    return app, settings, canboso, chan, mailer


def test_parse_env_channel16688_restock_rules_validation():
    rules = parse_env_channel16688_restock_rules(
        json.dumps(
            [
                {"sku_key": "ultra_shared_slot", "min_stock": 5, "target_stock": 15, "max_push": 20, "enabled": True},
                {"sku_key": "ultra_exclusive_admin", "goods_no": "G10000002", "min_stock": 3, "target_stock": 8},
            ]
        )
    )
    assert len(rules) == 2
    assert rules[0]["goods_no"] == ""
    assert rules[1]["goods_no"] == "G10000002"
    assert rules[1]["max_push"] == 20

    multi = parse_env_channel16688_restock_rules(
        json.dumps(
            [
                {"sku_key": "ultra_shared_slot", "goods_no": "G10000001", "min_stock": 5, "target_stock": 15},
                {"sku_key": "ultra_shared_slot", "goods_no": "G10000003", "min_stock": 3, "target_stock": 10},
            ]
        )
    )
    assert [item["goods_no"] for item in multi] == ["G10000001", "G10000003"]

    with pytest.raises(ValueError):
        parse_env_channel16688_restock_rules('{"sku_key":"ultra_shared_slot"}')
    with pytest.raises(ValueError):
        parse_env_channel16688_restock_rules('[{"sku_key":"ultra_shared_slot","min_stock":10,"target_stock":5}]')
    with pytest.raises(ValueError):
        parse_env_channel16688_restock_rules('[{"sku_key":"ultra_shared_slot","min_stock":true,"target_stock":5}]')
    with pytest.raises(ValueError):
        parse_env_channel16688_restock_rules(
            '[{"sku_key":"ultra_shared_slot","target_stock":5},{"sku_key":"ultra_shared_slot","target_stock":8}]'
        )
    with pytest.raises(ValueError):
        parse_env_channel16688_restock_rules(
            '[{"sku_key":"ultra_shared_slot","goods_no":"G10000001","target_stock":5},{"sku_key":"ultra_exclusive_admin","goods_no":"G10000001","target_stock":8}]'
        )


def test_verify_order_handles_multiline_sold_card_unsold_duplicate_and_fallback():
    client_ = Channel16688Client("https://16688.example", "app-id", "secret")

    # 1. Multi-line sold card matched via stockSecret/list
    calls = []
    responses = [
        {"list": [{"id": 1, "secret_code": "提货链接：https://redeem.example/r/U20S-ABCD-1234\n兑换码：U20S-ABCD-1234", "status": 2, "trade_no": "T100"}]},
        {"identity": "merchant", "goods_no": "G10000001", "cards": [{"code": "提货链接：https://redeem.example/r/U20S-ABCD-1234\n兑换码：U20S-ABCD-1234"}], "status": 5},
        {"list": []},
    ]
    client_._call = lambda path, params: (calls.append((path, params)), responses.pop(0))[1]
    assert client_.verify_order("U20S-ABCD-1234", "G10000001") == {"ok": True, "trade_no": "T100"}
    assert calls[0] == ("openApi/stockSecret/list", {"goods_no": "G10000001", "page_no": 1, "page_size": 20, "keywords": "U20S-ABCD-1234"})

    # 2. Unsold card in stockSecret/list (status=0) is rejected without calling order/list
    calls.clear()
    responses = [
        {"list": [{"id": 2, "secret_code": "提货链接：https://redeem.example/r/U20S-UNSD-0001\n兑换码：U20S-UNSD-0001", "status": 0, "trade_no": ""}]},
    ]
    assert client_.verify_order("U20S-UNSD-0001", "G10000001") == {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
    assert len(calls) == 1

    # 3. Duplicate stock matches rejected
    calls.clear()
    responses = [
        {
            "list": [
                {"id": 3, "secret_code": "U20S-DUP1-0001", "status": 2, "trade_no": "T1"},
                {"id": 4, "secret_code": "U20S-DUP1-0001", "status": 2, "trade_no": "T2"},
            ]
        }
    ]
    assert client_.verify_order("U20S-DUP1-0001", "G10000001") == {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
    assert len(calls) == 1

    # 4. Zero stock matches falls back to order/list
    calls.clear()
    responses = [
        {"list": []},
        {"list": [{"identity": "merchant", "trade_no": "T200"}]},
        {"identity": "merchant", "goods_no": "G10000001", "cards": [{"code": "U20S-FALL-0001"}], "status": 4},
        {"list": []},
    ]
    assert client_.verify_order("U20S-FALL-0001", "G10000001") == {"ok": True, "trade_no": "T200"}
    assert [c[0] for c in calls] == [
        "openApi/stockSecret/list",
        "openApi/order/list",
        "openApi/order/info",
        "openApi/complaint/list",
    ]

    # 5. Active complaint blocks order
    calls.clear()
    responses = [
        {"list": [{"id": 5, "secret_code": "兑换码：U20S-COMP-0001", "status": 2, "trade_no": "T300"}]},
        {"identity": "merchant", "goods_no": "G10000001", "cards": [{"code": "兑换码：U20S-COMP-0001"}], "status": 4},
        {"list": [{"trade_no": "T300", "status": 1, "complaint_no": "CP1"}]},
    ]
    assert client_.verify_order("U20S-COMP-0001", "G10000001") == {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": "T300", "final_rejection": True}

    # 6. Non-merchant identity on order/info rejected
    calls.clear()
    responses = [
        {"list": [{"id": 6, "secret_code": "兑换码：U20S-BUY1-0001", "status": 2, "trade_no": "T400"}]},
        {"identity": "buyer", "goods_no": "G10000001", "cards": [{"code": "兑换码：U20S-BUY1-0001"}], "status": 4},
    ]
    assert client_.verify_order("U20S-BUY1-0001", "G10000001") == {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": "T400"}

    # 7. Null card code and malformed complaint entries handled safely
    calls.clear()
    responses = [
        {"list": [{"id": 7, "secret_code": "兑换码：U20S-SAFE-0001", "status": 2, "trade_no": "T500"}]},
        {"identity": "merchant", "goods_no": "G10000001", "cards": [{"code": None}, None, {"code": "兑换码：U20S-SAFE-0001"}], "status": 4},
        {"list": ["unexpected-str", {"trade_no": "T500", "status": "bad"}]},
    ]
    assert client_.verify_order("U20S-SAFE-0001", "G10000001") == {"ok": True, "trade_no": "T500"}

    # 8. Closed complaint with empty complaint_no fails closed
    calls.clear()
    responses = [
        {"list": [{"id": 8, "secret_code": "兑换码：U20S-SAFE-0002", "status": 2, "trade_no": "T501"}]},
        {"identity": "merchant", "goods_no": "G10000001", "cards": [{"code": "兑换码：U20S-SAFE-0002"}], "status": 4},
        {"list": [{"trade_no": "T501", "status": 2, "complaint_no": ""}]},
    ]
    assert client_.verify_order("U20S-SAFE-0002", "G10000001") == {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": "T501"}


def test_redeem_16688_binds_verified_trade_no(tmp_path: Path):
    app, _, _, chan, _ = _make_app(tmp_path)
    with TestClient(app) as c:
        assert c.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 16.0, "goods_no": "G10000001"}).status_code == 200
        batch = c.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "16688", "count": 1, "batch_label": "test"},
        ).json()
        code = batch["codes"][0]
        assert app.state.ledger.get_code(code)["external_order_id"] == ""

        res = c.post(
            f"/api/v1/public/codes/{code}/redeem",
            json={"buyer_email": "buyer@gmail.com", "preflight_confirmed": True, "channel_order_no": "DRY-16688-" + code[-8:]},
        )
        assert res.status_code == 200
        updated = app.state.ledger.get_code(code)
        assert updated["external_order_id"] == "DRY-16688-" + code[-8:]


def test_channel16688_stock_sold_sync_and_auto_restock(tmp_path: Path):
    rules = [
        {"sku_key": "ultra_shared_slot", "min_stock": 2, "target_stock": 5, "max_push": 10, "enabled": True},
        {"sku_key": "ultra_exclusive_admin", "min_stock": 1, "target_stock": 3, "max_push": 5, "enabled": True},
    ]
    app, _, _, chan, _ = _make_app(tmp_path, rules=rules, enabled=True)
    with TestClient(app) as c:
        assert c.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 16.0, "goods_no": "G10000001"}).status_code == 200
        assert c.patch("/api/v1/admin/settings/skus/ultra_exclusive_admin", headers=ADMIN, json={"max_cost_usd": 72.0, "goods_no": "G10000002"}).status_code == 200

        # 1. Inspect empty stock
        stock = c.get("/api/v1/admin/channel16688/stock", headers=ADMIN)
        assert stock.status_code == 200
        body = stock.json()
        assert body["restock_enabled"] is True
        assert {item["sku_key"]: (item["current_qty"], item["planned_push"]) for item in body["items"]} == {
            "ultra_shared_slot": (0, 5),
            "ultra_exclusive_admin": (0, 3),
        }

        # 2. Dry check does not push codes
        dry = c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": True})
        assert dry.status_code == 200
        dry_body = dry.json()
        assert dry_body["restocked"] == 0
        assert dry_body["total_pushed"] == 0
        assert dry_body["sync_sold"]["skipped"] is True
        assert {item["sku_key"]: (item["action"], item["planned_push"]) for item in dry_body["items"]} == {
            "ultra_shared_slot": ("would_restock", 5),
            "ultra_exclusive_admin": ("would_restock", 3),
        }

        # 3. Live restock populates both goods
        run = c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": False})
        assert run.status_code == 200
        run_body = run.json()
        assert run_body["restocked"] == 2
        assert run_body["total_pushed"] == 8
        assert len(chan.stock["G10000001"]) == 5
        assert len(chan.stock["G10000002"]) == 3

        # 4. Simulate 3 sold cards on G10000001 -> available drops to 2 (<= min_stock=2)
        sold_trades = ["T16688-001", "T16688-002", "T16688-003"]
        for idx, trade_no in enumerate(sold_trades):
            chan.stock["G10000001"][idx]["status"] = 2
            chan.stock["G10000001"][idx]["trade_no"] = trade_no

        # 5. Next restock syncs sold trade_nos and tops up 3 cards back to 5
        run2 = c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": False})
        assert run2.status_code == 200
        run2_body = run2.json()
        assert run2_body["sync_sold"]["bound"] == 3
        items_by_sku = {item["sku_key"]: item for item in run2_body["items"]}
        assert items_by_sku["ultra_shared_slot"]["action"] == "restocked"
        assert items_by_sku["ultra_shared_slot"]["pushed_count"] == 3
        assert items_by_sku["ultra_shared_slot"]["after_qty"] == 5
        assert items_by_sku["ultra_exclusive_admin"]["action"] == "sufficient"

        # Verify local DB reflects dispatched_unused vs in_stock accurately
        stock_after = c.get("/api/v1/admin/channel16688/stock?sku_keys=ultra_shared_slot", headers=ADMIN).json()
        shared = stock_after["items"][0]
        assert shared["current_qty"] == 5
        assert shared["sold_qty"] == 3
        assert shared["local_in_stock_16688"] == 5
        assert shared["local_dispatched_unused_16688"] == 3


def test_channel16688_restock_stock_query_error_does_not_alert(tmp_path: Path):
    class FailingStock16688(DryRun16688):
        def stock_secret_list(self, goods_no, **kwargs):
            raise UpstreamError(502, "仅卡密商品支持查询库存", rejected=True)

    rules = [{"sku_key": "ultra_shared_slot", "min_stock": 5, "target_stock": 15, "enabled": True}]
    app, _, _, _, mailer = _make_app(tmp_path, rules=rules, enabled=True, channel=FailingStock16688())
    sent = []
    mailer.send = lambda subject, body, attachment=None: sent.append((subject, body))
    with TestClient(app) as c:
        assert c.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 16.0, "goods_no": "G10000001"}).status_code == 200
        # Dry check must neither send email nor claim notification dedupe keys
        dry_resp = c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": True, "sync_sold": False})
        assert dry_resp.status_code == 200
        assert dry_resp.json()["items"][0]["action"] == "stock_error"
        assert len(sent) == 0
        resp = c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": False, "sync_sold": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["restocked"] == 0
        assert body["items"][0]["action"] == "stock_error"
        assert sent == []
        c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": False, "sync_sold": False})
        assert sent == []

    class InvalidStock16688(DryRun16688):
        def stock_secret_list(self, goods_no, **kwargs):
            return {"available_quantity": "bad", "list": []}

    app_bad, _, _, _, mailer_bad = _make_app(tmp_path / "bad_stock", rules=rules, enabled=True, channel=InvalidStock16688())
    sent_bad = []
    mailer_bad.send = lambda subject, body, attachment=None: sent_bad.append((subject, body))
    with TestClient(app_bad) as c_bad:
        assert c_bad.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 16.0, "goods_no": "G10000001"}).status_code == 200
        bad_resp = c_bad.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": False, "sync_sold": False})
        assert bad_resp.status_code == 200
        assert bad_resp.json()["items"][0]["action"] == "stock_error"
        assert bad_resp.json()["items"][0]["error"] == "INVALID_STOCK_RESPONSE"
        assert sent_bad == []


def test_channel16688_restock_openapi_pause_does_not_alert_and_recovers(tmp_path: Path):
    paused_message = "系统长时间因爬虫、AI自动化操作OpenApi超负载，已影响平台稳定性，短时间OpenAPI不再恢复"

    class Pausable16688(DryRun16688):
        paused = True

        def stock_secret_list(self, goods_no, **kwargs):
            if self.paused:
                raise UpstreamError(502, paused_message, rejected=True)
            return super().stock_secret_list(goods_no, **kwargs)

    def restock_actions(client: TestClient) -> dict[str, str]:
        response = client.post(
            "/api/v1/admin/channel16688/restock",
            headers=ADMIN,
            json={"dry_check": False, "sync_sold": False},
        )
        assert response.status_code == 200
        return {item["goods_no"]: item["action"] for item in response.json()["items"]}

    goods_by_sku = {"ultra_shared_slot": "G10000001", "ultra_exclusive_admin": "G10000002"}
    rules = [
        {"sku_key": sku_key, "goods_no": goods_no, "min_stock": 1, "target_stock": 2, "enabled": True}
        for sku_key, goods_no in goods_by_sku.items()
    ]
    channel = Pausable16688()
    for goods_no in goods_by_sku.values():
        channel.stock_secret_add(goods_no, "TEST-CARD-1\n\nTEST-CARD-2")
    app, _, _, _, mailer = _make_app(tmp_path, rules=rules, channel=channel)
    sent = []
    mailer.send = lambda subject, body, attachment=None: sent.append((subject, body))

    stock_errors = dict.fromkeys(goods_by_sku.values(), "stock_error")
    with TestClient(app) as client:
        assert restock_actions(client) == stock_errors
        assert sent == []
        assert restock_actions(client) == stock_errors
        assert sent == []

        channel.paused = False
        assert restock_actions(client) == dict.fromkeys(goods_by_sku.values(), "sufficient")
        assert sent == []

        channel.paused = True
        assert restock_actions(client) == stock_errors
        assert sent == []


def test_16688_unverified_attempt_does_not_block_the_owner(tmp_path: Path):
    class Strange16688(DryRun16688):
        def __init__(self):
            super().__init__()
            self.mode = "unknown"

        def verify_order(self, code: str, goods_no: str):
            if self.mode == "unknown":
                return {"ok": False, "error": "UNKNOWN_STATE"}
            if self.mode == "not_eligible":
                return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
            return {"ok": True, "trade_no": "T-RETRY-OK"}

    chan = Strange16688()
    app, _, canboso, _, _ = _make_app(tmp_path, channel=chan)
    with TestClient(app) as c:
        assert c.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 16.0, "goods_no": "G10000001"}).status_code == 200
        batch = c.post(
            "/api/v1/admin/codes/batch",
            headers=ADMIN,
            json={"sku_key": "ultra_shared_slot", "channel": "16688", "count": 1, "batch_label": "test"},
        ).json()
        code = batch["codes"][0]
        endpoint = f"/api/v1/public/codes/{code}/redeem"
        payload = {"buyer_email": "buyer@gmail.com", "preflight_confirmed": True}
        res = c.post(
            endpoint,
            json={**payload, "buyer_email": "other@gmail.com"},
        )
        assert res.status_code == 503 and res.json()["error_code"] == "CHANNEL16688_VERIFICATION_FAILED"
        assert app.state.ledger.get_code(code)["status"] == "unused"
        assert not app.state.ledger.get_code(code)["buyer_email"]
        assert canboso.charge_count == 0

        chan.mode = "not_eligible"
        retry_blocked = c.post(endpoint, json=payload)
        assert retry_blocked.status_code == 409
        assert retry_blocked.json()["error_code"] == "ORDER_NOT_ELIGIBLE"
        assert canboso.charge_count == 0
        assert app.state.ledger.get_code(code)["status"] == "unused"

        chan.mode = "ok"
        retry_ok = c.post(endpoint, json=payload)
        assert retry_ok.status_code == 200
        assert canboso.charge_count == 1
        updated = app.state.ledger.get_code(code)
        assert updated["external_order_id"] == "T-RETRY-OK"
        assert updated["buyer_email"] == "buyer@gmail.com"
        assert updated["status"] == "waiting_fulfillment"


def test_channel16688_sold_sync_reports_truncated_and_handles_blank_or_bad_qty(tmp_path: Path):
    class QuirkyStock16688(DryRun16688):
        def __init__(self):
            super().__init__()
            self.stock_mode = "blank"

        def stock_secret_list(self, goods_no, *, status=None, keywords="", page_no=1, page_size=20):
            if status == 1:
                return {
                    "goods_no": goods_no,
                    "total": 10,
                    "list": [
                        {"id": page_no, "secret_code": f"U20S-SYNC-000{page_no}", "status": 2, "trade_no": f"TSYNC-{page_no}"}
                    ] * page_size,
                }
            if self.stock_mode == "blank":
                return {"goods_no": goods_no, "available_quantity": "", "total": "4", "locked_quantity": "", "sold_quantity": None, "list": []}
            return {"goods_no": goods_no, "available_quantity": "not-a-number", "total": "bad", "list": []}

    rules = [{"sku_key": "ultra_shared_slot", "min_stock": 5, "target_stock": 10, "enabled": True}]
    chan = QuirkyStock16688()
    app, _, _, _, _ = _make_app(tmp_path, rules=rules, enabled=True, channel=chan)
    with TestClient(app) as c:
        assert c.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 16.0, "goods_no": "G10000001"}).status_code == 200

        sync_res = app.state.service.sync_channel16688_sold_codes(sku_keys=["ultra_shared_slot"], max_pages=1, page_size=2)
        assert sync_res["truncated"] is True
        assert sync_res["items"][0]["truncated"] is True

        # Blank available_quantity falls back cleanly to numeric total=4
        stock_blank = c.get("/api/v1/admin/channel16688/stock", headers=ADMIN).json()
        assert stock_blank["items"][0]["current_qty"] == 4
        assert stock_blank["items"][0]["stock_found"] is True

        # Non-numeric garbage response fails safely as stock_error instead of raising ValueError or over-restocking
        chan.stock_mode = "bad"
        stock_bad = c.get("/api/v1/admin/channel16688/stock", headers=ADMIN).json()
        assert stock_bad["items"][0]["current_qty"] is None
        assert stock_bad["items"][0]["stock_found"] is False
        assert stock_bad["items"][0]["error"] == "INVALID_STOCK_RESPONSE"

        restock_bad = c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": True, "sync_sold": False}).json()
        assert restock_bad["items"][0]["action"] == "stock_error"
        assert restock_bad["total_pushed"] == 0


def test_channel16688_multi_goods_per_sku_restock_sync_and_redeem(tmp_path: Path):
    class RecordingMultiGoods16688(DryRun16688):
        def __init__(self):
            super().__init__()
            self.verify_calls: list[tuple[str, str]] = []

        def verify_order(self, code: str, goods_no: str) -> dict:
            self.verify_calls.append((code, goods_no))
            for item in self.stock.get(goods_no, []):
                if code in str(item.get("secret_code") or "") and int(item.get("status", -1)) == 2 and item.get("trade_no"):
                    return {"ok": True, "trade_no": str(item["trade_no"])}
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}

    rules = [
        {"sku_key": "ultra_shared_slot", "goods_no": "G10000001", "min_stock": 2, "target_stock": 4, "max_push": 10, "enabled": True},
        {"sku_key": "ultra_shared_slot", "goods_no": "G10000003", "min_stock": 2, "target_stock": 4, "max_push": 10, "enabled": True},
        {"sku_key": "ultra_shared_slot", "min_stock": 2, "target_stock": 4, "max_push": 10, "enabled": True},
        {"sku_key": "ultra_exclusive_admin", "goods_no": "G10000002", "min_stock": 1, "target_stock": 2, "max_push": 10, "enabled": True},
    ]
    chan = RecordingMultiGoods16688()
    app, _, canboso, _, _ = _make_app(tmp_path, rules=rules, enabled=True, channel=chan)
    with TestClient(app) as c:
        assert c.patch("/api/v1/admin/settings/skus/ultra_shared_slot", headers=ADMIN, json={"max_cost_usd": 16.0, "goods_no": "G10000001"}).status_code == 200
        assert c.patch("/api/v1/admin/settings/skus/ultra_exclusive_admin", headers=ADMIN, json={"max_cost_usd": 65.0, "goods_no": "G10000002"}).status_code == 200

        # Duplicate default rule resolving to G10000001 is deduplicated cleanly
        active_rules = app.state.service.channel16688_restock_rules(enabled_only=True)
        assert [rule["goods_no"] for rule in active_rules] == ["G10000001", "G10000003", "G10000002"]

        # Initial restock pushes cards to all 3 goods independently
        first = c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": False})
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["checked"] == 3
        assert first_body["restocked"] == 3
        assert first_body["total_pushed"] == 10
        assert len(chan.stock["G10000001"]) == 4
        assert len(chan.stock["G10000003"]) == 4
        assert len(chan.stock["G10000002"]) == 2

        # Simulate 2 sold cards on secondary goods G10000003 (remaining=2 <= min_stock=2)
        chan.stock["G10000003"][0]["status"] = 2
        chan.stock["G10000003"][0]["trade_no"] = "T-SEC-001"
        chan.stock["G10000003"][1]["status"] = 2
        chan.stock["G10000003"][1]["trade_no"] = "T-SEC-002"

        # Next restock syncs sold trade_nos on G10000003 and tops up only G10000003
        second = c.post("/api/v1/admin/channel16688/restock", headers=ADMIN, json={"dry_check": False})
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["sync_sold"]["bound"] == 2
        items_by_goods = {item["goods_no"]: item for item in second_body["items"]}
        assert items_by_goods["G10000001"]["action"] == "sufficient"
        assert items_by_goods["G10000003"]["action"] == "restocked"
        assert items_by_goods["G10000003"]["pushed_count"] == 2
        assert items_by_goods["G10000003"]["after_qty"] == 4
        assert items_by_goods["G10000002"]["action"] == "sufficient"

        # Stock endpoint reports per-goods_no local counts cleanly
        stock_body = c.get("/api/v1/admin/channel16688/stock?sku_keys=ultra_shared_slot", headers=ADMIN).json()
        stock_by_goods = {item["goods_no"]: item for item in stock_body["items"]}
        assert set(stock_by_goods.keys()) == {"G10000001", "G10000003"}
        assert stock_by_goods["G10000001"]["local_in_stock_16688"] == 4
        assert stock_by_goods["G10000001"]["local_dispatched_unused_16688"] == 0
        assert stock_by_goods["G10000003"]["local_in_stock_16688"] == 4
        assert stock_by_goods["G10000003"]["local_dispatched_unused_16688"] == 2

        # Redeeming a sold code from secondary goods G10000003 verifies against G10000003
        sold_secret = str(chan.stock["G10000003"][0]["secret_code"])
        sold_code = sold_secret.rsplit("兑换码：", 1)[-1].strip()
        redeem_res = c.post(
            f"/api/v1/public/codes/{sold_code}/redeem",
            json={"buyer_email": "buyer@gmail.com", "preflight_confirmed": True, "channel_order_no": "T-SEC-001"},
        )
        assert redeem_res.status_code == 200
        assert redeem_res.json()["status"] == "waiting_fulfillment"
        assert chan.verify_calls == [(sold_code, "G10000003")]
        assert canboso.charge_count == 1
