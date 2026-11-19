import pytest
from test_api_agent import client,auth
from app import agent

@pytest.fixture
def ranked_client(client,monkeypatch):
    c,_=client;headers=auth(c)
    data=('item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\n'
          'i1,o1,SKU_A,1,2026-10-01,completed,20,success,1\n'
          'i2,o2,SKU_A,1,2026-10-02,completed,20,success,1\n'
          'i3,o3,SKU_B,1,2026-10-03,completed,20,success,10\n').encode()
    preview=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('o.csv',data,'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{preview['batch_id']}/commit",headers=headers,json={}).status_code==200
    for k,v in {'MULTISTORE_MODEL_URL':'http://127.0.0.1:9999/v1','MULTISTORE_MODEL_API_KEY':'fake','MULTISTORE_MODEL_NAME':'fake'}.items():monkeypatch.setenv(k,v)
    def model(messages,specs,*args):
        if any(m.get('role')=='tool' for m in messages):return {'role':'assistant','content':'查询完成'},{}
        return {'role':'assistant','tool_calls':[{'id':'a','function':{'name':'get_refund_summary','arguments':'{}'}}]},{}
    original=agent.run;monkeypatch.setattr(agent,'run',lambda *a,**kw:original(*a,**kw,model_call=model))
    return c,headers

def ask(c,headers,q):
    return c.post('/api/stores/1/agent/query',headers=headers,json={'question':q,'start':'2026-10-01','end':'2026-10-31'})

def test_refund_count_amount_and_ambiguous_ranking_use_distinct_metrics(ranked_client):
    c,h=ranked_client
    count=ask(c,h,'哪个规格退款笔数最多？').json();aliases={sku:alias for alias,sku in count['aliases'].items()}
    assert f"最多的是 {aliases['SKU_A']}，各 2 笔" in count['answer']
    assert '快照金额，最高的是' not in count['answer']
    ranks=count['evidence'][0]['排行依据']
    assert ranks['笔数最高']['winners']==[aliases['SKU_A']]
    assert ranks['金额最高']['winners']==[aliases['SKU_B']] and ranks['金额最高']['value']=='10.00'
    amount=ask(c,h,'哪个规格退款金额最多？').json()
    assert f"最高的是 {aliases['SKU_B']}，各 10.00 元" in amount['answer']
    assert '子订单笔数，最多的是' not in amount['answer']
    ambiguous=ask(c,h,'哪个规格退款最多？').json()['answer']
    assert f"最多的是 {aliases['SKU_A']}，各 2 笔" in ambiguous
    assert f"最高的是 {aliases['SKU_B']}，各 10.00 元" in ambiguous
    assert ask(c,h,'哪个规格退款率最高？').status_code==400

def test_missing_refund_amount_is_not_presented_as_a_complete_amount_ranking():
    items=[{'sku':'A','refund_status':'success','refund_fen':100},{'sku':'B','refund_status':'success','refund_fen':None}]
    r=agent.refund_ranking_data(items,{'A':'商品1','B':'商品2'})
    assert r['missing']==1 and r['amount']['value']==100 and r['count']['winner_count']==2
