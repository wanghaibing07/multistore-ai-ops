from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import qianniu_orders
from app import importer
from app.db import Base, make_engine
from app.models import Store


@pytest.fixture
def db(tmp_path):
    engine = make_engine("sqlite:///" + (tmp_path / "test.db").as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Store(name="shop"))
        session.commit()
        yield session
    engine.dispose()


def test_qianniu_ai_preview_reuses_order_import_pipeline(db):
    def runner(_script):
        return {
            "ok": True,
            "pages": 2,
            "rows": [
                {
                    "item_id": "item-1",
                    "order_id": "order-1",
                    "title": "Gemini Pro 成品号",
                    "property": "18个月",
                    "quantity": "1",
                    "paid_at": "2026-11-20 10:00:00",
                    "status": "卖家已发货",
                    "payment": "15.99",
                    "refund_status": "无退款",
                    "refund_amount": "",
                },
                {
                    "item_id": "item-2",
                    "order_id": "order-1",
                    "title": "Muse 成品号",
                    "property": "已激活",
                    "quantity": "1",
                    "paid_at": "2026-11-20 10:00:00",
                    "status": "卖家已发货",
                    "payment": "9.99",
                    "refund_status": "无退款",
                    "refund_amount": "",
                },
            ],
        }

    result = qianniu_orders.preview_from_browser(db, 1, runner=runner)

    assert result["source"] == "qianniu-ai"
    assert result["source_pages"] == 2
    assert result["row_count"] == 2
    assert result["errors"] == []
    assert result["preview"][0]["order_external_id"] == "order-1"
    assert result["preview"][0]["sku"] == "Gemini Pro 成品号 | 18个月"


def test_qianniu_ai_requires_login():
    def runner(_script):
        return {"ok": False, "status": "needs_login"}

    with pytest.raises(importer.ImportProblem, match="完成登录"):
        qianniu_orders.preview_from_browser(None, 1, runner=runner)
