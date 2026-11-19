from test_api_agent import client, auth
from app import agent


def upload(client,headers,kind,body,basis='unknown'):
    preview=client.post('/api/stores/1/imports/preview',headers=headers,
        data={'kind':kind,'basis':basis},files={'file':(kind+'.csv',body.encode(),'text/csv')})
    assert preview.status_code==200 and not preview.json()['errors']
    response=client.post(f"/api/stores/1/imports/{preview.json()['batch_id']}/commit",headers=headers,json={})
    assert response.status_code==200 and not response.json().get('conflicts')
    return preview.json()['batch_id']


def test_cashflow_refund_disagreement_requires_review_and_new_cashflow_invalidates(client):
    c,_=client;headers=auth(c)
    order='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-01,completed,20,none,0\n'
    upload(c,headers,'orders',order,'gross')
    assert c.post('/api/stores/1/costs',headers=headers,json={'sku':'A','valid_from':'2026-01-01','unit_cost':'3'}).status_code==200
    report_url='/api/stores/1/report?start=2026-10-01&end=2026-10-31'
    assert c.get(report_url).json()['known']['profit_fen']==1700
    header='bill_type,business_flow,order_id,item_id,refund_id,settled_at,bill_income,bill_refund\n'
    batch=upload(c,headers,'cashflows',header+'goods_income,F1,o1,i1,R1,2026-11-01,0,5\n')
    report=c.get(report_url).json();item=report['items'][0]
    assert report['counts']['settled']==0 and item['profit_fen'] is None
    assert '货款账单有售后退款而订单快照无退款，需核对退款历史' in item['reasons']
    assert item['cashflow_refund_records'][0]['amount_fen']==500 and batch in report['batch_ids']
    body={'request_id':'4d182541-6354-4cf8-a30d-3370b4ebed66','item_id':'i1',
          'source_fingerprint':item['refund_review_fingerprint'],'refund_amount':'0','note':'核对完整退款历史'}
    assert c.post('/api/stores/1/refund-reviews',headers=headers,json=body).status_code==400
    # A merchant cash outflow need not equal the customer refund; the operator
    # records the latter only after reviewing the full history.
    body['refund_amount']='6'
    assert c.post('/api/stores/1/refund-reviews',headers=headers,json=body).status_code==200
    report=c.get(report_url).json();assert report['known']['profit_fen']==1100
    data,_=agent.safe_data(report)
    detail=data['get_order_details']['条目'][0]
    assert detail['退款快照元']=='0.00' and detail['已核对累计退款元']=='6.00'
    answer=agent.verified_answer(report,'退款情况',{})
    assert '另有 1 笔订单经人工核对' in answer and '关闭申请' not in answer
    upload(c,headers,'cashflows',header+'goods_income,F2,o1,i1,R2,2026-11-02,0,1\n')
    report=c.get(report_url).json();item=report['items'][0]
    assert report['counts']['settled']==0 and item['refund_review_stale']
    assert c.post('/api/stores/1/refund-reviews',headers=headers,json=body).status_code==200 # idempotent old review
    body['request_id']='36e3e94c-5803-4e45-b736-1baee72b94d9'
    assert c.post('/api/stores/1/refund-reviews',headers=headers,json=body).status_code==409


def test_cashflow_refund_from_other_parent_does_not_attach(client):
    c,_=client;headers=auth(c)
    upload(c,headers,'orders','item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-01,completed,20,none,0\n','gross')
    assert c.post('/api/stores/1/costs',headers=headers,json={'sku':'A','valid_from':'2026-01-01','unit_cost':'3'}).status_code==200
    upload(c,headers,'cashflows','bill_type,business_flow,order_id,item_id,refund_id,settled_at,bill_income,bill_refund\ngoods_income,F1,wrong-parent,i1,R1,2026-11-01,0,5\n')
    item=c.get('/api/stores/1/report?start=2026-10-01&end=2026-10-31').json()['items'][0]
    assert item['profit_fen']==1700 and not item['cashflow_refund_records']
