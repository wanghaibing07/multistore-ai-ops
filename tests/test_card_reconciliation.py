from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import card_query_ai, card_reconciliation, importer
from app.accounting import calculate
from app.auth import passwords
from app.db import Base, get_db, make_engine
from app.main import app
from app.models import Adjustment, CardMatch, CardPurchase, ImportBatch, ImportRow, Membership, OrderItem, Store, StoreSetting, User


def setup_db(tmp_path):
    engine = make_engine("sqlite:///" + (tmp_path / "cards.db").as_posix())
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(Store(id=1, name="shop"))
    db.commit()
    return engine, db


def import_order(db: Session):
    text = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "i1,o1,A,Gemini Pro 18个月兑换链接,1,2026-10-01 12:00:00,completed,10,none,0\n"
    ).encode()
    preview = importer.preview(db, 1, "orders", "orders.csv", text, "gross")
    importer.commit(db, 1, preview["batch_id"])


def test_card_json_is_fully_retained_and_can_be_confirmed(tmp_path):
    engine, db = setup_db(tmp_path)
    import_order(db)
    payload = [{
        "order_id": "FF001",
        "product": "Gemini Pro 18个月兑换链接",
        "merchant": "测试供货商",
        "qty": 1,
        "amount": "3.19",
        "payment_refund": "0.00",
        "paid_at": "2026-10-01 12:01:00",
        "status": "已完成",
        "extra_field": "完整保留",
    }]
    result = card_reconciliation.import_purchases(
        db, 1, "card.json", json.dumps(payload, ensure_ascii=False).encode()
    )
    assert result["inserted"] == 1
    purchase = db.scalar(select(CardPurchase))
    assert purchase.raw_data["extra_field"] == "完整保留"

    report = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31")
    assert report["suggestion_count"] == 1
    candidate = report["suggestions"][0]["candidates"][0]
    assert candidate["purchase_external_id"] == "FF001"

    body = SimpleNamespace(
        request_id=uuid4(),
        item_id="i1",
        purchase_id=purchase.id,
        score=candidate["score"],
        cost="",
        note="人工确认",
    )
    confirmed = card_reconciliation.confirm_match(db, 1, body, reviewer_id=1)
    assert confirmed["duplicate"] is False
    assert db.scalar(select(CardMatch)).status == "confirmed"
    assert db.scalar(select(Adjustment)).amount_fen == 319
    assert calculate(db, 1, "2026-10-01", "2026-10-31")["known"]["profit_fen"] == 681
    db.close()
    engine.dispose()


def test_multi_unit_16688_inference_cost_set_deduction_and_muse_margin(tmp_path):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_done_adj,ord_done_adj,1093 | 颜色分类:18个月独享满血Pro（激活资格）,GeminiPro 18个月Pro会员,1,2026-10-28 16:00:00,completed,9.70,none,0\n"
        "it_act_old,ord_act_old,1093 | 颜色分类:18个月独享满血Pro（激活资格）,GeminiPro 18个月Pro会员,1,2026-10-28 15:40:00,completed,9.70,none,0\n"
        "it_act_1,ord_act_1,1093 | 颜色分类:18个月独享满血Pro（激活资格）,GeminiPro 18个月Pro会员,1,2026-10-28 18:41:00,completed,9.70,none,0\n"
        "it_act_2,ord_act_2,1093 | 颜色分类:18个月独享满血Pro（激活资格）,GeminiPro 18个月Pro会员,2,2026-10-28 18:45:00,completed,18.40,none,0\n"
        "it_muse,ord_muse,1091 | 颜色分类:（独享已激活）成品号直接使用10亿词元,Muse成品号 10亿词元,1,2026-10-26 02:00:00,completed,12.61,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "FF261028BASE01",
            "product": "Gemini 18个月激活链接 | 无需绑卡",
            "merchant": "珠世AI",
            "qty": 1,
            "amount": "3.61",
            "paid_at": "2026-10-28 16:01:00",
            "status": "已完成",
            "_source": "16688",
        },
        {
            "order_id": "FF261028PACK03",
            "product": "Gemini 18个月激活链接 | 无需绑卡",
            "merchant": "珠世AI",
            "qty": 1,
            "amount": "10.82",
            "paid_at": "2026-10-28 18:41:30",
            "status": "已完成",
            "_source": "16688",
        },
        {
            "order_id": "FF261021OLD001",
            "product": "Gemini pro 18个月激活链接",
            "merchant": "樱花AI",
            "qty": 1,
            "amount": "4.50",
            "paid_at": "2026-10-21 14:00:00",
            "status": "已完成",
            "_source": "16688",
        },
        {
            "order_id": "FF261024SURGE1",
            "product": "Gemini pro 18个月激活链接",
            "merchant": "樱花AI",
            "qty": 1,
            "amount": "11.00",
            "paid_at": "2026-10-24 16:00:00",
            "status": "已完成",
            "_source": "16688",
        },
        {
            "order_id": "FF261026MUSE01",
            "product": "muse ai 10亿 tokens 成品号| 质保首登",
            "merchant": "棱镜",
            "qty": 1,
            "amount": "1.95",
            "paid_at": "2026-10-26 02:02:00",
            "status": "已完成",
            "_source": "16688",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )
    db.add(Adjustment(
        store_id=1,
        item_external_id="it_done_adj",
        kind="cost_set",
        amount_fen=361,
        note="16688订单 FF261028BASE01 逐字核对一致",
        request_key=str(uuid4()),
    ))
    db.commit()

    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31")
    by_item = {s["item_id"]: s for s in rep["suggestions"]}

    # Cited purchase FF261028BASE01 is already consumed by cost_set adjustment
    assert by_item.get("it_act_old") is None or by_item["it_act_old"]["candidates"][0]["purchase_external_id"] != "FF261028BASE01"
    # Inferred 3-pack covers both x1 and x2 orders at ~3.61 unit cost
    c1 = by_item["it_act_1"]["candidates"][0]
    c2 = by_item["it_act_2"]["candidates"][0]
    assert c1["purchase_external_id"] == "FF261028PACK03"
    assert c1["inferred_quantity"] == 3
    assert c1["effective_cost_fen"] == 361
    assert c2["purchase_external_id"] == "FF261028PACK03"
    assert c2["effective_cost_fen"] == 721

    # Same-form high-margin Muse account has no false low-price warning
    cm = by_item["it_muse"]["candidates"][0]
    assert cm["purchase_external_id"] == "FF261026MUSE01"
    assert cm["score"] >= 85
    assert "采购价偏低，请核对规格" not in cm["warnings"]

    # Confirming inferred 3-pack expands purchase.quantity and supports confirming the second order
    pack = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "FF261028PACK03"))
    res1 = card_reconciliation.confirm_match(
        db,
        1,
        SimpleNamespace(
            request_id=uuid4(),
            item_id="it_act_1",
            purchase_id=pack.id,
            purchase_quantity=3,
            score=c1["score"],
            cost="",
            note="确认推算3件拆单",
        ),
        reviewer_id=1,
    )
    assert res1["amount_fen"] == 361
    res2 = card_reconciliation.confirm_match(
        db,
        1,
        SimpleNamespace(
            request_id=uuid4(),
            item_id="it_act_2",
            purchase_id=pack.id,
            purchase_quantity=3,
            score=c2["score"],
            cost="",
            note="确认推算3件余下2件",
        ),
        reviewer_id=1,
    )
    assert res2["allocated_quantity"] == 2
    assert res2["amount_fen"] == 721

    # Ledger also marks FF261028BASE01 (from cost_set adjustment) as matched
    led_base = card_reconciliation.ledger(db, 1, q="FF261028BASE01")
    assert led_base["purchases"][0]["reconcile_status"] == "matched"
    assert led_base["purchases"][0]["matched_order_ids"] == ["ord_done_adj"]

    # Non-integer >48h surge price FF261024SURGE1 is NOT falsely split
    led_surge = card_reconciliation.ledger(db, 1, q="FF261024SURGE1")
    assert led_surge["purchases"][0]["quantity"] == 1

    db.close()
    engine.dispose()


def test_import_rows_keep_raw_source_fields(tmp_path):
    engine, db = setup_db(tmp_path)
    text = (
        "item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount,买家姓名,手机\n"
        "i1,o1,A,1,2026-10-01,completed,10,none,0,测试姓名,13800138000\n"
    ).encode()
    preview = importer.preview(db, 1, "orders", "orders.csv", text, "gross")
    row = db.scalar(select(ImportRow).where(ImportRow.batch_id == preview["batch_id"]))
    assert row.raw_data["买家姓名"] == "测试姓名"
    assert row.raw_data["手机"] == "13800138000"
    db.close()
    engine.dispose()


def test_clipboard_rows_without_order_id_are_rejected(tmp_path):
    _engine, db = setup_db(tmp_path)
    text = (
        "商品名称\t商家名称\t数量\t金额\t退款金额\t下单时间\t状态\n"
        "Gemini Pro 18个月兑换链接\t测试供货商\t1\t3.19\t0.00\t2026-10-01 12:01:00\t已完成\n"
    ).encode()

    with pytest.raises(card_reconciliation.CardPurchaseImportError) as error:
        card_reconciliation.parse_purchases("card-page.tsv", text)

    assert error.value.issues == ["第 1 条：缺少卡网订单号"]
    db.close()


def test_duplicate_real_order_id_is_not_imported_twice(tmp_path):
    engine, db = setup_db(tmp_path)
    text = (
        "卡网订单号\t商品名称\t商家\t数量\t金额\t退款金额\t下单时间\t状态\n"
        "FF001\tGemini Pro 18个月兑换链接\t测试供货商\t1\t3.19\t0.00\t2026-10-01 12:01:00\t已完成\n"
    ).encode()

    first = card_reconciliation.import_purchases(db, 1, "first.tsv", text)
    second = card_reconciliation.import_purchases(db, 1, "second.tsv", text)

    assert first["inserted"] == 1
    assert second["duplicate"] is True
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert len(list(db.scalars(select(CardPurchase)))) == 1
    db.close()
    engine.dispose()


def test_card_import_error_keeps_row_details_structured(tmp_path):
    _engine, db = setup_db(tmp_path)
    text = (
        "卡网订单号\t商品名称\t商家\t数量\t金额\n"
        "FF001\tGemini Pro 18个月兑换链接\t测试供货商\t1\t\n"
    ).encode()

    with pytest.raises(card_reconciliation.CardPurchaseImportError) as error:
        card_reconciliation.parse_purchases("card-page.tsv", text)

    assert error.value.issues == ["第 1 条：缺少采购金额"]
    assert "有 1 条采购记录无法识别" in str(error.value)
    db.close()


def test_parse_16688_and_wzyp_formats_and_dedup(tmp_path):
    engine, db = setup_db(tmp_path)
    sample_16688 = (
        "16688您好，欢迎访问16688交易平台！\n"
        "查询结果\n"
        "邮箱 test@example.com\n"
        "全部\n待支付\n待发货\n已发货\n已完成\n"
        "优质AI店\n优质AI店\n已发货\n"
        "Gemini Pro 18个月激活链接\nGemini Pro 18个月激活链接\n"
        "订单号 FF2611170001·2026-11-17 18:59:49\n\n"
        "¥12.50\n\n联系卖家\n查看详情\n"
        "Muse批发\n已退款\n"
        "Muse AI 10亿 成品号\n"
        "订单号 FF2611150002·2026-11-15 20:39:39\n"
        "¥5.00\n已退款\n"
    ).encode("utf-8")

    res1 = card_reconciliation.import_purchases(db, 1, "16688.txt", sample_16688)
    assert res1["inserted"] == 2

    sample_wzyp_md = (
        "[网站首页](https://wzyp.cn/)[订单查询/投诉](https://wzyp.cn/order)\n"
        "| **商品名** | **订单号** | **金额** | **状态** | **查看权益** | **操作** | | |\n"
        "| :--- | :--- | :--- | :--- | :--- | :--- | - | - |\n"
        "| Gemini Pro 6个月pixel成品号 | 2026-11-08 18:17:10 | LD261108AAA111 | **￥15.45** | 共1件 | **已付款** | | |\n"
        "| Plus（upi渠道）CPA格式 | 2026-11-17 18:26:24 | LD261117BBB222 | **￥17.47** | 共2件 | **已关闭** | | |\n"
    ).encode("utf-8")
    res2 = card_reconciliation.import_purchases(db, 1, "wzyp.md", sample_wzyp_md)
    assert res2["inserted"] == 2

    # Shifted browser-copied TSV for wzyp should also parse and dedup against Markdown import
    sample_wzyp_tsv = (
        "商品名\t订单号\t金额\t状态\t查看权益\t操作\n"
        "Gemini Pro 6个月pixel成品号\t2026-11-08 18:17:10\tLD261108AAA111\t￥15.45\t共1件\t已付款\n"
        "Gemini Pro 18个月会员 成品号\t2026-10-31 20:43:08\tLD261031BX92DH\t￥22.25\t共1件\t已付款\n"
    ).encode("utf-8")
    res3 = card_reconciliation.import_purchases(db, 1, "wzyp.tsv", sample_wzyp_tsv)
    assert res3["inserted"] == 1
    assert res3["skipped"] == 1
    bx = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "LD261031BX92DH"))
    assert bx is not None
    assert bx.quantity == 1

    # Repeat import of exact 16688 text is completely deduplicated
    res4 = card_reconciliation.import_purchases(db, 1, "16688-again.txt", sample_16688)
    assert res4["duplicate"] is True
    assert res4["inserted"] == 0
    assert len(list(db.scalars(select(CardPurchase)))) == 5
    db.close()
    engine.dispose()


def test_feature_classification_and_smart_matching_with_bulk_allocation(tmp_path):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_fee,ord_fee,1094913930934 | 补差价专用,补差价专用,1,2026-10-28 12:00:00,completed,10.00,none,0\n"
        "it_act,ord_act,1093 | 颜色分类:18个月独享满血Pro（激活资格）,满血独享GeminiPro 18个月会员成品号激活,1,2026-10-28 15:00:00,completed,15.00,none,0\n"
        "it_fin,ord_fin,1093 | 颜色分类:18个月独享满血Pro（带邮箱成品号）,满血独享GeminiPro 18个月会员成品号激活,1,2026-10-28 15:00:00,completed,62.00,none,0\n"
        "it_muse,ord_muse,1091 | 颜色分类:（独享已激活）成品号直接使用10亿词元,Muse成品号/帮填邀请码,1,2026-10-28 16:00:00,completed,12.00,none,0\n"
        "it_fam1,ord_fam1,1094 | 套餐类型:拼车 1个月Pro会员家庭组,Gemini一个月家庭组,1,2026-10-28 17:00:00,completed,10.00,none,0\n"
        "it_fam2,ord_fam2,1094 | 套餐类型:拼车 1个月Pro会员家庭组,Gemini一个月家庭组,1,2026-10-28 18:00:00,completed,10.00,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "FF_ACT_18",
            "product": "Gemini pro 18个月激活链接",
            "merchant": "卡商A",
            "qty": 1,
            "amount": "4.50",
            "paid_at": "2026-10-28 15:02:00",
            "status": "已完成",
        },
        {
            "order_id": "LD_FIN_18",
            "product": "gemini pro 18个月会员 成品号",
            "merchant": "链动小铺",
            "qty": 1,
            "amount": "20.00",
            "paid_at": "2026-10-28 15:01:00",
            "status": "已付款",
        },
        {
            "order_id": "FF_MUSE_BOOST",
            "product": "Muse AI 代刷邀请助力",
            "merchant": "卡商B",
            "qty": 1,
            "amount": "2.00",
            "paid_at": "2026-10-28 16:00:30",
            "status": "已完成",
        },
        {
            "order_id": "FF_MUSE_ACC",
            "product": "Muse独享成品号 10亿token",
            "merchant": "卡商C",
            "qty": 1,
            "amount": "4.80",
            "paid_at": "2026-10-28 16:01:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_FAM_BULK",
            "product": "【拼车】Gemini pro家庭组席位（月卡）",
            "merchant": "卡商D",
            "qty": 6,
            "amount": "29.58",
            "paid_at": "2026-10-28 12:00:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_REFUNDED",
            "product": "Gemini pro 18个月激活链接",
            "merchant": "卡商A",
            "qty": 1,
            "amount": "4.50",
            "paid_at": "2026-10-28 15:00:30",
            "status": "已退款",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )

    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31")
    by_item = {s["item_id"]: s for s in rep["suggestions"]}

    # 补差价专用 is excluded from suggestions
    assert "it_fee" not in by_item
    # SKU spec separates activation link vs finished account on identical generic titles
    assert by_item["it_act"]["candidates"][0]["purchase_external_id"] == "FF_ACT_18"
    assert by_item["it_fin"]["candidates"][0]["purchase_external_id"] == "LD_FIN_18"
    # Muse finished account rejects Muse invite boost
    assert by_item["it_muse"]["candidates"][0]["purchase_external_id"] == "FF_MUSE_ACC"
    # Bulk 6-unit family-slot purchase matches both family orders with unit cost 493 fen
    assert by_item["it_fam1"]["candidates"][0]["purchase_external_id"] == "FF_FAM_BULK"
    assert by_item["it_fam1"]["candidates"][0]["effective_cost_fen"] == 493
    assert by_item["it_fam2"]["candidates"][0]["purchase_external_id"] == "FF_FAM_BULK"

    # Confirm both family orders against the same 6-unit bulk purchase
    fam_purchase = db.scalar(select(CardPurchase).where(CardPurchase.purchase_external_id == "FF_FAM_BULK"))
    c1 = card_reconciliation.confirm_match(
        db, 1, SimpleNamespace(request_id=uuid4(), item_id="it_fam1", purchase_id=fam_purchase.id, score=86, cost="", note="家庭组车位1"), 1
    )
    c2 = card_reconciliation.confirm_match(
        db, 1, SimpleNamespace(request_id=uuid4(), item_id="it_fam2", purchase_id=fam_purchase.id, score=86, cost="", note="家庭组车位2"), 1
    )
    assert c1["amount_fen"] == 493
    assert c2["amount_fen"] == 493

    # Ledger tracks category filters, status filters, search, matched time & IDs
    led_all = card_reconciliation.ledger(db, 1)
    assert led_all["counts"]["category_counts"]["gemini_finished"] == 1
    assert led_all["counts"]["category_counts"]["activation"] == 3
    assert led_all["counts"]["category_counts"]["muse_finished"] == 1
    assert led_all["counts"]["category_counts"]["gemini_family"] == 1
    assert led_all["counts"]["category_group_counts"] == {
        "finished": 2,
        "activation": 3,
        "family": 1,
        "other": 0,
    }

    led_finished = card_reconciliation.ledger(db, 1, category="finished")
    assert {purchase["category_group"] for purchase in led_finished["purchases"]} == {"finished"}
    assert len(led_finished["purchases"]) == 2

    led_fam = card_reconciliation.ledger(db, 1, category="gemini_family", status="matched")
    assert len(led_fam["purchases"]) == 1
    assert led_fam["purchases"][0]["matched_at"]
    assert set(led_fam["purchases"][0]["matched_order_ids"]) == {"ord_fam1", "ord_fam2"}
    assert led_fam["purchases"][0]["remaining_quantity"] == 4

    led_search = card_reconciliation.ledger(db, 1, q="ord_fam2")
    assert led_search["purchases"][0]["purchase_external_id"] == "FF_FAM_BULK"
    assert led_search["orders"][0]["order_id"] == "ord_fam2"
    db.close()
    engine.dispose()


def test_card_query_ai_captcha_and_api_workbench_flow(tmp_path, monkeypatch):
    engine = make_engine("sqlite:///" + (tmp_path / "api_cards.db").as_posix())
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([
            Store(id=1, name="shop1"),
            Store(id=2, name="shop2"),
            User(id=1, username="owner", password_hash=passwords.hash("pw123456"), is_admin=True),
        ])
        db.flush()
        db.add(Membership(user_id=1, store_id=1))
        db.commit()

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            login_resp = client.post("/api/login", json={"username": "owner", "password": "pw123456"})
            assert login_resp.status_code == 200
            headers = {"X-CSRF-Token": login_resp.json()["csrf"]}

            cfg = client.get("/api/config").json()
            assert cfg["card_sites"]["16688"]["order_url"] == "https://www.16688.com.cn/order"
            assert cfg["card_sites"]["wzyp"]["order_url"] == "https://wzyp.cn/order"

            # Save contact email and verify tenant isolation
            assert client.get("/api/stores/2/card-settings", headers=headers).status_code == 403
            assert client.put("/api/stores/1/card-settings", headers=headers, json={"contact_email": "bad-email@"}).status_code == 400
            saved = client.put("/api/stores/1/card-settings", headers=headers, json={"contact_email": "buyer@example.com"})
            assert saved.status_code == 200
            assert saved.json()["contact_email"] == "buyer@example.com"
            assert client.get("/api/stores/1/card-settings", headers=headers).json()["contact_email"] == "buyer@example.com"

            # Mock card_query_ai runner for 16688 success + wzyp captcha challenge -> submit captcha -> done
            def fake_runner(script_body, timeout=120):
                if "URL_16688" in script_body:
                    return {
                        "ok": True,
                        "cards": [{
                            "store": "测试店",
                            "status": "已发货",
                            "title": "Gemini 18个月Pro激活链接",
                            "meta": "订单号 FF2611180001·2026-11-18 01:00:00",
                            "price": "¥9.50",
                            "refunded": False,
                        }],
                        "apiList": [],
                    }
                if "URL_WZYP" in script_body:
                    return {"ok": True, "status": "captcha", "dataUrl": "data:image/png;base64,AAAA"}
                if "REFRESH_ON_FAIL" in script_body:
                    return {
                        "ok": True,
                        "status": "done",
                        "pages": [{
                            "page": "1",
                            "rows": [["Gemini Pro 18个月会员 成品号", "2026-11-18 01:05:00", "LD2611180002", "￥20.00", "共1件", "已付款"]],
                        }],
                    }
                return {"ok": True}

            orig_query = card_query_ai.query_card_sites
            monkeypatch.setattr(
                card_query_ai,
                "query_card_sites",
                lambda **kw: orig_query(**kw, runner=fake_runner, captcha_solver=lambda _u: ""),
            )

            q1 = client.post("/api/stores/1/card-purchases/ai-query", headers=headers, json={"site": "all"})
            assert q1.status_code == 200, q1.text
            body1 = q1.json()
            assert body1["status"] == "partial_captcha"
            assert body1["queried_count"] == 1
            assert body1["import_result"]["inserted"] == 1
            assert body1["captcha_session_id"]

            q2 = client.post(
                "/api/stores/1/card-purchases/ai-query",
                headers=headers,
                json={"site": "wzyp", "captcha_session_id": body1["captcha_session_id"], "captcha_code": "1234"},
            )
            assert q2.status_code == 200, q2.text
            assert q2.json()["status"] == "ok"
            assert q2.json()["import_result"]["inserted"] == 1

            # Paste import + dedup + ledger endpoint
            paste_resp = client.post(
                "/api/stores/1/card-purchases/import-text",
                headers=headers,
                json={"text": "| 商品名 | 订单号 | 金额 | 状态 | 查看权益 | 操作 |\n| - | - | - | - | - | - |\n| Gemini Pro 18个月会员 成品号 | 2026-11-18 01:05:00 | LD2611180002 | ￥20.00 | 共1件 | 已付款 |"},
            )
            assert paste_resp.status_code == 200
            assert paste_resp.json()["duplicate"] is True

            led_resp = client.get("/api/stores/1/card-ledger?category=gemini_finished&q=LD2611180002", headers=headers)
            assert led_resp.status_code == 200
            assert len(led_resp.json()["purchases"]) == 1
            assert led_resp.json()["purchases"][0]["reconcile_status"] == "pending"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_smart_matching_prioritizes_completed_and_high_price_tier_and_stderr_runner(tmp_path, monkeypatch):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_closed,ord_closed,1093 | 颜色分类:18个月独享满血Pro（带邮箱成品号）,GeminiPro 18个月Pro会员,1,2026-10-28 23:16:50,closed,62.00,success,62.00\n"
        "it_done,ord_done,1093 | 颜色分类:18个月独享满血Pro（带邮箱成品号）,GeminiPro 18个月Pro会员,1,2026-10-28 23:16:00,completed,62.00,none,0\n"
        "it_high,ord_high,1093 | 颜色分类:18个月Pro会员,GeminiPro 18个月Pro会员,1,2026-10-29 20:48:00,completed,66.86,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "LD_SINGLE_FIN",
            "product": "gemini pro 18个月会员 成品号",
            "merchant": "链动小铺",
            "qty": 1,
            "amount": "20.14",
            "paid_at": "2026-10-28 23:17:00",
            "status": "已付款",
        },
        {
            "order_id": "LD_BULK_FIN",
            "product": "gemini pro 18个月会员 成品号",
            "merchant": "链动小铺",
            "qty": 10,
            "amount": "189.50",
            "paid_at": "2026-10-28 06:00:00",
            "status": "已付款",
        },
        {
            "order_id": "FF_CHEAP_ACT",
            "product": "Gemini pro 18个月激活链接",
            "merchant": "16688",
            "qty": 1,
            "amount": "8.02",
            "paid_at": "2026-10-29 20:50:00",
            "status": "已完成",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )

    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31")
    by_item = {s["item_id"]: s for s in rep["suggestions"]}
    # Completed order gets the closely timed single purchase instead of closed/refunded order stealing it
    assert by_item["it_done"]["candidates"][0]["purchase_external_id"] == "LD_SINGLE_FIN"
    # High-price 66.86 sale matches bulk finished account (18.95) instead of cheap activation link (8.02)
    assert by_item["it_high"]["candidates"][0]["purchase_external_id"] == "LD_BULK_FIN"
    assert by_item["it_high"]["candidates"][0]["effective_cost_fen"] == 1895

    # _run_ego_script parses sentinel printed to stderr by ego-browser.exe on Windows
    fake_proc = SimpleNamespace(stdout="", stderr='__MS_CARD_QUERY_RESULT__{"ok": true, "site": "16688"}')
    monkeypatch.setattr(card_query_ai.subprocess, "run", lambda *a, **kw: fake_proc)
    monkeypatch.setattr(card_query_ai, "find_ego_browser", lambda: "ego-browser.exe")
    parsed = card_query_ai._run_ego_script("console.log('hi')")
    assert parsed == {"ok": True, "site": "16688"}

    db.close()
    engine.dispose()


def test_split_bundle_smart_summary_batch_confirm_partial_ledger_and_supersede(tmp_path):
    engine = make_engine("sqlite:///" + (tmp_path / "smart_recon.db").as_posix())
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([
            Store(id=1, name="shop1"),
            User(id=1, username="owner", password_hash=passwords.hash("pw123456"), is_admin=True),
        ])
        db.flush()
        db.add(Membership(user_id=1, store_id=1))
        db.commit()

        orders_csv = (
            "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
            "it_refunded,ord_ref,101 | 颜色分类:Gemini家庭组月卡,Gemini家庭组席位,1,2026-10-23 11:00:00,closed,9.90,success,9.90\n"
            "it_split10,ord_split10,101 | 颜色分类:Gemini家庭组月卡,Gemini家庭组席位,10,2026-10-23 12:39:18,completed,56.00,none,0\n"
            "it_auto1,ord_auto1,102 | 颜色分类:18个月成品号,Gemini Pro 18个月成品号,1,2026-10-24 15:00:00,completed,39.90,none,0\n"
            "it_part1,ord_part1,103 | 颜色分类:Muse成品号10亿词元,Muse 10亿词元成品号,1,2026-10-24 16:00:00,completed,15.00,none,0\n"
        ).encode("utf-8")
        prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
        importer.commit(db, 1, prev["batch_id"])

        purchases_payload = [
            {
                "order_id": "FF261023PART1",
                "product": "【拼车】Gemini pro家庭组席位（月卡）",
                "merchant": "16688",
                "qty": 1,
                "amount": "4.93",
                "paid_at": "2026-10-23 12:43:35",
                "status": "已完成",
            },
            {
                "order_id": "FF261023PART9",
                "product": "【拼车】Gemini pro家庭组席位（月卡）",
                "merchant": "16688",
                "qty": 9,
                "amount": "44.41",
                "paid_at": "2026-10-23 12:55:55",
                "status": "已完成",
            },
            {
                "order_id": "LD261024AUTO1",
                "product": "Gemini Pro 18个月会员 成品号",
                "merchant": "链动小铺",
                "qty": 1,
                "amount": "20.14",
                "paid_at": "2026-10-24 15:01:10",
                "status": "已付款",
            },
            {
                "order_id": "FF261024PACK3",
                "product": "Muse 10亿词元成品号",
                "merchant": "16688",
                "qty": 3,
                "amount": "18.00",
                "paid_at": "2026-10-24 15:50:00",
                "status": "已完成",
            },
        ]
        card_reconciliation.import_purchases(
            db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
        )

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            login_resp = client.post("/api/login", json={"username": "owner", "password": "pw123456"})
            headers = {"X-CSRF-Token": login_resp.json()["csrf"]}

            sug_resp = client.get("/api/stores/1/cost-match-suggestions?scope=all", headers=headers)
            assert sug_resp.status_code == 200, sug_resp.text
            sug_data = sug_resp.json()
            assert sug_data["skipped_refunded_count"] == 1
            summary = sug_data["smart_summary"]
            assert summary["actionable_missing_count"] == 3
            assert summary["matched_count"] == 3
            assert summary["window_coverage_pct"] == 100
            assert summary["auto_confirmable_count"] >= 1

            by_item = {s["item_id"]: s for s in sug_data["suggestions"]}
            split_cand = by_item["it_split10"]["candidates"][0]
            assert split_cand["is_bundle"] is True
            assert split_cand["bundle_kind"] == "split_qty"
            assert split_cand["effective_cost_fen"] == 4934
            assert len(split_cand["allocations"]) == 2

            # Confirm bundle match via single endpoint
            bundle_resp = client.post(
                "/api/stores/1/card-matches",
                headers=headers,
                json={
                    "request_id": str(uuid4()),
                    "item_id": "it_split10",
                    "purchase_id": split_cand["purchase_id"],
                    "score": split_cand["score"],
                    "note": "测试凑单确认",
                    "allocations": [
                        {
                            "purchase_id": a["purchase_id"],
                            "allocated_quantity": a["allocated_quantity"],
                            "purchase_quantity": a["purchase_quantity"],
                        }
                        for a in split_cand["allocations"]
                    ],
                },
            )
            assert bundle_resp.status_code == 200, bundle_resp.text
            assert bundle_resp.json()["amount_fen"] == 4934

            # Batch confirm remaining items
            auto_cand = by_item["it_auto1"]["candidates"][0]
            part_cand = by_item["it_part1"]["candidates"][0]
            batch_resp = client.post(
                "/api/stores/1/card-matches/batch",
                headers=headers,
                json={
                    "items": [
                        {
                            "request_id": str(uuid4()),
                            "item_id": "it_auto1",
                            "purchase_id": auto_cand["purchase_id"],
                            "score": auto_cand["score"],
                        },
                        {
                            "request_id": str(uuid4()),
                            "item_id": "it_part1",
                            "purchase_id": part_cand["purchase_id"],
                            "score": part_cand["score"],
                        },
                    ]
                },
            )
            assert batch_resp.status_code == 200, batch_resp.text
            assert batch_resp.json()["confirmed_count"] == 2
            assert batch_resp.json()["total_cost_fen"] == 2014 + 600

            # Ledger shows partial status (1/3) on FF261024PACK3
            partial_led = client.get("/api/stores/1/card-ledger?status=partial", headers=headers)
            assert partial_led.status_code == 200
            partial_rows = partial_led.json()["purchases"]
            assert len(partial_rows) == 1
            assert partial_rows[0]["purchase_external_id"] == "FF261024PACK3"
            assert partial_rows[0]["reconcile_status"] == "partial"
            assert "1/3" in partial_rows[0]["reconcile_label"]

            # Supersede via cost_invalidate and re-confirm cleanly
            with factory() as s_inv:
                s_inv.add(
                    Adjustment(
                        store_id=1,
                        item_external_id="it_auto1",
                        kind="cost_invalidate",
                        amount_fen=0,
                        note="规格变更自动失效旧成本",
                        request_key=str(uuid4()),
                    )
                )
                s_inv.commit()

            re_sug = client.get("/api/stores/1/cost-match-suggestions?scope=all", headers=headers).json()
            assert any(s["item_id"] == "it_auto1" for s in re_sug["suggestions"])

            re_confirm = client.post(
                "/api/stores/1/card-matches",
                headers=headers,
                json={
                    "request_id": str(uuid4()),
                    "item_id": "it_auto1",
                    "purchase_id": auto_cand["purchase_id"],
                    "score": auto_cand["score"],
                    "note": "重新确认",
                },
            )
            assert re_confirm.status_code == 200, re_confirm.text
            active_matches = client.get("/api/stores/1/card-matches", headers=headers).json()
            assert sum(1 for m in active_matches if m["item_external_id"] == "it_auto1") == 1
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_equivalent_batch_auto_confirm_finished_prestock_and_ai_rerank(tmp_path, monkeypatch):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_fin_b1,ord_fin_b1,1093 | 颜色分类:18个月独享满血Pro（带邮箱成品号）,Gemini Pro 18个月,1,2026-10-27 12:00:00,completed,52.00,none,0\n"
        "it_fin_pre,ord_fin_pre,1093 | 颜色分类:3个月独享满血Pro（带邮箱成品号）,Gemini Pro 18个月,1,2026-10-28 16:57:00,completed,47.99,none,0\n"
        "it_noise,ord_noise,1093 | 颜色分类:18个月独享满血Pro（激活资格）,Gemini Pro 18个月,1,2026-10-28 18:00:00,completed,26.88,none,0\n"
        "it_border,ord_border,1093 | 颜色分类:定制AI工具代开,定制AI工具,1,2026-10-28 19:00:00,completed,30.00,none,0\n"
        "it_short,ord_short,1093 | 颜色分类:18个月独享满血Pro（激活资格）,Gemini Pro 18个月,1,2026-10-28 21:00:00,completed,19.90,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "LD_BATCH_A",
            "product": "gemini pro 18个月会员 成品号",
            "merchant": "链动小铺",
            "qty": 5,
            "amount": "94.75",
            "paid_at": "2026-10-27 09:00:00",
            "status": "已付款",
        },
        {
            "order_id": "LD_BATCH_B",
            "product": "gemini pro 18个月会员 成品号",
            "merchant": "链动小铺",
            "qty": 5,
            "amount": "94.75",
            "paid_at": "2026-10-27 10:00:00",
            "status": "已付款",
        },
        {
            "order_id": "FF_FIN_3M",
            "product": "Gemini 成品Pro 3个月链接开通，成品会员号 包首登 无客服，不会用勿下单",
            "merchant": "源头卡商",
            "qty": 1,
            "amount": "14.00",
            "paid_at": "2026-10-28 16:49:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_NOISY_ACT",
            "product": "Gemini 18个月激活链接 | 无需绑卡 | 直接激活 | 批量合作免手续费 | 对接 DC2610254D",
            "merchant": "柯基AI",
            "qty": 1,
            "amount": "7.55",
            "paid_at": "2026-10-28 18:08:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_OTHER_BORDER",
            "product": "AI工具会员月卡充值",
            "merchant": "杂货卡商",
            "qty": 1,
            "amount": "12.00",
            "paid_at": "2026-10-28 19:04:00",
            "status": "已完成",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )

    seen_prompts = []

    def fake_rerank(pairs):
        seen_prompts.extend(pairs)
        return {p["pair_id"]: 6 for p in pairs if "定制AI" in p["sale_sku"]}

    monkeypatch.setattr(card_reconciliation, "_optional_ai_rerank", fake_rerank)
    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31", use_ai=True)
    by_item = {s["item_id"]: s for s in rep["suggestions"]}

    # Identical-cost sibling batches from same seller must not block auto_confirmable
    assert by_item["it_fin_b1"]["candidates"][0]["auto_confirmable"] is True
    # Noisy boilerplate stripped cleanly so exact 18m link scores high
    assert by_item["it_noise"]["candidates"][0]["confidence_level"] == "high"
    # Finished account pre-stocked 8 mins earlier scores high directly without manual review penalty
    assert by_item["it_fin_pre"]["candidates"][0]["score"] >= 85
    assert by_item["it_fin_pre"]["candidates"][0]["confidence_level"] == "high"
    # Ambiguous "other" SKU triggers deduplicated AI rerank
    assert rep["ai_stats"]["enabled"] is True
    assert rep["ai_stats"]["adjusted_candidates"] >= 1
    assert seen_prompts
    assert any("AI语义复核规格吻合" in r for r in by_item["it_border"]["candidates"][0]["reasons"])

    sm = rep["smart_summary"]
    assert sm["total_active_orders"] == 5
    assert sm["reconciled_progress_pct"] == 0
    assert sm["projected_coverage_pct"] >= 50
    assert sm["effective_window_coverage_pct"] == 80
    assert sm["matched_sales_pct"] > 0
    assert sm["shortage_breakdown"] and sm["shortage_breakdown"][0]["count"] == 1
    db.close()
    engine.dispose()


def test_suggestions_use_ai_does_not_crash_when_gemini_finished_has_zero_candidates(tmp_path):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_empty_fin,ord_empty_fin,1093 | 颜色分类:18个月独享满血Pro（带邮箱成品号）,Gemini Pro 18个月,1,2026-10-28 12:00:00,completed,45.00,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31", use_ai=True)
    assert rep["suggestions"] == []
    assert rep["smart_summary"]["actionable_missing_count"] == 1
    assert rep["smart_summary"]["unmatched_reasons"]["supply_exhausted"] == 1
    db.close()
    engine.dispose()


def test_duration_fallback_one_way_for_1m_activation_and_not_reverse(tmp_path):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_1m,ord_1m,1093 | 颜色分类:一个月（包售后）,Gemini Pro 18个月Pro会员充到自己号 无需账密一键领取,1,2026-10-20 21:09:33,completed,6.11,none,0\n"
        "it_18m,ord_18m,1093 | 颜色分类:18个月（激活资格）,Gemini Pro 18个月,1,2026-10-21 10:00:00,completed,26.88,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "FF_18M_FOR_1M",
            "product": "JIO Gemini pro 18个月 兑换链接",
            "merchant": "卡商A",
            "qty": 1,
            "amount": "3.11",
            "paid_at": "2026-10-20 21:16:31",
            "status": "已完成",
        },
        {
            "order_id": "FF_1M_FOR_18M",
            "product": "Gemini pro 1个月 兑换链接",
            "merchant": "卡商B",
            "qty": 1,
            "amount": "4.00",
            "paid_at": "2026-10-21 10:02:00",
            "status": "已完成",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )

    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31")
    by_item = {s["item_id"]: s for s in rep["suggestions"]}
    assert "it_1m" in by_item
    c_1m = by_item["it_1m"]["candidates"][0]
    assert c_1m["purchase_external_id"] == "FF_18M_FOR_1M"
    assert c_1m["confidence_level"] == "review"
    assert c_1m["auto_confirmable"] is False
    assert any("周期不一致" in w for w in c_1m["warnings"])
    assert "it_18m" not in by_item
    db.close()
    engine.dispose()


def test_bulk_finished_account_72h_backward_trace_and_single_limit(tmp_path):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_bulk_hit,ord_bulk_hit,1093 | 颜色分类:3个月独享满血Pro（带邮箱成品号）,Gemini Pro,1,2026-10-24 12:00:00,completed,48.00,none,0\n"
        "it_single_miss,ord_single_miss,1093 | 颜色分类:3个月独享满血Pro（带邮箱成品号）,Gemini Pro,1,2026-10-10 12:00:00,completed,48.00,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "LD_BULK_72H",
            "product": "Gemini Pro 3个月 成品号",
            "merchant": "链动小铺",
            "qty": 3,
            "amount": "45.00",
            "paid_at": "2026-10-26 12:00:00",
            "status": "已付款",
        },
        {
            "order_id": "FF_SINGLE_5H",
            "product": "Gemini Pro 3个月 成品号",
            "merchant": "卡商C",
            "qty": 1,
            "amount": "15.00",
            "paid_at": "2026-10-10 17:00:00",
            "status": "已完成",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )

    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31")
    by_item = {s["item_id"]: s for s in rep["suggestions"]}
    assert "it_bulk_hit" in by_item
    c_bulk = by_item["it_bulk_hit"]["candidates"][0]
    assert c_bulk["purchase_external_id"] == "LD_BULK_72H"
    assert c_bulk["confidence_level"] == "review"
    assert c_bulk["auto_confirmable"] is False
    assert any("晚于下单" in w for w in c_bulk["warnings"])
    assert "it_single_miss" not in by_item
    db.close()
    engine.dispose()


def test_margin_lead_non_negative_and_backup_remaining_excludes_exhausted_stock(tmp_path):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_o1,ord_o1,1093 | 颜色分类:18个月独享满血Pro（激活资格）,Gemini Pro 18个月,1,2026-10-22 10:00:00,completed,26.88,none,0\n"
        "it_o2,ord_o2,1093 | 颜色分类:18个月独享满血Pro（激活资格）,Gemini Pro 18个月,1,2026-10-22 10:04:00,completed,26.88,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "FF_P1",
            "product": "Gemini Pro 18个月 激活链接",
            "merchant": "卡商",
            "qty": 1,
            "amount": "7.55",
            "paid_at": "2026-10-22 10:01:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_P2",
            "product": "Gemini Pro 18个月 激活链接",
            "merchant": "卡商",
            "qty": 1,
            "amount": "7.55",
            "paid_at": "2026-10-22 10:05:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_P3",
            "product": "Gemini Pro 18个月 激活链接",
            "merchant": "卡商",
            "qty": 1,
            "amount": "7.55",
            "paid_at": "2026-10-22 10:20:00",
            "status": "已完成",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )

    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31")
    assert rep["suggestion_count"] == 2
    for s in rep["suggestions"]:
        assert s["candidates"][0]["margin_lead"] >= 0
        for backup in s["candidates"][1:]:
            assert backup["remaining_quantity"] >= backup["allocated_quantity"]
    db.close()
    engine.dispose()


def test_unmatched_reasons_subcategories_sum_to_supply_exhausted_and_distinguish_time_vs_stock(tmp_path):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_act_win,ord_act_win,1093 | 颜色分类:18个月独享满血Pro（激活资格）,Gemini Pro 18个月,1,2026-10-22 10:01:00,completed,26.88,none,0\n"
        "it_act_time_miss,ord_act_time_miss,1093 | 颜色分类:18个月独享满血Pro（激活资格）,Gemini Pro 18个月,1,2026-10-22 10:00:00,completed,26.88,none,0\n"
        "it_fin_win,ord_fin_win,1093 | 颜色分类:3个月独享满血Pro（带邮箱成品号）,Gemini Pro 3个月,1,2026-10-26 10:01:00,completed,48.00,none,0\n"
        "it_fin_exhaust,ord_fin_exhaust,1093 | 颜色分类:3个月独享满血Pro（带邮箱成品号）,Gemini Pro 3个月,1,2026-10-26 10:00:00,completed,48.00,none,0\n"
        "it_no_stock,ord_no_stock,1093 | 颜色分类:Claude Pro月卡,Claude Pro,1,2026-10-26 11:00:00,completed,99.00,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "FF_ACT_INWIN",
            "product": "Gemini Pro 18个月 激活链接",
            "merchant": "卡商",
            "qty": 1,
            "amount": "7.55",
            "paid_at": "2026-10-22 10:02:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_ACT_LATER",
            "product": "Gemini Pro 18个月 激活链接",
            "merchant": "卡商",
            "qty": 1,
            "amount": "7.55",
            "paid_at": "2026-10-29 10:00:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_FIN_INWIN",
            "product": "Gemini Pro 3个月 成品号",
            "merchant": "卡商",
            "qty": 1,
            "amount": "15.00",
            "paid_at": "2026-10-26 10:02:00",
            "status": "已完成",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )

    rep = card_reconciliation.suggestions(db, 1, "2026-10-01", "2026-10-31")
    ur = rep["smart_summary"]["unmatched_reasons"]
    sub_sum = (
        ur["no_compatible_stock"]
        + ur["inventory_exhausted"]
        + ur["time_window_mismatch"]
        + ur["spec_mismatch"]
        + ur["low_confidence"]
    )
    assert ur["supply_exhausted"] == 3
    assert sub_sum == ur["supply_exhausted"]
    assert ur["time_window_mismatch"] == 1
    assert ur["inventory_exhausted"] == 1
    assert ur["no_compatible_stock"] == 1
    db.close()
    engine.dispose()


def test_ledger_counts_include_purchases_and_orders_excluded(tmp_path):
    engine, db = setup_db(tmp_path)
    orders_csv = (
        "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
        "it_pend,ord_pend,1093 | 颜色分类:18个月独享满血Pro（激活资格）,Gemini Pro,1,2026-10-22 10:00:00,completed,26.88,none,0\n"
        "it_ref,ord_ref,1093 | 颜色分类:18个月独享满血Pro（激活资格）,Gemini Pro,1,2026-10-22 10:05:00,closed,26.88,success,26.88\n"
        "it_diff,ord_diff,1093 | 颜色分类:补差价专拍,补差价,1,2026-10-22 10:10:00,completed,5.00,none,0\n"
    ).encode("utf-8")
    prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
    importer.commit(db, 1, prev["batch_id"])

    purchases_payload = [
        {
            "order_id": "FF_ACT_KEEP",
            "product": "Gemini Pro 18个月 激活链接",
            "merchant": "卡商",
            "qty": 1,
            "amount": "7.55",
            "paid_at": "2026-10-22 10:01:00",
            "status": "已完成",
        },
        {
            "order_id": "FF_REF_EXCL",
            "product": "Gemini Pro 18个月 激活链接",
            "merchant": "卡商",
            "qty": 1,
            "amount": "7.55",
            "payment_refund": "7.55",
            "paid_at": "2026-10-22 10:02:00",
            "status": "已退款",
        },
    ]
    card_reconciliation.import_purchases(
        db, 1, "cards.json", json.dumps(purchases_payload, ensure_ascii=False).encode("utf-8")
    )

    led = card_reconciliation.ledger(db, 1, limit=50)
    counts = led["counts"]
    assert counts["purchases_excluded"] == 1
    assert counts["orders_excluded"] == 2
    assert "purchases_partial" in counts
    db.close()
    engine.dispose()
