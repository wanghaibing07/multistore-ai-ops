import hashlib
import json

def fingerprint(order, refunds, cashflows, review):
    source = [order.store_id, order.item_external_id, order.order_external_id, order.payment_fen,
              order.payment_basis, order.paid_at, order.status, order.refund_status,
              order.refund_snapshot_fen, order.batch_id]
    refund_rows = sorted([[row.external_id, row.status, row.amount_fen, row.completed_at, row.batch_id] for row in refunds], key=lambda row: row[0])
    cashflow_rows = sorted([[row.business_flow, row.kind, row.refund_external_id, row.amount_fen, row.settled_at, row.batch_id] for row in cashflows], key=lambda row: (row[0], row[1], row[2]))
    payload = [source, refund_rows, cashflow_rows, [review.id, review.source_fingerprint, review.refund_fen] if review else None]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
