from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import importer, qianniu_chats
from app.auth import passwords
from app.db import Base, get_db, make_engine
from app.main import app
from app.models import Membership, Store, User


def test_sanitize_preserves_order_ids_and_masks_credentials():
    raw = (
        "你好，订单号 5127828516012069932 卡网单号 LD261117SUN89L 这个Gemini 18个月成品号质保多久？"
        "登录密码 testpass123456 密码：MySecret#99 2FA密钥 JBSWY3DPEHPK3PXP 辅助邮箱 demo_user@gmail.com "
        "提示要2FA验证码怎么弄？"
    )
    cleaned = qianniu_chats.sanitize_chat_text(raw)
    assert "5127828516012069932" in cleaned
    assert "LD261117SUN89L" in cleaned
    assert "testpass123456" not in cleaned
    assert "MySecret#99" not in cleaned
    assert "JBSWY3DPEHPK3PXP" not in cleaned
    assert "demo_user@gmail.com" not in cleaned
    assert qianniu_chats.REDACTED_TOKEN in cleaned


def test_qianniu_chat_import_dedup_knowledge_and_api_flow(tmp_path):
    engine = make_engine("sqlite:///" + (tmp_path / "chats.db").as_posix())
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    with factory() as db:
        db.add_all([
            Store(id=1, name="小猫测试店"),
            Store(id=2, name="隔离店铺"),
            User(id=1, username="owner", password_hash=passwords.hash("pw123456"), is_admin=True),
        ])
        db.flush()
        db.add(Membership(user_id=1, store_id=1))
        db.commit()

        orders_csv = (
            "item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\n"
            "I_CHAT_1,5127828516012069932,18个月成品号,Gemini Pro 18个月成品号,1,2026-11-17 10:00:00,completed,36.80,none,0\n"
        ).encode("utf-8")
        prev = importer.preview(db, 1, "orders", "orders.csv", orders_csv, "gross")
        importer.commit(db, 1, prev["batch_id"])

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            login_resp = client.post("/api/login", json={"username": "owner", "password": "pw123456"})
            assert login_resp.status_code == 200
            headers = {"X-CSRF-Token": login_resp.json()["csrf"]}

            # Tenant isolation check
            assert client.get("/api/stores/2/chats/overview", headers=headers).status_code == 403

            paste_dialogue = """买家_阿哲 2026-11-17 10:05:12
你好，订单号 5127828516012069932 这个Gemini 18个月成品号质保多久？登录密码 testpass123456 提示要2FA验证码怎么弄？到期能续费吗？
小猫测试店:小二 2026-11-17 10:06:00
亲，成品号首登质保6小时，打开2FA接码工具粘贴发货卡密里的密钥即可生成6位验证码登录，后续到期也支持续费或换新号哦！
---
买家_林木 2026-11-17 11:20:00
请问店里有Claude独享号吗？或者一次买10个Gemini激活链接有团购优惠吗？
小猫测试店:小二 2026-11-17 11:21:10
亲，目前现货主营Gemini Pro成品号和激活链接，拍5件以上可享工作室阶梯团购价，Claude需求已帮您登记反馈选品啦！"""

            imp1 = client.post(
                "/api/stores/1/chats/import-text",
                headers=headers,
                json={"text": paste_dialogue, "use_ai": False},
            )
            assert imp1.status_code == 200, imp1.text
            b1 = imp1.json()
            assert b1["inserted"] == 2
            assert b1["knowledge_updated"] >= 1

            # Re-importing identical text deduplicates cleanly
            imp_dup = client.post(
                "/api/stores/1/chats/import-text",
                headers=headers,
                json={"text": paste_dialogue, "use_ai": False},
            )
            assert imp_dup.status_code == 200
            assert imp_dup.json()["duplicate_count"] == 2

            # Markdown quality-desk table file import
            md_table = (
                "| 会员名称 | 订单编号 | 接待客服 | 命中会话文本 | 会话时间 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 张三丰 | 5127828516012069932 | 小二 | 买家: 家庭组提示不同国家无法加入怎么办？ 客服: 亲，需谷歌账号地区一致才能加入同区家庭组哦 | 2026-11-17 14:00:00 |\n"
            ).encode("utf-8")
            imp_file = client.post(
                "/api/stores/1/chats/import-file",
                headers=headers,
                files={"file": ("quality.md", md_table, "text/markdown")},
                data={"use_ai": "false"},
            )
            assert imp_file.status_code == 200, imp_file.text
            assert imp_file.json()["inserted"] == 1

            # Overview + linked order + redaction check
            ov = client.get("/api/stores/1/chats/overview", headers=headers).json()
            assert ov["counts"]["total_sessions"] == 3
            assert ov["counts"]["linked_order_sessions"] >= 1
            all_msgs = " ".join(
                m["text"] for s in ov["sessions"] for m in s["messages"]
            )
            assert "testpass123456" not in all_msgs
            assert "5127828516012069932" in all_msgs

            # Toggle resolved on first session
            first_id = ov["sessions"][0]["id"]
            tog = client.post(
                f"/api/stores/1/chats/sessions/{first_id}/toggle-resolved",
                headers=headers,
                json={"resolved": True},
            )
            assert tog.status_code == 200
            assert tog.json()["resolved"] is True

            # Upsert & delete custom knowledge card
            kn_create = client.post(
                "/api/stores/1/chats/knowledge",
                headers=headers,
                json={
                    "topic": "login_2fa",
                    "product_category": "gemini_finished",
                    "title": "自定义2FA快捷回复",
                    "trigger_question": "2FA验证码在哪里看？",
                    "standard_reply": "打开接码站输入密钥即可获取6位动态码。",
                    "is_pinned": True,
                },
            )
            assert kn_create.status_code == 200, kn_create.text
            kn_id = kn_create.json()["id"]
            assert kn_create.json()["is_pinned"] is True

            # Deterministic Q&A query
            qa = client.post(
                "/api/stores/1/chats/query",
                headers=headers,
                json={"question": "买家最常问什么痛点？有哪些新需求？", "use_ai": False},
            )
            assert qa.status_code == 200, qa.text
            qa_body = qa.json()
            assert qa_body["counts"]["total_sessions"] == 3
            assert len(qa_body["topic_ranking"]) >= 1
            assert qa_body["answer"]

            kn_del = client.delete(f"/api/stores/1/chats/knowledge/{kn_id}", headers=headers)
            assert kn_del.status_code == 200
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
