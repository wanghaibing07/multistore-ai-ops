from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

import httpx


class UpstreamTimeout(Exception):
    pass


class UpstreamError(Exception):
    def __init__(self, status_code: int, message: str, retry_after: Optional[int] = None):
        self.status_code, self.message, self.retry_after = status_code, message, retry_after
        super().__init__(message)


class CanbosoClient:
    def __init__(self, base_url: str, api_secret: str, timeout: float = 15):
        self.base_url, self.api_secret, self.timeout = base_url.rstrip("/"), api_secret, timeout

    def _request(self, method: str, path: str, _retry_429: bool = True, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        try:
            r = httpx.request(method, self.base_url + path, headers=headers, timeout=self.timeout, **kwargs)
        except httpx.TimeoutException as e:
            raise UpstreamTimeout(str(e)) from e
        except httpx.HTTPError as e:
            raise UpstreamError(503, "upstream network error") from e
        retry = r.headers.get("Retry-After")
        retry_after = int(retry) if retry and retry.isdigit() else None
        if r.status_code == 429 and _retry_429:
            import time as _time
            _time.sleep(min(max(retry_after or 1, 1), 60))
            return self._request(method, path, _retry_429=False, headers=headers, **kwargs)
        if r.status_code >= 400:
            raise UpstreamError(r.status_code, "upstream rejected request", retry_after)
        try:
            return r.json()
        except ValueError as e:
            raise UpstreamError(502, "invalid upstream response") from e

    def list_products(self) -> dict[str, Any]:
        return self._request("GET", "/api/v2/telegram-buyer/products", params={"key": self.api_secret})

    def get_balance(self) -> dict[str, Any]:
        return self._request("GET", "/api/v2/telegram-buyer/balance", params={"key": self.api_secret})

    def purchase(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_payload = {**payload, "key": self.api_secret}
        return self._request("POST", "/api/v2/telegram-buyer/purchase", headers={"Idempotency-Key": key}, json=request_payload)

    def get_order(self, order_code: str) -> dict[str, Any]:
        return self._request("GET", "/api/v2/telegram-buyer/orders", params={"key": self.api_secret, "orderCode": order_code})


class Channel16688Client:
    """16688 Open API adapter. Paths keep the platform's camelCase spelling."""

    def __init__(self, base_url: str, api_key: str, api_secret: str, timeout: float = 15):
        self.base_url, self.api_key, self.api_secret, self.timeout = base_url.rstrip("/"), api_key, api_secret, timeout

    def _call(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        nonce = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(24))
        params = {**params, "app_id": self.api_key, "nonce": nonce, "timestamp": int(time.time())}
        controller, action = path.strip("/").split("/")[-2:]
        canonical = "&".join(f"{key}={_canonical_value(params[key])}" for key in sorted(params))
        plain = f"POST\n{controller.lower()}\n{action.lower()}\n{canonical}"
        params["sign"] = hmac.new(self.api_secret.encode(), plain.encode(), hashlib.sha256).hexdigest()
        try:
            r = httpx.post(self.base_url + "/" + path.strip("/"), json=params, timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise UpstreamTimeout(str(e)) from e
        except httpx.HTTPError as e:
            raise UpstreamError(503, "16688 network error") from e
        if r.status_code >= 400:
            raise UpstreamError(r.status_code, "16688 rejected request")
        try:
            body = r.json()
        except ValueError as e:
            raise UpstreamError(502, "invalid 16688 response") from e
        if body.get("code") not in (None, 1, "1"):
            raise UpstreamError(502, "16688 business error")
        return body.get("data") or body

    def verify_order(self, code: str, goods_no: str) -> dict[str, Any]:
        listing = self._call("openApi/order/list", {"card_no": code, "page_no": 1, "page_size": 20})
        orders = listing.get("list", [])
        if len(orders) != 1:
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
        summary = orders[0]
        if summary.get("identity") != "merchant":
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
        detail = self._call("openApi/order/info", {"trade_no": summary["trade_no"]})
        cards = detail.get("cards") or []
        code_in_cards = any(code in (item.get("code", "") if isinstance(item, dict) else str(item)) for item in cards)
        if detail.get("goods_no") != goods_no or not code_in_cards:
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": summary["trade_no"]}
        status = int(detail.get("status", -1))
        if status in (2, 3) or status not in (4, 5):
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": summary["trade_no"]}
        complaints = self._call("openApi/complaint/list", {"keywords": summary["trade_no"], "page_no": 1, "page_size": 20})
        for item in complaints.get("list", []):
            if item.get("trade_no") == summary["trade_no"] and int(item.get("status", -1)) in (0, 1):
                return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": summary["trade_no"]}
            if item.get("trade_no") == summary["trade_no"] and int(item.get("status", -1)) == 2:
                detail = self._call("openApi/complaint/detail", {"complaint_no": item.get("complaint_no")})
                if int(detail.get("result", 0)) == 2:
                    return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": summary["trade_no"]}
        return {"ok": True, "trade_no": summary["trade_no"]}

    def stock_secret_add(self, goods_no: str, content: str) -> dict[str, Any]:
        return self._call("openApi/stockSecretBatch/add", {"goods_no": goods_no, "content": content, "split_type": 2, "remove_repeat": 1})

    def invalidate(self, goods_no: str, secret_ids: list[int]) -> dict[str, Any]:
        return self._call("openApi/stockSecret/invalidate", {"goods_no": goods_no, "ids": secret_ids})


def _canonical_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        normalized = _normalize_sign_value(value)
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def _normalize_sign_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_sign_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_sign_value(item) for item in value]
    return value


class DryRunCanboso:
    def __init__(self, initial_balance: float = 1000.0):
        self.balance = initial_balance
        self.products = {
            "6ac26aaeec73a42d6430a837": {"productId": "6ac26aaeec73a42d6430a837", "name": "Google AI Ultra shared slot", "productType": "slot", "price": {"amount": 13.47, "currency": "USD"}, "availability": {"available": None}},
            "6ae03419c0080f9e15f695a8": {"productId": "6ae03419c0080f9e15f695a8", "name": "Google AI Ultra exclusive admin", "productType": "account", "price": {"amount": 67.50, "currency": "USD"}, "availability": {"available": 50}},
        }
        self.idempotent: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.charge_count = 0

    def list_products(self) -> dict[str, Any]:
        return {"walletCurrency": "USD", "products": list(self.products.values())}

    def get_balance(self) -> dict[str, Any]:
        return {"balance": self.balance, "balanceUsd": self.balance, "walletCurrency": "USD"}

    def purchase(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if key in self.idempotent:
            return self.idempotent[key]
        product = self.products[payload["product_id"]]
        quantity = int(payload["quantity"])
        amount = float(product["price"]["amount"]) * quantity
        if self.balance < amount:
            raise UpstreamError(400, "insufficient balance")
        self.balance -= amount
        self.charge_count += 1
        order_code = "DRY-" + secrets.token_hex(6).upper()
        if product["productType"] == "account":
            delivery = {"accounts": [{"user": "store-ultra@example.com", "password": "redacted", "verifyEmail": "redacted"}]}
            response = {"order": {"orderCode": order_code, "status": "completed", "productId": payload["product_id"]}, "payment": {"amount": amount}, "delivery": delivery}
        else:
            response = {"order": {"orderCode": order_code, "status": "paid", "fulfillmentStatus": "waiting_seller", "productId": payload["product_id"]}}
            self.orders[order_code] = {"orderCode": order_code, "status": "paid", "deliveredAt": None}
        self.idempotent[key] = response
        return response

    def get_order(self, order_code: str) -> dict[str, Any]:
        return {"orders": [self.orders[order_code]]}


class DryRun16688:
    def __init__(self):
        self.orders: dict[str, dict[str, Any]] = {}
        self.complaints: set[str] = set()
        self.imported: list[str] = []

    def stock_secret_add(self, goods_no: str, content: str) -> dict[str, Any]:
        self.imported.extend([line for line in content.splitlines() if line.startswith("CODE-")])
        return {"import_count": len(self.imported)}

    def verify_order(self, code: str, goods_no: str) -> dict[str, Any]:
        return {"ok": True, "trade_no": "DRY-16688-" + code[-8:]}

