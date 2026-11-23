from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from plan1_auto_restock.client_16688 import (
    Platform16688AuthError,
    Platform16688Client,
    Platform16688NotFoundError,
    Platform16688ReplayError,
)
from plan1_auto_restock.client_canboso import (
    CanbosoAuthError,
    CanbosoClient,
    CanbosoIdempotencyConflictError,
    CanbosoInsufficientBalanceError,
    CanbosoNetworkUncertainError,
    CanbosoRateLimitError,
)


def test_16688_client_enforces_post_clears_cookies_and_maps_stock_status() -> None:
    seen_requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_requests.append((request.method, request.url.path, body))
        if request.url.path == "/openApi/stockSecret/list":
            assert body["status"] == 1  # input 1 = sold
            return httpx.Response(
                200,
                headers={"Set-Cookie": "PHPSESSID=testcookie; path=/"},
                json={
                    "code": 1,
                    "msg": "success",
                    "data": {
                        "goods_no": "G10001",
                        "total": 2,
                        "available_quantity": 3,
                        "locked_quantity": 1,
                        "sold_quantity": 1,
                        "list": [
                            {"id": 1, "status": 1},
                            {"id": 2, "status": 2},
                        ],
                    },
                },
            )
        if request.url.path == "/openApi/goods/detail":
            return httpx.Response(200, json={"code": 0, "msg": "商品不存在"})
        if request.url.path == "/openApi/merchant/balance":
            return httpx.Response(200, json={"code": 0, "msg": "签名验证失败"})
        if request.url.path == "/openApi/order/list":
            return httpx.Response(200, json={"code": 0, "msg": "请求重复"})
        return httpx.Response(404, text="<html>404</html>")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = Platform16688Client(
        gateway="https://www.16688.com.cn/openApi",
        app_id="2088000000000001",
        secret="a" * 64,
        http_client=http_client,
    )
    stock = client.list_stock_secrets("G10001", filter_status="sold")
    assert seen_requests[0][0] == "POST"
    assert len(http_client.cookies) == 0
    assert stock["list"][0]["status_label"] == "locked"
    assert stock["list"][1]["status_label"] == "sold"

    with pytest.raises(Platform16688NotFoundError):
        client.get_goods_detail("G_MISSING")
    with pytest.raises(Platform16688AuthError):
        client.get_balance()
    with pytest.raises(Platform16688ReplayError):
        client.list_orders()
    with pytest.raises(ValueError):
        client.upsert_goods(name="Bad", image="https://x/a.png", price=Decimal("0.01"))


def test_canboso_client_guardrails_and_auth_circuit_breaker() -> None:
    call_count = {"balance": 0, "products": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/balance"):
            call_count["balance"] += 1
            if request.headers.get("x-buyer-key") == "bad_key":
                return httpx.Response(401, json={"message": "Invalid key"})
            return httpx.Response(
                200,
                headers={"X-RateLimit-Remaining": "58"},
                json={
                        "success": True,
                    "walletCurrency": "USD",
                    "balance": 17.3,
                    "balanceUsd": 17.3,
                    "balanceVnd": 449800,
                    "usdRate": 26000,
                    "balanceText": "$17.30",
                },
            )
        if request.url.path.endswith("/products"):
            call_count["products"] += 1
            return httpx.Response(
                200,
                json={
                        "success": True,
                    "walletCurrency": "USD",
                    "products": [
                        {
                                "productId": "prod_acc_1",
                            "name": "Gemini Account",
                            "productType": "account",
                                "price": {"amount": 1.49, "currency": "USD", "text": "$1.49"},
                                "availability": {"available": 5, "sold": 12},
                        },
                        {
                                "productId": "slot_chatgpt_business",
                            "name": "GPT Slot",
                            "productType": "slot",
                                "price": {"amount": 2.0, "currency": "USD", "text": "$2.00"},
                                "availability": {"available": None, "sold": 3},
                        },
                    ],
                },
            )
        if request.url.path.endswith("/purchase"):
            idem = request.headers.get("Idempotency-Key")
            if idem == "idem-conflict-001":
                return httpx.Response(409, json={"message": "Idempotency conflict"})
            if idem == "idem-lowbal-001":
                    return httpx.Response(400, json={"message": "Insufficient balance", "payment": {"balance": 0.5}})
            if idem == "idem-ratelimit-001":
                return httpx.Response(429, headers={"Retry-After": "12"}, json={"message": "Too many requests"})
            if idem == "idem-timeout-001":
                raise httpx.ReadTimeout("simulated read timeout")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "order": {
                        "orderCode": "CB-1001",
                        "status": "completed",
                        "productId": "prod_acc_1",
                        "productName": "Gemini Account",
                        "productType": "account",
                        "quantity": 1,
                        "bonusQuantity": 0,
                        "finalQuantity": 1,
                    },
                    "payment": {"amount": 1.49, "currency": "USD", "balance": 15.81},
                    "delivery": {"accounts": [{"user": "u1@example.com", "password": "pw1"}]},
                },
            )
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = CanbosoClient(
        base_url="https://canboso.com",
        supplier_keys={"primary": "good_key", "bad": "bad_key"},
        catalog_cache_ttl_seconds=180,
        max_qty_guard=2,
        http_client=http_client,
    )

    # Quantity <= 0 and > max_qty_guard and slot must be hard-blocked before HTTP
    with pytest.raises(ValueError):
        client.purchase_account(supplier="primary", product_id="prod_acc_1", quantity=0, idempotency_key="idem-zero-001")
    with pytest.raises(ValueError):
        client.purchase_account(supplier="primary", product_id="prod_acc_1", quantity=-1, idempotency_key="idem-neg-001")
    with pytest.raises(ValueError):
        client.purchase_account(supplier="primary", product_id="prod_acc_1", quantity=3, idempotency_key="idem-big-001")
    with pytest.raises(ValueError):
        client.purchase_account(
            supplier="primary",
            product_id="slot_chatgpt_business",
            quantity=1,
            idempotency_key="idem-slot-001",
        )

    # Catalog caching + available: null handling
    products_first = client.list_products("primary")
    products_second = client.list_products("primary")
    assert len(products_first) == len(products_second) == 2
    assert call_count["products"] == 1
    slot_prod = client.get_product("primary", "slot_chatgpt_business")
    assert slot_prod is not None and slot_prod.is_available and not slot_prod.is_plan1_compatible

    # 401 trips auth circuit breaker so second call never hits network
    with pytest.raises(CanbosoAuthError):
        client.get_balance("bad")
    assert call_count["balance"] == 1
    with pytest.raises(CanbosoAuthError):
        client.get_balance("bad")
    assert call_count["balance"] == 1

    # Error classification on purchase
    with pytest.raises(CanbosoIdempotencyConflictError):
        client.purchase_account(supplier="primary", product_id="prod_acc_1", quantity=1, idempotency_key="idem-conflict-001")
    with pytest.raises(CanbosoInsufficientBalanceError) as bal_exc:
        client.purchase_account(supplier="primary", product_id="prod_acc_1", quantity=1, idempotency_key="idem-lowbal-001")
    assert bal_exc.value.reported_balance == Decimal("0.5")
    with pytest.raises(CanbosoRateLimitError) as rl_exc:
        client.purchase_account(supplier="primary", product_id="prod_acc_1", quantity=1, idempotency_key="idem-ratelimit-001")
    assert rl_exc.value.retry_after == 12
    with pytest.raises(CanbosoNetworkUncertainError):
        client.purchase_account(supplier="primary", product_id="prod_acc_1", quantity=1, idempotency_key="idem-timeout-001")

    res = client.purchase_account(supplier="primary", product_id="prod_acc_1", quantity=1, idempotency_key="idem-ok-0001")
    assert res.order_code == "CB-1001"
    assert len(res.accounts) == 1 and res.accounts[0].user == "u1@example.com"
