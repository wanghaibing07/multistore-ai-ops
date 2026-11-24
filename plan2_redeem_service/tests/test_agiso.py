from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agiso import (
    AgisoClient,
    AgisoError,
    DryRunAgisoClient,
    match_agiso_sku,
    sign_agiso_api_params,
    verify_agiso_push_signature,
)
from backend.app import create_app
from backend.config import Settings
from backend.gateways import DryRunCanboso
from backend.service import Mailer
from tests.conftest import Fake16688


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
TEST_APP_SECRET = "demo_agiso_secret_value_0123456789"
TEST_ACCESS_TOKEN = "demo_agiso_access_token_abcdef"


def make_push_sign(raw_json: str, timestamp: str, secret: str = TEST_APP_SECRET) -> str:
    raw = f"{secret}json{raw_json}timestamp{timestamp}{secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().lower()


def build_agiso_env(tmp_path: Path, **setting_overrides):
    defaults = dict(
        db_path=str(tmp_path / "redeem.sqlite3"),
        storage_dir=str(tmp_path / "private"),
        admin_token="test-admin",
        public_base_url="http://testserver",
        execution_mode="dry_run",
        merchant_email="merchant@example.com",
        agiso_app_id="2026000000000000001",
        agiso_app_secret=TEST_APP_SECRET,
        agiso_access_token=TEST_ACCESS_TOKEN,
    )
    defaults.update(setting_overrides)
    settings = Settings(**defaults)
    canboso = DryRunCanboso(initial_balance=1000)
    channel = Fake16688()
    mailer = Mailer(settings)
    agiso = DryRunAgisoClient()
    app = create_app(settings, canboso=canboso, channel=channel, mailer=mailer, agiso=agiso)
    return app, settings, canboso, channel, mailer, agiso


def post_webhook(
    client: TestClient,
    payload: dict,
    *,
    aopic: int = 2,
    from_platform: str = "TbAlds",
    timestamp: str | None = None,
    sign: str | None = None,
    secret: str = TEST_APP_SECRET,
):
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ts_value = timestamp if timestamp is not None else str(int(time.time()))
    sign_value = sign if sign is not None else make_push_sign(raw_json, ts_value, secret)
    return client.post(
        "/api/v1/webhooks/agiso",
        params={
            "fromPlatform": from_platform,
            "timestamp": ts_value,
            "aopic": str(aopic),
            "sign": sign_value,
        },
        data={"json": raw_json},
    )


def test_sign_agiso_api_params_and_verify_push_signature():
    params = {
        "tid": "1001",
        "AldsType": 1,
        "flag": True,
        "disabled": False,
        "ignored_none": None,
        "sign": "old-sign",
    }
    expected_raw = f"{TEST_APP_SECRET}AldsType1disabled0flag1tid1001{TEST_APP_SECRET}"
    expected_md5 = hashlib.md5(expected_raw.encode("utf-8")).hexdigest().lower()
    assert sign_agiso_api_params(params, TEST_APP_SECRET) == expected_md5

    sample_json = '{"Tid":1001,"Created":"2026-11-25 12:00:00"}'
    sample_ts = "1795500000"
    good_sign = make_push_sign(sample_json, sample_ts)
    assert verify_agiso_push_signature(sample_json, sample_ts, good_sign, TEST_APP_SECRET)
    assert verify_agiso_push_signature(sample_json, sample_ts, good_sign.upper(), TEST_APP_SECRET)
    assert not verify_agiso_push_signature(sample_json, sample_ts, "deadbeef", TEST_APP_SECRET)
    assert not verify_agiso_push_signature(sample_json, "", good_sign, TEST_APP_SECRET)
    assert not verify_agiso_push_signature(sample_json, sample_ts, good_sign, "")


def test_webhook_validation_plus_decoding_and_open_simulation(tmp_path: Path):
    unconfigured_app, *_ = build_agiso_env(tmp_path / "unconf", agiso_app_secret="")
    with TestClient(unconfigured_app) as client:
        resp = post_webhook(client, {"Tid": "1"}, aopic=2)
        assert resp.status_code == 503
        assert resp.text == "服务未配置"

    app, _, _, _, mailer, _ = build_agiso_env(tmp_path / "conf")
    with TestClient(app) as client:
        bad_ts = post_webhook(client, {"Tid": "1"}, timestamp="not-a-number")
        assert bad_ts.status_code == 403
        assert bad_ts.text == "时间戳无效"

        expired_ts = str(int(time.time()) - 3600)
        expired = post_webhook(client, {"Tid": "1"}, timestamp=expired_ts)
        assert expired.status_code == 403
        assert expired.text == "时间戳无效"

        bad_sign = post_webhook(client, {"Tid": "1"}, sign="0" * 32)
        assert bad_sign.status_code == 403
        assert bad_sign.text == "验签失败"

        decoded_json = '{"Tid":90001,"Created":"2026-11-25 15:00:00","Status":"WAIT_SELLER_SEND_GOODS"}'
        encoded_body = f"json={quote_plus(decoded_json)}"
        assert "+" in encoded_body
        ts_now = str(int(time.time()))
        valid_sign = make_push_sign(decoded_json, ts_now)
        sim_resp = client.post(
            "/api/v1/webhooks/agiso",
            params={
                "fromPlatform": "Open",
                "timestamp": ts_now,
                "aopic": "2",
                "sign": valid_sign,
            },
            content=encoded_body.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert sim_resp.status_code == 200
        assert sim_resp.text == ""
        assert len(mailer.sent) == 0

        events_resp = client.get("/api/v1/admin/agiso/events", params={"tid": "90001"}, headers=ADMIN_HEADERS)
        assert events_resp.status_code == 200
        assert events_resp.json()["items"][0]["status"] == "simulated_verified"
        assert client.get("/api/v1/admin/codes", headers=ADMIN_HEADERS).json()["total"] == 0


def test_sku_matching_priority_and_unmapped_safety():
    rules = [
        {"sku_key": "ultra_shared_slot", "num_iid": "9900000000001", "enabled": True},
        {"sku_key": "ultra_exclusive_admin", "sku_props_contains": "独享", "enabled": True},
        {"sku_key": "ultra_shared_slot", "outer_iid": "OUT-ITEM-1", "enabled": True},
        {"sku_key": "ultra_exclusive_admin", "num_iid": "9900000000001", "sku_props_contains": "ultra", "enabled": True},
        {"sku_key": "ultra_shared_slot", "sku_id": "SKU-40", "enabled": True},
        {"sku_key": "ultra_exclusive_admin", "outer_sku_id": "OUT-SKU-50", "enabled": True},
    ]
    valid_keys = {"ultra_shared_slot", "ultra_exclusive_admin"}

    assert match_agiso_sku(
        {"NumIid": "9900000000001", "SkuId": "SKU-40", "OuterSkuId": "OUT-SKU-50", "SkuPropertiesName": "Ultra 独享"},
        rules,
        valid_sku_keys=valid_keys,
    )["sku_key"] == "ultra_exclusive_admin"

    assert match_agiso_sku(
        {"NumIid": "9900000000001", "SkuId": "SKU-40", "SkuPropertiesName": "Ultra 独享"},
        rules,
        valid_sku_keys=valid_keys,
    )["sku_key"] == "ultra_shared_slot"

    assert match_agiso_sku(
        {"NumIid": "9900000000001", "OuterIid": "OUT-ITEM-1", "SkuPropertiesName": "颜色分类:Ultra共享"},
        rules,
        valid_sku_keys=valid_keys,
    )["sku_key"] == "ultra_exclusive_admin"

    strict_rules = [
        {"sku_key": "ultra_shared_slot", "num_iid": "9900000000001", "sku_props_contains": "ultra", "enabled": True},
    ]
    pro_order = {"NumIid": "9900000000001", "SkuId": "SKU-PRO", "SkuPropertiesName": "颜色分类:Pro 1个月家庭组"}
    assert match_agiso_sku(pro_order, strict_rules, valid_sku_keys=valid_keys) is None


def test_webhook_aopic_2_and_2097152_fulfillment_and_no_success_email(tmp_path: Path):
    app, _, _, _, mailer, agiso = build_agiso_env(tmp_path)
    with TestClient(app) as client:
        assert client.post(
            "/api/v1/admin/agiso/sku-mappings",
            json={"sku_key": "ultra_shared_slot", "num_iid": "9900000000002", "sku_id": "SKU-SHARED"},
            headers=ADMIN_HEADERS,
        ).status_code == 201
        assert client.post(
            "/api/v1/admin/agiso/sku-mappings",
            json={"sku_key": "ultra_exclusive_admin", "num_iid": "9900000000002", "sku_id": "SKU-PRIVATE"},
            headers=ADMIN_HEADERS,
        ).status_code == 201

        ignored = post_webhook(
            client,
            {"Tid": "5000", "Status": "WAIT_BUYER_PAY", "Orders": [{"Oid": "5001", "NumIid": "9900000000002", "SkuId": "SKU-SHARED", "Num": 1}]},
            aopic=2,
        )
        assert ignored.status_code == 200
        assert ignored.text == ""
        assert len(agiso.calls) == 0

        trade_payload = {
            "Tid": "5000",
            "Status": "WAIT_SELLER_SEND_GOODS",
            "BuyerNick": "buyer_demo",
            "Orders": [
                {"Oid": "5001", "NumIid": "9900000000002", "SkuId": "SKU-SHARED", "Num": 2},
                {"Oid": "5002", "NumIid": "9900000000002", "SkuId": "SKU-PRIVATE", "Num": 1},
                {"Oid": "5003", "NumIid": "9900000000001", "SkuId": "SKU-PRO-2490", "Num": 1},
            ],
        }
        resp = post_webhook(client, trade_payload, aopic=2)
        assert resp.status_code == 200
        assert resp.text == ""
        assert len(mailer.sent) == 0

        methods = [call["method"] for call in agiso.calls]
        assert methods == ["create_order_result", "logistics_dummy_send", "send_tb_app_card"]
        result_content = agiso.calls[0]["content"]
        assert len(result_content) == 2
        cards_oid_5001 = result_content[0]["CardPwdContent"][0]["CardPwdArr"]
        cards_oid_5002 = result_content[1]["CardPwdContent"][0]["CardPwdArr"]
        assert len(cards_oid_5001) == 2
        assert len(cards_oid_5002) == 1
        assert all(entry["p"].startswith("U20S-") for entry in cards_oid_5001)
        assert cards_oid_5002[0]["p"].startswith("U20E-")

        agiso.calls.clear()
        agiso.trades["6000"] = {
            "Tid": "6000",
            "Status": "WAIT_SELLER_SEND_GOODS",
            "BuyerNick": "buyer_simple",
            "Orders": [
                {"Oid": "6001", "NumIid": "9900000000002", "SkuId": "SKU-SHARED", "Num": 1},
            ],
        }
        simple_resp = post_webhook(client, {"Tid": "6000", "Status": "WAIT_SELLER_SEND_GOODS"}, aopic=2097152)
        assert simple_resp.status_code == 200
        assert simple_resp.text == ""
        assert [call["method"] for call in agiso.calls] == [
            "trade_info",
            "create_order_result",
            "logistics_dummy_send",
            "send_tb_app_card",
        ]
        assert len(mailer.sent) == 0

        agiso.calls.clear()
        duplicate = post_webhook(client, {"Tid": "6000", "Status": "WAIT_SELLER_SEND_GOODS"}, aopic=2097152)
        assert duplicate.status_code == 200
        assert [call["method"] for call in agiso.calls] == ["trade_info"]


def test_concurrent_duplicate_webhooks_idempotent(tmp_path: Path):
    app, _, _, _, mailer, agiso = build_agiso_env(tmp_path)
    lock = threading.Lock()
    original_create_result = agiso.create_order_result

    def slow_create_order_result(tid: str, content, alds_type: int = 1):
        time.sleep(0.08)
        with lock:
            return original_create_result(tid, content, alds_type=alds_type)

    agiso.create_order_result = slow_create_order_result

    with TestClient(app) as client:
        client.post(
            "/api/v1/admin/agiso/sku-mappings",
            json={"sku_key": "ultra_shared_slot", "num_iid": "9900000000002", "sku_id": "SKU-CONC"},
            headers=ADMIN_HEADERS,
        )
        payload = {
            "Tid": "7000",
            "Status": "WAIT_SELLER_SEND_GOODS",
            "Orders": [{"Oid": "7001", "NumIid": "9900000000002", "SkuId": "SKU-CONC", "Num": 1}],
        }
        ts_now = str(int(time.time()))
        raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        sign = make_push_sign(raw_json, ts_now)

        def worker():
            return app.state.service.handle_agiso_webhook(
                from_platform="TbAlds",
                aopic=2,
                payload=payload,
                timestamp=ts_now,
                sign=sign,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: worker(), range(6)))

        assert all(status_code == 200 and body == "" for status_code, body, _ in results)
        deliveries = app.state.ledger.list_agiso_deliveries_by_tid("7000")
        assert len(deliveries) == 1
        assert deliveries[0]["delivery_status"] == "delivered"
        assert sum(1 for call in agiso.calls if call["method"] == "create_order_result") == 1
        assert sum(1 for call in agiso.calls if call["method"] == "logistics_dummy_send") == 1
        assert len(mailer.sent) == 0


def test_partial_delivery_failure_alerts_and_retry_completes_remaining_step(tmp_path: Path):
    app, _, _, _, mailer, agiso = build_agiso_env(tmp_path, agiso_auto_send_ww_msg=True)
    agiso.fail_methods["send_tb_app_card"] = AgisoError("Agiso error 50: 手淘卡片发送失败", error_code=50, status_code=200)

    with TestClient(app) as client:
        client.post(
            "/api/v1/admin/agiso/sku-mappings",
            json={"sku_key": "ultra_shared_slot", "num_iid": "9900000000002", "sku_id": "SKU-PARTIAL"},
            headers=ADMIN_HEADERS,
        )
        resp = post_webhook(
            client,
            {
                "Tid": "7500",
                "Status": "WAIT_SELLER_SEND_GOODS",
                "Orders": [{"Oid": "7501", "NumIid": "9900000000002", "SkuId": "SKU-PARTIAL", "Num": 1}],
            },
            aopic=2,
        )
        assert resp.status_code == 200
        assert len(mailer.sent) == 1
        assert "异常" in mailer.sent[0]["subject"]

        deliveries = app.state.ledger.list_agiso_deliveries_by_tid("7500")
        assert len(deliveries) == 1
        assert deliveries[0]["delivery_status"] == "partial"
        assert deliveries[0]["result_created"] == 1
        assert deliveries[0]["dummy_sent"] == 1
        assert deliveries[0]["card_sent"] == 0
        assert deliveries[0]["ww_sent"] == 1
        issued_code = deliveries[0]["code"]

        agiso.fail_methods.clear()
        agiso.calls.clear()
        retry_resp = client.post("/api/v1/admin/agiso/orders/7500/retry", json={}, headers=ADMIN_HEADERS)
        assert retry_resp.status_code == 200
        retry_data = retry_resp.json()
        assert retry_data["status"] == "delivered"
        assert retry_data["codes"] == [issued_code]
        assert [call["method"] for call in agiso.calls] == ["send_tb_app_card"]


def test_refund_auto_revoke_unused_alert_used_and_suborder_isolation(tmp_path: Path):
    app, _, _, _, mailer, _ = build_agiso_env(tmp_path)
    with TestClient(app) as client:
        client.post(
            "/api/v1/admin/agiso/sku-mappings",
            json={"sku_key": "ultra_shared_slot", "num_iid": "9900000000002", "sku_id": "SKU-REFUND"},
            headers=ADMIN_HEADERS,
        )
        post_webhook(
            client,
            {
                "Tid": "8000",
                "Status": "WAIT_SELLER_SEND_GOODS",
                "Orders": [
                    {"Oid": "8001", "NumIid": "9900000000002", "SkuId": "SKU-REFUND", "Num": 1},
                    {"Oid": "8002", "NumIid": "9900000000001", "SkuId": "SKU-PRO-UNMAPPED", "Num": 1},
                ],
            },
            aopic=2,
        )
        unused_code = app.state.ledger.list_agiso_deliveries_by_tid("8000")[0]["code"]

        unmapped_refund = post_webhook(
            client,
            {"Tid": "8000", "Oid": "8002", "RefundId": "RF-UNMAPPED", "RefundFee": "24.90"},
            aopic=256,
        )
        assert unmapped_refund.status_code == 200
        assert app.state.ledger.get_code(unused_code)["status"] == "unused"
        assert len(mailer.sent) == 0

        valid_refund = post_webhook(
            client,
            {"Tid": "8000", "Oid": "8001", "RefundId": "RF-UNUSED", "RefundFee": "199.90"},
            aopic=256,
        )
        assert valid_refund.status_code == 200
        assert app.state.ledger.get_code(unused_code)["status"] == "revoked"
        assert app.state.ledger.list_agiso_deliveries_by_tid("8000")[0]["delivery_status"] == "revoked"
        assert len(mailer.sent) == 0

        replay_after_revoke = app.state.service.handle_agiso_webhook(
            from_platform="TbAlds",
            aopic=2,
            payload={
                "Tid": "8000",
                "Status": "WAIT_SELLER_SEND_GOODS",
                "Orders": [{"Oid": "8001", "NumIid": "9900000000002", "SkuId": "SKU-REFUND", "Num": 1}],
            },
            timestamp=str(int(time.time())),
            sign="dummy",
        )
        assert replay_after_revoke[2]["status"] == "already_revoked"

        post_webhook(
            client,
            {
                "Tid": "8100",
                "Status": "WAIT_SELLER_SEND_GOODS",
                "Orders": [{"Oid": "8101", "NumIid": "9900000000002", "SkuId": "SKU-REFUND", "Num": 1}],
            },
            aopic=2,
        )
        used_code = app.state.ledger.list_agiso_deliveries_by_tid("8100")[0]["code"]
        redeem_resp = client.post(
            f"/api/v1/public/codes/{used_code}/redeem",
            json={
                "buyer_email": "buyer8100@gmail.com",
                "buyer_email_confirm": "buyer8100@gmail.com",
                "preflight_confirmed": True,
            },
        )
        assert redeem_resp.status_code == 200
        assert len(mailer.sent) == 0

        used_refund = post_webhook(
            client,
            {"Tid": "8100", "Oid": "8101", "RefundId": "RF-USED", "RefundFee": "199.90"},
            aopic=65536,
        )
        assert used_refund.status_code == 200
        assert app.state.ledger.get_code(used_code)["status"] != "revoked"
        assert len(mailer.sent) == 1
        assert "已使用" in mailer.sent[0]["subject"]


def test_aopic_2048_binds_external_order_id(tmp_path: Path):
    app, *_ = build_agiso_env(tmp_path)
    with TestClient(app) as client:
        code_shared = client.post(
            "/api/v1/admin/codes/issue",
            json={"sku_key": "ultra_shared_slot", "channel": "taobao"},
            headers=ADMIN_HEADERS,
        ).json()["code"]
        code_private = client.post(
            "/api/v1/admin/codes/issue",
            json={"sku_key": "ultra_exclusive_admin", "channel": "taobao"},
            headers=ADMIN_HEADERS,
        ).json()["code"]

        payload = {
            "Tid": "9200",
            "Orders": [
                {
                    "Oid": "9201",
                    "SendCards": [
                        {
                            "CpcId": 1001,
                            "Cards": [
                                {"Card": f"http://testserver/r/{code_shared}", "Pwd": code_shared},
                                {"Card": f"http://testserver/r/{code_private}", "Pwd": code_private},
                            ],
                        }
                    ],
                }
            ],
        }
        resp = post_webhook(client, payload, aopic=2048)
        assert resp.status_code == 200
        assert app.state.ledger.get_code(code_shared)["external_order_id"] == "9200"
        assert app.state.ledger.get_code(code_private)["external_order_id"] == "9200"

        conflict_payload = {**payload, "Tid": "9201"}
        conflict_resp = post_webhook(client, conflict_payload, aopic=2048)
        assert conflict_resp.status_code == 200
        assert app.state.ledger.get_code(code_shared)["external_order_id"] == "9200"
        events = client.get("/api/v1/admin/agiso/events", params={"tid": "9201"}, headers=ADMIN_HEADERS).json()["items"]
        assert events[0]["status"] == "cards_conflict"


def test_admin_agiso_endpoints_security_sync_remote_skus_and_push_cards(tmp_path: Path):
    env_rules = json.dumps([{"sku_key": "ultra_shared_slot", "outer_sku_id": "ENV-ULTRA"}])
    app, _, _, _, _, agiso = build_agiso_env(tmp_path, agiso_sku_rules_json=env_rules)
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/agiso/status").status_code == 401

        status_resp = client.get("/api/v1/admin/agiso/status", headers=ADMIN_HEADERS)
        assert status_resp.status_code == 200
        assert TEST_APP_SECRET not in status_resp.text
        assert TEST_ACCESS_TOKEN not in status_resp.text
        status_body = status_resp.json()
        assert status_body["configured"] is True
        assert status_body["has_app_secret"] is True
        assert status_body["has_access_token"] is True
        assert status_body["sku_mappings_count"] == 1

        assert client.post(
            "/api/v1/admin/agiso/sku-mappings",
            json={"sku_key": "ultra_shared_slot"},
            headers=ADMIN_HEADERS,
        ).status_code == 422
        assert client.post(
            "/api/v1/admin/agiso/sku-mappings",
            json={"sku_key": "nonexistent_sku", "num_iid": "123"},
            headers=ADMIN_HEADERS,
        ).status_code == 422

        created = client.post(
            "/api/v1/admin/agiso/sku-mappings",
            json={"sku_key": "ultra_shared_slot", "num_iid": "9900000000002", "sku_id": "SKU-SYNC"},
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 201
        mapping_id = created.json()["mapping_id"]
        listed = client.get("/api/v1/admin/agiso/sku-mappings", headers=ADMIN_HEADERS).json()
        assert len(listed["items"]) == 1
        assert len(listed["env_rules"]) == 1

        agiso.pending_trades = [
            {
                "Tid": "9500",
                "Status": "WAIT_SELLER_SEND_GOODS",
                "Orders": [{"Oid": "9501", "NumIid": "9900000000002", "SkuId": "SKU-SYNC", "Num": 1}],
            },
            {
                "Tid": "9502",
                "Status": "TRADE_FINISHED",
                "Orders": [{"Oid": "9503", "NumIid": "9900000000002", "SkuId": "SKU-SYNC", "Num": 1}],
            },
        ]
        sync_resp = client.post("/api/v1/admin/agiso/sync-orders", json={"lookback_minutes": 30}, headers=ADMIN_HEADERS)
        assert sync_resp.status_code == 200
        sync_body = sync_resp.json()
        assert sync_body["fetched"] == 2
        assert sync_body["fulfilled"] == 1
        assert sync_body["skipped"] == 1

        agiso.remote_skus = [{"NumIid": "9900000000002", "SkuId": "SKU-SYNC", "PropertiesName": "Ultra"}]
        remote_resp = client.get(
            "/api/v1/admin/agiso/remote-skus",
            params={"num_iids": "9900000000002"},
            headers=ADMIN_HEADERS,
        )
        assert remote_resp.status_code == 200
        assert len(remote_resp.json()["items"]) == 1
        too_many = ",".join(str(idx) for idx in range(41))
        assert client.get(
            "/api/v1/admin/agiso/remote-skus",
            params={"num_iids": too_many},
            headers=ADMIN_HEADERS,
        ).status_code == 422

        push_ok = client.post(
            "/api/v1/admin/agiso/push-cards",
            json={"sku_key": "ultra_shared_slot", "cpc_id": 888, "count": 2, "batch_label": "batch-ok"},
            headers=ADMIN_HEADERS,
        )
        assert push_ok.status_code == 201
        assert len(push_ok.json()["codes"]) == 2
        assert push_ok.json()["agiso_result"]["AddSuccessCount"] == 2

        agiso.fail_methods["card_pwd_add"] = AgisoError("Agiso error 12: 卡种不存在", error_code=12, status_code=200, transient=False)
        before_codes = {row["code"] for row in client.get("/api/v1/admin/codes", params={"page_size": 100}, headers=ADMIN_HEADERS).json()["items"]}
        push_fail = client.post(
            "/api/v1/admin/agiso/push-cards",
            json={"sku_key": "ultra_shared_slot", "cpc_id": 999, "count": 2, "batch_label": "batch-fail"},
            headers=ADMIN_HEADERS,
        )
        assert push_fail.status_code == 503
        after_items = client.get("/api/v1/admin/codes", params={"page_size": 100}, headers=ADMIN_HEADERS).json()["items"]
        rolled_back = [row for row in after_items if row["code"] not in before_codes]
        assert len(rolled_back) == 2
        assert all(row["status"] == "revoked" for row in rolled_back)

        agiso.fail_methods["card_pwd_add"] = AgisoError("Agiso timeout", status_code=504, transient=True)
        before_transient = {row["code"] for row in client.get("/api/v1/admin/codes", params={"page_size": 100}, headers=ADMIN_HEADERS).json()["items"]}
        push_timeout = client.post(
            "/api/v1/admin/agiso/push-cards",
            json={"sku_key": "ultra_shared_slot", "cpc_id": 999, "count": 1, "batch_label": "batch-timeout"},
            headers=ADMIN_HEADERS,
        )
        assert push_timeout.status_code == 503
        after_transient = client.get("/api/v1/admin/codes", params={"page_size": 100}, headers=ADMIN_HEADERS).json()["items"]
        kept_codes = [row for row in after_transient if row["code"] not in before_transient]
        assert len(kept_codes) == 1
        assert kept_codes[0]["status"] == "unused"

        assert client.delete(f"/api/v1/admin/agiso/sku-mappings/{mapping_id}", headers=ADMIN_HEADERS).status_code == 200
        assert client.delete(f"/api/v1/admin/agiso/sku-mappings/{mapping_id}", headers=ADMIN_HEADERS).status_code == 404


def test_agiso_client_headers_redaction_and_trades_buyer_pay_shapes(monkeypatch: pytest.MonkeyPatch):
    captured_requests: list[dict] = []
    queued_responses: list[httpx.Response] = []

    def fake_post(url: str, data=None, headers=None, timeout=None):
        captured_requests.append({"url": url, "data": dict(data or {}), "headers": dict(headers or {}), "timeout": timeout})
        return queued_responses.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)
    client = AgisoClient(
        base_url="https://gw-api.agiso.com",
        app_secret=TEST_APP_SECRET,
        access_token=TEST_ACCESS_TOKEN,
        timeout=10.0,
    )

    for data_shape in (
        [{"Tid": "1"}],
        {"List": [{"Tid": "2"}]},
        {"Trades": [{"Tid": "3"}]},
        {"Tid": "4"},
    ):
        queued_responses.append(
            httpx.Response(200, json={"IsSuccess": True, "Data": data_shape, "Error_Code": 0, "Error_Msg": ""})
        )
        trades = client.trades_buyer_pay("2026-11-25 00:00:00", page_no=1, page_size=20)
        assert len(trades) == 1

    first_req = captured_requests[0]
    assert first_req["headers"]["Authorization"] == f"Bearer {TEST_ACCESS_TOKEN}"
    assert first_req["headers"]["ApiVersion"] == "1"
    assert first_req["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "timestamp" in first_req["data"]
    assert first_req["data"]["sign"] == sign_agiso_api_params(first_req["data"], TEST_APP_SECRET)

    queued_responses.append(
        httpx.Response(
            200,
            json={
                "IsSuccess": False,
                "Error_Code": 4,
                "Error_Msg": f"bad token {TEST_ACCESS_TOKEN} and secret {TEST_APP_SECRET}",
                "AllowRetry": False,
            },
        )
    )
    with pytest.raises(AgisoError) as exc_info:
        client.trade_info("1001")
    assert TEST_ACCESS_TOKEN not in exc_info.value.message
    assert TEST_APP_SECRET not in exc_info.value.message
    assert "[REDACTED]" in exc_info.value.message
