from test_api_agent import client,auth
from app import agent

def test_matching_single_refund_is_verified_automatically(client):
    c,_=client;headers=auth(c)
    orders=('item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\n'
            'i1,o1,A,1,2026-10-01,completed,10,success,2\n').encode()
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('o.csv',orders,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    assert c.post('/api/stores/1/costs',headers=headers,json={'sku':'A','valid_from':'2026-01-01','unit_cost':'3'}).status_code==200
    refunds=('refund_id,item_id,order_id,refund_status,refund_amount\n'
             'r1,i1,o1,success,2\n').encode()
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'refunds'},files={'file':('r.csv',refunds,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    url='/api/stores/1/report?start=2026-10-01&end=2026-10-31'
    report=c.get(url).json();item=report['items'][0]
    assert report['counts']['settled']==1 and item['profit_fen']==500 and item['refund_auto_verified']
    answer=agent.verified_answer(report,'退款情况',{})
    assert '已自动核对完整退款历史' in answer and '这些订单也包含在上述快照中' in answer
    assert '另有 1 笔关闭申请' not in answer
    extra=('refund_id,item_id,order_id,refund_status,refund_amount\n'
           'r2,i1,o1,success,3\n').encode()
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'refunds'},files={'file':('r2.csv',extra,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    report=c.get(url).json();item=report['items'][0]
    assert report['counts']['settled']==0 and '退款流水与订单快照不一致' in item['reasons']
    body={'request_id':'f1636f89-5f9d-46ae-b153-504dc7c7544f','item_id':'i1','source_fingerprint':item['refund_review_fingerprint'],'refund_amount':'5','note':'核对合成退款历史'}
    assert c.post('/api/stores/1/refund-reviews',headers=headers,json=body).status_code==200
    report=c.get(url).json();assert report['known']['profit_fen']==200
    answer=agent.verified_answer(report,'退款情况',{})
    assert '累计成功退款 5.00 元' in answer and '退款金额合计 2.00 元' in answer
