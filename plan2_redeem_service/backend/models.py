from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RedeemRequest(BaseModel):
    buyer_email: Optional[str] = None
    buyer_email_confirm: Optional[str] = None
    preflight_confirmed: bool = False


class WarrantyForm(BaseModel):
    admin_email: str
    admin_email_confirm: Optional[str] = None
    note: Optional[str] = ""


class IssueRequest(BaseModel):
    sku_key: str
    channel: str = "taobao"
    external_order_id: str = ""
    remark: str = ""


class BatchRequest(BaseModel):
    sku_key: str
    channel: str
    count: int = Field(ge=1, le=500)
    goods_no: Optional[str] = None
    batch_label: str = ""


class RevokeRequest(BaseModel):
    reason: str = "商家手动作废"


class RetryRequest(BaseModel):
    override_price_limit: bool = False
    note: str = ""


class WarrantyAckRequest(BaseModel):
    note: str = ""


class SkuPatchRequest(BaseModel):
    max_cost_usd: float = Field(gt=0)
    goods_no: Optional[str] = None


class AgisoSkuMappingRequest(BaseModel):
    sku_key: str
    num_iid: str = ""
    sku_id: str = ""
    outer_iid: str = ""
    outer_sku_id: str = ""
    sku_props_contains: str = ""
    enabled: bool = True
    note: str = ""


class AgisoSyncOrdersRequest(BaseModel):
    lookback_minutes: int = Field(default=60, ge=1, le=10080)
    page_size: int = Field(default=50, ge=1, le=100)
    max_pages: int = Field(default=5, ge=1, le=20)


class AgisoOrderRetryRequest(BaseModel):
    fetch_from_agiso: bool = True


class AgisoPushCardsRequest(BaseModel):
    sku_key: str
    cpc_id: int = Field(ge=1)
    count: int = Field(ge=1, le=500)
    batch_label: str = ""
    is_use_first: bool = False
    allow_replace: bool = False


class AgisoRestockRequest(BaseModel):
    dry_check: bool = False
    sku_keys: Optional[list[str]] = None


class Channel16688RestockRequest(BaseModel):
    dry_check: bool = False
    sku_keys: Optional[list[str]] = None
    sync_sold: bool = True


class PublicCodeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str
    sku_key: str
    sku_title: str
    product_type: str
    channel: str
    status: str
    buyer_email_masked: Optional[str]
    delivery: Optional[dict[str, Any]]
    warranty: Optional[dict[str, Any]]
    fulfillment: dict[str, Any]
    buyer_notice: Optional[str]
    support_hint: str
    created_at: str
    updated_at: str

