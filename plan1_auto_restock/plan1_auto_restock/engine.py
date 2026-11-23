from __future__ import annotations

import re
import secrets
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterator

from .alerts import AlertDispatcher, EmailAlertSettings, parse_email_recipients
from .client_16688 import (
    COMPLAINT_STATUS_LABELS,
    ORDER_STATUS_LABELS,
    Platform16688Client,
    Platform16688Error,
)
from .client_canboso import (
    CanbosoAuthError,
    CanbosoClient,
    CanbosoError,
    CanbosoIdempotencyConflictError,
    CanbosoInsufficientBalanceError,
    CanbosoInsufficientStockError,
    CanbosoNetworkUncertainError,
    CanbosoRateLimitError,
)
from .config import AppConfig
from .models import CanbosoBalance, CanbosoPurchaseResult, CycleAction, Platform16688Goods, SkuMapping
from .pricing import (
    DEFAULT_VND_PER_USD,
    convert_to_usd,
    convert_usd_to_cny,
    evaluate_pricing,
    format_cards_for_16688,
)
from .storage import DualDatabaseStore, RestockIntentRecord

UNRESOLVED_INTENT_STATUSES = ("PENDING", "UNCERTAIN_REPLAY", "PURCHASED", "FAILED_UPLOAD")
SAFE_IDEMPOTENCY_SEGMENT = re.compile(r"[^A-Za-z0-9_-]+")


def build_idempotency_key(sku_id: str, now_ts: int) -> str:
    cleaned = SAFE_IDEMPOTENCY_SEGMENT.sub("-", sku_id).strip("-")[:28] or "sku"
    return f"plan1-{cleaned}-{now_ts}-{secrets.token_hex(6)}"


class RestockEngine:
    """Orchestrates Plan 1: 16688 self-operated card goods + Canboso low-stock auto-restock."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: DualDatabaseStore,
        client_16688: Platform16688Client,
        client_canboso: CanbosoClient,
        alerts: AlertDispatcher,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client_16688 = client_16688
        self.client_canboso = client_canboso
        self.alerts = alerts
        self._clock = clock or time.time
        self._skus_by_id: dict[str, SkuMapping] = {sku.sku_id: sku for sku in config.skus}

    def health_check(self) -> dict[str, Any]:
        """Read-only diagnostic across 16688, all Canboso suppliers, and configured SKUs."""
        report: dict[str, Any] = {
            "dry_run_default": self.config.runtime.dry_run,
            "platform_16688": {"ok": False},
            "canboso_suppliers": {},
            "skus": [],
            "store_summary": self.store.summary(),
        }

        try:
            balances_16688 = self.client_16688.get_balance()
            categories = self.client_16688.list_categories()
            goods = self.client_16688.list_goods(page_no=1, page_size=100)
            report["platform_16688"] = {
                "ok": True,
                "balances": {k: str(v) for k, v in balances_16688.items()},
                "categories_count": len(categories),
                "goods_count": len(goods),
                "goods_nos": [item.goods_no for item in goods],
            }
        except Platform16688Error as exc:
            report["platform_16688"] = {"ok": False, "error": str(exc)}

        supplier_balances: dict[str, CanbosoBalance] = {}
        for supplier_name, supplier_cfg in self.config.canboso.suppliers.items():
            try:
                bal = self.client_canboso.get_balance(supplier_name)
                products = self.client_canboso.list_products(supplier_name)
                account_products = [prod for prod in products if prod.is_plan1_compatible]
                supplier_balances[supplier_name] = bal
                report["canboso_suppliers"][supplier_name] = {
                    "ok": True,
                    "label": supplier_cfg.label,
                    "wallet_currency": bal.wallet_currency,
                    "balance_usd": str(bal.balance_usd),
                    "balance_text": bal.balance_text,
                    "total_products": len(products),
                    "account_products": len(account_products),
                    "rate_limits": self.client_canboso.last_rate_limits.get(supplier_name, {}),
                }
            except CanbosoError as exc:
                report["canboso_suppliers"][supplier_name] = {
                    "ok": False,
                    "label": supplier_cfg.label,
                    "error": str(exc),
                }

        for sku in self.config.skus:
            sku_entry: dict[str, Any] = {
                "sku_id": sku.sku_id,
                "enabled": sku.enabled,
                "goods_no_16688": sku.goods_no_16688,
                "canboso_supplier": sku.canboso_supplier,
                "canboso_product_id": sku.canboso_product_id,
                "ok": True,
                "issues": [],
            }
            if not sku.goods_no_16688 or sku.goods_no_16688 == "G_REPLACE_ME":
                sku_entry["ok"] = False
                sku_entry["issues"].append("goods_no_16688 未填写或仍为占位符")
            else:
                try:
                    goods = self.client_16688.get_goods_detail(sku.goods_no_16688)
                    sku_entry["goods_16688"] = {
                        "name": goods.name,
                        "source": goods.source,
                        "delivery_method": goods.delivery_method,
                        "available_quantity": goods.available_quantity,
                    }
                    if goods.source != "local" or goods.delivery_method != 1:
                        sku_entry["ok"] = False
                        sku_entry["issues"].append(
                            f"16688 商品非自营卡密 (source={goods.source}, delivery_method={goods.delivery_method})"
                        )
                except Platform16688Error as exc:
                    sku_entry["ok"] = False
                    sku_entry["issues"].append(f"16688 商品查询失败: {exc}")

            if not sku.canboso_product_id:
                sku_entry["ok"] = False
                sku_entry["issues"].append("canboso_product_id 未填写")
            else:
                try:
                    product = self.client_canboso.get_product(sku.canboso_supplier, sku.canboso_product_id)
                    if product is None:
                        sku_entry["ok"] = False
                        sku_entry["issues"].append("Canboso 商品不存在于目录中")
                    else:
                        vnd_rate = self._get_vnd_rate(sku.canboso_supplier, supplier_balances.get(sku.canboso_supplier))
                        unit_cost_usd = convert_to_usd(product.price_amount, product.price_currency, vnd_rate)
                        decision = evaluate_pricing(
                            unit_cost_usd=unit_cost_usd,
                            usd_cny_rate=self.config.canboso.usd_cny_rate,
                            policy=sku.pricing,
                            baseline_cost_usd=sku.pricing.baseline_cost_usd,
                            max_spike_ratio=self.config.safety.max_upstream_price_spike_ratio,
                        )
                        sku_entry["canboso_product"] = {
                            "name": product.name,
                            "product_type": product.product_type,
                            "price_text": product.price_text,
                            "available": product.available,
                            "unit_cost_usd": str(unit_cost_usd),
                            "suggested_price_cny": str(decision.suggested_price_cny),
                            "price_spiked": decision.price_spiked,
                        }
                        if not product.is_plan1_compatible:
                            sku_entry["ok"] = False
                            sku_entry["issues"].append(
                                f"上游商品类型 {product.product_type!r} 不支持方案 1（仅支持 account 成品号）"
                            )
                except CanbosoError as exc:
                    sku_entry["ok"] = False
                    sku_entry["issues"].append(f"Canboso 商品查询失败: {exc}")
            report["skus"].append(sku_entry)

        return report

    def recover_pending(self, *, dry_run: bool | None = None) -> list[CycleAction]:
        effective_dry_run = self.config.runtime.dry_run if dry_run is None else bool(dry_run)
        actions: list[CycleAction] = []
        actions.extend(self._recover_pending_uploads(effective_dry_run))
        actions.extend(self._recover_uncertain_replays(effective_dry_run))
        return actions

    def run_cycle(self, *, dry_run_override: bool | None = None) -> list[CycleAction]:
        effective_dry_run = self.config.runtime.dry_run if dry_run_override is None else bool(dry_run_override)
        actions: list[CycleAction] = []

        actions.extend(self.recover_pending(dry_run=effective_dry_run))

        enabled_skus = [sku for sku in self.config.skus if sku.enabled]
        referenced_suppliers = sorted({sku.canboso_supplier for sku in enabled_skus})

        balances: dict[str, CanbosoBalance] = {}
        remaining_balance_usd: dict[str, Decimal] = {}
        for supplier in referenced_suppliers:
            try:
                bal = self.client_canboso.get_balance(supplier)
                balances[supplier] = bal
                remaining_balance_usd[supplier] = bal.balance_usd
            except CanbosoAuthError as exc:
                self.alerts.emit(
                    level="CRITICAL",
                    event_type="CANBOSO_AUTH_CIRCUIT_BROKEN",
                    message=str(exc),
                    detail={"supplier": supplier},
                )
                actions.append(CycleAction(sku_id="*", action="SUPPLIER_AUTH_ERROR", detail=str(exc), dry_run=effective_dry_run))
            except CanbosoRateLimitError as exc:
                self.alerts.emit(
                    level="WARNING",
                    event_type="CANBOSO_RATE_LIMITED",
                    message=str(exc),
                    detail={"supplier": supplier, "retry_after": exc.retry_after},
                )
                actions.append(CycleAction(sku_id="*", action="SUPPLIER_RATE_LIMITED", detail=str(exc), dry_run=effective_dry_run))
            except CanbosoError as exc:
                self.alerts.emit(
                    level="ERROR",
                    event_type="CANBOSO_BALANCE_ERROR",
                    message=str(exc),
                    detail={"supplier": supplier},
                )
                actions.append(CycleAction(sku_id="*", action="SUPPLIER_ERROR", detail=str(exc), dry_run=effective_dry_run))

        unresolved_intents = self.store.list_intents_by_statuses(UNRESOLVED_INTENT_STATUSES)
        unresolved_skus = {intent.sku_id for intent in unresolved_intents}

        for sku in enabled_skus:
            if sku.canboso_supplier not in balances:
                actions.append(
                    CycleAction(
                        sku_id=sku.sku_id,
                        action="SKIP_SUPPLIER_UNAVAILABLE",
                        detail=f"供货商 {sku.canboso_supplier} 余额拉取失败，跳过本轮",
                        dry_run=effective_dry_run,
                    )
                )
                continue
            actions.extend(
                self._process_sku(
                    sku=sku,
                    balance=balances[sku.canboso_supplier],
                    remaining_balance_usd=remaining_balance_usd,
                    has_unresolved_intent=sku.sku_id in unresolved_skus,
                    dry_run=effective_dry_run,
                )
            )

        actions.extend(self.sync_orders_and_complaints(dry_run=effective_dry_run))
        return actions

    def sync_orders_and_complaints(self, *, dry_run: bool = True) -> list[CycleAction]:
        actions: list[CycleAction] = []
        try:
            order_page = self.client_16688.list_orders(page_no=1, page_size=50)
            synced_orders = 0
            for order in order_page.get("list") or []:
                if not isinstance(order, dict):
                    continue
                status_code = int(order.get("status", -1))
                status_label = ORDER_STATUS_LABELS.get(status_code, f"unknown_{status_code}")
                self.store.upsert_16688_order(order, status_label=status_label)
                synced_orders += 1
            if synced_orders > 0:
                actions.append(
                    CycleAction(
                        sku_id="*",
                        action="SYNCED_16688_ORDERS",
                        detail=f"同步 16688 店铺订单 {synced_orders} 条（已脱敏）",
                        dry_run=dry_run,
                        data={"count": synced_orders},
                    )
                )
        except Platform16688Error as exc:
            self.alerts.emit(
                level="WARNING",
                event_type="SYNC_16688_ORDERS_FAILED",
                message=str(exc),
            )

        try:
            complaint_page = self.client_16688.list_complaints(page_no=1, page_size=50)
            alerted = 0
            for item in complaint_page.get("list") or []:
                if not isinstance(item, dict):
                    continue
                status_code = int(item.get("status", -1))
                complaint_no = str(item.get("complaint_no") or "").strip()
                trade_no = str(item.get("trade_no") or "").strip()
                if not complaint_no:
                    continue
                status_label = COMPLAINT_STATUS_LABELS.get(status_code, f"unknown_{status_code}")
                reason = str(item.get("reason") or "")
                should_alert = self.store.record_complaint_and_check_alert(
                    complaint_no=complaint_no,
                    trade_no=trade_no,
                    status=status_code,
                    status_label=status_label,
                    reason=reason,
                )
                if should_alert and status_code in (0, 1):
                    alerted += 1
                    self.alerts.emit(
                        level="CRITICAL" if status_code == 1 else "WARNING",
                        event_type="COMPLAINT_16688",
                        message=f"16688 投诉 {complaint_no}（订单 {trade_no}）状态={status_label}：{reason}",
                        detail={
                            "complaint_no": complaint_no,
                            "trade_no": trade_no,
                            "status": status_code,
                            "status_label": status_label,
                        },
                    )
            if alerted > 0:
                actions.append(
                    CycleAction(
                        sku_id="*",
                        action="ALERTED_16688_COMPLAINTS",
                        detail=f"发现并推送 {alerted} 条 16688 待处理/介入投诉",
                        dry_run=dry_run,
                        data={"alerted": alerted},
                    )
                )
        except Platform16688Error as exc:
            self.alerts.emit(
                level="WARNING",
                event_type="SYNC_16688_COMPLAINTS_FAILED",
                message=str(exc),
            )

        return actions

    def bootstrap_goods(
        self,
        *,
        name: str,
        price_cny: Decimal,
        goods_category_no: str = "",
        description: str = "",
        instruction: str = "",
        image: str = "",
        cover_text: str = "",
        cost_price_cny: Decimal | None = None,
        warning_stock_quantity: int = 5,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Previews or creates a 16688 self-operated card goods (delivery_method=1)."""
        preview = {
            "dry_run": dry_run,
            "name": name.strip()[:255],
            "price": str(price_cny.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "cost_price": str(cost_price_cny.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if cost_price_cny is not None
            else None,
            "goods_category_no": goods_category_no.strip(),
            "delivery_method": 1,
            "warning_stock_quantity": warning_stock_quantity,
            "image": image.strip() or f"<auto-generate via /openApi/goods/cover: {(cover_text or name).strip()[:40]}>",
        }
        if dry_run:
            return preview

        final_image = image.strip()
        if not final_image:
            final_image = self.client_16688.generate_cover((cover_text or name).strip()[:80])
        result = self.client_16688.upsert_goods(
            name=name,
            image=final_image,
            price=price_cny,
            description=description,
            delivery_method=1,
            delivery_order=0,
            limit_quantity=1,
            warning_stock_quantity=warning_stock_quantity,
            instruction=instruction,
            cost_price=cost_price_cny,
            goods_category_no=goods_category_no,
        )
        return {"dry_run": False, "image": final_image, "result": result}

    def _recover_pending_uploads(self, dry_run: bool) -> list[CycleAction]:
        actions: list[CycleAction] = []
        pending_intents = self.store.list_intents_by_statuses(("PURCHASED", "FAILED_UPLOAD"))
        for intent in pending_intents:
            vault_items = self.store.get_pending_vault_items(intent.idempotency_key)
            if not vault_items:
                self.alerts.emit(
                    level="CRITICAL",
                    event_type="VAULT_MISSING_FOR_PURCHASED_INTENT",
                    sku_id=intent.sku_id,
                    message=f"补库单 {intent.idempotency_key} 状态为 {intent.status} 但保险库无待上传卡密，需人工核查",
                    detail={"idempotency_key": intent.idempotency_key, "order_code": intent.canboso_order_code},
                )
                actions.append(
                    CycleAction(
                        sku_id=intent.sku_id,
                        action="RECOVERY_MISSING_VAULT_ITEMS",
                        detail=f"{intent.idempotency_key} 缺少保险库卡密",
                        dry_run=dry_run,
                    )
                )
                continue

            sku = self._skus_by_id.get(intent.sku_id)
            split_type = sku.split_type if sku else 1
            remove_repeat = sku.remove_repeat if sku else True
            separator = "\n" if split_type == 1 else "\n\n"
            content = separator.join(item.card_line for item in vault_items)

            if dry_run:
                actions.append(
                    CycleAction(
                        sku_id=intent.sku_id,
                        action="DRY_RUN_RECOVER_UPLOAD",
                        detail=f"演练：待补传 {len(vault_items)} 条卡密至 16688 商品 {intent.goods_no_16688}",
                        dry_run=True,
                        data={"idempotency_key": intent.idempotency_key, "cards_count": len(vault_items)},
                    )
                )
                continue

            upload_action = self._upload_content_to_16688(
                intent_key=intent.idempotency_key,
                sku_id=intent.sku_id,
                goods_no=intent.goods_no_16688,
                content=content,
                split_type=split_type,
                remove_repeat=remove_repeat,
                is_recovery=True,
            )
            actions.append(upload_action)
        return actions

    def _recover_uncertain_replays(self, dry_run: bool) -> list[CycleAction]:
        actions: list[CycleAction] = []
        uncertain_intents = self.store.list_intents_by_statuses(("UNCERTAIN_REPLAY", "PENDING"))
        for intent in uncertain_intents:
            if dry_run:
                actions.append(
                    CycleAction(
                        sku_id=intent.sku_id,
                        action="DRY_RUN_PENDING_REPLAY",
                        detail=(
                            f"演练：存在未决采购单 {intent.idempotency_key} (status={intent.status}, "
                            f"replays={intent.replay_attempts})，实盘模式将复用原 Idempotency-Key 恢复"
                        ),
                        dry_run=True,
                        data={"idempotency_key": intent.idempotency_key, "status": intent.status},
                    )
                )
                continue

            if intent.replay_attempts >= self.config.safety.max_idempotent_replay_attempts:
                self.alerts.emit(
                    level="CRITICAL",
                    event_type="IDEMPOTENT_REPLAY_EXHAUSTED",
                    sku_id=intent.sku_id,
                    message=(
                        f"未决采购单 {intent.idempotency_key} 已达最大重放次数 "
                        f"({intent.replay_attempts}/{self.config.safety.max_idempotent_replay_attempts})，请人工核对 Canboso 订单"
                    ),
                    detail={"idempotency_key": intent.idempotency_key, "supplier": intent.supplier},
                )
                actions.append(
                    CycleAction(
                        sku_id=intent.sku_id,
                        action="REPLAY_EXHAUSTED",
                        detail=f"{intent.idempotency_key} 重放次数用尽，阻断该 SKU 新购",
                        dry_run=False,
                    )
                )
                continue

            sku = self._skus_by_id.get(intent.sku_id)
            split_type = sku.split_type if sku else 1
            delimiter = sku.card_delimiter if sku else "----"
            remove_repeat = sku.remove_repeat if sku else True

            try:
                result = self.client_canboso.purchase_account(
                    supplier=intent.supplier,
                    product_id=intent.product_id,
                    quantity=intent.requested_qty,
                    idempotency_key=intent.idempotency_key,
                )
            except CanbosoNetworkUncertainError as exc:
                self.store.mark_intent_uncertain(intent.idempotency_key, str(exc), increment_replay=True)
                self.alerts.emit(
                    level="WARNING",
                    event_type="REPLAY_NETWORK_UNCERTAIN",
                    sku_id=intent.sku_id,
                    message=str(exc),
                    detail={"idempotency_key": intent.idempotency_key},
                )
                actions.append(
                    CycleAction(
                        sku_id=intent.sku_id,
                        action="REPLAY_STILL_UNCERTAIN",
                        detail=str(exc),
                        dry_run=False,
                    )
                )
                continue
            except CanbosoIdempotencyConflictError as exc:
                self.store.mark_intent_uncertain(intent.idempotency_key, str(exc), increment_replay=True)
                self.alerts.emit(
                    level="CRITICAL",
                    event_type="IDEMPOTENCY_CONFLICT",
                    sku_id=intent.sku_id,
                    message=str(exc),
                    detail={"idempotency_key": intent.idempotency_key},
                )
                actions.append(
                    CycleAction(
                        sku_id=intent.sku_id,
                        action="REPLAY_CONFLICT",
                        detail=str(exc),
                        dry_run=False,
                    )
                )
                continue
            except (CanbosoInsufficientBalanceError, CanbosoInsufficientStockError, CanbosoAuthError, CanbosoError) as exc:
                self.store.mark_intent_failed_pre_purchase(intent.idempotency_key, str(exc))
                self.alerts.emit(
                    level="ERROR",
                    event_type="REPLAY_FAILED_PRE_PURCHASE",
                    sku_id=intent.sku_id,
                    message=str(exc),
                    detail={"idempotency_key": intent.idempotency_key},
                )
                actions.append(
                    CycleAction(
                        sku_id=intent.sku_id,
                        action="REPLAY_FAILED_PRE_PURCHASE",
                        detail=str(exc),
                        dry_run=False,
                    )
                )
                continue

            upload_action = self._persist_purchase_and_upload(
                intent=intent,
                result=result,
                split_type=split_type,
                delimiter=delimiter,
                remove_repeat=remove_repeat,
                is_recovery=True,
            )
            actions.append(upload_action)
        return actions

    def _process_sku(
        self,
        *,
        sku: SkuMapping,
        balance: CanbosoBalance,
        remaining_balance_usd: dict[str, Decimal],
        has_unresolved_intent: bool,
        dry_run: bool,
    ) -> list[CycleAction]:
        actions: list[CycleAction] = []
        if not sku.goods_no_16688 or sku.goods_no_16688 == "G_REPLACE_ME" or not sku.canboso_product_id:
            actions.append(
                CycleAction(
                    sku_id=sku.sku_id,
                    action="SKIP_INCOMPLETE_MAPPING",
                    detail="SKU 缺少有效的 goods_no_16688 或 canboso_product_id",
                    dry_run=dry_run,
                )
            )
            return actions

        try:
            goods = self.client_16688.get_goods_detail(sku.goods_no_16688)
        except Platform16688Error as exc:
            self.alerts.emit(
                level="ERROR",
                event_type="GOODS_16688_FETCH_FAILED",
                sku_id=sku.sku_id,
                message=str(exc),
                detail={"goods_no": sku.goods_no_16688},
            )
            actions.append(CycleAction(sku_id=sku.sku_id, action="GOODS_FETCH_ERROR", detail=str(exc), dry_run=dry_run))
            return actions

        if goods.source != "local" or goods.delivery_method != 1:
            msg = f"16688 商品 {goods.goods_no} 非自营卡密商品 (source={goods.source}, delivery_method={goods.delivery_method})"
            self.alerts.emit(level="ERROR", event_type="INVALID_16688_GOODS_TYPE", sku_id=sku.sku_id, message=msg)
            actions.append(CycleAction(sku_id=sku.sku_id, action="SKIP_INVALID_16688_GOODS", detail=msg, dry_run=dry_run))
            return actions

        try:
            product = self.client_canboso.get_product(sku.canboso_supplier, sku.canboso_product_id)
        except CanbosoError as exc:
            self.alerts.emit(
                level="ERROR",
                event_type="CANBOSO_CATALOG_FETCH_FAILED",
                sku_id=sku.sku_id,
                message=str(exc),
                detail={"supplier": sku.canboso_supplier, "product_id": sku.canboso_product_id},
            )
            actions.append(CycleAction(sku_id=sku.sku_id, action="CATALOG_FETCH_ERROR", detail=str(exc), dry_run=dry_run))
            return actions

        sku_state = self.store.get_sku_state(sku.sku_id)
        if product is None:
            msg = f"Canboso 目录中未找到商品 {sku.canboso_product_id}"
            self.alerts.emit(level="ERROR", event_type="UPSTREAM_PRODUCT_NOT_FOUND", sku_id=sku.sku_id, message=msg)
            actions.append(CycleAction(sku_id=sku.sku_id, action="UPSTREAM_NOT_FOUND", detail=msg, dry_run=dry_run))
            if goods.available_quantity == 0 and sku.auto_off_shelf_when_oos:
                actions.append(self._handle_auto_off_shelf(sku, goods, reason="上游商品下架且16688库存归零", dry_run=dry_run))
            return actions

        if not product.is_plan1_compatible:
            msg = f"拒绝非成品号商品 {product.product_id} (productType={product.product_type})，仅允许 account"
            self.alerts.emit(level="ERROR", event_type="INCOMPATIBLE_PRODUCT_TYPE", sku_id=sku.sku_id, message=msg)
            actions.append(CycleAction(sku_id=sku.sku_id, action="SKIP_INCOMPATIBLE_TYPE", detail=msg, dry_run=dry_run))
            return actions

        vnd_rate = self._get_vnd_rate(sku.canboso_supplier, balance)
        unit_cost_usd = convert_to_usd(product.price_amount, product.price_currency, vnd_rate)
        baseline_cost_usd = (
            sku.pricing.baseline_cost_usd
            if sku.pricing.baseline_cost_usd is not None
            else (sku_state.baseline_cost_usd if sku_state and sku_state.baseline_cost_usd is not None else unit_cost_usd)
        )
        decision = evaluate_pricing(
            unit_cost_usd=unit_cost_usd,
            usd_cny_rate=self.config.canboso.usd_cny_rate,
            policy=sku.pricing,
            baseline_cost_usd=baseline_cost_usd,
            max_spike_ratio=self.config.safety.max_upstream_price_spike_ratio,
        )

        self.store.upsert_sku_state(
            sku_id=sku.sku_id,
            baseline_cost_usd=baseline_cost_usd,
            last_cost_usd=unit_cost_usd,
            last_stock_16688=goods.available_quantity,
            last_upstream_available=product.available,
        )

        if decision.price_spiked:
            spike_pct = (decision.spike_ratio or Decimal("0")) * Decimal("100")
            reason = f"上游成本从 ${baseline_cost_usd} 暴涨至 ${unit_cost_usd} (+{spike_pct:.1f}%)，触发熔断"
            self.store.upsert_sku_state(sku_id=sku.sku_id, circuit_broken=True, circuit_reason=reason)
            self.alerts.emit(
                level="CRITICAL",
                event_type="PRICE_SPIKE_CIRCUIT_BREAKER",
                sku_id=sku.sku_id,
                message=reason,
                detail={
                    "baseline_cost_usd": str(baseline_cost_usd),
                    "current_cost_usd": str(unit_cost_usd),
                    "spike_ratio": str(decision.spike_ratio),
                },
            )
            actions.append(
                CycleAction(
                    sku_id=sku.sku_id,
                    action="PRICE_SPIKE_CIRCUIT_BROKEN",
                    detail=reason,
                    dry_run=dry_run,
                    data={"suggested_price_cny": str(decision.suggested_price_cny)},
                )
            )
            if sku.auto_off_shelf_when_oos:
                actions.append(self._handle_auto_off_shelf(sku, goods, reason=reason, dry_run=dry_run))
            return actions

        if sku_state and sku_state.circuit_broken:
            self.store.upsert_sku_state(sku_id=sku.sku_id, circuit_broken=False, circuit_reason="")
            self.alerts.emit(
                level="INFO",
                event_type="PRICE_SPIKE_RECOVERED",
                sku_id=sku.sku_id,
                message=f"上游成本回落至 ${unit_cost_usd}，解除价格熔断",
            )

        if sku.pricing.auto_sync_price:
            price_action = self._maybe_sync_price(
                sku=sku,
                goods=goods,
                last_sell_price_cny=sku_state.last_sell_price_cny if sku_state else None,
                suggested_price_cny=decision.suggested_price_cny,
                unit_cost_cny=decision.unit_cost_cny,
                dry_run=dry_run,
            )
            if price_action is not None:
                actions.append(price_action)

        upstream_oos = product.available is not None and product.available <= 0
        if upstream_oos:
            self.alerts.emit(
                level="WARNING",
                event_type="UPSTREAM_OOS",
                sku_id=sku.sku_id,
                message=f"Canboso 上游商品 {product.product_id} 已断货（16688 剩余可售 {goods.available_quantity}）",
            )
            actions.append(
                CycleAction(
                    sku_id=sku.sku_id,
                    action="UPSTREAM_OOS",
                    detail=f"上游断货，16688 剩余 {goods.available_quantity}",
                    dry_run=dry_run,
                )
            )
            if goods.available_quantity == 0 and sku.auto_off_shelf_when_oos and not (sku_state and sku_state.auto_off_shelved):
                actions.append(self._handle_auto_off_shelf(sku, goods, reason="上游断货且16688库存归零", dry_run=dry_run))
            return actions

        if goods.available_quantity >= sku.min_stock:
            if sku_state and sku_state.auto_off_shelved and sku.auto_on_shelf_when_restocked:
                actions.append(self._handle_auto_on_shelf(sku, goods, dry_run=dry_run))
            actions.append(
                CycleAction(
                    sku_id=sku.sku_id,
                    action="SKIP_SUFFICIENT_STOCK",
                    detail=f"16688 库存 {goods.available_quantity} >= 水位线 {sku.min_stock}，无需补库",
                    dry_run=dry_run,
                    data={
                        "stock_16688": goods.available_quantity,
                        "unit_cost_usd": str(unit_cost_usd),
                        "suggested_price_cny": str(decision.suggested_price_cny),
                    },
                )
            )
            return actions

        if has_unresolved_intent:
            msg = "该 SKU 存在未闭环采购/上传任务，阻断重复下单"
            self.alerts.emit(level="WARNING", event_type="SKIP_UNRESOLVED_INTENT", sku_id=sku.sku_id, message=msg)
            actions.append(CycleAction(sku_id=sku.sku_id, action="SKIP_UNRESOLVED_INTENT", detail=msg, dry_run=dry_run))
            return actions

        now_ts = int(self._clock())
        last_restock_at = sku_state.last_restock_at if sku_state else 0
        cooldown = self.config.safety.restock_cooldown_seconds
        if last_restock_at > 0 and (now_ts - last_restock_at) < cooldown:
            remaining_sec = cooldown - (now_ts - last_restock_at)
            actions.append(
                CycleAction(
                    sku_id=sku.sku_id,
                    action="SKIP_COOLDOWN",
                    detail=f"补库冷却中，剩余 {remaining_sec} 秒",
                    dry_run=dry_run,
                )
            )
            return actions

        deficit = sku.target_stock - goods.available_quantity
        qty = min(deficit, sku.max_qty_per_run, self.config.safety.max_qty_per_purchase)
        if product.available is not None:
            qty = min(qty, product.available)
        if qty < 1:
            actions.append(
                CycleAction(
                    sku_id=sku.sku_id,
                    action="SKIP_ZERO_QTY",
                    detail="计算补库数量 < 1，跳过",
                    dry_run=dry_run,
                )
            )
            return actions

        estimated_cost_usd = (unit_cost_usd * Decimal(qty)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        avail_balance_usd = remaining_balance_usd.get(sku.canboso_supplier, balance.balance_usd)
        if avail_balance_usd < self.config.safety.min_wallet_balance_usd or avail_balance_usd < estimated_cost_usd:
            msg = (
                f"供货商 {sku.canboso_supplier} 余额不足：当前 ${avail_balance_usd}，"
                f"预警线 ${self.config.safety.min_wallet_balance_usd}，本次预计 ${estimated_cost_usd}"
            )
            self.alerts.emit(level="WARNING", event_type="LOW_WALLET_BALANCE", sku_id=sku.sku_id, message=msg)
            actions.append(CycleAction(sku_id=sku.sku_id, action="SKIP_LOW_BALANCE", detail=msg, dry_run=dry_run))
            return actions

        spent_today = self.store.today_spent_usd()
        if spent_today + estimated_cost_usd > self.config.safety.daily_budget_usd:
            msg = (
                f"触发单日预算上限：今日已用 ${spent_today} + 本次预计 ${estimated_cost_usd} "
                f"> 上限 ${self.config.safety.daily_budget_usd}"
            )
            self.alerts.emit(level="WARNING", event_type="DAILY_BUDGET_EXCEEDED", sku_id=sku.sku_id, message=msg)
            actions.append(CycleAction(sku_id=sku.sku_id, action="SKIP_DAILY_BUDGET", detail=msg, dry_run=dry_run))
            return actions

        if dry_run:
            actions.append(
                CycleAction(
                    sku_id=sku.sku_id,
                    action="DRY_RUN_RESTOCK",
                    detail=(
                        f"演练：16688 库存 {goods.available_quantity} < {sku.min_stock}，"
                        f"计划向 {sku.canboso_supplier} 采购 {qty} 件（预计 ${estimated_cost_usd} / "
                        f"约 ¥{convert_usd_to_cny(estimated_cost_usd, self.config.canboso.usd_cny_rate)}），"
                        f"建议售价 ¥{decision.suggested_price_cny}"
                    ),
                    dry_run=True,
                    data={
                        "qty": qty,
                        "estimated_cost_usd": str(estimated_cost_usd),
                        "suggested_price_cny": str(decision.suggested_price_cny),
                    },
                )
            )
            return actions

        idempotency_key = build_idempotency_key(sku.sku_id, now_ts)
        self.store.create_restock_intent(
            idempotency_key=idempotency_key,
            sku_id=sku.sku_id,
            goods_no_16688=sku.goods_no_16688,
            supplier=sku.canboso_supplier,
            product_id=sku.canboso_product_id,
            product_name=product.name,
            requested_qty=qty,
            estimated_cost_usd=estimated_cost_usd,
        )

        try:
            purchase_result = self.client_canboso.purchase_account(
                supplier=sku.canboso_supplier,
                product_id=sku.canboso_product_id,
                quantity=qty,
                idempotency_key=idempotency_key,
            )
        except CanbosoNetworkUncertainError as exc:
            self.store.mark_intent_uncertain(idempotency_key, str(exc), increment_replay=False)
            self.alerts.emit(
                level="ERROR",
                event_type="PURCHASE_NETWORK_UNCERTAIN",
                sku_id=sku.sku_id,
                message=str(exc),
                detail={"idempotency_key": idempotency_key},
            )
            actions.append(
                CycleAction(
                    sku_id=sku.sku_id,
                    action="PURCHASE_UNCERTAIN_QUEUED_REPLAY",
                    detail=f"网络中断，已保留幂等键 {idempotency_key} 等待下轮重放",
                    dry_run=False,
                )
            )
            return actions
        except CanbosoError as exc:
            self.store.mark_intent_failed_pre_purchase(idempotency_key, str(exc))
            self.alerts.emit(
                level="ERROR",
                event_type="PURCHASE_FAILED_PRE_CHARGE",
                sku_id=sku.sku_id,
                message=str(exc),
                detail={"idempotency_key": idempotency_key},
            )
            actions.append(CycleAction(sku_id=sku.sku_id, action="PURCHASE_FAILED", detail=str(exc), dry_run=False))
            return actions

        spent_usd = convert_to_usd(purchase_result.amount, purchase_result.currency, vnd_rate)
        remaining_balance_usd[sku.canboso_supplier] = max(Decimal("0"), avail_balance_usd - spent_usd)

        intent = RestockIntentRecord(
            idempotency_key=idempotency_key,
            sku_id=sku.sku_id,
            goods_no_16688=sku.goods_no_16688,
            supplier=sku.canboso_supplier,
            product_id=sku.canboso_product_id,
            product_name=product.name,
            requested_qty=qty,
            estimated_cost_usd=estimated_cost_usd,
            status="PENDING",
            replay_attempts=0,
            canboso_order_code="",
            actual_qty=0,
            bonus_qty=0,
            actual_amount=Decimal("0"),
            currency="USD",
            amount_usd=Decimal("0"),
            amount_cny=Decimal("0"),
            upload_import_count=0,
            upload_dup_count=0,
            error_message="",
            created_at="",
            updated_at="",
        )
        upload_action = self._persist_purchase_and_upload(
            intent=intent,
            result=purchase_result,
            split_type=sku.split_type,
            delimiter=sku.card_delimiter,
            remove_repeat=sku.remove_repeat,
            is_recovery=False,
            vnd_rate=vnd_rate,
        )
        actions.append(upload_action)

        if upload_action.action == "RESTOCK_UPLOADED":
            if sku_state and sku_state.auto_off_shelved and sku.auto_on_shelf_when_restocked:
                actions.append(self._handle_auto_on_shelf(sku, goods, dry_run=False))

        return actions

    def _persist_purchase_and_upload(
        self,
        *,
        intent: RestockIntentRecord,
        result: CanbosoPurchaseResult,
        split_type: int,
        delimiter: str,
        remove_repeat: bool,
        is_recovery: bool,
        vnd_rate: Decimal | None = None,
    ) -> CycleAction:
        rate = vnd_rate or self._get_vnd_rate(intent.supplier, None)
        amount_usd = convert_to_usd(result.amount, result.currency, rate)
        amount_cny = convert_usd_to_cny(amount_usd, self.config.canboso.usd_cny_rate)

        if not result.accounts:
            self.store.mark_intent_purchased(
                idempotency_key=intent.idempotency_key,
                canboso_order_code=result.order_code,
                product_name=result.product_name or intent.product_name,
                actual_qty=result.final_quantity,
                bonus_qty=result.bonus_quantity,
                actual_amount=result.amount,
                currency=result.currency,
                amount_usd=amount_usd,
                amount_cny=amount_cny,
            )
            msg = f"Canboso 订单 {result.order_code} 返回空账号列表（fulfillment={result.fulfillment_status}），无法自动入库"
            self.store.mark_intent_upload_failed(intent.idempotency_key, msg)
            self.alerts.emit(
                level="CRITICAL",
                event_type="CANBOSO_EMPTY_ACCOUNTS",
                sku_id=intent.sku_id,
                message=msg,
                detail={"order_code": result.order_code, "idempotency_key": intent.idempotency_key},
            )
            return CycleAction(sku_id=intent.sku_id, action="UPLOAD_FAILED_EMPTY_DELIVERY", detail=msg, dry_run=False)

        content, card_lines = format_cards_for_16688(result.accounts, split_type=split_type, delimiter=delimiter)

        # Atomically persist raw credentials into isolated secrets_vault.db BEFORE marking ledger purchased.
        self.store.save_vault_accounts(
            idempotency_key=intent.idempotency_key,
            canboso_order_code=result.order_code,
            supplier=intent.supplier,
            sku_id=intent.sku_id,
            goods_no_16688=intent.goods_no_16688,
            accounts=result.accounts,
            card_lines=card_lines,
        )
        self.store.mark_intent_purchased(
            idempotency_key=intent.idempotency_key,
            canboso_order_code=result.order_code,
            product_name=result.product_name or intent.product_name,
            actual_qty=result.final_quantity,
            bonus_qty=result.bonus_quantity,
            actual_amount=result.amount,
            currency=result.currency,
            amount_usd=amount_usd,
            amount_cny=amount_cny,
        )
        return self._upload_content_to_16688(
            intent_key=intent.idempotency_key,
            sku_id=intent.sku_id,
            goods_no=intent.goods_no_16688,
            content=content,
            split_type=split_type,
            remove_repeat=remove_repeat,
            is_recovery=is_recovery,
            order_code=result.order_code,
        )

    def _upload_content_to_16688(
        self,
        *,
        intent_key: str,
        sku_id: str,
        goods_no: str,
        content: str,
        split_type: int,
        remove_repeat: bool,
        is_recovery: bool,
        order_code: str = "",
    ) -> CycleAction:
        try:
            upload_res = self.client_16688.add_stock_secrets(
                goods_no=goods_no,
                content=content,
                split_type=split_type,
                remove_repeat=remove_repeat,
            )
        except Platform16688Error as exc:
            self.store.mark_intent_upload_failed(intent_key, str(exc))
            self.alerts.emit(
                level="CRITICAL",
                event_type="UPLOAD_TO_16688_FAILED",
                sku_id=sku_id,
                message=f"卡密已安全落入保险库，但上传 16688 ({goods_no}) 失败，下轮将自动重试：{exc}",
                detail={"idempotency_key": intent_key, "goods_no": goods_no, "order_code": order_code},
            )
            return CycleAction(
                sku_id=sku_id,
                action="UPLOAD_RECOVERY_FAILED" if is_recovery else "RESTOCK_UPLOAD_FAILED",
                detail=str(exc),
                dry_run=False,
                data={"idempotency_key": intent_key, "order_code": order_code},
            )

        self.store.mark_intent_uploaded(
            intent_key,
            import_count=upload_res.import_count,
            dup_count=upload_res.stock_dup_count,
        )
        self.store.upsert_sku_state(
            sku_id=sku_id,
            last_restock_at=int(self._clock()),
            last_stock_16688=upload_res.available_quantity,
        )
        event_type = "UPLOAD_RECOVERED" if is_recovery else "RESTOCK_UPLOADED"
        detail_msg = (
            f"已入库 16688 ({goods_no})：新增 {upload_res.import_count} 张，去重 {upload_res.stock_dup_count} 张，"
            f"当前可售 {upload_res.available_quantity}"
        )
        self.alerts.emit(
            level="INFO",
            event_type=event_type,
            sku_id=sku_id,
            message=detail_msg,
            detail={
                "idempotency_key": intent_key,
                "order_code": order_code,
                "import_count": upload_res.import_count,
                "dup_count": upload_res.stock_dup_count,
                "available_quantity": upload_res.available_quantity,
            },
        )
        return CycleAction(
            sku_id=sku_id,
            action=event_type,
            detail=detail_msg,
            dry_run=False,
            data={
                "idempotency_key": intent_key,
                "order_code": order_code,
                "import_count": upload_res.import_count,
                "available_quantity": upload_res.available_quantity,
            },
        )

    def _maybe_sync_price(
        self,
        *,
        sku: SkuMapping,
        goods: Platform16688Goods,
        last_sell_price_cny: Decimal | None,
        suggested_price_cny: Decimal,
        unit_cost_cny: Decimal,
        dry_run: bool,
    ) -> CycleAction | None:
        if last_sell_price_cny is not None:
            diff = abs(suggested_price_cny - last_sell_price_cny)
            if diff < sku.pricing.price_sync_threshold_cny:
                return None

        if dry_run:
            return CycleAction(
                sku_id=sku.sku_id,
                action="DRY_RUN_SYNC_PRICE",
                detail=(
                    f"演练：计划更新 16688 商品 {goods.goods_no} 售价为 ¥{suggested_price_cny} "
                    f"（上次记录 ¥{last_sell_price_cny if last_sell_price_cny is not None else '未知'}，成本约 ¥{unit_cost_cny}）"
                ),
                dry_run=True,
                data={"suggested_price_cny": str(suggested_price_cny)},
            )

        image_url = goods.image.strip()
        if not image_url:
            try:
                image_url = self.client_16688.generate_cover(goods.name or sku.sku_id)
            except Platform16688Error as exc:
                self.alerts.emit(
                    level="WARNING",
                    event_type="PRICE_SYNC_SKIPPED_NO_IMAGE",
                    sku_id=sku.sku_id,
                    message=f"商品 {goods.goods_no} 缺少封面图且自动生成失败，跳过自动调价：{exc}",
                )
                return None

        try:
            self.client_16688.upsert_goods(
                goods_no=goods.goods_no,
                name=goods.name,
                image=image_url,
                price=suggested_price_cny,
                cost_price=unit_cost_cny,
                description=goods.description,
                delivery_method=goods.delivery_method,
                limit_quantity=goods.limit_quantity,
                goods_category_no=sku.goods_category_no,
            )
        except Platform16688Error as exc:
            self.alerts.emit(
                level="WARNING",
                event_type="PRICE_SYNC_FAILED",
                sku_id=sku.sku_id,
                message=f"更新 16688 商品 {goods.goods_no} 售价失败：{exc}",
            )
            return CycleAction(sku_id=sku.sku_id, action="PRICE_SYNC_FAILED", detail=str(exc), dry_run=False)

        self.store.upsert_sku_state(sku_id=sku.sku_id, last_sell_price_cny=suggested_price_cny)
        msg = f"已更新 16688 商品 {goods.goods_no} 售价至 ¥{suggested_price_cny}"
        self.alerts.emit(level="INFO", event_type="PRICE_SYNCED", sku_id=sku.sku_id, message=msg)
        return CycleAction(sku_id=sku.sku_id, action="PRICE_SYNCED", detail=msg, dry_run=False)

    def _handle_auto_off_shelf(
        self,
        sku: SkuMapping,
        goods: Platform16688Goods,
        *,
        reason: str,
        dry_run: bool,
    ) -> CycleAction:
        if dry_run:
            return CycleAction(
                sku_id=sku.sku_id,
                action="DRY_RUN_OFF_SHELF",
                detail=f"演练：计划下架 16688 商品 {goods.goods_no}（原因：{reason}）",
                dry_run=True,
            )
        try:
            self.client_16688.change_goods_status(goods.goods_no, 0)
        except Platform16688Error as exc:
            self.alerts.emit(
                level="ERROR",
                event_type="AUTO_OFF_SHELF_FAILED",
                sku_id=sku.sku_id,
                message=f"自动下架 16688 商品 {goods.goods_no} 失败：{exc}",
            )
            return CycleAction(sku_id=sku.sku_id, action="AUTO_OFF_SHELF_FAILED", detail=str(exc), dry_run=False)

        self.store.upsert_sku_state(sku_id=sku.sku_id, auto_off_shelved=True)
        msg = f"已自动下架 16688 商品 {goods.goods_no}（原因：{reason}）"
        self.alerts.emit(level="WARNING", event_type="AUTO_OFF_SHELF", sku_id=sku.sku_id, message=msg)
        return CycleAction(sku_id=sku.sku_id, action="AUTO_OFF_SHELF", detail=msg, dry_run=False)

    def _handle_auto_on_shelf(
        self,
        sku: SkuMapping,
        goods: Platform16688Goods,
        *,
        dry_run: bool,
    ) -> CycleAction:
        if dry_run:
            return CycleAction(
                sku_id=sku.sku_id,
                action="DRY_RUN_ON_SHELF",
                detail=f"演练：库存已恢复，计划重新上架 16688 商品 {goods.goods_no}",
                dry_run=True,
            )
        try:
            self.client_16688.change_goods_status(goods.goods_no, 1)
        except Platform16688Error as exc:
            self.alerts.emit(
                level="WARNING",
                event_type="AUTO_ON_SHELF_FAILED",
                sku_id=sku.sku_id,
                message=f"重新上架 16688 商品 {goods.goods_no} 失败：{exc}",
            )
            return CycleAction(sku_id=sku.sku_id, action="AUTO_ON_SHELF_FAILED", detail=str(exc), dry_run=False)

        self.store.upsert_sku_state(sku_id=sku.sku_id, auto_off_shelved=False)
        msg = f"库存恢复，已自动重新上架 16688 商品 {goods.goods_no}"
        self.alerts.emit(level="INFO", event_type="AUTO_ON_SHELF", sku_id=sku.sku_id, message=msg)
        return CycleAction(sku_id=sku.sku_id, action="AUTO_ON_SHELF", detail=msg, dry_run=False)

    def _get_vnd_rate(self, supplier: str, balance: CanbosoBalance | None) -> Decimal:
        if balance is not None and balance.usd_rate > 0:
            return balance.usd_rate
        return self.client_canboso.supplier_usd_rates.get(supplier, DEFAULT_VND_PER_USD)


def build_alert_dispatcher(config: AppConfig, store: DualDatabaseStore) -> AlertDispatcher:
    webhook_url = config.resolve_env(config.runtime.alert_webhook_env, required=False)
    email_cfg = config.runtime.alert_email
    email_user = config.resolve_env(email_cfg.username_env, required=False)
    email_password = config.resolve_env(email_cfg.password_env, required=False)
    raw_to = config.resolve_env(email_cfg.to_addr_env, required=False)
    email_settings = EmailAlertSettings(
        enabled=email_cfg.enabled,
        smtp_host=email_cfg.smtp_host,
        smtp_port=email_cfg.smtp_port,
        use_ssl=email_cfg.use_ssl,
        username=email_user,
        password=email_password,
        to_addrs=parse_email_recipients(raw_to, fallback_addr=email_user),
        min_level=email_cfg.min_level,
        cooldown_seconds=email_cfg.cooldown_seconds,
        subject_prefix=email_cfg.subject_prefix,
        timeout_seconds=email_cfg.timeout_seconds,
        exclude_event_types=email_cfg.exclude_event_types,
    )
    return AlertDispatcher(store=store, webhook_url=webhook_url, email_settings=email_settings)


@contextmanager
def open_engine(config: AppConfig, *, require_16688: bool = True) -> Iterator[RestockEngine]:
    app_id = config.resolve_env(config.platform_16688.app_id_env, required=require_16688)
    secret = config.resolve_env(config.platform_16688.secret_env, required=require_16688)
    supplier_keys = {
        name: config.resolve_env(supplier_cfg.api_key_env, required=False)
        for name, supplier_cfg in config.canboso.suppliers.items()
    }

    store = DualDatabaseStore(config.runtime.ledger_db_path, config.runtime.secrets_vault_db_path)
    client_16688 = Platform16688Client(
        gateway=config.platform_16688.gateway,
        app_id=app_id,
        secret=secret,
        timeout_seconds=config.platform_16688.timeout_seconds,
    )
    client_canboso = CanbosoClient(
        base_url=config.canboso.base_url,
        supplier_keys=supplier_keys,
        timeout_seconds=config.canboso.timeout_seconds,
        catalog_cache_ttl_seconds=config.canboso.catalog_cache_ttl_seconds,
        max_qty_guard=config.safety.max_qty_per_purchase,
    )
    alerts = build_alert_dispatcher(config, store)
    try:
        yield RestockEngine(
            config=config,
            store=store,
            client_16688=client_16688,
            client_canboso=client_canboso,
            alerts=alerts,
        )
    finally:
        alerts.close()
        client_canboso.close()
        client_16688.close()
        store.close()
