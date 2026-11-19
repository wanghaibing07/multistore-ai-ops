"""Run 30 synthetic-only scenarios against the configured model; output contains no real merchant data."""
from __future__ import annotations
import argparse,json,tempfile,sys,hashlib,os
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy.orm import Session,sessionmaker
from app.agent import AgentProblem,run,normal_numbers
from app.db import Base,make_engine
from app.models import Store,CostVersion,OrderItem,ImportBatch,User,RefundReview
from app.refund_review import fingerprint
from app import importer

PERIODS=[
 ('2026-10-01','2026-10-01',40,10,1,0,0),
 ('2026-10-02','2026-10-02',40,0,1,0,0),
 ('2026-10-03','2026-10-03',-10,30,1,0,0),
 ('2026-10-01','2026-10-02',80,10,2,0,0),
 ('2026-10-01','2026-10-03',70,40,3,0,0),
 ('2026-10-01','2026-10-04',70,40,4,0,1),
 ('2026-10-01','2026-10-05',70,40,5,1,1),
 ('2026-10-04','2026-10-05',None,0,2,1,1),
 ('2026-10-05','2026-10-05',None,0,1,1,0),
 ('2026-10-06','2026-10-06',None,0,0,0,0),
]
QUESTIONS=[
 ('profit','这段时间已核算订单的贡献利润是多少？'),
 ('refund','这段时间订单中已成功退款的快照金额合计是多少？'),
 ('orders','这段时间按付款日期统计有多少笔付款订单？'),
]
HEADER='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\n'
ROWS=(
 'I01,O01,A,1,2026-10-01 10:00:00,completed,100,success,10\n'
 'I02,O02,B,1,2026-10-02 10:00:00,completed,50,none,0\n'
 'I03,O03,C,1,2026-10-03 10:00:00,closed,30,success,30\n'
 'I04,O04,D,1,2026-10-04 10:00:00,pending,20,none,0\n'
 'I05,O05,E,1,2026-10-05 10:00:00,completed,60,none,0\n')

def seed(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Store(id=1,name='synthetic'),User(id=1,username='synthetic-reviewer',password_hash='unused')])
        db.flush()
        for sku,cost in [('A',5000),('B',1000),('C',1000),('D',500)]:
            db.add(CostVersion(store_id=1,sku=sku,valid_from='2026-01-01 00:00:00',unit_cost_fen=cost,supplier='synthetic'))
        db.commit()
        raw=(HEADER+ROWS).encode();p=importer.preview(db,1,'orders','synthetic.csv',raw,'gross');importer.commit(db,1,p['batch_id'])
        for order in db.query(OrderItem).filter(OrderItem.refund_status=='success'):
            db.add(RefundReview(store_id=1,item_external_id=order.item_external_id,reviewer_id=1,source_fingerprint=fingerprint(order,[]),refund_fen=order.refund_snapshot_fen,note='synthetic reviewed history',request_key='synthetic-'+order.item_external_id))
        db.commit()

def evaluate(factory,case):
    index,kind,question,start,end,expected,review,pending=case
    try:
        with factory() as db: out=run(db,1,start,end,question)
        tools=[x['tool'] for x in out['trace']]
        expected_tool={'profit':'get_profit_summary','refund':'get_refund_summary','orders':'get_profit_summary'}[kind]
        nums=normal_numbers(out['answer'])
        if expected is None:
            content_ok=any(x in out['answer'] for x in ('未','无','缺','待','不能','暂'))
        else:content_ok=expected in nums
        # Orders questions can legitimately use the order-details tool instead.
        tool_ok=expected_tool in tools or (kind=='orders' and 'get_order_details' in tools)
        return {'id':index,'kind':kind,'period':[start,end],'expected':expected,'review':review,'pending':pending,
                'ok':bool(content_ok and tool_ok),'content_ok':bool(content_ok),'tool_ok':bool(tool_ok),
                'tools':tools,'answer':out['answer']}
    except Exception as exc:
        return {'id':index,'kind':kind,'period':[start,end],'expected':expected,'review':review,'pending':pending,
                'ok':False,'error':str(exc) if isinstance(exc,AgentProblem) else type(exc).__name__}

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--workers',type=int,default=3);a=p.parse_args()
    if a.output.exists():raise SystemExit('Output file exists; choose a new name')
    cases=[]
    for i,(start,end,profit,refund,orders,review,pending) in enumerate(PERIODS):
        for j,(kind,question) in enumerate(QUESTIONS):cases.append((i*3+j+1,kind,question,start,end,{'profit':profit,'refund':refund,'orders':orders}[kind],review,pending))
    with tempfile.TemporaryDirectory(prefix='multistore-synthetic-') as temp:
        engine=make_engine('sqlite:///'+(Path(temp)/'eval.db').as_posix());seed(engine);factory=sessionmaker(engine,expire_on_commit=False)
        results=[]
        with ThreadPoolExecutor(max_workers=max(1,min(a.workers,4))) as pool:
            futures=[pool.submit(evaluate,factory,x) for x in cases]
            for future in as_completed(futures):
                r=future.result();results.append(r);print('case',r['id'],'pass' if r['ok'] else 'review',flush=True)
        results.sort(key=lambda x:x['id']);engine.dispose()
    a.output.parent.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parents[1]
    files=['app/agent.py','app/accounting.py','app/importer.py','app/models.py','app/refund_review.py','app/payment_basis_review.py','scripts/evaluate_agent.py']
    hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}
    with a.output.open('x',encoding='utf-8') as f:json.dump({'dataset':'synthetic','model':os.getenv('MULTISTORE_MODEL_NAME',''),'source_sha256':hashes,'results':results,'automated_pass':sum(x['ok'] for x in results),'total':len(results),'note':'Automated checks do not replace semantic review. Model selects tools; server renders facts.'},f,ensure_ascii=False,indent=2)
    print('automated_pass',sum(x['ok'] for x in results),'/',len(results),flush=True)
if __name__=='__main__':main()
