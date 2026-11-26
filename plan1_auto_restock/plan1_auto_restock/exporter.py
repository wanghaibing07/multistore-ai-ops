from __future__ import annotations

import csv
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import re

from .config import SupplierConfig
from .storage import DualDatabaseStore

EXPORT_COLUMNS = [
    "采购单号",
    "商品名称",
    "商家名称",
    "来源",
    "数量",
    "实付金额",
    "退款金额",
    "付款时间",
    "状态",
    "原币金额USD",
    "16688商品编号",
    "SKU",
    "补库状态",
]


def resolve_source(supplier: str, supplier_label: str) -> str:
    supplier_norm = (supplier or "").strip().lower()
    label_norm = (supplier_label or "").strip().lower()
    if supplier_norm == "primary" or "fatbunny" in label_norm or "兔子" in label_norm:
        return "tg_fatbunny"
    if supplier_norm == "secondary" or "chugax" in label_norm:
        return "tg_chugax"
    slug = re.sub(r"[^a-z0-9_]+", "_", supplier_norm).strip("_") or "canboso"
    return f"tg_{slug}"


def export_multistore_csv(
    *,
    store: DualDatabaseStore,
    suppliers: dict[str, SupplierConfig],
    output_path: Path,
) -> int:
    """Exports Canboso purchases in a CSV format compatible with multistore-ai-ops (zero secrets)."""
    purchases = store.list_completed_purchases()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for intent in purchases:
            supplier_cfg = suppliers.get(intent.supplier)
            supplier_label = supplier_cfg.label if supplier_cfg else intent.supplier
            amount_cny = intent.amount_cny.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            amount_usd = intent.amount_usd.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            qty = intent.actual_qty if intent.actual_qty > 0 else intent.requested_qty
            writer.writerow(
                {
                    "采购单号": intent.canboso_order_code or intent.idempotency_key,
                    "商品名称": intent.product_name or intent.sku_id,
                    "商家名称": f"Canboso-{supplier_label}",
                    "来源": resolve_source(intent.supplier, supplier_label),
                    "数量": str(qty),
                    "实付金额": f"{amount_cny:.2f}",
                    "退款金额": "0.00",
                    "付款时间": intent.created_at,
                    "状态": "已完成",
                    "原币金额USD": f"{amount_usd:.4f}",
                    "16688商品编号": intent.goods_no_16688,
                    "SKU": intent.sku_id,
                    "补库状态": intent.status,
                }
            )
    return len(purchases)
