from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Optional

import httpx


logger = logging.getLogger(__name__)
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
_KEY_PARAM_RE = re.compile(r"(key=)[^&\s\"']+", re.IGNORECASE)


def _sanitize_error_text(text: str, secret: str = "", *extra_secrets: str) -> str:
    cleaned = (text or "").strip()
    for candidate in (secret, *extra_secrets):
        if candidate:
            cleaned = cleaned.replace(candidate, "[REDACTED]")
    cleaned = _KEY_PARAM_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:200]


def _extract_response_error_detail(response: Any, secret: str = "", *extra_secrets: str) -> str:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("msg") or payload.get("error") or "")
    except Exception:
        detail = str(getattr(response, "text", "") or "")
    return _sanitize_error_text(detail, secret, *extra_secrets)


class UpstreamTimeout(Exception):
    pass


class UpstreamError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        retry_after: Optional[int] = None,
        rejected: bool = False,
    ):
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        self.rejected = rejected
        super().__init__(message)


class CanbosoClient:
    def __init__(
        self,
        base_url: str,
        api_secret: str,
        timeout: float = 25.0,
        products_cache_ttl_seconds: float = 60.0,
        max_get_retries: int = 2,
    ):
        self.base_url, self.api_secret, self.timeout = base_url.rstrip("/"), api_secret, timeout
        self.products_cache_ttl_seconds = max(0.0, float(products_cache_ttl_seconds))
        self.max_get_retries = max(0, int(max_get_retries))
        self._products_cache: Optional[tuple[float, dict[str, Any]]] = None
        self._products_lock = threading.Lock()

    def _request(self, method: str, path: str, _retry_429: bool = True, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        is_get = method.upper() == "GET"
        max_attempts = 1 + (self.max_get_retries if is_get else 0)
        for attempt in range(max_attempts):
            try:
                r = httpx.request(method, self.base_url + path, headers=headers, timeout=self.timeout, **kwargs)
            except httpx.TimeoutException as e:
                if attempt + 1 < max_attempts:
                    backoff = 0.5 * (attempt + 1)
                    logger.warning("canboso %s %s timed out (attempt %s/%s), retrying in %.1fs", method, path, attempt + 1, max_attempts, backoff)
                    time.sleep(backoff)
                    continue
                raise UpstreamTimeout(f"{method} {path} timed out after {self.timeout:g}s") from e
            except httpx.HTTPError as e:
                if attempt + 1 < max_attempts:
                    backoff = 0.5 * (attempt + 1)
                    logger.warning("canboso %s %s network error %s (attempt %s/%s), retrying in %.1fs", method, path, type(e).__name__, attempt + 1, max_attempts, backoff)
                    time.sleep(backoff)
                    continue
                raise UpstreamError(503, f"upstream network error ({type(e).__name__})") from e
            retry = r.headers.get("Retry-After")
            retry_after = int(retry) if retry and retry.isdigit() else None
            if r.status_code == 429 and _retry_429:
                time.sleep(min(max(retry_after or 1, 1), 60))
                return self._request(method, path, _retry_429=False, headers=headers, **kwargs)
            if r.status_code in TRANSIENT_HTTP_STATUSES and attempt + 1 < max_attempts:
                backoff = 0.5 * (attempt + 1)
                logger.warning("canboso %s %s returned HTTP %s (attempt %s/%s), retrying in %.1fs", method, path, r.status_code, attempt + 1, max_attempts, backoff)
                time.sleep(backoff)
                continue
            if r.status_code >= 400:
                detail = _extract_response_error_detail(r, self.api_secret)
                message = f"upstream HTTP {r.status_code}: {detail}" if detail else f"upstream HTTP {r.status_code}"
                raise UpstreamError(r.status_code, message, retry_after)
            try:
                data = r.json()
            except ValueError as e:
                if attempt + 1 < max_attempts:
                    backoff = 0.5 * (attempt + 1)
                    logger.warning("canboso %s %s returned invalid JSON (attempt %s/%s), retrying in %.1fs", method, path, attempt + 1, max_attempts, backoff)
                    time.sleep(backoff)
                    continue
                raise UpstreamError(502, "invalid upstream response") from e
            if isinstance(data, dict) and data.get("success") is False:
                detail = _sanitize_error_text(str(data.get("message") or data.get("error") or "upstream rejected request"), self.api_secret)
                raise UpstreamError(502, detail or "upstream rejected request", retry_after)
            return data
        raise UpstreamError(503, "upstream request exhausted retries")

    def invalidate_products_cache(self) -> None:
        with self._products_lock:
            self._products_cache = None

    def list_products(self, force_refresh: bool = False) -> dict[str, Any]:
        if self.products_cache_ttl_seconds > 0 and not force_refresh:
            now = time.monotonic()
            with self._products_lock:
                if self._products_cache is not None:
                    cached_at, cached_data = self._products_cache
                    if now - cached_at < self.products_cache_ttl_seconds:
                        return cached_data
        data = self._request("GET", "/api/v2/telegram-buyer/products", params={"key": self.api_secret})
        if self.products_cache_ttl_seconds > 0:
            with self._products_lock:
                self._products_cache = (time.monotonic(), data)
        return data

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
        normalized_path = path.strip("/")
        controller, action = normalized_path.split("/")[-2:]
        canonical = "&".join(f"{key}={_canonical_value(params[key])}" for key in sorted(params))
        plain = f"POST\n{controller.lower()}\n{action.lower()}\n{canonical}"
        params["sign"] = hmac.new(self.api_secret.encode(), plain.encode(), hashlib.sha256).hexdigest()
        if self.base_url.lower().endswith("/openapi") and normalized_path.lower().startswith("openapi/"):
            url = self.base_url + "/" + normalized_path[len("openapi/"):]
        else:
            url = self.base_url + "/" + normalized_path
        try:
            r = httpx.post(url, json=params, timeout=self.timeout)
        except httpx.TimeoutException as e:
            detail = _sanitize_error_text(str(e) or "16688 request timed out", self.api_secret, self.api_key)
            raise UpstreamTimeout(detail or "16688 request timed out") from e
        except httpx.ConnectError as e:
            raise UpstreamError(503, f"16688 network error ({type(e).__name__})", rejected=True) from e
        except httpx.HTTPError as e:
            raise UpstreamError(503, f"16688 network error ({type(e).__name__})", rejected=False) from e
        if r.status_code >= 400:
            detail = _extract_response_error_detail(r, self.api_secret, self.api_key)
            message = f"16688 HTTP {r.status_code}: {detail}" if detail else f"16688 HTTP {r.status_code}"
            raise UpstreamError(r.status_code, message, rejected=(400 <= r.status_code < 500))
        try:
            body = r.json()
        except ValueError as e:
            raise UpstreamError(502, "invalid 16688 response", rejected=False) from e
        if not isinstance(body, dict):
            raise UpstreamError(502, "invalid 16688 response", rejected=False)
        if body.get("code") not in (None, 1, "1"):
            detail = _sanitize_error_text(
                str(body.get("msg") or body.get("message") or body.get("error") or ""),
                self.api_secret,
                self.api_key,
            )
            message = detail or f"16688 business error (code={body.get('code')})"
            raise UpstreamError(502, message, rejected=True)
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

    def list_products(self, force_refresh: bool = False) -> dict[str, Any]:
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

