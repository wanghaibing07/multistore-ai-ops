from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class PricingPolicy:
    markup_ratio: Decimal = Decimal("0.30")
    fixed_markup_cny: Decimal = Decimal("0.00")
    min_margin_cny: Decimal = Decimal("1.00")
    min_sell_price_cny: Decimal = Decimal("0.02")
    round_to: Decimal = Decimal("0.10")
    auto_sync_price: bool = False
    price_sync_threshold_cny: Decimal = Decimal("1.00")
    baseline_cost_usd: Decimal | None = None


@dataclass(frozen=True)
class SkuMapping:
    sku_id: str
    enabled: bool
    goods_no_16688: str
    canboso_supplier: str
    canboso_product_id: str
    min_stock: int = 1
    target_stock: int = 2
    max_qty_per_run: int = 1
    split_type: int = 1
    card_delimiter: str = "----"
    remove_repeat: bool = True
    goods_category_no: str = ""
    pricing: PricingPolicy = field(default_factory=PricingPolicy)
    auto_off_shelf_when_oos: bool = True
    auto_on_shelf_when_restocked: bool = False


@dataclass(frozen=True)
class CanbosoProduct:
    supplier: str
    product_id: str
    name: str
    product_type: str
    price_amount: Decimal
    price_currency: str
    price_text: str
    available: int | None
    sold: int | None
    raw_promotions_count: int = 0

    @property
    def is_available(self) -> bool:
        return self.available is None or self.available > 0

    @property
    def is_plan1_compatible(self) -> bool:
        return self.product_type == "account"


@dataclass(frozen=True)
class CanbosoBalance:
    supplier: str
    wallet_currency: str
    balance: Decimal
    balance_usd: Decimal
    balance_vnd: Decimal
    usd_rate: Decimal
    balance_text: str
    updated_at: str


@dataclass(frozen=True)
class CanbosoAccountItem:
    user: str
    password: str
    verify_email: str | None = None
    expiry_text: str | None = None
    other_info: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "password": self.password,
            "verifyEmail": self.verify_email,
            "expiryText": self.expiry_text,
            "otherInfo": self.other_info,
        }


@dataclass(frozen=True)
class CanbosoPurchaseResult:
    supplier: str
    idempotency_key: str
    order_code: str
    status: str
    product_id: str
    product_name: str
    product_type: str
    quantity: int
    bonus_quantity: int
    final_quantity: int
    amount: Decimal
    currency: str
    balance_after: Decimal
    accounts: tuple[CanbosoAccountItem, ...]
    fulfillment_status: str = ""
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Platform16688Goods:
    goods_no: str
    source: str
    name: str
    image: str
    description: str
    delivery_method: int
    available_quantity: int
    limit_quantity: int
    unit_price: Decimal
    sales_count: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Platform16688StockUploadResult:
    goods_no: str
    import_count: int
    stock_dup_count: int
    available_quantity: int


@dataclass(frozen=True)
class PricingDecision:
    unit_cost_usd: Decimal
    unit_cost_cny: Decimal
    suggested_price_cny: Decimal
    min_safe_price_cny: Decimal
    price_spiked: bool
    spike_ratio: Decimal | None


@dataclass(frozen=True)
class CycleAction:
    sku_id: str
    action: str
    detail: str
    dry_run: bool
    data: dict[str, Any] = field(default_factory=dict)
