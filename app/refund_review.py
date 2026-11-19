"""Fingerprint the source facts a person saw when reviewing an order."""
import hashlib
import json

def fingerprint(order, refunds, cashflow_refunds=()):
    payload={
        'order':[order.store_id,order.item_external_id,order.order_external_id,order.status,order.payment_fen,
                 order.payment_basis,order.refund_status,order.refund_snapshot_fen,order.batch_id],
        'refunds':sorted([[r.external_id,r.status,r.amount_fen,r.completed_at,r.batch_id] for r in refunds],key=lambda r:r[0]),
    }
    # Preserve old fingerprints when there are no associated refund cashflows.
    # A newly imported or corrected refund cashflow invalidates a prior review.
    if cashflow_refunds:
        payload['cashflow_refunds']=sorted([
            [x.business_flow,x.order_external_id,x.item_external_id,x.refund_external_id,
             x.amount_fen,x.settled_at,x.batch_id] for x in cashflow_refunds
        ],key=lambda x:(x[0],x[3],x[5]))
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()

def eligible(order, cashflow_refunds=()):
    return bool(cashflow_refunds) or order.refund_status in ('closed','success') or (order.status=='closed' and order.refund_status=='none')
