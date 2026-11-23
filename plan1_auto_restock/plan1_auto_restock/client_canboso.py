from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .models import CanbosoAccountItem, CanbosoBalance, CanbosoProduct, CanbosoPurchaseResult


class CanbosoError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, supplier: str = "", raw: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.supplier = supplier
        self.raw = raw


class CanbosoAuthError(CanbosoError):
    pass


class CanbosoRateLimitError(CanbosoError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        status_code: int | None = 429,
        supplier: str = "",
        raw: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, supplier=supplier, raw=raw)
        self.retry_after = retry_after


class CanbosoInsufficientStockError(CanbosoError):
    pass


class CanbosoIdempotencyConflictError(CanbosoError):
    pass


class CanbosoInsufficientBalanceError(CanbosoError):
    def __init__(
        self,
        message: str,
        *,
        reported_balance: Decimal | None = None,
        status_code: int | None = 400,
        supplier: str = "",
        raw: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, supplier=supplier, raw=raw)
        self.reported_balance = reported_balance


class CanbosoNetworkUncertainError(CanbosoError):
    """Raised when POST /purchase timed out or dropped; caller MUST replay with same Idempotency-Key."""


@dataclass
class _CatalogCacheEntry:
    fetched_at: float
    products_by_id: dict[str, CanbosoProduct]
    wallet_currency: str


class CanbosoClient:
    def __init__(
        self,
        *,
        base_url: str,
        supplier_keys: dict[str, str],
        timeout_seconds: float = 20.0,
        catalog_cache_ttl_seconds: int = 180,
        max_qty_guard: int = 10,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._supplier_keys = {name: api_key.strip() for name, api_key in supplier_keys.items() if api_key.strip()}
        self._catalog_ttl = max(30, int(catalog_cache_ttl_seconds))
        self._max_qty_guard = max(1, int(max_qty_guard))
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._catalog_cache: dict[str, _CatalogCacheEntry] = {}
        self._auth_tripped_suppliers: dict[str, str] = {}
        self.last_rate_limits: dict[str, dict[str, str]] = {}
        self.supplier_usd_rates: dict[str, Decimal] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CanbosoClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _require_key(self, supplier: str) -> str:
        if supplier in self._auth_tripped_suppliers:
            raise CanbosoAuthError(
                f"供货商 {supplier} 已因鉴权失败触发熔断（防 5次/分 阶梯封禁）：{self._auth_tripped_suppliers[supplier]}",
                supplier=supplier,
            )
        api_key = self._supplier_keys.get(supplier, "")
        if not api_key:
            raise CanbosoAuthError(f"供货商 {supplier} 未配置 Canboso API Key", supplier=supplier)
        return api_key

    def _record_rate_limits(self, supplier: str, headers: httpx.Headers) -> None:
        captured = {
            header_name: header_value
            for header_name, header_value in headers.items()
            if header_name.lower().startswith("x-ratelimit") or header_name.lower() == "retry-after"
        }
        if captured:
            self.last_rate_limits[supplier] = captured

    def _handle_error_response(self, supplier: str, response: httpx.Response, *, is_purchase: bool = False) -> None:
        self._record_rate_limits(supplier, response.headers)
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text[:200]}

        message = str(body.get("message") or f"HTTP {response.status_code}") if isinstance(body, dict) else str(body)
        code = str(body.get("code") or "") if isinstance(body, dict) else ""
        low_msg = message.lower()

        if response.status_code in (401, 403) or "invalid api key" in low_msg or "unauthorized" in low_msg:
            self._auth_tripped_suppliers[supplier] = message
            raise CanbosoAuthError(
                f"Canboso 鉴权失败，已立即熔断供货商 {supplier}: {message}",
                status_code=response.status_code,
                supplier=supplier,
                raw=body,
            )

        if response.status_code == 429:
            retry_after_raw = response.headers.get("Retry-After")
            retry_after = int(retry_after_raw) if retry_after_raw and retry_after_raw.isdigit() else None
            raise CanbosoRateLimitError(
                f"Canboso 触发限流 ({supplier}): {message}",
                retry_after=retry_after,
                status_code=429,
                supplier=supplier,
                raw=body,
            )

        if is_purchase:
            if response.status_code == 409 and (code == "IDEMPOTENCY_CONFLICT" or "idempotency" in low_msg):
                raise CanbosoIdempotencyConflictError(
                    f"Canboso 幂等键参数冲突 ({supplier}): {message}",
                    status_code=409,
                    supplier=supplier,
                    raw=body,
                )
            if response.status_code == 409 or "库存" in message or "stock" in low_msg:
                raise CanbosoInsufficientStockError(
                    f"Canboso 库存不足 ({supplier}): {message}",
                    status_code=response.status_code,
                    supplier=supplier,
                    raw=body,
                )
            if response.status_code == 400 and ("余额" in message or "balance" in low_msg or "insufficient" in low_msg):
                payment = body.get("payment") if isinstance(body, dict) else None
                reported = None
                if isinstance(payment, dict) and payment.get("balance") is not None:
                    reported = Decimal(str(payment["balance"]))
                elif isinstance(body, dict) and body.get("balance") is not None:
                    reported = Decimal(str(body["balance"]))
                raise CanbosoInsufficientBalanceError(
                    f"Canboso 钱包余额不足 ({supplier}): {message}",
                    reported_balance=reported,
                    status_code=400,
                    supplier=supplier,
                    raw=body,
                )

        raise CanbosoError(
            f"Canboso 请求失败 ({supplier}, HTTP {response.status_code}): {message}",
            status_code=response.status_code,
            supplier=supplier,
            raw=body,
        )

    def get_balance(self, supplier: str) -> CanbosoBalance:
        api_key = self._require_key(supplier)
        url = f"{self.base_url}/api/v2/telegram-buyer/balance"
        try:
            response = self._client.get(url, headers={"x-buyer-key": api_key})
        except httpx.HTTPError as exc:
            raise CanbosoError(f"Canboso 查询余额网络异常 ({supplier}): {exc}", supplier=supplier) from exc
        if response.status_code != 200:
            self._handle_error_response(supplier, response)
        self._record_rate_limits(supplier, response.headers)
        data = response.json()
        if not data.get("success"):
            self._handle_error_response(supplier, response)
        wallet_currency = str(data.get("walletCurrency") or "USD").upper()
        balance = Decimal(str(data.get("balance", 0)))
        raw_usd_rate = data.get("usdRate")
        usd_rate = Decimal(str(raw_usd_rate)) if raw_usd_rate not in (None, "", 0, "0") else Decimal("26000")
        self.supplier_usd_rates[supplier] = usd_rate
        if data.get("balanceUsd") is not None:
            balance_usd = Decimal(str(data["balanceUsd"]))
        elif wallet_currency == "VND" and usd_rate > 0:
            balance_usd = (balance / usd_rate).quantize(Decimal("0.0001"))
        else:
            balance_usd = balance
        return CanbosoBalance(
            supplier=supplier,
            wallet_currency=wallet_currency,
            balance=balance,
            balance_usd=balance_usd,
            balance_vnd=Decimal(str(data.get("balanceVnd") or 0)),
            usd_rate=usd_rate,
            balance_text=str(data.get("balanceText") or ""),
            updated_at=str(data.get("updatedAt") or ""),
        )

    def list_products(self, supplier: str, *, force_refresh: bool = False) -> list[CanbosoProduct]:
        cached = self._catalog_cache.get(supplier)
        now = time.monotonic()
        if not force_refresh and cached is not None and (now - cached.fetched_at) < self._catalog_ttl:
            return list(cached.products_by_id.values())

        api_key = self._require_key(supplier)
        url = f"{self.base_url}/api/v2/telegram-buyer/products"
        try:
            response = self._client.get(url, headers={"x-buyer-key": api_key})
        except httpx.HTTPError as exc:
            raise CanbosoError(f"Canboso 拉取商品目录网络异常 ({supplier}): {exc}", supplier=supplier) from exc
        if response.status_code != 200:
            self._handle_error_response(supplier, response)
        self._record_rate_limits(supplier, response.headers)
        data = response.json()
        if not data.get("success"):
            self._handle_error_response(supplier, response)

        wallet_currency = str(data.get("walletCurrency") or "USD")
        parsed: dict[str, CanbosoProduct] = {}
        for item in data.get("products") or []:
            if not isinstance(item, dict):
                continue
            product_id = str(item.get("productId") or "").strip()
            if not product_id:
                continue
            price_info = item.get("price") or {}
            avail_info = item.get("availability") or {}
            raw_avail = avail_info.get("available")
            raw_sold = avail_info.get("sold")
            promotions = item.get("promotions") or []
            parsed[product_id] = CanbosoProduct(
                supplier=supplier,
                product_id=product_id,
                name=str(item.get("name") or ""),
                product_type=str(item.get("productType") or "account"),
                price_amount=Decimal(str(price_info.get("amount", 0))),
                price_currency=str(price_info.get("currency") or wallet_currency),
                price_text=str(price_info.get("text") or ""),
                available=None if raw_avail is None else int(raw_avail),
                sold=None if raw_sold is None else int(raw_sold),
                raw_promotions_count=len(promotions) if isinstance(promotions, list) else 0,
            )

        self._catalog_cache[supplier] = _CatalogCacheEntry(
            fetched_at=now,
            products_by_id=parsed,
            wallet_currency=wallet_currency,
        )
        return list(parsed.values())

    def get_product(self, supplier: str, product_id: str, *, force_refresh: bool = False) -> CanbosoProduct | None:
        self.list_products(supplier, force_refresh=force_refresh)
        entry = self._catalog_cache.get(supplier)
        if entry is None:
            return None
        return entry.products_by_id.get(product_id)

    def list_orders(self, supplier: str, *, order_code: str | None = None) -> list[dict[str, Any]]:
        api_key = self._require_key(supplier)
        url = f"{self.base_url}/api/v2/telegram-buyer/orders"
        params: dict[str, str] = {}
        if order_code:
            params["orderCode"] = order_code.strip()
        try:
            response = self._client.get(url, params=params or None, headers={"x-buyer-key": api_key})
        except httpx.HTTPError as exc:
            raise CanbosoError(f"Canboso 查询订单网络异常 ({supplier}): {exc}", supplier=supplier) from exc
        if response.status_code != 200:
            self._handle_error_response(supplier, response)
        self._record_rate_limits(supplier, response.headers)
        data = response.json()
        orders = data.get("orders")
        return list(orders) if isinstance(orders, list) else []

    def purchase_account(
        self,
        *,
        supplier: str,
        product_id: str,
        quantity: int,
        idempotency_key: str,
    ) -> CanbosoPurchaseResult:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError(f"拦截危险请求：quantity 必须是 >= 1 的正整数（当前 {quantity!r}），防止 Canboso 兜底成 1 误扣款")
        if quantity > self._max_qty_guard:
            raise ValueError(f"拦截危险请求：quantity={quantity} 超过硬上限 {self._max_qty_guard}")
        clean_key = idempotency_key.strip()
        if len(clean_key) < 8 or len(clean_key) > 128:
            raise ValueError("Idempotency-Key 长度必须在 8 到 128 之间")
        clean_product_id = product_id.strip()
        if not clean_product_id or clean_product_id == "slot_chatgpt_business":
            raise ValueError(f"方案 1 仅允许购买成品号（account），拒绝商品 ID: {product_id!r}")

        api_key = self._require_key(supplier)
        url = f"{self.base_url}/api/v2/telegram-buyer/purchase"
        body = {
            "key": api_key,
            "product_id": clean_product_id,
            "quantity": quantity,
        }
        try:
            response = self._client.post(
                url,
                json=body,
                headers={"Idempotency-Key": clean_key, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise CanbosoNetworkUncertainError(
                f"Canboso 下单网络中断（结果未知，请保留原 Idempotency-Key={clean_key} 重放）: {exc}",
                supplier=supplier,
            ) from exc

        if response.status_code != 200:
            self._handle_error_response(supplier, response, is_purchase=True)
        self._record_rate_limits(supplier, response.headers)
        data = response.json()
        if not data.get("success"):
            self._handle_error_response(supplier, response, is_purchase=True)

        order_info = data.get("order") or {}
        payment_info = data.get("payment") or {}
        delivery_info = data.get("delivery") or {}
        accounts_raw = delivery_info.get("accounts") or []
        known_account_keys = {"user", "password", "verifyEmail", "expiryText", "otherInfo"}
        parsed_accounts: list[CanbosoAccountItem] = []
        for item in accounts_raw:
            if isinstance(item, dict):
                extra_parts = [
                    f"{k}:{v}"
                    for k, v in item.items()
                    if k not in known_account_keys and v not in (None, "")
                ]
                base_other = str(item["otherInfo"]) if item.get("otherInfo") else ""
                merged_other = " | ".join(part for part in (base_other, *extra_parts) if part) or None
                parsed_accounts.append(
                    CanbosoAccountItem(
                        user=str(item.get("user") or ""),
                        password=str(item.get("password") or ""),
                        verify_email=str(item["verifyEmail"]) if item.get("verifyEmail") else None,
                        expiry_text=str(item["expiryText"]) if item.get("expiryText") else None,
                        other_info=merged_other,
                    )
                )
            elif isinstance(item, str) and item.strip():
                parsed_accounts.append(CanbosoAccountItem(user=item.strip(), password=""))
        accounts = tuple(parsed_accounts)
        return CanbosoPurchaseResult(
            supplier=supplier,
            idempotency_key=clean_key,
            order_code=str(order_info.get("orderCode") or ""),
            status=str(order_info.get("status") or ""),
            product_id=str(order_info.get("productId") or clean_product_id),
            product_name=str(order_info.get("productName") or ""),
            product_type=str(order_info.get("productType") or "account"),
            quantity=int(order_info.get("quantity", quantity)),
            bonus_quantity=int(order_info.get("bonusQuantity", 0)),
            final_quantity=int(order_info.get("finalQuantity", len(accounts) or quantity)),
            amount=Decimal(str(payment_info.get("amount", 0))),
            currency=str(payment_info.get("currency") or "USD"),
            balance_after=Decimal(str(payment_info.get("balance", 0))),
            accounts=accounts,
            fulfillment_status=str(order_info.get("fulfillmentStatus") or ""),
            raw_meta={
                "order": {
                    "orderCode": order_info.get("orderCode"),
                    "status": order_info.get("status"),
                    "productId": order_info.get("productId"),
                    "productName": order_info.get("productName"),
                    "productType": order_info.get("productType"),
                    "quantity": order_info.get("quantity"),
                    "bonusQuantity": order_info.get("bonusQuantity"),
                    "finalQuantity": order_info.get("finalQuantity"),
                },
                "payment": payment_info,
            },
        )
