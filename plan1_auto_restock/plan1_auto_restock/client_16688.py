from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from .models import Platform16688Goods, Platform16688StockUploadResult
from .signing_16688 import build_signed_payload, split_controller_action

STOCK_FILTER_STATUS_MAP: dict[str | int, int] = {
    "unsold": 0,
    "sold": 1,
    "invalid": 3,
    "expired": 4,
    0: 0,
    1: 1,
    3: 3,
    4: 4,
}

STOCK_RESPONSE_STATUS_LABELS: dict[int, str] = {
    0: "unsold",
    1: "locked",
    2: "sold",
    3: "invalid",
}

ORDER_STATUS_LABELS: dict[int, str] = {
    0: "待支付",
    1: "待发货",
    2: "已关闭",
    3: "已退款",
    4: "已发货",
    5: "已完成",
}

COMPLAINT_STATUS_LABELS: dict[int, str] = {
    0: "待商家处理",
    1: "平台介入",
    2: "已结案",
    3: "已撤销",
}


class Platform16688Error(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, path: str = "", raw: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.raw = raw


class Platform16688AuthError(Platform16688Error):
    pass


class Platform16688ReplayError(Platform16688Error):
    pass


class Platform16688NotFoundError(Platform16688Error):
    pass


class Platform16688HttpError(Platform16688Error):
    pass


class Platform16688Client:
    def __init__(
        self,
        *,
        gateway: str,
        app_id: str,
        secret: str,
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.gateway = gateway.rstrip("/")
        self.app_id = app_id.strip()
        self.secret = secret.strip()
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Platform16688Client:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def call(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        route_path, _, _ = split_controller_action(path)
        url = f"{self.gateway}{route_path}"
        signed_payload, _, _ = build_signed_payload(
            app_id=self.app_id,
            secret=self.secret,
            path=route_path,
            params=params,
            method="POST",
        )
        try:
            response = self._client.post(
                url,
                json=signed_payload,
                headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise Platform16688HttpError(f"16688 请求网络异常 ({route_path}): {exc}", path=route_path) from exc
        finally:
            self._client.cookies.clear()

        if response.status_code == 404:
            raise Platform16688NotFoundError(f"16688 接口路径不存在: {route_path}", code=404, path=route_path)
        if response.status_code != 200:
            raise Platform16688HttpError(
                f"16688 HTTP 状态异常 ({response.status_code}): {route_path}",
                code=response.status_code,
                path=route_path,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise Platform16688HttpError(
                f"16688 返回非 JSON 响应 ({route_path}): {response.text[:160]}",
                code=response.status_code,
                path=route_path,
            ) from exc

        if not isinstance(payload, dict):
            raise Platform16688Error(f"16688 响应结构异常 ({route_path})", path=route_path, raw=payload)

        code = payload.get("code")
        msg = str(payload.get("msg") or "")
        if code != 1:
            if "签名" in msg or "app_id" in msg.lower() or "应用" in msg or "过期" in msg:
                raise Platform16688AuthError(f"16688 鉴权失败 ({route_path}): {msg}", code=code, path=route_path, raw=payload)
            if "重复" in msg:
                raise Platform16688ReplayError(f"16688 Nonce 重放冲突 ({route_path}): {msg}", code=code, path=route_path, raw=payload)
            if "不存在" in msg:
                raise Platform16688NotFoundError(f"16688 资源不存在 ({route_path}): {msg}", code=code, path=route_path, raw=payload)
            raise Platform16688Error(f"16688 业务失败 ({route_path}): {msg}", code=code, path=route_path, raw=payload)

        data = payload.get("data")
        return data if isinstance(data, dict) else {"value": data}

    def get_balance(self) -> dict[str, Decimal]:
        data = self.call("/openApi/merchant/balance")
        return {
            "platform_money": Decimal(str(data.get("platform_money", 0))),
            "deposit_money": Decimal(str(data.get("deposit_money", 0))),
            "operate_money": Decimal(str(data.get("operate_money", 0))),
        }

    def list_categories(self, *, keywords: str = "", status: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if keywords:
            params["keywords"] = keywords
        if status is not None:
            params["status"] = int(status)
        data = self.call("/openApi/goodsCategory/list", params)
        items = data.get("list")
        return list(items) if isinstance(items, list) else []

    def list_goods(
        self,
        *,
        page_no: int = 1,
        page_size: int = 50,
        keywords: str = "",
        source: str = "",
    ) -> list[Platform16688Goods]:
        params: dict[str, Any] = {"page_no": max(1, int(page_no)), "page_size": max(1, min(100, int(page_size)))}
        if keywords:
            params["keywords"] = keywords
        if source:
            params["source"] = source
        data = self.call("/openApi/goods/list", params)
        raw_list = data.get("list") or []
        return [self._parse_goods(item) for item in raw_list if isinstance(item, dict)]

    def get_goods_detail(self, goods_no: str) -> Platform16688Goods:
        data = self.call("/openApi/goods/detail", {"goods_no": goods_no})
        return self._parse_goods(data)

    def generate_cover(self, text: str) -> str:
        trimmed = text.strip()[:80]
        if not trimmed:
            raise ValueError("封面文字不能为空")
        data = self.call("/openApi/goods/cover", {"text": trimmed})
        url = str(data.get("url") or "").strip()
        if not url:
            raise Platform16688Error("16688 生成封面未返回 url", path="/goods/cover", raw=data)
        return url

    def upsert_goods(
        self,
        *,
        name: str,
        image: str,
        price: Decimal,
        goods_no: str = "",
        description: str = "",
        delivery_method: int = 1,
        delivery_order: int = 0,
        limit_quantity: int = 1,
        warning_stock_quantity: int = 5,
        instruction: str = "",
        cost_price: Decimal | None = None,
        goods_category_no: str = "",
    ) -> dict[str, Any]:
        if price < Decimal("0.02"):
            raise ValueError("16688 商品售价不能低于 0.02 元")
        params: dict[str, Any] = {
            "name": name.strip()[:255],
            "image": image.strip()[:500],
            "delivery_method": int(delivery_method),
            "delivery_order": int(delivery_order),
            "limit_quantity": max(1, int(limit_quantity)),
            "warning_stock_quantity": max(0, int(warning_stock_quantity)),
            "price": price.quantize(Decimal("0.01")),
        }
        if goods_no:
            params["goods_no"] = goods_no.strip()
        if description:
            params["description"] = description
        if instruction:
            params["instruction"] = instruction
        if cost_price is not None and cost_price >= Decimal("0"):
            params["cost_price"] = cost_price.quantize(Decimal("0.01"))
        if goods_category_no:
            params["goods_category_no"] = goods_category_no.strip()
        return self.call("/openApi/goods/create", params)

    def change_goods_status(self, goods_no: str, status: int) -> dict[str, Any]:
        if status not in (0, 1):
            raise ValueError("16688 商品状态仅允许 1（上架）或 0（下架）")
        return self.call("/openApi/goods/changeStatus", {"goods_no": goods_no, "status": int(status)})

    def delete_goods(self, goods_no: str) -> dict[str, Any]:
        return self.call("/openApi/goods/delete", {"goods_no": goods_no})

    def add_stock_secrets(
        self,
        *,
        goods_no: str,
        content: str,
        split_type: int = 1,
        remove_repeat: bool = True,
        first: int = 0,
        expire_time: int = 0,
    ) -> Platform16688StockUploadResult:
        if not content.strip():
            raise ValueError("上传卡密内容不能为空")
        if split_type not in (1, 2, 3):
            raise ValueError("split_type 仅支持 1/2/3")
        data = self.call(
            "/openApi/stockSecretBatch/add",
            {
                "goods_no": goods_no,
                "content": content,
                "split_type": int(split_type),
                "remove_repeat": 1 if remove_repeat else 0,
                "first": 1 if first else 0,
                "expire_time": max(0, int(expire_time)),
            },
        )
        return Platform16688StockUploadResult(
            goods_no=str(data.get("goods_no") or goods_no),
            import_count=int(data.get("import_count", 0)),
            stock_dup_count=int(data.get("stock_dup_count", 0)),
            available_quantity=int(data.get("available_quantity", 0)),
        )

    def list_stock_secrets(
        self,
        goods_no: str,
        *,
        filter_status: str | int | None = None,
        page_no: int = 1,
        page_size: int = 20,
        keywords: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "goods_no": goods_no,
            "page_no": max(1, int(page_no)),
            "page_size": max(1, min(100, int(page_size))),
        }
        if filter_status is not None:
            if filter_status not in STOCK_FILTER_STATUS_MAP:
                raise ValueError(
                    f"非法的 stockSecret/list 筛选状态 {filter_status!r}（仅支持 unsold/0, sold/1, invalid/3, expired/4）"
                )
            params["status"] = STOCK_FILTER_STATUS_MAP[filter_status]
        if keywords:
            params["keywords"] = keywords
        data = self.call("/openApi/stockSecret/list", params)
        normalized_items = []
        for entry in data.get("list") or []:
            if isinstance(entry, dict):
                copied = dict(entry)
                raw_status = copied.get("status")
                if isinstance(raw_status, int):
                    copied["status_label"] = STOCK_RESPONSE_STATUS_LABELS.get(raw_status, f"unknown_{raw_status}")
                normalized_items.append(copied)
        return {
            "goods_no": str(data.get("goods_no") or goods_no),
            "total": int(data.get("total", 0)),
            "available_quantity": int(data.get("available_quantity", 0)),
            "locked_quantity": int(data.get("locked_quantity", 0)),
            "sold_quantity": int(data.get("sold_quantity", 0)),
            "list": normalized_items,
        }

    def list_orders(
        self,
        *,
        page_no: int = 1,
        page_size: int = 50,
        status: int | None = None,
        platform: str = "",
        keywords: str = "",
        card_no: str = "",
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page_no": max(1, int(page_no)),
            "page_size": max(1, min(100, int(page_size))),
        }
        if status is not None:
            params["status"] = int(status)
        if platform:
            params["platform"] = platform
        if keywords:
            params["keywords"] = keywords
        if card_no:
            params["card_no"] = card_no
        if start_time is not None:
            params["start_time"] = int(start_time)
        if end_time is not None:
            params["end_time"] = int(end_time)
        return self.call("/openApi/order/list", params)

    def get_order_info(self, trade_no: str) -> dict[str, Any]:
        return self.call("/openApi/order/info", {"trade_no": trade_no})

    def get_order_statistics(self) -> dict[str, Any]:
        return self.call("/openApi/order/statistics")

    def list_complaints(
        self,
        *,
        page_no: int = 1,
        page_size: int = 50,
        status: int | None = None,
        keywords: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page_no": max(1, int(page_no)),
            "page_size": max(1, min(100, int(page_size))),
        }
        if status is not None:
            params["status"] = int(status)
        if keywords:
            params["keywords"] = keywords
        return self.call("/openApi/complaint/list", params)

    @staticmethod
    def _parse_goods(raw: dict[str, Any]) -> Platform16688Goods:
        return Platform16688Goods(
            goods_no=str(raw.get("goods_no") or ""),
            source=str(raw.get("source") or "local"),
            name=str(raw.get("name") or ""),
            image=str(raw.get("image") or ""),
            description=str(raw.get("description") or ""),
            delivery_method=int(raw.get("delivery_method", 1)),
            available_quantity=int(raw.get("available_quantity", 0)),
            limit_quantity=int(raw.get("limit_quantity", 1)),
            unit_price=Decimal(str(raw.get("unit_price", 0))),
            sales_count=int(raw.get("sales_count", 0)),
            raw=dict(raw),
        )
