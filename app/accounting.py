from __future__ import annotations

from collections import defaultdict
from datetime import date
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .models import Adjustment, CardMatch, Cashflow, CostVersion, ImportBatch, ImportRow, OrderItem, Refund, RefundReview, PaymentBasisReview
from .refund_review import fingerprint, eligible as review_eligible
from .payment_basis_review import fingerprint as basis_fingerprint

def effective_fulfillment_status(order: OrderItem, source_row: dict | None = None) -> str:
    value = getattr(order, "fulfillment_status", "unknown") or "unknown"
    if value in ("shipped", "unshipped"):
        return value
    source_row = source_row or {}
    shipped_at = next((source_row.get(key) for key in ("发货时间", "发货日期", "shipping_time", "shipped_at") if source_row.get(key)), "")
    if str(shipped_at).strip() not in ("", "-", "--", "无", "None"):
        return "shipped"
    source_status = str(next((source_row.get(key) for key in ("订单状态", "交易状态", "履约/售后状态", "status") if source_row.get(key)), "")).strip()
    if any(token in source_status for token in ("卖家已发货", "已发货", "交易成功", "交易完成", "已完成", "等待买家确认", "资金保护中")):
        return "shipped"
    if any(token in source_status for token in ("等待卖家发货", "待发货", "买家已付款", "待付款", "等待买家付款")):
        return "unshipped"
    if order.status == "completed":
        return "shipped"
    if order.status in ("pending", "unpaid"):
        return "unshipped"
    if (
        order.status == "closed"
        and order.refund_status == "success"
        and order.payment_fen is not None
        and (order.refund_snapshot_fen or 0) >= order.payment_fen
    ):
        return "unshipped"
    return "unknown"


def calculate(db: Session, store_id: int, start: str, end: str, sku: str | None = None, item_id: str | None = None, order_id: str | None = None) -> dict:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if last < first or (last-first).days > 366*5: raise ValueError
    except ValueError: raise ValueError("日期范围无效") from None
    query=select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.paid_at.is_not(None),OrderItem.paid_at>=start,OrderItem.paid_at<end+" 23:59:59.999999")
    if sku: query=query.where(OrderItem.sku==sku)
    if item_id: query=query.where(OrderItem.item_external_id==item_id)
    if order_id: query=query.where(OrderItem.order_external_id==order_id)
    orders=list(db.scalars(query.order_by(OrderItem.paid_at.desc(),OrderItem.id.desc())))
    missing_paid_time=db.scalar(select(func.count(OrderItem.id)).where(OrderItem.store_id==store_id,OrderItem.paid_at.is_(None))) or 0
    refunds=list(db.scalars(select(Refund).where(Refund.store_id==store_id)))
    refund_cashflows=list(db.scalars(select(Cashflow).where(Cashflow.store_id==store_id,Cashflow.kind=='refund')))
    all_cashflows=list(db.scalars(select(Cashflow).where(Cashflow.store_id==store_id)))
    basis_reviews=list(db.scalars(select(PaymentBasisReview).where(PaymentBasisReview.store_id==store_id).order_by(PaymentBasisReview.id)))
    adjustments=list(db.scalars(select(Adjustment).where(Adjustment.store_id==store_id)))
    card_matches=list(db.scalars(select(CardMatch).where(CardMatch.store_id==store_id,CardMatch.status=="confirmed")))
    reviews=list(db.scalars(select(RefundReview).where(RefundReview.store_id==store_id).order_by(RefundReview.id)))
    cost_ids={o.cost_version_id for o in orders if o.cost_version_id is not None}
    import_batch_ids={o.batch_id for o in orders}
    import_rows=list(db.scalars(select(ImportRow).where(ImportRow.batch_id.in_(import_batch_ids)))) if import_batch_ids else []
    source_rows_by_item={row.data.get("item_external_id"): (row.raw_data or {}) for row in import_rows if row.data.get("item_external_id")}
    cost_batches={c.id:c.batch_id for c in db.scalars(select(CostVersion).where(CostVersion.store_id==store_id,CostVersion.id.in_(cost_ids)))} if cost_ids else {}
    by_ref=defaultdict(list);by_cashflow=defaultdict(list);by_all_cashflow=defaultdict(list);by_adj=defaultdict(list);by_card_match=defaultdict(list);latest_review={};latest_basis={}
    for r in refunds: by_ref[r.item_external_id].append(r)
    for flow in refund_cashflows: by_cashflow[(flow.order_external_id,flow.item_external_id)].append(flow)
    for flow in all_cashflows: by_all_cashflow[(flow.order_external_id,flow.item_external_id)].append(flow)
    for entry in basis_reviews: latest_basis[entry.item_external_id]=entry
    for a in adjustments: by_adj[a.item_external_id].append(a)
    for match in card_matches: by_card_match[match.item_external_id].append(match)
    for review in reviews: latest_review[review.item_external_id]=review
    items=[];batch_ids=set();known_profit=0;unknown_count=0;pending_count=0;known_cost=0;known_fee=0;confirmed_sales=0;refund_total=0
    source_payment=sum(o.payment_fen or 0 for o in orders)
    source_refunds=sum(o.refund_snapshot_fen or 0 for o in orders if o.refund_status=="success")
    source_refund_missing=sum(o.refund_snapshot_fen is None for o in orders if o.refund_status=="success")
    for o in orders:
        reasons=[];ref_rows=by_ref[o.item_external_id]; cashflow_rows=by_cashflow[(o.order_external_id,o.item_external_id)]; adj=by_adj[o.item_external_id]
        fulfillment=effective_fulfillment_status(o, source_rows_by_item.get(o.item_external_id))
        last_invalid=max((a for a in adj if a.kind=="cost_invalidate"),key=lambda a:a.id,default=None)
        cost_override=max((a for a in adj if a.kind=="cost_set" and (not last_invalid or a.id>last_invalid.id)),key=lambda a:a.id,default=None)
        cost_needs_review=bool(last_invalid and not cost_override)
        base_cost=cost_override.amount_fen if cost_override else (None if last_invalid else o.cost_fen)
        successful=[r for r in ref_rows if r.status=="success"]
        event_sum=sum(r.amount_fen for r in successful)
        source_hash=fingerprint(o,ref_rows,cashflow_rows) if review_eligible(o,cashflow_rows) else None
        review=latest_review.get(o.item_external_id)
        active_review=review if source_hash and review and review.source_fingerprint==source_hash else None
        snapshot=o.refund_snapshot_fen
        refund_auto_verified=(
            o.refund_status=="success"
            and snapshot is not None
            and snapshot>0
            and not any(r.status in ("pending","unknown") for r in ref_rows)
            and (not successful or event_sum==snapshot)
        )
        refund_verified=bool(active_review or refund_auto_verified)
        basis_hash=basis_fingerprint(o,ref_rows,by_all_cashflow[(o.order_external_id,o.item_external_id)],active_review)
        basis_review=latest_basis.get(o.item_external_id)
        active_basis=basis_review if basis_review and basis_review.source_fingerprint==basis_hash else None
        effective_basis=active_basis.basis if active_basis else ("unknown" if basis_review else o.payment_basis)
        if any(r.status in ("pending","unknown") for r in ref_rows): reasons.append("退款流水尚未完成")
        if o.refund_status in ("pending","unknown"): reasons.append("订单退款状态未确认")
        if o.refund_status=="success" and not refund_verified: reasons.append("退款数据无法自动确认")
        if o.refund_status=="closed" and not refund_verified: reasons.append("退款申请已关闭，需核对历史退款")
        if o.status=="closed" and o.refund_status=="none" and not refund_verified: reasons.append("已关闭订单需核对退款结果")
        if cashflow_rows and o.refund_status=="none" and not refund_verified: reasons.append("货款账单有售后退款而订单快照无退款，需核对退款历史")
        if o.payment_fen is None: reasons.append("缺少实付金额")
        if effective_basis=="unknown": reasons.append("未确认金额是否已扣退款")
        if o.status=="unknown": reasons.append("订单状态未识别")
        if o.status in ("completed", "closed") and fulfillment=="unknown": reasons.append("未确认是否已发货")
        if cost_needs_review: reasons.append("订单信息已变化，请核对原成本和调整后重新设置逐单成本")
        if o.refund_status=="success" and (snapshot is None or snapshot==0): reasons.append("缺少已成功退款金额")
        if not active_review:
            if successful and (snapshot is None or snapshot!=event_sum): reasons.append("退款流水与订单快照不一致")
            if o.refund_status=="none" and snapshot not in (None,0): reasons.append("退款状态与金额不一致")
            if o.refund_status=="none" and successful: reasons.append("退款流水与订单状态不一致")
        elif successful and active_review.refund_fen!=event_sum:
            reasons.append("人工核对与退款流水不一致")
        refund_value=active_review.refund_fen if active_review else (snapshot or 0)
        if o.payment_fen is not None and effective_basis=="gross" and refund_value>o.payment_fen: reasons.append("退款金额超过实付金额")
        cost_adjust=sum(a.amount_fen for a in adj if a.kind=="cost_delta")
        supplier_refund=sum(a.amount_fen for a in adj if a.kind=="supplier_refund")
        fee=sum(a.amount_fen for a in adj if a.kind=="expense")
        auto_zero_cost=(
            fulfillment=="unshipped"
            and o.refund_status=="success"
            and cost_override is None
            and not cost_needs_review
        )
        if cost_needs_review:
            total_cost=None
        elif auto_zero_cost:
            total_cost=0
        else:
            total_cost=None if base_cost is None else base_cost+cost_adjust-supplier_refund
        if total_cost is None and not cost_needs_review and not auto_zero_cost: reasons.append("缺少商品成本")
        if total_cost is not None and total_cost<0: reasons.append("供应商返款超过成本")
        net_sales=None if o.payment_fen is None or effective_basis=="unknown" else o.payment_fen-(refund_value if effective_basis=="gross" else 0)
        estimate=None if net_sales is None or total_cost is None else net_sales-total_cost-fee
        soft_review_reasons={
            "退款数据无法自动确认",
            "退款申请已关闭，需核对历史退款",
        }
        estimated_profit = (
            estimate
            if estimate is not None and reasons and set(reasons).issubset(soft_review_reasons)
            else None
        )
        is_pending=o.status in ("pending","unpaid")
        if is_pending: pending_count+=1
        if reasons: unknown_count+=1
        eligible=not reasons and not is_pending and o.status in ("completed","closed")
        if eligible:
            known_profit+=estimate;known_cost+=total_cost;known_fee+=fee;confirmed_sales+=net_sales;refund_total+=refund_value
        batch_ids.add(o.batch_id)
        if not cost_override and cost_batches.get(o.cost_version_id) is not None: batch_ids.add(cost_batches[o.cost_version_id])
        for r in ref_rows: batch_ids.add(r.batch_id)
        for flow in cashflow_rows: batch_ids.add(flow.batch_id)
        items.append({"item_id":o.item_external_id,"order_id":o.order_external_id,"sku":o.sku,"title":o.product_title,
            "paid_at":o.paid_at,"status":o.status,"fulfillment_status":fulfillment,"quantity":o.quantity,"payment_fen":o.payment_fen,"basis":effective_basis,
            "basis_source_fingerprint":basis_hash,"basis_review_id":active_basis.id if active_basis else None,"basis_review_stale":bool(basis_review and not active_basis),
            "refund_status":o.refund_status,"refund_fen":snapshot,"cost_fen":total_cost,"fee_fen":fee,"net_sales_fen":net_sales,
            "profit_fen":estimate if eligible else None,"estimated_profit_fen":(
                estimate if is_pending and not reasons else estimated_profit
            ),
            "reasons":reasons,"batch_id":o.batch_id,"cost_version_id":None if cost_override else o.cost_version_id,
            "cost_batch_id":None if cost_override else cost_batches.get(o.cost_version_id),"cost_override_id":cost_override.id if cost_override else None,"cost_needs_review":cost_needs_review,
            "refund_review_fingerprint":source_hash,"refund_review_id":active_review.id if active_review else None,
            "refund_auto_verified":refund_auto_verified and not active_review,
            "reviewed_refund_fen":active_review.refund_fen if active_review else None,
            "refund_review_stale":bool(review and not active_review and not refund_auto_verified),
            "refund_records":[{"refund_id":r.external_id,"status":r.status,"amount_fen":r.amount_fen,"completed_at":r.completed_at,"batch_id":r.batch_id} for r in ref_rows],
            "cashflow_refund_records":[{"refund_id":x.refund_external_id,"amount_fen":x.amount_fen,"settled_at":x.settled_at,"batch_id":x.batch_id} for x in cashflow_rows],
            "adjustment_ids":[a.id for a in adj],"refund_batch_ids":sorted({r.batch_id for r in ref_rows})})
        items[-1]["card_match_ids"]=[match.id for match in by_card_match[o.item_external_id]]
    batches=list(db.scalars(select(ImportBatch).where(ImportBatch.store_id==store_id,ImportBatch.status=="committed").order_by(ImportBatch.committed_at.desc())))
    return {"cashflow_review_version":2,"start":start,"end":end,"as_of":batches[0].committed_at if batches else None,"as_of_note":"最近一次导入时间，不代表平台实时数据",
        "counts":{"paid_orders":len({o.order_external_id for o in orders}),"paid_items":len(orders),"settled":sum(x["profit_fen"] is not None for x in items),"needs_review":unknown_count,"pending":pending_count,
                  "cost_known":sum(x["cost_fen"] is not None for x in items),"missing_paid_time_all_dates":missing_paid_time},
        "known":{"profit_fen":known_profit,"net_sales_fen":confirmed_sales,"cost_fen":known_cost,"fees_fen":known_fee,"refund_fen":refund_total},
        "source_snapshot":{"payment_fen":source_payment,"successful_refund_fen":source_refunds,"refund_missing":source_refund_missing},
        "batch_ids":sorted(batch_ids),"items":items}
