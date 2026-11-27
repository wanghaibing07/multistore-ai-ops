import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session,sessionmaker
from app.db import Base,get_db,make_engine
from app.auth import passwords
from app.models import Adjustment, CardMatch, CardPurchase, Cashflow, ImportBatch, Membership, Store, User, OrderItem, AgentRun
from app.main import app
from app import agent

@pytest.fixture
def client(tmp_path):
    engine=make_engine('sqlite:///'+(tmp_path/'api.db').as_posix());Base.metadata.create_all(engine);factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        db.add_all([Store(id=1,name='first'),Store(id=2,name='second'),User(id=1,username='owner',password_hash=passwords.hash('correct-password'),is_admin=True)])
        db.flush();db.add(Membership(user_id=1,store_id=1));db.commit()
    def override():
        with factory() as session:yield session
    app.dependency_overrides[get_db]=override
    with TestClient(app) as c:yield c,factory
    app.dependency_overrides.clear();engine.dispose()

def auth(client):
    r=client.post('/api/login',json={'username':'owner','password':'correct-password'});assert r.status_code==200
    return {'X-CSRF-Token':r.json()['csrf']}

def test_auth_csrf_and_tenant_isolation(client):
    c,_=client
    assert c.get('/api/stores/1/report').status_code==401
    headers=auth(c)
    assert c.get('/api/stores/1/report').status_code==200
    assert c.get('/api/stores/2/report').status_code==403
    assert c.get('/api/stores/2/cashflows').status_code==403
    assert c.post('/api/stores/1/costs',json={}).status_code==403
    assert c.post('/api/stores/2/costs',headers=headers,json={'sku':'A','valid_from':'2026-01-01','unit_cost':'2'}).status_code==403

def test_public_mode_disables_quick_login_but_keeps_password_login(client,monkeypatch):
    c,_=client
    monkeypatch.setenv('MULTISTORE_PUBLIC_MODE','1')
    config=c.get('/api/config')
    assert config.status_code==200
    assert config.json()['public_mode'] is True
    assert config.json()['quick_login_enabled'] is False
    assert config.json()['qianniu_url'].startswith('https://')
    assert config.json()['card_site_url'].startswith('https://')
    assert c.post('/api/quick-login',json={}).status_code==404
    assert c.get('/api/auth/users').status_code==404
    assert c.post('/api/login',json={'username':'owner','password':'correct-password'}).status_code==200

def test_store_date_range_uses_imported_payment_bounds(client):
    c,_=client;headers=auth(c)
    data='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-17,completed,10,none,0\ni2,o2,B,1,2026-10-31,completed,20,none,0\n'.encode()
    preview=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('orders.csv',data,'text/csv')})
    assert preview.status_code==200,preview.text
    batch=preview.json()['batch_id']
    assert c.post(f'/api/stores/1/imports/{batch}/commit',headers=headers,json={}).status_code==200
    result=c.get('/api/stores/1/date-range',headers=headers)
    assert result.status_code==200
    assert result.json()['start']=='2026-10-17'
    assert result.json()['end']=='2026-10-31'
    assert result.json()['has_orders'] is True

def test_delete_selected_orders_removes_order_scoped_records(client):
    c,factory=client;headers=auth(c)
    with factory() as db:
        batch=ImportBatch(store_id=1,kind='orders',file_hash='delete-test',status='committed',row_count=2,committed_at='2026-11-19T10:00:00+00:00')
        db.add(batch);db.flush()
        db.add_all([
            OrderItem(store_id=1,item_external_id='delete-1',order_external_id='order-1',sku='A',product_title='测试订单 1',quantity=1,paid_at='2026-11-19 10:00:00',status='completed',payment_fen=1000,payment_basis='gross',refund_status='none',refund_snapshot_fen=0,batch_id=batch.id),
            OrderItem(store_id=1,item_external_id='delete-2',order_external_id='order-2',sku='B',product_title='测试订单 2',quantity=1,paid_at='2026-11-19 11:00:00',status='completed',payment_fen=1200,payment_basis='gross',refund_status='none',refund_snapshot_fen=0,batch_id=batch.id),
            Adjustment(store_id=1,item_external_id='delete-1',kind='expense',amount_fen=50,note='测试删除'),
            Cashflow(store_id=1,business_flow='flow-1',order_external_id='order-1',item_external_id='delete-1',refund_external_id='',kind='income',amount_fen=1000,settled_at='2026-11-19 12:00:00',batch_id=batch.id),
        ])
        db.commit()
    deleted=c.request('DELETE','/api/stores/1/orders',headers=headers,json={'item_ids':['delete-1','missing-item']})
    assert deleted.status_code==200,deleted.text
    assert deleted.json()['deleted']==1
    assert deleted.json()['missing']==1
    with factory() as db:
        assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='delete-1')) is None
        assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='delete-2')) is not None
        assert db.scalar(select(Adjustment).where(Adjustment.item_external_id=='delete-1')) is None
        assert db.scalar(select(Cashflow).where(Cashflow.item_external_id=='delete-1')) is None
    forbidden=c.request('DELETE','/api/stores/2/orders',headers=headers,json={'item_ids':['delete-2']})
    assert forbidden.status_code==403

def test_delete_selected_card_purchases_removes_matches_and_is_tenant_scoped(client):
    c,factory=client;headers=auth(c)
    with factory() as db:
        batch=ImportBatch(store_id=1,kind='card_purchases',file_hash='card-delete-test',status='committed',row_count=2,committed_at='2026-11-19T10:00:00+00:00')
        db.add(batch);db.flush()
        db.add_all([
            CardPurchase(store_id=1,purchase_external_id='FF-DELETE-1',product_title='删除测试采购单',quantity=1,amount_fen=500,refund_fen=0,paid_at='2026-11-19 10:00:00',status='已付款',source='16688',raw_data={},batch_id=batch.id),
            CardPurchase(store_id=1,purchase_external_id='FF-KEEP-1',product_title='保留测试采购单',quantity=1,amount_fen=600,refund_fen=0,paid_at='2026-11-19 11:00:00',status='已付款',source='16688',raw_data={},batch_id=batch.id),
        ])
        db.flush()
        db.add(CardMatch(store_id=1,item_external_id='item-delete',purchase_id=db.scalar(select(CardPurchase.id).where(CardPurchase.purchase_external_id=='FF-DELETE-1')),score=92,allocated_quantity=1,status='confirmed',evidence={},metadata_json={},request_key='delete-card-match'))
        db.commit()
        deleted_id=db.scalar(select(CardPurchase.id).where(CardPurchase.purchase_external_id=='FF-DELETE-1'))
        kept_id=db.scalar(select(CardPurchase.id).where(CardPurchase.purchase_external_id=='FF-KEEP-1'))
    deleted=c.request('DELETE','/api/stores/1/card-purchases',headers=headers,json={'purchase_ids':[deleted_id,999999]})
    assert deleted.status_code==200,deleted.text
    assert deleted.json()['deleted']==1
    assert deleted.json()['missing']==1
    assert deleted.json()['matched_deleted']==1
    with factory() as db:
        assert db.get(CardPurchase,deleted_id) is None
        assert db.get(CardPurchase,kept_id) is not None
        assert db.scalar(select(CardMatch).where(CardMatch.purchase_id==deleted_id)) is None
    forbidden=c.request('DELETE','/api/stores/2/card-purchases',headers=headers,json={'purchase_ids':[kept_id]})
    assert forbidden.status_code==403

def test_agent_tools_are_scoped_and_original_question_never_sent(client,monkeypatch):
    c,factory=client;headers=auth(c)
    monkeypatch.setenv('MULTISTORE_MODEL_URL','http://127.0.0.1:9999/v1');monkeypatch.setenv('MULTISTORE_MODEL_API_KEY','fake');monkeypatch.setenv('MULTISTORE_MODEL_NAME','fake')
    calls=[]
    def model(messages,specs,*args):
        calls.append(json.dumps(messages,ensure_ascii=False))
        if len(calls)==1:return {'role':'assistant','tool_calls':[{'id':'call1','type':'function','function':{'name':'get_profit_summary','arguments':'{}'}}]},{}
        return {'role':'assistant','content':'目前已核算 0 笔。'},{}
    monkeypatch.setattr(agent,'call_model',model)
    # default argument binds the function at definition time, so inject through a small wrapper.
    original=agent.run
    monkeypatch.setattr(agent,'run',lambda *a,**kw:original(*a,**kw,model_call=model))
    r=c.post('/api/stores/1/agent/query',headers=headers,json={'question':'利润，买家电话13800138000','start':'2026-10-01','end':'2026-10-31'})
    assert r.status_code==200,r.text
    assert all('13800138000' not in m for m in calls)
    assert r.json()['trace'][0]['tool']=='get_profit_summary'
    with factory() as db:assert '13800138000' not in json.dumps(db.query(AgentRun).first().trace)

def test_agent_refuses_unallowed_tool_and_fabricated_number(client,monkeypatch):
    c,_=client;headers=auth(c)
    for k,v in {'MULTISTORE_MODEL_URL':'http://127.0.0.1:9999/v1','MULTISTORE_MODEL_API_KEY':'fake','MULTISTORE_MODEL_NAME':'fake'}.items():monkeypatch.setenv(k,v)
    original=agent.run
    def bad(messages,specs,*args):return {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'execute_sql','arguments':'{}'}}]},{}
    monkeypatch.setattr(agent,'run',lambda *a,**kw:original(*a,**kw,model_call=bad))
    r=c.post('/api/stores/1/agent/query',headers=headers,json={'question':'利润','start':'2026-10-01','end':'2026-10-31'});assert r.status_code==400 and '未授权' in r.text
    calls=[]
    def lying(messages,specs,*args):
        calls.append(1)
        if len(calls)==1:return {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'get_profit_summary','arguments':'{}'}}]},{}
        return {'role':'assistant','content':'利润为 999999 元'},{}
    monkeypatch.setattr(agent,'run',lambda *a,**kw:original(*a,**kw,model_call=lying))
    r=c.post('/api/stores/1/agent/query',headers=headers,json={'question':'利润','start':'2026-10-01','end':'2026-10-31'});assert r.status_code==200 and '999999' not in r.json()['answer'] and r.json()['output_mode']=='verified_summary'

def test_http_import_preview_commit_and_order_list(client):
    c,_=client;headers=auth(c)
    data='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-01,completed,10,none,0\n'.encode()
    r=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'unknown'},files={'file':('orders.csv',data,'text/csv')});assert r.status_code==200,r.text
    bid=r.json()['batch_id'];r=c.post(f'/api/stores/1/imports/{bid}/commit',headers=headers,json={});assert r.status_code==200,r.text
    r=c.get('/api/stores/1/report?start=2026-10-01&end=2026-10-31');assert r.json()['counts']['needs_review']==1

def test_confirmed_card_match_is_visible_in_cost_history(client):
    c,_=client;headers=auth(c)
    orders='item_id,order_id,sku,title,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,Gemini Pro 18个月兑换链接,1,2026-10-01 12:00:00,completed,10,none,0\n'.encode()
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('orders.csv',orders,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    cards=[{'order_id':'FF001','product':'Gemini Pro 18个月兑换链接','merchant':'测试供货商','qty':1,'amount':'3.19','payment_refund':'0.00','paid_at':'2026-10-01 12:01:00','status':'已完成'}]
    imported=c.post('/api/stores/1/card-purchases/import',headers=headers,files={'file':('cards.json',json.dumps(cards).encode(),'application/json')})
    assert imported.status_code==200,imported.text
    suggestions=c.get('/api/stores/1/cost-match-suggestions?start=2026-10-01&end=2026-10-31').json()
    candidate=suggestions['suggestions'][0]['candidates'][0]
    confirmed=c.post('/api/stores/1/card-matches',headers=headers,json={
        'request_id':str(uuid4()),'item_id':'i1','purchase_id':candidate['purchase_id'],
        'score':candidate['score'],'cost':'','note':'人工确认',
    })
    assert confirmed.status_code==200,confirmed.text
    history=c.get('/api/stores/1/card-matches').json()
    assert history[0]['purchase_external_id']=='FF001'
    assert history[0]['cost_fen']==319

def test_bulk_gross_basis_creates_auditable_reviews(client):
    c,_=client;headers=auth(c)
    data='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-01,completed,10,none,0\n'.encode()
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'unknown'},files={'file':('orders.csv',data,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    first=c.post('/api/stores/1/payment-basis-reviews/apply-gross',headers=headers,json={})
    assert first.status_code==200 and first.json()=={'created':1,'skipped':0}
    again=c.post('/api/stores/1/payment-basis-reviews/apply-gross',headers=headers,json={})
    assert again.status_code==200 and again.json()=={'created':0,'skipped':1}
    report=c.get('/api/stores/1/report?start=2026-10-01&end=2026-10-31').json()
    assert report['items'][0]['basis']=='gross'
    assert report['items'][0]['basis_review_id'] is not None

def test_cashflow_http_import_is_scoped_and_reported_separately(client):
    c,_=client;headers=auth(c)
    data=('账单大类,业务流水号,订单号,子订单号,退款单号,打款时间,订单实际金额（元）,退款金额（元）,买家电话\n'
          '货款收入,F1,O1,I1,,2026-10-01,12.50,0,13800138000\n').encode()
    request={'kind':'cashflows'};files={'file':('bill.csv',data,'text/csv')}
    assert c.post('/api/stores/2/imports/preview',headers=headers,data=request,files=files).status_code==403
    p=c.post('/api/stores/1/imports/preview',headers=headers,data=request,files=files).json()
    assert not p['errors'] and '13800138000' not in json.dumps(p,ensure_ascii=False)
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    report=c.get('/api/stores/1/cashflows?start=2026-10-01&end=2026-10-31').json()
    assert report['income_fen']==1250 and report['unmatched_orders']==1
    assert c.get('/api/stores/1/report?start=2026-10-01&end=2026-10-31').json()['counts']['paid_orders']==0

def test_adjustment_retries_are_idempotent(client):
    c,factory=client;headers=auth(c)
    with factory() as db:
        from app.models import ImportBatch,OrderItem
        b=ImportBatch(id=1,store_id=1,kind='orders',file_hash='x'*64,basis='gross',status='committed',row_count=1)
        db.add(b);db.flush()
        db.add(OrderItem(store_id=1,item_external_id='i1',order_external_id='o1',sku='A',product_title='',quantity=1,paid_at='2026-10-01 00:00:00',status='completed',payment_fen=1000,payment_basis='gross',refund_status='none',refund_snapshot_fen=0,batch_id=1));db.commit()
    body={'request_id':'a5f0eeef-2831-475e-bbe5-0e9b4e483ca6','item_id':'i1','kind':'expense','amount':'2.00','note':'补发'}
    first=c.post('/api/stores/1/adjustments',headers=headers,json=body)
    again=c.post('/api/stores/1/adjustments',headers=headers,json=body)
    assert first.status_code==200 and again.status_code==200
    assert first.json()['id']==again.json()['id'] and again.json()['duplicate'] is True
    body['amount']='3.00'
    assert c.post('/api/stores/1/adjustments',headers=headers,json=body).status_code==409
    assert len(c.get('/api/stores/1/adjustments/i1').json())==1

def test_enter_verified_per_order_cost_without_sku_cost(client):
    c,_=client;headers=auth(c)
    data='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,2,2026-10-01,completed,10,none,0\n'.encode()
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('orders.csv',data,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    body={'request_id':'a5f0eeef-2831-475e-bbe5-0e9b4e483ca6','item_id':'i1','kind':'cost_set','amount':'3.00','note':''}
    assert c.post('/api/stores/1/adjustments',headers=headers,json=body).status_code==400
    body['note']='采购凭证已核对'
    first=c.post('/api/stores/1/adjustments',headers=headers,json=body)
    again=c.post('/api/stores/1/adjustments',headers=headers,json=body)
    assert first.status_code==200 and again.json()['duplicate']
    report=c.get('/api/stores/1/report?start=2026-10-01&end=2026-10-31').json()
    assert report['counts']['settled']==1 and report['known']['profit_fen']==700
    assert report['items'][0]['cost_override_id']==first.json()['id']
    assert c.get('/api/stores/1/skus').json()[0]['missing_cost']==0
    body.update(request_id='f1636f89-5f9d-46ae-b153-504dc7c7544f',amount='4.00')
    assert c.post('/api/stores/1/adjustments',headers=headers,json=body).status_code==200
    report=c.get('/api/stores/1/report?start=2026-10-01&end=2026-10-31').json()
    assert report['known']['profit_fen']==600
    assert len(c.get('/api/stores/1/adjustments/i1').json())==2

def test_closed_refund_review_is_auditable_and_invalidated_by_new_data(client):
    c,_=client;headers=auth(c)
    data='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-01,completed,10,closed,0\n'.encode()
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('orders.csv',data,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    assert c.post('/api/stores/1/costs',headers=headers,json={'sku':'A','valid_from':'2026-01-01','unit_cost':'3'}).status_code==200
    url='/api/stores/1/report?start=2026-10-01&end=2026-10-31'
    report=c.get(url).json();assert report['counts']['settled']==0
    body={'request_id':'a5f0eeef-2831-475e-bbe5-0e9b4e483ca6','item_id':'i1','source_fingerprint':report['items'][0]['refund_review_fingerprint'],'refund_amount':'0','note':'已核对退款历史'}
    assert c.post('/api/stores/2/refund-reviews',headers=headers,json=body).status_code==403
    first=c.post('/api/stores/1/refund-reviews',headers=headers,json=body)
    assert first.status_code==200 and c.post('/api/stores/1/refund-reviews',headers=headers,json=body).json()['duplicate']
    report=c.get(url).json();assert report['known']['profit_fen']==700 and report['items'][0]['refund_review_id']==first.json()['id']
    assert c.get('/api/stores/1/refund-reviews/i1').json()[0]['note']=='已核对退款历史'
    refund='refund_id,item_id,order_id,refund_status,refund_amount\nr1,i1,o1,success,2\n'.encode()
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'refunds'},files={'file':('refunds.csv',refund,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    changed=c.get(url).json();assert changed['counts']['settled']==0 and changed['items'][0]['refund_review_stale']
    body['request_id']='f1636f89-5f9d-46ae-b153-504dc7c7544f'
    assert c.post('/api/stores/1/refund-reviews',headers=headers,json=body).status_code==409
    body['source_fingerprint']=changed['items'][0]['refund_review_fingerprint']
    assert c.post('/api/stores/1/refund-reviews',headers=headers,json=body).status_code==400
    body['refund_amount']='2'
    assert c.post('/api/stores/1/refund-reviews',headers=headers,json=body).status_code==200
    report=c.get(url).json();assert report['known']['profit_fen']==500
    answer=agent.verified_answer(report,'退款情况',{})
    assert '人工核对' in answer and '2.00' in answer and '与上述快照分开统计' in answer

def test_agent_must_call_relevant_tool(client,monkeypatch):
    c,_=client;headers=auth(c)
    for k,v in {'MULTISTORE_MODEL_URL':'http://127.0.0.1:9999/v1','MULTISTORE_MODEL_API_KEY':'fake','MULTISTORE_MODEL_NAME':'fake'}.items():monkeypatch.setenv(k,v)
    calls=[]
    def wrong_tool(messages,specs,*args):
        calls.append(1)
        if len(calls)==1:return {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'get_order_details','arguments':'{}'}}]},{}
        return {'role':'assistant','content':'查询完成'},{}
    original=agent.run
    monkeypatch.setattr(agent,'run',lambda *a,**kw:original(*a,**kw,model_call=wrong_tool))
    r=c.post('/api/stores/1/agent/query',headers=headers,json={'question':'退款多少','start':'2026-10-01','end':'2026-10-31'})
    assert r.status_code==400 and '退款汇总' in r.text


def test_verified_answer_uses_ui_metric_names(client):
    c,_=client;headers=auth(c)
    data='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-01,completed,10,none,0\n'.encode()
    preview=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('orders.csv',data,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{preview['batch_id']}/commit",headers=headers,json={}).status_code==200
    assert c.post('/api/stores/1/costs',headers=headers,json={'sku':'A','valid_from':'2026-01-01','unit_cost':'3'}).status_code==200
    report=c.get('/api/stores/1/report?start=2026-10-01&end=2026-10-31').json()
    answer=agent.verified_answer(report,'请逐项列出纯利润、营业额、成本和有效订单。',{})
    assert '纯利润 7.00 元' in answer
    assert '营业额 10.00 元（退款前实付快照）' in answer
    assert '商品成本 3.00 元' in answer
    assert '有效订单 1 笔' in answer


def test_specific_order_is_resolved_locally_without_sending_id_to_model(client,monkeypatch):
    c,factory=client;headers=auth(c)
    with factory() as db:
        from app.models import ImportBatch,OrderItem
        b=ImportBatch(id=1,store_id=1,kind='orders',file_hash='y'*64,basis='gross',status='committed',row_count=1)
        db.add(b);db.flush()
        db.add(OrderItem(store_id=1,item_external_id='1234567890123456789',order_external_id='2234567890123456789',sku='A',product_title='',quantity=1,paid_at='2026-10-01 00:00:00',status='completed',payment_fen=1234,payment_basis='gross',refund_status='none',refund_snapshot_fen=0,batch_id=1));db.commit()
    for k,v in {'MULTISTORE_MODEL_URL':'http://127.0.0.1:9999/v1','MULTISTORE_MODEL_API_KEY':'fake','MULTISTORE_MODEL_NAME':'fake'}.items():monkeypatch.setenv(k,v)
    calls=[]
    def model(messages,specs,*args):
        calls.append(json.dumps(messages,ensure_ascii=False))
        if len(calls)==1:return {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'get_order_details','arguments':'{}'}}]},{}
        return {'role':'assistant','content':'完成'},{}
    original=agent.run;monkeypatch.setattr(agent,'run',lambda *a,**kw:original(*a,**kw,model_call=model))
    r=c.post('/api/stores/1/agent/query',headers=headers,json={'question':'订单1234567890123456789实付多少？','start':'2026-10-01','end':'2026-10-31'})
    assert r.status_code==200,r.text
    assert '12.34' in r.json()['answer']
    assert all('1234567890123456789' not in message for message in calls)

def test_product_scope_is_checked_and_not_sent_to_model(client,monkeypatch):
    c,factory=client;headers=auth(c)
    with factory() as db:
        from app.models import ImportBatch,OrderItem
        db.add(ImportBatch(id=1,store_id=1,kind='orders',file_hash='z'*64,basis='gross',status='committed',row_count=1));db.flush()
        db.add(OrderItem(store_id=1,item_external_id='i1',order_external_id='o1',sku='private-product-code',product_title='',quantity=1,paid_at='2026-10-01 00:00:00',status='completed',payment_fen=1000,payment_basis='gross',refund_status='none',refund_snapshot_fen=0,batch_id=1));db.commit()
    for k,v in {'MULTISTORE_MODEL_URL':'http://127.0.0.1:9999/v1','MULTISTORE_MODEL_API_KEY':'fake','MULTISTORE_MODEL_NAME':'fake'}.items():monkeypatch.setenv(k,v)
    calls=[]
    def model(messages,specs,*args):
        calls.append(json.dumps(messages,ensure_ascii=False))
        if len(calls)==1:return {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'get_profit_summary','arguments':'{}'}}]},{}
        return {'role':'assistant','content':'完成'},{}
    original=agent.run;monkeypatch.setattr(agent,'run',lambda *a,**kw:original(*a,**kw,model_call=model))
    body={'question':'这个商品利润多少','start':'2026-10-01','end':'2026-10-31'}
    assert c.post('/api/stores/1/agent/query',headers=headers,json=body).status_code==400
    body['sku']='private-product-code';r=c.post('/api/stores/1/agent/query',headers=headers,json=body)
    assert r.status_code==200,r.text
    assert all('private-product-code' not in msg for msg in calls)
    body['sku']='someone-else';assert c.post('/api/stores/1/agent/query',headers=headers,json=body).status_code==400

def test_password_reset_revokes_existing_session(client,monkeypatch):
    import sys
    from app import cli
    c,factory=client;auth(c)
    monkeypatch.setattr(cli,'SessionLocal',factory)
    monkeypatch.setattr(cli.command,'upgrade',lambda *a,**k:None)
    monkeypatch.setattr(cli.getpass,'getpass',lambda *_:'new-synthetic-password')
    monkeypatch.setattr(sys,'argv',['multistore','reset-password','--username','owner'])
    cli.main()
    assert c.get('/api/me').status_code==401
    assert c.post('/api/login',json={'username':'owner','password':'correct-password'}).status_code==401
    assert c.post('/api/login',json={'username':'owner','password':'new-synthetic-password'}).status_code==200

def test_quick_login_flow(client):
    c, factory = client
    r = c.post('/api/quick-login')
    assert r.status_code == 200
    data = r.json()
    assert 'csrf' in data
    assert data['username'] == 'owner'
    assert len(data['stores']) > 0

    me = c.get('/api/me')
    assert me.status_code == 200
    assert me.json()['username'] == 'owner'

    users = c.get('/api/auth/users')
    assert users.status_code == 200
    assert any(u['username'] == 'owner' for u in users.json())

    with factory() as db:
        db.add(User(id=2, username='partner', password_hash='hash', is_admin=False))
        db.flush()
        db.add(Membership(user_id=2, store_id=2))
        db.commit()

    r_partner = c.post('/api/quick-login', json={'username': 'partner'})
    assert r_partner.status_code == 200
    assert r_partner.json()['username'] == 'partner'
    assert c.get('/api/me').json()['username'] == 'partner'


def test_quick_login_rejects_non_loopback_and_forwarded_requests(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv('MULTISTORE_PUBLIC_MODE', raising=False)
    monkeypatch.delenv('MULTISTORE_ALLOW_QUICK_LOGIN', raising=False)

    for headers in (
        {'X-Forwarded-For': '203.0.113.10'},
        {'X-Forwarded-For': '203.0.113.10, 127.0.0.1'},
        {'Forwarded': 'for=203.0.113.10;proto=https'},
    ):
        cfg = c.get('/api/config', headers=headers)
        assert cfg.status_code == 200
        assert cfg.json()['public_mode'] is False
        assert cfg.json()['quick_login_enabled'] is False
        assert c.post('/api/quick-login', json={}, headers=headers).status_code == 403
        assert c.post('/api/auth/quick-login', json={}, headers=headers).status_code == 403
        assert c.get('/api/auth/users', headers=headers).status_code == 403

    assert c.post(
        '/api/login',
        json={'username': 'owner', 'password': 'correct-password'},
        headers={'X-Forwarded-For': '203.0.113.10'},
    ).status_code == 200

    with TestClient(app, client=('198.51.100.25', 50000)) as remote:
        cfg = remote.get('/api/config')
        assert cfg.status_code == 200
        assert cfg.json()['quick_login_enabled'] is False
        assert remote.post('/api/quick-login', json={}).status_code == 403
        assert remote.post('/api/auth/quick-login', json={}).status_code == 403
        assert remote.get('/api/auth/users').status_code == 403

    loopback_cfg = c.get('/api/config', headers={'X-Forwarded-For': '127.0.0.1'})
    assert loopback_cfg.status_code == 200
    assert loopback_cfg.json()['quick_login_enabled'] is True
    assert c.get('/api/auth/users', headers={'X-Forwarded-For': '127.0.0.1'}).status_code == 200
    assert c.post('/api/quick-login', json={}, headers={'X-Forwarded-For': 'testclient'}).status_code == 403


def test_quick_login_allow_env_and_public_mode_precedence(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv('MULTISTORE_PUBLIC_MODE', raising=False)
    monkeypatch.setenv('MULTISTORE_ALLOW_QUICK_LOGIN', '1')

    proxy_headers = {'X-Forwarded-For': '203.0.113.10'}
    cfg = c.get('/api/config', headers=proxy_headers)
    assert cfg.status_code == 200
    assert cfg.json()['public_mode'] is False
    assert cfg.json()['quick_login_enabled'] is True
    assert c.post('/api/quick-login', json={}, headers=proxy_headers).status_code == 200
    assert c.post('/api/auth/quick-login', json={}, headers=proxy_headers).status_code == 200
    assert c.get('/api/auth/users', headers=proxy_headers).status_code == 200

    with TestClient(app, client=('198.51.100.25', 50000)) as remote:
        assert remote.get('/api/config').json()['quick_login_enabled'] is True
        assert remote.post('/api/quick-login', json={}).status_code == 200

    monkeypatch.setenv('MULTISTORE_PUBLIC_MODE', '1')
    for headers in ({}, proxy_headers):
        pub_cfg = c.get('/api/config', headers=headers)
        assert pub_cfg.status_code == 200
        assert pub_cfg.json()['public_mode'] is True
        assert pub_cfg.json()['quick_login_enabled'] is False
        assert c.post('/api/quick-login', json={}, headers=headers).status_code == 404
        assert c.post('/api/auth/quick-login', json={}, headers=headers).status_code == 404
        assert c.get('/api/auth/users', headers=headers).status_code == 404
