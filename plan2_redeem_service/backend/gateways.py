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

from .agiso import PLAN2_CODE_RE


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

    @property
    def is_ready(self) -> bool:
        return bool(self.base_url and self.api_key.strip() and self.api_secret.strip())

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

    def stock_secret_list(
        self,
        goods_no: str,
        *,
        status: Optional[int] = None,
        keywords: str = "",
        page_no: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "goods_no": goods_no,
            "page_no": max(1, int(page_no)),
            "page_size": max(1, min(100, int(page_size))),
        }
        if status is not None:
            params["status"] = int(status)
        if keywords:
            params["keywords"] = keywords
        return self._call("openApi/stockSecret/list", params)

    def verify_order(self, code: str, goods_no: str) -> dict[str, Any]:
        card_pattern = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(code)}(?![A-Za-z0-9-])")

        def matches_single_code(text: str) -> bool:
            return bool(card_pattern.search(text)) and len(set(PLAN2_CODE_RE.findall(text))) <= 1

        trade_no = ""
        stock = self.stock_secret_list(goods_no, keywords=code, page_no=1, page_size=20)
        stock_matches = [
            item
            for item in (stock.get("list") or [])
            if isinstance(item, dict) and card_pattern.search(str(item.get("secret_code") or ""))
        ]
        if len(stock_matches) > 1:
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
        if len(stock_matches) == 1:
            matched = stock_matches[0]
            try:
                secret_status = int(matched.get("status", -1))
            except (TypeError, ValueError):
                secret_status = -1
            trade_no = str(matched.get("trade_no") or "").strip()
            if secret_status != 2 or not trade_no or not matches_single_code(str(matched.get("secret_code") or "")):
                return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
        else:
            listing = self._call("openApi/order/list", {"card_no": code, "page_no": 1, "page_size": 20})
            orders = listing.get("list") or []
            if len(orders) != 1:
                return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
            summary = orders[0]
            if summary.get("identity") != "merchant":
                return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
            trade_no = str(summary.get("trade_no") or "").strip()
            if not trade_no:
                return {"ok": False, "error": "ORDER_NOT_ELIGIBLE"}
        detail = self._call("openApi/order/info", {"trade_no": trade_no})
        detail_identity = detail.get("identity")
        if detail_identity is not None and detail_identity != "merchant":
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": trade_no}
        cards = detail.get("cards") or []
        code_in_cards = any(
            matches_single_code(str((item.get("code") if isinstance(item, dict) else item) or ""))
            for item in cards
        )
        if detail.get("trade_no") not in (None, "", trade_no) or detail.get("goods_no") != goods_no or not code_in_cards:
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": trade_no}
        try:
            status = int(detail.get("status", -1))
        except (TypeError, ValueError):
            status = -1
        if status in (2, 3):
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": trade_no, "final_rejection": True}
        if status not in (4, 5):
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": trade_no}
        complaints = self._call("openApi/complaint/list", {"keywords": trade_no, "page_no": 1, "page_size": 20})
        for item in complaints.get("list") or []:
            if not isinstance(item, dict) or item.get("trade_no") != trade_no:
                continue
            try:
                complaint_status = int(item.get("status", -1))
            except (TypeError, ValueError):
                complaint_status = -1
            if complaint_status in (0, 1):
                return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": trade_no, "final_rejection": True}
            if complaint_status == 2:
                complaint_no = str(item.get("complaint_no") or "").strip()
                if not complaint_no:
                    return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": trade_no}
                complaint_detail = self._call("openApi/complaint/detail", {"complaint_no": complaint_no})
                try:
                    complaint_result = int(complaint_detail.get("result", 0))
                except (TypeError, ValueError):
                    complaint_result = 0
                if complaint_result == 2:
                    return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": trade_no, "final_rejection": True}
        return {"ok": True, "trade_no": trade_no}

    def stock_secret_add(self, goods_no: str, content: str) -> dict[str, Any]:
        return self._call("openApi/stockSecretBatch/add", {"goods_no": goods_no, "content": content, "split_type": 2, "remove_repeat": 1})

    def invalidate(self, goods_no: str, secret_ids: list[int]) -> dict[str, Any]:
        return self._call("openApi/stockSecret/invalidate", {"goods_no": goods_no, "ids": secret_ids})


def _parse_rule_int(
    entry: dict[str, Any],
    key: str,
    *,
    default: Optional[int] = None,
    minimum: int = 0,
    maximum: Optional[int] = None,
    idx: int = 0,
) -> int:
    raw = entry.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if default is None:
            raise ValueError(f"16688 restock rule at index {idx} requires {key}")
        value = default
    elif isinstance(raw, bool):
        raise ValueError(f"16688 restock rule at index {idx} field {key} must be an integer")
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"16688 restock rule at index {idx} field {key} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"16688 restock rule at index {idx} field {key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"16688 restock rule at index {idx} field {key} must be <= {maximum}")
    return value


def parse_env_channel16688_restock_rules(raw_json: str) -> list[dict[str, Any]]:
    text = (raw_json or "").strip()
    if not text:
        return []
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("PLAN2_CHANNEL16688_RESTOCK_RULES_JSON must be a JSON array")
    normalized: list[dict[str, Any]] = []
    seen_default_skus: set[str] = set()
    seen_goods: set[str] = set()
    for idx, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ValueError(f"16688 restock rule at index {idx} must be an object")
        sku_key = str(entry.get("sku_key") or "").strip()
        if not sku_key:
            raise ValueError(f"16688 restock rule at index {idx} requires sku_key")
        goods_no = str(entry.get("goods_no") or "").strip()
        if goods_no:
            if goods_no in seen_goods:
                raise ValueError(f"16688 restock rule at index {idx} has duplicate goods_no: {goods_no}")
            seen_goods.add(goods_no)
        else:
            if sku_key in seen_default_skus:
                raise ValueError(f"16688 restock rule at index {idx} has duplicate default sku_key: {sku_key}")
            seen_default_skus.add(sku_key)
        min_stock = _parse_rule_int(entry, "min_stock", default=0, minimum=0, idx=idx)
        target_stock = _parse_rule_int(entry, "target_stock", minimum=max(1, min_stock), idx=idx)
        max_push = _parse_rule_int(entry, "max_push", default=20, minimum=1, maximum=500, idx=idx)
        normalized.append(
            {
                "sku_key": sku_key,
                "goods_no": goods_no,
                "min_stock": min_stock,
                "target_stock": target_stock,
                "max_push": max_push,
                "enabled": bool(entry.get("enabled", True)),
                "note": str(entry.get("note") or "").strip()[:200],
            }
        )
    return normalized


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
        self.stock: dict[str, list[dict[str, Any]]] = {}
        self._next_id = 1

    @property
    def is_ready(self) -> bool:
        return True

    def stock_secret_add(self, goods_no: str, content: str) -> dict[str, Any]:
        blocks = [block.strip() for block in content.split("\n\n") if block.strip()]
        bucket = self.stock.setdefault(goods_no, [])
        existing = {entry["secret_code"] for entry in bucket}
        added = 0
        dup = 0
        for block in blocks:
            if block in existing:
                dup += 1
                continue
            existing.add(block)
            bucket.append(
                {
                    "id": self._next_id,
                    "secret_code": block,
                    "batch_no": "DRY-BATCH",
                    "status": 0,
                    "first": 0,
                    "trade_no": "",
                }
            )
            self._next_id += 1
            added += 1
        self.imported.extend([line for line in content.splitlines() if line.startswith("CODE-")])
        available = sum(1 for item in bucket if item["status"] == 0)
        return {
            "goods_no": goods_no,
            "import_count": added,
            "stock_dup_count": dup,
            "available_quantity": available,
        }

    def stock_secret_list(
        self,
        goods_no: str,
        *,
        status: Optional[int] = None,
        keywords: str = "",
        page_no: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        bucket = list(self.stock.get(goods_no, []))
        available = sum(1 for item in bucket if item["status"] == 0)
        locked = sum(1 for item in bucket if item["status"] == 1)
        sold = sum(1 for item in bucket if item["status"] == 2)
        filter_map = {0: 0, 1: 2, 3: 3, 4: 4}
        filtered = bucket
        if status is not None:
            target = filter_map.get(int(status), int(status))
            filtered = [item for item in filtered if item["status"] == target]
        if keywords:
            filtered = [
                item
                for item in filtered
                if keywords in item["secret_code"] or keywords in str(item.get("trade_no") or "")
            ]
        start = (max(1, page_no) - 1) * max(1, page_size)
        end = start + max(1, page_size)
        return {
            "goods_no": goods_no,
            "total": len(filtered),
            "available_quantity": available,
            "locked_quantity": locked,
            "sold_quantity": sold,
            "list": [dict(item) for item in filtered[start:end]],
        }

    def verify_order(self, code: str, goods_no: str) -> dict[str, Any]:
        return {"ok": True, "trade_no": "DRY-16688-" + code[-8:]}

