from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import card_reconciliation, importer, upstream_sync
from app.accounting import calculate
from app.auth import passwords
from app.db import Base, get_db, make_engine
from app.main import app
from app.models import Adjustment, CardMatch, CardPurchase, ImportRow, Membership, OrderItem, Store, StoreSetting, User


def _setup_db(tmp_path: Path):
    engine = make_engine("sqlite:///" + (tmp_path / "multi_source.db").as_posix())
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(Store(id=1, name="multi-shop"))
    db.commit()
    return engine, db


def _import_qianniu_csv(db: Session, csv_body: str):
    preview = importer.preview(db, 1, "orders", "qianniu.csv", csv_body.encode("utf-8"), "gross")
    assert preview.get("batch_id"), f"preview failed: {preview}"
    res = importer.commit(db, 1, preview["batch_id"], confirm_item_amounts=True)
    assert not res.get("requires_item_amounts_confirmation")
    assert not res.get("conflicts")


def test_parsers_fx_rates_and_plan1_no_double_conversion(tmp_path):
    engine, db = _setup_db(tmp_path)
    try:
        db.add(
            StoreSetting(
                store_id=1,
                contact_email="",
                usd_cny_rate="7.50",
                vnd_cny_rate="0.0003",
                updated_at="2026-11-26T10:00:00+08:00",
            )
        )
        db.commit()

        canboso_payload = {
            "success": True,
            "orders": [
                {
                    "orderCode": "ORD-FB-1001",
                    "productName": "ChatGPT Plus 1M Account",
                    "quantity": 1,
                    "status": "DELIVERED",
                    "paidAt": "2026-11-20T04:00:00Z",
                    "payment": {"amountUsd": 10.0, "amountVnd": 260000},
                },
                {
                    "orderCode": "ORD-FB-REFUND-1",
                    "productName": "Refund wallet - ChatGPT Plus",
                    "quantity": 1,
                    "status": "completed",
                    "paidAt": "2026-11-20T05:00:00Z",
                    "payment": {"amountUsd": -10.0, "amountVnd": -260000},
                },
            ],
        }
        r_fb = card_reconciliation.import_purchases(
            db, 1, "fatbunny.json", json.dumps(canboso_payload).encode("utf-8"), source_hint="tg_fatbunny"
        )
        assert r_fb["inserted"] == 2
        p_fb = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "ORD-FB-1001"))
        assert p_fb is not None
        assert p_fb.source == "tg_fatbunny"
        assert p_fb.original_currency == "USD"
        assert p_fb.original_amount == "10"
        assert p_fb.amount_fen == 7500  # 10 USD * 7.50 CNY
        assert p_fb.paid_at == "2026-11-20 12:00:00"
        p_fb_ref = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "ORD-FB-REFUND-1"))
        assert p_fb_ref is not None
        assert p_fb_ref.status == "已退款"
        assert p_fb_ref.amount_fen == 0
        assert p_fb_ref.refund_fen == 7500

        thatim_json = {
            "source": "thatim",
            "vndPerUsd": 25000,
            "orders": [
                {
                    "orderId": "889901",
                    "productName": "Gemini Pro",
                    "variantName": "12 months",
                    "quantity": 1,
                    "amountVnd": 400000,
                    "status": "Hoàn thành",
                    "createdAt": "2026-11-20 13:30:00",
                },
                {
                    "orderId": "889902",
                    "productName": "Claude Pro",
                    "quantity": 1,
                    "amountVnd": 0,
                    "status": "Đã hủy",
                    "createdAt": "2026-11-20 13:45:00",
                },
            ],
        }
        r_th = card_reconciliation.import_purchases(
            db, 1, "thatim.json", json.dumps(thatim_json, ensure_ascii=False).encode("utf-8"), source_hint="thatim"
        )
        assert r_th["inserted"] == 2
        p_th = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "889901"))
        assert p_th.source == "thatim"
        assert p_th.original_currency == "VND"
        assert p_th.amount_fen == 12000  # 400,000 * 0.0003 = 120.00 CNY
        p_th_cancel = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "889902"))
        assert p_th_cancel.status == "已取消"
        assert p_th_cancel.amount_fen == 0

        thatim_paste = (
            "#889903\n"
            "Gemini Ultra 1M\n"
            "20/11/2026 14:20:00\n"
            "500.000₫\n"
            "Hoàn thành\n"
            "#889904\n"
            "ChatGPT Team\n"
            "20/11/2026 15:00:00\n"
            "17,59$\n"
            "发表\n"
        )
        r_paste = card_reconciliation.import_purchases(
            db, 1, "thatim-paste.txt", thatim_paste.encode("utf-8"), source_hint="thatim"
        )
        assert r_paste["inserted"] == 2
        p_vi = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "889903"))
        assert p_vi.amount_fen == 15000  # 500,000 * 0.0003 = 150.00 CNY
        assert p_vi.paid_at == "2026-11-20 14:20:00"
        p_eu = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "889904"))
        assert p_eu.original_currency == "USD"
        assert p_eu.amount_fen == 13193  # round(17.59 * 7.50 * 100) = 13192.5 -> 13193

        plan1_csv = (
            "采购单号,商品名称,商家名称,来源,数量,实付金额,退款金额,付款时间,状态,原币金额USD,16688商品编号,SKU,补库状态\n"
            "ORD-P1-9001,ChatGPT Plus 卡密,ChugAx Global,tg_chugax,2,145.00,0.00,2026-11-20 16:00:00,已完成,20.0000,G100,SKU1,synced\n"
        )
        r_p1 = card_reconciliation.import_purchases(db, 1, "plan1-export.csv", plan1_csv.encode("utf-8-sig"))
        assert r_p1["inserted"] == 1
        p_p1 = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "ORD-P1-9001"))
        assert p_p1.source == "tg_chugax"
        assert p_p1.amount_fen == 14500  # Must keep CNY 145.00 without double conversion
        assert p_p1.original_currency == "USD"
        assert p_p1.original_amount == "20"
    finally:
        db.close()
        engine.dispose()


def test_bidirectional_non_destructive_merge_plan2_and_canboso(tmp_path):
    engine, db = _setup_db(tmp_path)
    try:
        db.add(StoreSetting(store_id=1, contact_email="", usd_cny_rate="7.20", vnd_cny_rate="0.000285", updated_at=""))
        db.commit()

        # Order A: Plan 2 first (cost=0 + link), then Canboso (real USD cost)
        plan2_a = {
            "source": "tg_fatbunny",
            "items": [
                {
                    "code": "RDM-AAA1",
                    "sku_key": "gpt_plus",
                    "sku_title": "GPT Plus 1M",
                    "channel": "qianniu",
                    "external_order_id": "TB-ORDER-A",
                    "agiso_tid": "TB-ORDER-A",
                    "agiso_oid": "TB-ITEM-A",
                    "upstream_order_code": "ORD-MERGE-A",
                    "upstream_status": "DELIVERED",
                    "submitted_at": "2026-11-21T10:05:00+08:00",
                }
            ],
        }
        canboso_a = {
            "success": True,
            "source": "tg_fatbunny",
            "orders": [
                {
                    "orderCode": "ORD-MERGE-A",
                    "productName": "GPT Plus 1M",
                    "quantity": 1,
                    "status": "DELIVERED",
                    "paidAt": "2026-11-21T02:05:00Z",
                    "payment": {"amountUsd": 12.5},
                }
            ],
        }
        card_reconciliation.import_purchases(db, 1, "p2a.json", json.dumps(plan2_a).encode("utf-8"))
        card_reconciliation.import_purchases(db, 1, "cba.json", json.dumps(canboso_a).encode("utf-8"), source_hint="tg_fatbunny")
        row_a = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "ORD-MERGE-A"))
        assert row_a.amount_fen == 9000  # 12.5 * 7.20
        assert row_a.original_currency == "USD"
        assert row_a.linked_order_external_id == "TB-ORDER-A"
        assert (row_a.raw_data or {}).get("agiso_oid") == "TB-ITEM-A"
        assert row_a.linked_redeem_code == "RDM-AAA1"

        # Order B: Canboso first (real USD cost), then Plan 2 CSV (cost=0 + link)
        canboso_b = {
            "success": True,
            "source": "tg_fatbunny",
            "orders": [
                {
                    "orderCode": "ORD-MERGE-B",
                    "productName": "GPT Plus 1M",
                    "quantity": 1,
                    "status": "DELIVERED",
                    "paidAt": "2026-11-21T03:05:00Z",
                    "payment": {"amountUsd": 15.0},
                }
            ],
        }
        plan2_b_csv = (
            "采购单号,来源,商品名称,商家名称,数量,原币币种,原币金额,付款时间,状态,关联渠道,关联销售单号,关联子单号,兑换码,SKU\n"
            "ORD-MERGE-B,tg_fatbunny,GPT Plus 1M,FatBunny Hub (方案二),1,USD,,2026-11-21 11:05:00,DELIVERED,qianniu,TB-ORDER-B,TB-ITEM-B,RDM-BBB2,gpt_plus\n"
        )
        card_reconciliation.import_purchases(db, 1, "cbb.json", json.dumps(canboso_b).encode("utf-8"), source_hint="tg_fatbunny")
        card_reconciliation.import_purchases(db, 1, "p2b.csv", plan2_b_csv.encode("utf-8-sig"), source_hint="tg_fatbunny")
        row_b = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "ORD-MERGE-B"))
        assert row_b.amount_fen == 10800  # 15.0 * 7.20 preserved
        assert row_b.original_currency == "USD"
        assert row_b.linked_order_external_id == "TB-ORDER-B"
        assert (row_b.raw_data or {}).get("agiso_oid") == "TB-ITEM-B"
        assert row_b.linked_redeem_code == "RDM-BBB2"
    finally:
        db.close()
        engine.dispose()


def test_stage0_exact_link_agiso_oid_guard_bundle_and_idempotent_auto_confirm(tmp_path):
    engine, db = _setup_db(tmp_path)
    try:
        db.add(StoreSetting(store_id=1, contact_email="", usd_cny_rate="7.00", vnd_cny_rate="0.000285", updated_at=""))
        db.commit()

        _import_qianniu_csv(
            db,
            "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
            "OID-1,TID-SAME,SKU-G,Gemini Pro 独享,1,2026-11-22 10:00:00,completed,120.00,none,0\n"
            "OID-2,TID-SAME,SKU-G,Gemini Pro 独享,1,2026-11-22 10:00:00,completed,120.00,none,0\n"
            "OID-BUNDLE,TID-BUNDLE,SKU-G,Gemini Pro 独享,2,2026-11-22 11:00:00,completed,240.00,none,0\n",
        )

        purchases_json = {
            "source": "tg_fatbunny",
            "items": [
                {
                    "code": "CODE-ONLY-OID2",
                    "sku_title": "Gemini Pro 独享",
                    "channel": "qianniu",
                    "external_order_id": "TID-SAME",
                    "agiso_tid": "TID-SAME",
                    "agiso_oid": "OID-2",
                    "upstream_order_code": "UP-OID2",
                    "upstream_status": "DELIVERED",
                    "cost_usd": "10.00",
                    "submitted_at": "2026-11-22 10:02:00",
                },
                {
                    "code": "CODE-B1",
                    "sku_title": "Gemini Pro 独享",
                    "channel": "qianniu",
                    "external_order_id": "TID-BUNDLE",
                    "agiso_oid": "OID-BUNDLE",
                    "upstream_order_code": "UP-B1",
                    "upstream_status": "DELIVERED",
                    "cost_usd": "9.00",
                    "submitted_at": "2026-11-22 11:01:00",
                },
                {
                    "code": "CODE-B2",
                    "sku_title": "Gemini Pro 独享",
                    "channel": "qianniu",
                    "external_order_id": "TID-BUNDLE",
                    "agiso_oid": "OID-BUNDLE",
                    "upstream_order_code": "UP-B2",
                    "upstream_status": "DELIVERED",
                    "cost_usd": "11.00",
                    "submitted_at": "2026-11-22 11:02:00",
                },
            ],
        }
        card_reconciliation.import_purchases(db, 1, "p2-exact.json", json.dumps(purchases_json).encode("utf-8"))

        rep = card_reconciliation.suggestions(db, 1, "", "", limit=100)
        by_item = {s["item_id"]: s["candidates"] for s in rep["suggestions"]}

        # OID-2 gets UP-OID2 via exact link; OID-1 must NOT receive UP-OID2 as exact link (agiso_oid guard)
        assert by_item["OID-2"][0]["is_exact_link"] is True
        assert by_item["OID-2"][0]["purchase_external_id"] == "UP-OID2"
        assert by_item["OID-2"][0]["effective_cost_fen"] == 7000
        for cand in by_item.get("OID-1", []):
            assert cand.get("is_exact_link") is not True
            assert cand.get("purchase_external_id") != "UP-OID2"

        # OID-BUNDLE gets multi-purchase exact bundle (6300 + 7700 = 14000 fen)
        bundle_cand = by_item["OID-BUNDLE"][0]
        assert bundle_cand["is_exact_link"] is True
        assert bundle_cand["is_bundle"] is True
        assert bundle_cand["effective_cost_fen"] == 14000
        assert len(bundle_cand["allocations"]) == 2

        # First auto-confirm confirms OID-2 and OID-BUNDLE
        first_res = card_reconciliation.auto_confirm_exact_matches(db, 1, reviewer_id=1)
        assert first_res["confirmed_count"] == 2
        assert first_res["total_cost_fen"] == 21000

        # Second run is strictly idempotent
        second_res = card_reconciliation.auto_confirm_exact_matches(db, 1, reviewer_id=1)
        assert second_res["confirmed_count"] == 0
        assert second_res["matched_exact_candidates"] == 0

        # Verify accounting settled profit: OID-2 (12000-7000=5000) + OID-BUNDLE (24000-14000=10000) = 15000
        acc = calculate(db, 1, "2026-11-01", "2026-11-30")
        assert acc["counts"]["settled"] == 2
        assert acc["known"]["cost_fen"] == 21000
        assert acc["known"]["profit_fen"] == 15000
    finally:
        db.close()
        engine.dispose()


def test_16688_signer_import_and_openapi_paused_or_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTISTORE_DISABLE_ENV_FALLBACK", "1")
    monkeypatch.setenv("MULTISTORE_16688_APP_ID", "app_demo_01")
    monkeypatch.setenv("MULTISTORE_16688_SECRET", "sec_demo_99")
    monkeypatch.setenv("MULTISTORE_16688_BASE_URL", "https://api.16688.test")

    signed, sig, plain = upstream_sync.build_16688_signed_payload(
        app_id="app_demo_01",
        secret="sec_demo_99",
        path="/openApi/order/list",
        params={"page": 1, "page_size": 50},
        timestamp=1700000000,
        nonce="abc1234567890123",
    )
    expected_plain = "POST\norder\nlist\napp_id=app_demo_01&nonce=abc1234567890123&page=1&page_size=50&timestamp=1700000000"
    assert plain == expected_plain
    expected_sig = hmac.new(b"sec_demo_99", expected_plain.encode("utf-8"), hashlib.sha256).hexdigest()
    assert sig == expected_sig
    assert signed["sign"] == expected_sig

    engine, db = _setup_db(tmp_path)
    try:
        # 1) Mock paused OpenAPI response
        paused_transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"code": 500, "msg": "平台当前超负载，已暂停OpenAPI接口"})
        )
        with httpx.Client(transport=paused_transport) as client:
            paused_res = upstream_sync.sync_16688_sales_orders(db, 1, http_client=client)
        assert paused_res["status"] == "paused"
        assert paused_res["paused"] is True
        assert paused_res["message"] == "平台当前超负载，已暂停OpenAPI接口"
        assert paused_res["fetched"] == 0

        # 2) Mock live OpenAPI success response (also strip sensitive fields from raw_data)
        ok_transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "code": 1,
                    "data": {
                        "list": [
                            {
                                "trade_no": "16688-SALE-01",
                                "identity": "merchant",
                                "goods_no": "G-GPT-01",
                                "goods_name": "ChatGPT Plus 独享月卡",
                                "quantity": 1,
                                "total_price": "118.00",
                                "status": 4,
                                "pay_time": 1763697600,  # 2025-11-21 12:00:00 UTC+8
                                "card_secret": "SHOULD-BE-STRIPPED",
                            },
                            {
                                "trade_no": "16688-BUYER-IGNORED",
                                "identity": "buyer",
                                "goods_no": "G-BUY-01",
                                "goods_name": "进货单不应混入销售",
                                "quantity": 1,
                                "total_price": "50.00",
                                "status": 4,
                                "pay_time": 1763697600,
                            },
                        ],
                        "total": 2,
                    },
                },
            )
        )
        with httpx.Client(transport=ok_transport) as client:
            ok_res = upstream_sync.sync_16688_sales_orders(db, 1, http_client=client)
        assert ok_res["status"] == "ok"
        assert ok_res["paused"] is False
        assert ok_res["inserted"] == 1
        assert ok_res["skipped_non_merchant"] == 1
        row = db.scalar(select(OrderItem).where(OrderItem.order_external_id == "16688-SALE-01"))
        assert row is not None
        assert row.channel == "16688"
        assert row.status == "completed"
        assert row.payment_fen == 11800
        imp_row = db.scalar(select(ImportRow).where(ImportRow.batch_id == ok_res["batch_id"]))
        assert imp_row is not None
        assert "card_secret" not in (imp_row.raw_data or {})
        assert db.scalar(select(OrderItem).where(OrderItem.order_external_id == "16688-BUYER-IGNORED")) is None

        # 3) CSV import + idempotency
        csv_16688 = (
            "订单编号,商品编号,商品名称,数量,实付金额,付款时间,订单状态\n"
            "16688-SALE-02,G-GEM-02,Gemini Pro 18M,1,68.50,2026-11-21 14:00:00,已发货\n"
        ).encode("utf-8")
        r_csv1 = upstream_sync.import_16688_orders_payload(db, 1, "sales.csv", csv_16688)
        assert r_csv1["inserted"] == 1
        r_csv2 = upstream_sync.import_16688_orders_payload(db, 1, "sales.csv", csv_16688)
        assert r_csv2["inserted"] == 0
    finally:
        db.close()
        engine.dispose()


def test_api_endpoints_multi_channel_upstreams_and_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTISTORE_DISABLE_ENV_FALLBACK", "1")
    monkeypatch.setenv("MULTISTORE_CANBOSO_BASE_URL", "https://canboso.test")
    monkeypatch.setenv("MULTISTORE_FATBUNNY_API_KEY", "tgb_test_fb_key_1234")
    monkeypatch.setenv("MULTISTORE_CHUGAX_API_KEY", "tgb_test_cx_key_5678")
    monkeypatch.setenv("MULTISTORE_PLAN2_BASE_URL", "https://plan2.test")
    monkeypatch.setenv("MULTISTORE_PLAN2_ADMIN_TOKEN", "p2_admin_secret_9999")
    monkeypatch.setenv("MULTISTORE_16688_APP_ID", "app_16688_demo")
    monkeypatch.setenv("MULTISTORE_16688_SECRET", "sec_16688_demo")
    monkeypatch.setenv("MULTISTORE_16688_BASE_URL", "https://api.16688.test")

    engine = make_engine("sqlite:///" + (tmp_path / "api_multi.db").as_posix())
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                Store(id=1, name="shop-main"),
                User(id=1, username="owner", password_hash=passwords.hash("pw123456"), is_admin=True),
            ]
        )
        db.flush()
        db.add(Membership(user_id=1, store_id=1))
        db.commit()

    def override_db():
        with factory() as session:
            yield session

    seen_requests: list[tuple[str, dict[str, str], str]] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((str(request.url), dict(request.headers), request.method))
        url = str(request.url)
        if url.startswith("https://canboso.test/api/v2/telegram-buyer/orders"):
            assert request.url.query == b""  # Canboso v2 requires no query params
            api_key = request.headers.get("x-buyer-key")
            if api_key == "tgb_test_fb_key_1234":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "orders": [
                            {
                                "orderCode": "CB-FB-01",
                                "productName": "GPT Plus 1M",
                                "quantity": 1,
                                "status": "DELIVERED",
                                "paidAt": "2026-11-22T02:10:00Z",
                                "payment": {"amountUsd": 10.0},
                            }
                        ],
                    },
                )
            if api_key == "tgb_test_cx_key_5678":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "orders": [
                            {
                                "orderCode": "CB-CX-01",
                                "productName": "Claude Pro 1M",
                                "quantity": 1,
                                "status": "DELIVERED",
                                "paidAt": "2026-11-22T03:10:00Z",
                                "payment": {"amountUsd": 12.0},
                            }
                        ],
                    },
                )
            return httpx.Response(401, text="unauthorized")
        if url.startswith("https://plan2.test/api/v1/admin/reconciliation-export"):
            assert request.headers.get("authorization") == "Bearer p2_admin_secret_9999"
            return httpx.Response(
                200,
                json={
                    "source": "tg_fatbunny",
                    "count": 1,
                    "items": [
                        {
                            "code": "RDM-API-01",
                            "sku_title": "GPT Plus 1M",
                            "channel": "qianniu",
                            "external_order_id": "QN-ORD-01",
                            "agiso_oid": "QN-ITEM-01",
                            "upstream_order_code": "CB-FB-01",
                            "upstream_status": "DELIVERED",
                            "submitted_at": "2026-11-22 10:10:00",
                        }
                    ],
                },
            )
        if url.startswith("https://api.16688.test/openApi/order/list"):
            return httpx.Response(200, json={"code": 500, "msg": "平台暂时暂停OpenAPI，请稍后重试"})
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(mock_handler)
    real_client_cls = httpx.Client
    monkeypatch.setattr(upstream_sync.httpx, "Client", lambda *a, **kw: real_client_cls(transport=transport))

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            login = client.post("/api/login", json={"username": "owner", "password": "pw123456"})
            assert login.status_code == 200
            headers = {"X-CSRF-Token": login.json()["csrf"]}

            # FX-only settings update (no email required)
            fx_res = client.put(
                "/api/stores/1/card-settings",
                headers=headers,
                json={"usd_cny_rate": "7.20", "vnd_cny_rate": "0.0003"},
            )
            assert fx_res.status_code == 200
            assert fx_res.json()["usd_cny_rate"] == "7.2"
            assert fx_res.json()["vnd_cny_rate"] == "0.0003"

            # Import 1 Qianniu order + 1 16688 sales order
            with factory() as db:
                _import_qianniu_csv(
                    db,
                    "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
                    "QN-ITEM-01,QN-ORD-01,SKU-GPT,GPT Plus 1M,1,2026-11-22 10:00:00,completed,120.00,none,0\n",
                )

            order_16688_json = json.dumps(
                {
                    "orders": [
                        {
                            "trade_no": "16688-API-01",
                            "goods_no": "G-CLAUDE",
                            "goods_name": "Claude Pro 1M",
                            "quantity": 1,
                            "total_price": "150.00",
                            "status": "已发货",
                            "pay_time": "2026-11-22 11:00:00",
                        }
                    ]
                }
            ).encode("utf-8")
            imp_16688 = client.post(
                "/api/stores/1/orders/import-16688",
                headers=headers,
                files={"file": ("16688.json", order_16688_json, "application/json")},
                data={"basis": "gross"},
            )
            assert imp_16688.status_code == 200
            assert imp_16688.json()["inserted"] == 1

            # 16688 OpenAPI paused returns HTTP 200 with status="paused"
            sync_16688 = client.post("/api/stores/1/upstreams/sync-16688-orders", headers=headers, json={})
            assert sync_16688.status_code == 200
            assert sync_16688.json()["status"] == "paused"
            assert sync_16688.json()["paused"] is True

            # Sync TG upstreams (FatBunny + ChugAx)
            sync_tg = client.post("/api/stores/1/upstreams/sync-tg", headers=headers, json={"supplier": "all"})
            assert sync_tg.status_code == 200
            assert sync_tg.json()["inserted"] == 2

            # Sync Plan 2 reconciliation export
            sync_p2 = client.post(
                "/api/stores/1/upstreams/sync-plan2",
                headers=headers,
                json={"auto_confirm_exact": False},
            )
            assert sync_p2.status_code == 200
            assert sync_p2.json()["updated"] == 1

            # Auto-confirm exact matches via endpoint
            auto_res = client.post("/api/stores/1/card-matches/auto-exact", headers=headers, json={})
            assert auto_res.status_code == 200
            assert auto_res.json()["confirmed_count"] == 1
            assert auto_res.json()["total_cost_fen"] == 7200  # 10 USD * 7.20

            # Check upstreams status (no secret leaked)
            st = client.get("/api/stores/1/upstreams/status", headers=headers)
            assert st.status_code == 200
            st_text = st.text
            assert "tgb_test_fb_key_1234" not in st_text
            assert "p2_admin_secret_9999" not in st_text
            st_json = st.json()
            assert st_json["configured"]["fatbunny"] is True
            assert st_json["configured"]["chugax"] is True
            assert st_json["configured"]["plan2"] is True
            assert st_json["order_counts"]["qianniu"] == 1
            assert st_json["order_counts"]["16688"] == 1
            assert st_json["purchase_counts"]["tg_fatbunny"] == 1
            assert st_json["purchase_counts"]["tg_chugax"] == 1

            # Check report channel_breakdown and channel filter
            rep_all = client.get("/api/stores/1/report?start=2026-11-01&end=2026-11-30", headers=headers).json()
            assert len(rep_all["channel_breakdown"]) == 2
            ch_map = {row["channel"]: row for row in rep_all["channel_breakdown"]}
            assert ch_map["qianniu"]["profit_fen"] == 4800  # 12000 - 7200
            assert ch_map["16688"]["payment_fen"] == 15000

            rep_16688 = client.get(
                "/api/stores/1/report?start=2026-11-01&end=2026-11-30&channel=16688",
                headers=headers,
            ).json()
            assert rep_16688["counts"]["paid_items"] == 1
            assert rep_16688["items"][0]["channel"] == "16688"

            # Check ledger source & channel filters
            led_fb = client.get("/api/stores/1/card-ledger?source=tg_fatbunny", headers=headers).json()
            assert len(led_fb["purchases"]) == 1
            assert led_fb["purchases"][0]["linked_redeem_code"] == "RDM-API-01"

            led_tg = client.get("/api/stores/1/card-ledger?source=tg", headers=headers).json()
            assert len(led_tg["purchases"]) == 2

            led_16688 = client.get("/api/stores/1/card-ledger?channel=16688", headers=headers).json()
            assert len(led_16688["orders"]) == 1
            assert led_16688["orders"][0]["order_id"] == "16688-API-01"

            # P0 regression: /api/stores/{store_id}/card-purchases must return 200 with linked_item_external_id
            purchases_resp = client.get("/api/stores/1/card-purchases", headers=headers)
            assert purchases_resp.status_code == 200
            purchases_rows = purchases_resp.json()
            assert len(purchases_rows) == 2
            by_ext = {row["purchase_external_id"]: row for row in purchases_rows}
            assert by_ext["CB-FB-01"]["linked_item_external_id"] == "QN-ITEM-01"
            assert by_ext["CB-CX-01"]["linked_item_external_id"] == ""
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_classify_plan2_and_thatim_product_titles():
    assert card_reconciliation.classify_product("", "Google Ultra x20 共享车位") == "gemini_family"
    assert card_reconciliation.classify_product("", "Google Ultra x20 独享整组（管理员版）") == "gemini_family"
    assert card_reconciliation.classify_product("", "AI Ultra 20x · Member Seat · 1mo") == "gemini_family"
    assert (
        card_reconciliation.classify_product(
            "",
            "Google Antiravity（1 个月） 升级到 Google Antigravity Ultra 20x，即可完成激活",
        )
        == "activation"
    )


def test_recursive_raw_payload_sanitization_on_canboso_and_16688(tmp_path):
    engine, db = _setup_db(tmp_path)
    try:
        canboso_payload = {
            "success": True,
            "orders": [
                {
                    "orderCode": "CB-SAN-01",
                    "productName": "Gemini Ultra Member Seat",
                    "quantity": 1,
                    "status": "DELIVERED",
                    "paidAt": "2026-11-22T02:10:00Z",
                    "payment": {"amountUsd": 12.0},
                    "delivery": {
                        "accounts": [{"email": "leak@example.com", "password": "PlainPassword123"}],
                        "cards": ["CARD-SECRET-999"],
                    },
                    "buyer_nick": "secret_buyer",
                    "卡密": "TOP-SECRET-CDK",
                    "nested": {"contact_email": "inner@example.com", "note": "contact me at hidden@example.com"},
                    "agiso_oid": "SAFE-OID-01",
                    "external_order_id": "SAFE-TID-01",
                }
            ],
        }
        card_reconciliation.import_purchases(
            db,
            1,
            "canboso.json",
            json.dumps(canboso_payload).encode("utf-8"),
            source_hint="tg_fatbunny",
        )
        row = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "CB-SAN-01"))
        assert row is not None
        raw_dump = json.dumps(row.raw_data, ensure_ascii=False)
        for forbidden in ("delivery", "accounts", "leak@example.com", "PlainPassword123", "CARD-SECRET-999", "secret_buyer", "TOP-SECRET-CDK", "inner@example.com", "hidden@example.com"):
            assert forbidden not in raw_dump
        assert row.raw_data.get("agiso_oid") == "SAFE-OID-01"
        assert row.raw_data.get("external_order_id") == "SAFE-TID-01"
        assert row.raw_data.get("orderCode") == "CB-SAN-01"

        orders_16688 = [
            {
                "trade_no": "16688-SAN-01",
                "goods_name": "Gemini Pro",
                "total_amount": "100.00",
                "status": 4,
                "pay_time": "2026-11-22 10:00:00",
                "identity": "merchant",
                "buyer_name": "张三",
                "联系方式": "13800138000",
                "cards": [{"card_pwd": "PWD-123", "secret_list": ["S1"]}],
            }
        ]
        upstream_sync.import_16688_orders_payload(
            db,
            1,
            "orders.json",
            json.dumps(orders_16688, ensure_ascii=False).encode("utf-8"),
        )
        from app.models import ImportRow
        imp_row = db.scalar(select(ImportRow).order_by(ImportRow.id.desc()))
        assert imp_row is not None
        imp_dump = json.dumps(imp_row.raw_data, ensure_ascii=False)
        for forbidden in ("张三", "13800138000", "PWD-123", "secret_list", "cards"):
            assert forbidden not in imp_dump
        assert imp_row.raw_data.get("trade_no") == "16688-SAN-01"
    finally:
        db.close()
        engine.dispose()


def test_status_normalization_and_merge_protection(tmp_path):
    assert card_reconciliation._normalize_upstream_status("waiting_fulfillment") == "处理中"
    assert card_reconciliation._normalize_upstream_status("pending") == "处理中"
    assert card_reconciliation._normalize_upstream_status("waiting") == "处理中"
    assert card_reconciliation._normalize_upstream_status("unpaid") == "待付款"
    assert card_reconciliation._normalize_upstream_status("waiting_payment") == "待付款"
    assert card_reconciliation._normalize_upstream_status("paid") == "已付款"

    engine, db = _setup_db(tmp_path)
    try:
        done_payload = {
            "success": True,
            "orders": [
                {
                    "orderCode": "CB-STAT-01",
                    "productName": "Gemini Pro",
                    "quantity": 1,
                    "status": "DELIVERED",
                    "paidAt": "2026-11-22T02:00:00Z",
                    "payment": {"amountUsd": 10.0},
                }
            ],
        }
        card_reconciliation.import_purchases(db, 1, "a.json", json.dumps(done_payload).encode("utf-8"), source_hint="tg_fatbunny")

        # Weak states (processing / unpaid) must NOT downgrade 已完成
        for weak_status in ("waiting_fulfillment", "unpaid"):
            weak_payload = {
                "success": True,
                "orders": [
                    {
                        "orderCode": "CB-STAT-01",
                        "productName": "Gemini Pro",
                        "quantity": 1,
                        "status": weak_status,
                        "paidAt": "2026-11-22T02:00:00Z",
                        "payment": {"amountUsd": 10.0},
                    }
                ],
            }
            card_reconciliation.import_purchases(db, 1, f"{weak_status}.json", json.dumps(weak_payload).encode("utf-8"), source_hint="tg_fatbunny")
            row = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "CB-STAT-01"))
            assert row.status == "已完成"

        # Terminal negative state (cancelled) MAY override 已完成
        cancel_payload = {
            "success": True,
            "orders": [
                {
                    "orderCode": "CB-STAT-01",
                    "productName": "Gemini Pro",
                    "quantity": 1,
                    "status": "CANCELLED",
                    "paidAt": "2026-11-22T02:00:00Z",
                    "payment": {"amountUsd": 10.0},
                }
            ],
        }
        card_reconciliation.import_purchases(db, 1, "cancel.json", json.dumps(cancel_payload).encode("utf-8"), source_hint="tg_fatbunny")
        row = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "CB-STAT-01"))
        assert row.status == "已取消"
    finally:
        db.close()
        engine.dispose()


def test_canboso_negative_refund_merge_all_four_orderings(tmp_path):
    engine, db = _setup_db(tmp_path)
    try:
        db.add(StoreSetting(store_id=1, contact_email="", usd_cny_rate="7.00", vnd_cny_rate="0.00028", updated_at=""))
        db.commit()

        def pos_row(code: str) -> dict:
            return {
                "orderCode": code,
                "productName": "GPT Plus 1M",
                "quantity": 1,
                "status": "DELIVERED",
                "paidAt": "2026-11-22T02:00:00Z",
                "payment": {"amountUsd": 10.0},
            }

        def neg_row(code: str) -> dict:
            return {
                "orderCode": code,
                "productName": "Refund Wallet Order",
                "quantity": 1,
                "status": "REFUNDED",
                "paidAt": "2026-11-22T04:00:00Z",
                "payment": {"amountUsd": -10.0},
            }

        # 1. Intra-batch pos -> neg
        # 2. Intra-batch neg -> pos
        intra = {"success": True, "orders": [pos_row("CB-REF-1"), neg_row("CB-REF-1"), neg_row("CB-REF-2"), pos_row("CB-REF-2")]}
        card_reconciliation.import_purchases(db, 1, "intra.json", json.dumps(intra).encode("utf-8"), source_hint="tg_fatbunny")

        # 3. Cross-sync pos -> neg
        card_reconciliation.import_purchases(db, 1, "c3a.json", json.dumps({"success": True, "orders": [pos_row("CB-REF-3")]}).encode("utf-8"), source_hint="tg_fatbunny")
        card_reconciliation.import_purchases(db, 1, "c3b.json", json.dumps({"success": True, "orders": [neg_row("CB-REF-3")]}).encode("utf-8"), source_hint="tg_fatbunny")

        # 4. Cross-sync neg -> pos
        card_reconciliation.import_purchases(db, 1, "c4a.json", json.dumps({"success": True, "orders": [neg_row("CB-REF-4")]}).encode("utf-8"), source_hint="tg_fatbunny")
        card_reconciliation.import_purchases(db, 1, "c4b.json", json.dumps({"success": True, "orders": [pos_row("CB-REF-4")]}).encode("utf-8"), source_hint="tg_fatbunny")

        for code in ("CB-REF-1", "CB-REF-2", "CB-REF-3", "CB-REF-4"):
            p = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == code))
            assert p is not None, code
            assert p.amount_fen == 7000, f"{code} amount_fen={p.amount_fen}"
            assert p.refund_fen == 7000, f"{code} refund_fen={p.refund_fen}"
            assert p.status == "已退款", f"{code} status={p.status}"
            assert p.product_title == "GPT Plus 1M", f"{code} title={p.product_title}"
    finally:
        db.close()
        engine.dispose()


def test_plan2_channel_mapping_and_symmetric_tg_canboso_dedup(tmp_path, monkeypatch):
    engine, db = _setup_db(tmp_path)
    try:
        seen_queries: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_queries.append(dict(request.url.params))
            return httpx.Response(200, json={"source": "tg_fatbunny", "count": 0, "items": []})

        monkeypatch.setattr(
            upstream_sync,
            "load_upstream_config",
            lambda: {
                "canboso_base_url": "https://canboso.test",
                "fatbunny_api_key": "",
                "chugax_api_key": "",
                "plan2_base_url": "https://plan2.test",
                "plan2_admin_token": "tok_test",
                "openapi_16688_base_url": "",
                "openapi_16688_app_id": "",
                "openapi_16688_secret": "",
            },
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            upstream_sync.sync_plan2_reconciliation(db, 1, channel="qianniu", http_client=client)
            upstream_sync.sync_plan2_reconciliation(db, 1, channel="16688_store", http_client=client)
        assert seen_queries[0]["channel"] == "taobao"
        assert seen_queries[1]["channel"] == "16688"

        # Symmetric tg_canboso dedup: existing tg_fatbunny + incoming generic tg_canboso
        fb = {"success": True, "orders": [{"orderCode": "CB-SYM-01", "productName": "Ultra Seat", "quantity": 1, "status": "DELIVERED", "paidAt": "2026-11-22T02:00:00Z", "payment": {"amountUsd": 10.0}}]}
        card_reconciliation.import_purchases(db, 1, "fb.json", json.dumps(fb).encode("utf-8"), source_hint="tg_fatbunny")
        generic = {"success": True, "orders": [{"orderCode": "CB-SYM-01", "productName": "Ultra Seat", "quantity": 1, "status": "DELIVERED", "paidAt": "2026-11-22T02:00:00Z", "payment": {"amountUsd": 10.0}}]}
        card_reconciliation.import_purchases(db, 1, "gen.json", json.dumps(generic).encode("utf-8"), source_hint="tg_canboso")
        rows = list(db.scalars(select(CardPurchase).where(CardPurchase.purchase_external_id == "CB-SYM-01")))
        assert len(rows) == 1
        assert rows[0].source == "tg_fatbunny"
    finally:
        db.close()
        engine.dispose()


def test_16688_paid_at_paused_ordering_file_filter_and_cross_channel_guards(tmp_path, monkeypatch):
    engine, db = _setup_db(tmp_path)
    try:
        # 16688 unpaid & closed without pay_time must NOT fall back to create_time
        unpaid_norm = upstream_sync._normalize_16688_order_dict(
            {"trade_no": "O-UNPAID", "goods_name": "Item", "total_amount": "10", "status": 0, "create_time": "2026-11-22 10:00:00"},
            1,
        )
        assert unpaid_norm["status"] == "unpaid"
        assert unpaid_norm["paid_at"] is None

        closed_unpaid_norm = upstream_sync._normalize_16688_order_dict(
            {"trade_no": "O-CLOSED-NO-PAY", "goods_name": "Item", "total_amount": "10", "status": 2, "create_time": "2026-11-22 10:00:00"},
            2,
        )
        assert closed_unpaid_norm["status"] == "closed"
        assert closed_unpaid_norm["paid_at"] is None

        closed_paid_norm = upstream_sync._normalize_16688_order_dict(
            {"trade_no": "O-CLOSED-PAID", "goods_name": "Item", "total_amount": "10", "status": 3, "pay_time": "2026-11-22 10:05:00", "create_time": "2026-11-22 10:00:00"},
            3,
        )
        assert closed_paid_norm["paid_at"] == "2026-11-22 10:05:00"

        # Qianniu CSV with only 创建时间 (no 付款时间) must be rejected by map_headers
        with pytest.raises(importer.ImportProblem):
            importer.map_headers(["主订单编号", "商家编码", "购买数量", "创建时间", "订单状态", "买家实付金额", "退款状态"], "orders", channel="qianniu")

        # 16688 JSON file import filters buyer rows & reports skipped_non_merchant
        file_rows = [
            {"trade_no": "16688-SELL-1", "goods_name": "商品A", "total_amount": "50.00", "status": 4, "pay_time": "2026-11-22 10:00:00", "identity": "merchant"},
            {"trade_no": "16688-BUY-1", "goods_name": "进货B", "total_amount": "30.00", "status": 4, "pay_time": "2026-11-22 10:05:00", "identity": "buyer"},
        ]
        file_res = upstream_sync.import_16688_orders_payload(db, 1, "mix.json", json.dumps(file_rows, ensure_ascii=False).encode("utf-8"))
        assert file_res["fetched"] == 1
        assert file_res["skipped_non_merchant"] == 1

        # 16688 OpenAPI HTTP 200 code=1 whose product name contains "接口维护" must NOT trigger paused
        monkeypatch.setattr(
            upstream_sync,
            "load_upstream_config",
            lambda: {
                "canboso_base_url": "",
                "fatbunny_api_key": "",
                "chugax_api_key": "",
                "plan2_base_url": "",
                "plan2_admin_token": "",
                "openapi_16688_base_url": "https://api.16688.test/openApi",
                "openapi_16688_app_id": "app1234",
                "openapi_16688_secret": "sec5678",
            },
        )
        def ok_with_keyword(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 1,
                    "msg": "ok",
                    "data": {
                        "total": 1,
                        "list": [
                            {
                                "trade_no": "16688-KW-1",
                                "goods_name": "服务器接口维护代购套餐",
                                "total_amount": "88.00",
                                "status": 4,
                                "pay_time": "2026-11-22 11:00:00",
                                "identity": "merchant",
                            }
                        ],
                    },
                },
            )
        with httpx.Client(transport=httpx.MockTransport(ok_with_keyword)) as client:
            sync_res = upstream_sync.sync_16688_sales_orders(db, 1, http_client=client)
        assert sync_res["status"] == "ok"
        assert sync_res["paused"] is False
        assert sync_res["inserted"] == 1

        # Cross-channel item_external_id conflict raises ImportProblem; paid_orders counts (channel, order_external_id)
        _import_qianniu_csv(
            db,
            "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
            "SHARED-ITEM-1,SAME-ORD-ID,SKU-A,商品A,1,2026-11-22 10:00:00,completed,100.00,none,0\n",
        )
        with pytest.raises(importer.ImportProblem):
            importer.import_channel_orders(
                db,
                1,
                "16688",
                [
                    {
                        "item_external_id": "SHARED-ITEM-1",
                        "order_external_id": "SAME-ORD-ID",
                        "sku": "SKU-A",
                        "product_title": "商品A",
                        "quantity": 1,
                        "paid_at": "2026-11-22 10:00:00",
                        "status": "completed",
                        "fulfillment_status": "shipped",
                        "payment_fen": 10000,
                        "refund_status": "none",
                        "refund_snapshot_fen": 0,
                    }
                ],
            )
        importer.import_channel_orders(
            db,
            1,
            "16688",
            [
                {
                    "item_external_id": "16688-ITEM-2",
                    "order_external_id": "SAME-ORD-ID",
                    "sku": "SKU-A",
                    "product_title": "商品A",
                    "quantity": 1,
                    "paid_at": "2026-11-22 10:30:00",
                    "status": "completed",
                    "fulfillment_status": "shipped",
                    "payment_fen": 10000,
                    "refund_status": "none",
                    "refund_snapshot_fen": 0,
                }
            ],
        )
        acc = calculate(db, 1, "2026-11-01", "2026-11-30")
        # 4 distinct (channel, order_external_id): 16688-SELL-1, 16688-KW-1, (qianniu, SAME-ORD-ID), (16688, SAME-ORD-ID)
        assert acc["counts"]["paid_orders"] == 4
    finally:
        db.close()
        engine.dispose()


def test_stage0_multi_child_parent_order_without_agiso_oid_requires_manual_review(tmp_path):
    engine, db = _setup_db(tmp_path)
    try:
        db.add(StoreSetting(store_id=1, contact_email="", usd_cny_rate="7.00", vnd_cny_rate="0.00028", updated_at=""))
        db.commit()

        _import_qianniu_csv(
            db,
            "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
            "CHILD-1,PARENT-MULTI,SKU-G,Gemini Pro,1,2026-11-22 10:00:00,completed,120.00,none,0\n"
            "CHILD-2,PARENT-MULTI,SKU-G,Gemini Pro,1,2026-11-22 10:00:00,completed,120.00,none,0\n",
        )
        purchases_json = {
            "source": "tg_fatbunny",
            "items": [
                {
                    "code": "CODE-NO-OID",
                    "sku_title": "Gemini Pro",
                    "channel": "qianniu",
                    "external_order_id": "PARENT-MULTI",
                    "agiso_oid": "",
                    "upstream_order_code": "UP-NO-OID",
                    "upstream_status": "DELIVERED",
                    "cost_usd": "10.00",
                    "submitted_at": "2026-11-22 10:01:00",
                }
            ],
        }
        card_reconciliation.import_purchases(db, 1, "p2.json", json.dumps(purchases_json).encode("utf-8"))
        rep = card_reconciliation.suggestions(db, 1, "", "", limit=100)
        exact_cands = [
            c
            for s in rep["suggestions"]
            for c in s["candidates"]
            if c.get("is_exact_link")
        ]
        assert exact_cands
        assert all(c["warnings"] for c in exact_cands)
        assert all(c["auto_confirmable"] is False for c in exact_cands)

        auto_res = card_reconciliation.auto_confirm_exact_matches(db, 1, reviewer_id=1)
        assert auto_res["confirmed_count"] == 0
    finally:
        db.close()
        engine.dispose()
