from test_api_agent import client, auth
from test_cashflow_refund_review import upload

def test_basis_review_is_scoped_idempotent_and_invalidated(client):
    browser, _ = client
    headers = auth(browser)
    upload(browser, headers, 'orders', 'item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-01,completed,20,none,0\n')
    assert browser.post('/api/stores/1/costs', headers=headers, json={'sku':'A','valid_from':'2026-01-01','unit_cost':'3'}).status_code==200
    url='/api/stores/1/report?start=2026-10-01&end=2026-10-31'
    item=browser.get(url).json()['items'][0]
    body={'request_id':'469876bc-6a50-4ae7-b325-2878c31be613','item_id':'i1','source_fingerprint':item['basis_source_fingerprint'],'basis':'gross','note':'核对原始凭据'}
    assert browser.post('/api/stores/2/payment-basis-reviews',headers=headers,json=body).status_code==403
    assert browser.post('/api/stores/1/payment-basis-reviews',headers=headers,json=body).status_code==200
    assert browser.post('/api/stores/1/payment-basis-reviews',headers=headers,json=body).json()['duplicate']
    assert browser.get(url).json()['items'][0]['profit_fen']==1700
    upload(browser,headers,'cashflows','bill_type,business_flow,order_id,item_id,refund_id,settled_at,bill_income,bill_refund\ngoods_income,F1,o1,i1,,2026-10-02,20,0\n')
    item=browser.get(url).json()['items'][0]
    assert item['basis_review_stale'] and item['basis']=='unknown' and item['profit_fen'] is None
    body['request_id']='6d75cf4f-ec1f-4ea3-a013-45022fd462c1'
    assert browser.post('/api/stores/1/payment-basis-reviews',headers=headers,json=body).status_code==409

def test_new_refund_review_invalidates_previous_payment_basis(client):
    browser, _ = client
    headers = auth(browser)
    upload(browser, headers, 'orders', 'item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,A,1,2026-10-01,completed,20,success,5\n')
    assert browser.post('/api/stores/1/costs',headers=headers,json={'sku':'A','valid_from':'2026-01-01','unit_cost':'3'}).status_code==200
    url='/api/stores/1/report?start=2026-10-01&end=2026-10-31'
    item=browser.get(url).json()['items'][0]
    refund={'request_id':'28df2348-01c3-41e4-857a-5d03bd42e05d','item_id':'i1','source_fingerprint':item['refund_review_fingerprint'],'refund_amount':'5','note':'完整退款历史'}
    assert browser.post('/api/stores/1/refund-reviews',headers=headers,json=refund).status_code==200
    item=browser.get(url).json()['items'][0]
    basis={'request_id':'d25ab2ed-79ca-4b3e-97d3-e4ed71616e89','item_id':'i1','source_fingerprint':item['basis_source_fingerprint'],'basis':'gross','note':'核对付款及退款'}
    assert browser.post('/api/stores/1/payment-basis-reviews',headers=headers,json=basis).status_code==200
    assert browser.get(url).json()['items'][0]['profit_fen']==1200
    refund['request_id']='cdf565d6-c833-49b4-94e8-34916389095b'
    refund['note']='复核退款历史'
    assert browser.post('/api/stores/1/refund-reviews',headers=headers,json=refund).status_code==200
    item=browser.get(url).json()['items'][0]
    assert item['basis_review_stale'] and item['basis']=='unknown' and item['profit_fen'] is None
    basis['request_id']='d9dd0fa4-3592-4b74-b381-b03881d0b662'
    assert browser.post('/api/stores/1/payment-basis-reviews',headers=headers,json=basis).status_code==409
