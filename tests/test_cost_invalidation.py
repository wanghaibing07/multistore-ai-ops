import pytest
from sqlalchemy import select
from app.accounting import calculate
from app.models import Adjustment, CostVersion, OrderItem
from test_accounting import HEADER, csv_bytes, import_file, db
from app import importer

@pytest.mark.parametrize('changed_row',[
    'i1,o1,A,2,2026-10-01,completed,20,none,0\n',
    'i1,o1,B,1,2026-10-01,completed,20,none,0\n',
    'i1,o1,A,1,2026-10-02,completed,20,none,0\n',
])
def test_changed_order_requires_new_cost_review(db,changed_row):
    db.add_all([CostVersion(store_id=1,sku='A',valid_from='2026-01-01 00:00:00',unit_cost_fen=300),
                CostVersion(store_id=1,sku='B',valid_from='2026-01-01 00:00:00',unit_cost_fen=600)])
    db.commit()
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,20,none,0\n')
    db.add(Adjustment(store_id=1,item_external_id='i1',kind='cost_set',amount_fen=400,note='old source'));db.commit()
    assert calculate(db,1,'2026-10-01','2026-10-31')['known']['profit_fen']==1600
    p=importer.preview(db,1,'orders','updated.csv',csv_bytes(HEADER+changed_row),'gross')
    conflict=importer.commit(db,1,p['batch_id']);assert conflict['conflicts']==[2]
    assert calculate(db,1,'2026-10-01','2026-10-31')['known']['profit_fen']==1600
    assert importer.commit(db,1,p['batch_id'],accept_changes=True,changes_token=conflict['changes_token'])['updated']==1
    item=calculate(db,1,'2026-10-01','2026-10-31')['items'][0]
    assert item['profit_fen'] is None and item['cost_needs_review'] and item['cost_override_id'] is None
    assert any('重新设置逐单成本' in reason for reason in item['reasons'])
    assert len([x for x in db.scalars(select(Adjustment)) if x.kind=='cost_invalidate'])==1
    import_file(db,'costs','sku,valid_from,unit_cost,supplier\nA,2026-09-01,9,test\n')
    assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i1')).cost_fen is None
    db.add(Adjustment(store_id=1,item_external_id='i1',kind='cost_set',amount_fen=700,note='new verified source'));db.commit()
    item=calculate(db,1,'2026-10-01','2026-10-31')['items'][0]
    assert not item['cost_needs_review'] and item['profit_fen']==1300

def test_new_cost_version_does_not_restore_invalidated_cost_in_api(client):
    c,_=client
    from test_api_agent import auth
    headers=auth(c)
    row=HEADER+'i1,o1,A,1,2026-10-01,completed,20,none,0\n'
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('o.csv',row.encode(),'text/csv')}).json()
    assert c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={}).status_code==200
    assert c.post('/api/stores/1/costs',headers=headers,json={'sku':'A','valid_from':'2026-01-01','unit_cost':'3'}).status_code==200
    row=HEADER+'i1,o1,A,2,2026-10-01,completed,20,none,0\n'
    p=c.post('/api/stores/1/imports/preview',headers=headers,data={'kind':'orders','basis':'gross'},files={'file':('u.csv',row.encode(),'text/csv')}).json()
    conflict=c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={'accept_changes':True}).json()
    assert conflict['conflicts']==[2]
    assert c.get('/api/stores/1/skus').json()[0]['missing_cost']==0
    result=c.post(f"/api/stores/1/imports/{p['batch_id']}/commit",headers=headers,json={'accept_changes':True,'changes_token':conflict['changes_token']})
    assert result.status_code==200 and result.json()['updated']==1
    assert c.get('/api/stores/1/skus').json()[0]['missing_cost']==1
    r=c.post('/api/stores/1/costs',headers=headers,json={'sku':'A','valid_from':'2026-09-01','unit_cost':'4'})
    assert r.status_code==200 and r.json()['matched_orders']==0
    assert c.get('/api/stores/1/skus').json()[0]['missing_cost']==1
    body={'request_id':'a5f0eeef-2831-475e-bbe5-0e9b4e483ca6','item_id':'i1','kind':'cost_set','amount':'8','note':'重新核对采购凭证和历史调整'}
    assert c.post('/api/stores/1/adjustments',headers=headers,json=body).status_code==200
    assert c.get('/api/stores/1/skus').json()[0]['missing_cost']==0
    report=c.get('/api/stores/1/report?start=2026-10-01&end=2026-10-31').json()
    assert report['known']['profit_fen']==1200

from test_api_agent import client

\n