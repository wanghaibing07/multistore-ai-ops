from datetime import date
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Cashflow, ImportBatch, OrderItem

def summarize(db:Session,store_id:int,start:str,end:str)->dict:
    try:
        first,last=date.fromisoformat(start),date.fromisoformat(end)
        if last<first or (last-first).days>366*5:raise ValueError
    except ValueError:raise ValueError('日期范围无效') from None
    entries=list(db.scalars(select(Cashflow).where(Cashflow.store_id==store_id,Cashflow.settled_at>=start,Cashflow.settled_at<end+' 23:59:59.999999').order_by(Cashflow.settled_at.desc(),Cashflow.id.desc())))
    orders={(o.order_external_id,o.item_external_id):o for o in db.scalars(select(OrderItem).where(OrderItem.store_id==store_id))}
    rows=[]
    for e in entries:
        order=orders.get((e.order_external_id,e.item_external_id))
        rows.append({'id':e.id,'order_id':e.order_external_id,'item_id':e.item_external_id,'kind':e.kind,
                     'amount_fen':e.amount_fen,'settled_at':e.settled_at,'batch_id':e.batch_id,
                     'order_payment_fen':order.payment_fen if order else None,'matched_order':order is not None})
    income=sum(e.amount_fen for e in entries if e.kind=='income')
    refund=sum(e.amount_fen for e in entries if e.kind=='refund')
    imported=db.scalar(select(ImportBatch.id).where(ImportBatch.store_id==store_id,ImportBatch.kind=='cashflows',ImportBatch.status=='committed').limit(1)) is not None
    return {'start':start,'end':end,'has_imported_bill':imported,'count':len(rows),'income_fen':income,'refund_fen':refund,
            'net_fen':income-refund,'batch_ids':sorted({e.batch_id for e in entries}),
            'unmatched_orders':sum(not x['matched_order'] for x in rows),'items':rows}
