from __future__ import annotations
import io
import json
import sqlite3
from pathlib import Path
import pytest
from openpyxl import Workbook
from sqlalchemy import func,select
from sqlalchemy.orm import Session
from app.db import Base, make_engine
from app.models import Adjustment, Cashflow, CostVersion, ImportBatch, ImportRow, OrderItem, Refund, RefundReview, Store, User
from app.refund_review import fingerprint
from app import importer
from app.accounting import calculate
from app.cashflow import summarize

@pytest.fixture
def db(tmp_path):
    e=make_engine('sqlite:///'+(tmp_path/'test.db').as_posix());Base.metadata.create_all(e)
    with Session(e) as s:
        s.add(Store(name='shop'));s.commit();yield s
    e.dispose()

def csv_bytes(body):return body.encode('utf-8')
def import_file(db,kind,text,basis='gross',accept=False):
    p=importer.preview(db,1,kind,kind+'.csv',csv_bytes(text),basis)
    assert not p['errors'],p['errors']
    return p,importer.commit(db,1,p['batch_id'],accept_changes=accept)

HEADER='item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\n'

def verify_synthetic_refund(db,item_id,amount):
    from uuid import uuid4
    if not db.get(User,1):
        db.add(User(id=1,username='synthetic-reviewer',password_hash='unused',is_admin=True));db.flush()
    order=db.scalar(select(OrderItem).where(OrderItem.store_id==1,OrderItem.item_external_id==item_id))
    refunds=list(db.scalars(select(Refund).where(Refund.store_id==1,Refund.item_external_id==item_id)))
    db.add(RefundReview(store_id=1,item_external_id=item_id,reviewer_id=1,source_fingerprint=fingerprint(order,refunds),refund_fen=amount,note='synthetic complete history',request_key=str(uuid4())))
    db.commit()

def test_refund_cost_and_adjustment_are_calculated_once(db):
    db.add(CostVersion(store_id=1,sku='A',valid_from='2026-01-01 00:00:00',unit_cost_fen=700,supplier=''));db.commit()
    data=HEADER+'i1,o1,A,1,2026-10-01 12:00:00,closed,10.00,success,10.00\ni2,o2,A,1,2026-10-02 12:00:00,completed,10.00,success,3.00\ni3,o3,A,1,,unpaid,,none,0.00\n'
    import_file(db,'orders',data)
    db.add(Adjustment(store_id=1,item_external_id='i1',kind='supplier_refund',amount_fen=200,note=''));db.add(Adjustment(store_id=1,item_external_id='i2',kind='expense',amount_fen=50,note=''));db.commit()
    verify_synthetic_refund(db,'i1',1000);verify_synthetic_refund(db,'i2',300)
    report=calculate(db,1,'2026-10-01','2026-10-31')
    assert report['counts']['paid_orders']==2
    assert report['counts']['settled']==2
    assert report['known']['profit_fen']==-50
    import_file(db,'refunds','refund_id,item_id,order_id,refund_status,refund_amount,completed_at\nr1,i1,o1,success,10.00,2026-11-02\n')
    stale=calculate(db,1,'2026-10-01','2026-10-31')
    auto_item=next(x for x in stale['items'] if x['item_id']=='i1')
    assert auto_item['refund_auto_verified'] and not auto_item['refund_review_stale']
    verify_synthetic_refund(db,'i1',1000)
    again=calculate(db,1,'2026-10-01','2026-10-31')
    assert again['known']['profit_fen']==-50
    assert again['known']['refund_fen']==1300

def test_refund_cost_follows_fulfillment_state(db):
    db.add(CostVersion(store_id=1,sku='A',valid_from='2026-01-01 00:00:00',unit_cost_fen=700));db.commit()
    header='item_id,order_id,sku,quantity,paid_at,status,shipped_at,payment,refund_status,refund_amount\n'
    data=(header+
          'unshipped,ou,A,1,2026-10-01,closed,,10,success,10\n'+
          'shipped,os,A,1,2026-10-02,closed,2026-10-02 10:00:00,10,success,10\n'+
          'partial,op,A,1,2026-10-03,completed,2026-10-03 10:00:00,10,success,3\n')
    import_file(db,'orders',data)
    auto_review=calculate(db,1,'2026-10-01','2026-10-31')
    auto_items={item['item_id']:item for item in auto_review['items']}
    assert auto_items['unshipped']['profit_fen']==0 and auto_items['unshipped']['refund_auto_verified']
    assert auto_items['shipped']['profit_fen']==-700 and auto_items['shipped']['refund_auto_verified']
    assert auto_items['partial']['profit_fen']==0 and auto_items['partial']['refund_auto_verified']
    verify_synthetic_refund(db,'unshipped',1000)
    verify_synthetic_refund(db,'shipped',1000)
    verify_synthetic_refund(db,'partial',300)
    report=calculate(db,1,'2026-10-01','2026-10-31')
    items={item['item_id']:item for item in report['items']}
    assert items['unshipped']['fulfillment_status']=='unshipped'
    assert items['unshipped']['cost_fen']==0 and items['unshipped']['profit_fen']==0
    assert items['shipped']['fulfillment_status']=='shipped'
    assert items['shipped']['cost_fen']==700 and items['shipped']['profit_fen']==-700
    assert items['partial']['cost_fen']==700 and items['partial']['profit_fen']==0

def test_net_basis_is_not_refunded_twice_and_unknown_is_not_profit(db):
    db.add(CostVersion(store_id=1,sku='A',valid_from='2026-01-01 00:00:00',unit_cost_fen=100));db.commit()
    row=HEADER+'i1,o1,A,1,2026-10-01,completed,7.00,success,3.00\n'
    import_file(db,'orders',row,basis='net')
    verify_synthetic_refund(db,'i1',300)
    r=calculate(db,1,'2026-10-01','2026-10-31');assert r['known']['profit_fen']==600
    row2=HEADER+'i2,o2,A,1,2026-10-02,completed,7.00,none,0.00\n'
    import_file(db,'orders',row2,basis='unknown')
    r=calculate(db,1,'2026-10-01','2026-10-31');assert r['counts']['settled']==1;assert r['counts']['needs_review']==1

def test_versions_and_missing_cost_snapshots(db):
    db.add(CostVersion(store_id=1,sku='A',valid_from='2026-01-01 00:00:00',unit_cost_fen=300));db.commit()
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\ni2,o2,B,1,2026-10-02,completed,10,none,0\n')
    import_file(db,'costs','sku,valid_from,unit_cost,supplier\nA,2026-09-01,8,source\nB,2026-01-01,4,source\n')
    a=db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i1'));b=db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i2'))
    assert a.cost_fen==300
    assert b.cost_fen==400
    import_file(db,'orders',HEADER+'i3,o3,A,1,2026-10-03,completed,10,none,0\n')
    assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i3')).cost_fen==800

def test_repeat_import_change_confirmation_and_atomic_rollback(db):
    text=HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\n'
    p,_=import_file(db,'orders',text)
    same=importer.preview(db,1,'orders','x.csv',csv_bytes(text),'gross');assert same['duplicate']
    changed=HEADER+'i2,o2,B,1,2026-10-02,completed,9,none,0\ni1,o1,A,1,2026-10-01,completed,8,none,0\n'
    p=importer.preview(db,1,'orders','x.csv',csv_bytes(changed),'gross')
    result=importer.commit(db,1,p['batch_id']);assert result['conflicts']==[3]
    assert result['differences'][0]['fields']['payment_fen']=={'old':1000,'new':800}
    assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i2')) is None
    result=importer.commit(db,1,p['batch_id'],accept_changes=True,changes_token=result['changes_token']);assert result['inserted']==1 and result['updated']==1
    assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i1')).payment_fen==800

def test_preview_shows_dates_changes_and_rechecks_stale_confirmation(db):
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\n')
    changed=HEADER+'i1,o1,A,1,2026-10-03,completed,8,none,0\ni2,o2,B,1,2026-10-04,completed,9,none,0\n'
    p=importer.preview(db,1,'orders','new.csv',csv_bytes(changed),'gross')
    assert (p['time_start'],p['time_end'],p['time_missing'])==('2026-10-03 00:00:00','2026-10-04 00:00:00',0)
    assert p['differences'][0]['fields']['payment_fen']=={'old':1000,'new':800}
    first=importer.commit(db,1,p['batch_id'])
    assert first['conflicts']==[2]
    order=db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i1'))
    order.payment_fen=1100;db.commit()
    stale=importer.commit(db,1,p['batch_id'],accept_changes=True,changes_token=first['changes_token'])
    assert stale['changes_token']!=first['changes_token']
    assert stale['differences'][0]['fields']['payment_fen']=={'old':1100,'new':800}
    assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i2')) is None
    result=importer.commit(db,1,p['batch_id'],accept_changes=True,changes_token=stale['changes_token'])
    assert result['inserted']==1 and result['updated']==1
    assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i1')).payment_fen==800

def test_same_file_can_correct_unknown_basis_with_confirmation(db):
    text=HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\n'
    import_file(db,'orders',text,basis='unknown')
    p=importer.preview(db,1,'orders','x.csv',csv_bytes(text),'gross');assert not p.get('duplicate')
    conflict=importer.commit(db,1,p['batch_id']);assert conflict['conflicts']==[2]
    importer.commit(db,1,p['batch_id'],True,changes_token=conflict['changes_token'])
    assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i1')).payment_basis=='gross'

def test_refund_disagreement_and_pending_never_claim_final_profit(db):
    db.add(CostVersion(store_id=1,sku='A',valid_from='2026-01-01 00:00:00',unit_cost_fen=300));db.commit()
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,success,3\ni2,o2,A,1,2026-10-02,pending,10,none,0\n')
    import_file(db,'refunds','refund_id,item_id,order_id,refund_status,refund_amount\nr1,i1,o1,success,4\n')
    r=calculate(db,1,'2026-10-01','2026-10-31')
    assert r['counts']['settled']==0 and r['counts']['pending']==1
    item=next(x for x in r['items'] if x['item_id']=='i1');assert '退款流水与订单快照不一致' in item['reasons']

def test_duplicate_and_unsafe_excel_identifier_rejected(db):
    text=HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\ni1,o1,A,1,2026-10-01,completed,10,none,0\n'
    p=importer.preview(db,1,'orders','a.csv',csv_bytes(text),'gross');assert p['batch_id'] is None and '重复' in p['errors'][0]['message']
    with pytest.raises(importer.ImportProblem):importer.identifier(1234567890123456789,'订单号')
    wb=Workbook();s=wb.active;s.append(['item_id']);s.append(['=1+1']);buf=io.BytesIO();wb.save(buf)
    with pytest.raises(importer.ImportProblem,match='公式'):importer.read_table('a.xlsx',buf.getvalue())

def test_qianniu_sold_order_export_maps_native_headers(db):
    wb=Workbook();sheet=wb.active
    sheet.append(['订单编号','买家应付货款','订单状态','订单付款时间','商品标题','宝贝总数量','订单关闭原因','退款金额','商品属性SKU','商家编码'])
    sheet.append(['Q-001','19.90','交易成功','2026-11-19 12:00:00','Gemini Pro 成品号',1,'订单未关闭','0.00','颜色分类:6个月',''])
    sheet.append(['Q-002','29.90','交易关闭','2026-11-18 12:00:00','Gemini Pro 家庭组',1,'退款','29.90','颜色分类:1个月',''])
    buf=io.BytesIO();wb.save(buf)
    preview=importer.preview(db,1,'orders','ExportOrderList27431083391.xlsx',buf.getvalue(),'gross')
    assert not preview['errors'],preview['errors']
    assert preview['mapping']['quantity']=='宝贝总数量'
    assert preview['mapping']['refund_status']=='订单关闭原因'
    assert preview['preview'][0]['item_external_id']=='Q-001'
    assert preview['preview'][0]['sku']=='颜色分类:6个月'
    assert preview['preview'][1]['refund_status']=='success'
    assert preview['preview'][1]['payment_fen']==2990
    assert preview['preview'][0]['fulfillment_status']=='shipped'
    assert preview['preview'][1]['fulfillment_status']=='unshipped'
    result=importer.commit(db,1,preview['batch_id'])
    assert result['inserted']==2

def test_sensitive_columns_not_persisted_in_preview(db):
    text=HEADER.strip()+',买家姓名,手机\ni1,o1,A,1,2026-10-01,completed,10,none,0,PrivateName,13800138000\n'
    p=importer.preview(db,1,'orders','a.csv',csv_bytes(text),'gross');row=db.scalar(select(ImportRow).where(ImportRow.batch_id==p['batch_id']))
    assert 'PrivateName' not in json.dumps(row.data,ensure_ascii=False)
    assert '13800138000' not in json.dumps(row.data,ensure_ascii=False)

def test_sqlite_backup_restore_same_report(db,tmp_path):
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\n')
    source=db.get_bind().url.database;db.commit()
    dest=tmp_path/'backup.db'
    with sqlite3.connect(source) as original,sqlite3.connect(dest) as backup:original.backup(backup)
    restored=make_engine('sqlite:///'+dest.as_posix())
    with Session(restored) as other:
        a=calculate(db,1,'2026-10-01','2026-10-31');b=calculate(other,1,'2026-10-01','2026-10-31')
        assert a['counts']==b['counts'] and a['known']==b['known']
    restored.dispose()

def test_multi_item_parent_requires_explicit_amount_confirmation(db):
    text=HEADER+'i1,parent,A,1,2026-10-01,completed,10,none,0\ni2,parent,B,1,2026-10-01,completed,10,none,0\n'
    p=importer.preview(db,1,'orders','multi.csv',csv_bytes(text),'gross')
    assert p['multi_parent_count']==1
    result=importer.commit(db,1,p['batch_id'])
    assert result['requires_item_amounts_confirmation']
    assert not list(db.scalars(select(OrderItem)))
    result=importer.commit(db,1,p['batch_id'],confirm_item_amounts=True)
    assert result['inserted']==2
    assert db.get(ImportBatch,p['batch_id']).item_amounts_confirmed is True

def test_embedded_personal_fields_are_redacted(db):
    text='item_id,order_id,sku,product_id,property,title,quantity,paid_at,status,payment,refund_status,refund_amount\ni1,o1,,123,颜色 红,演示商品 联系13800138000 密码:secret,1,2026-10-01,completed,10,none,0\n'
    p=importer.preview(db,1,'orders','a.csv',csv_bytes(text),'gross')
    row=db.scalar(select(ImportRow).where(ImportRow.batch_id==p['batch_id']))
    assert '13800138000' not in row.data['product_title'] and 'secret' not in row.data['product_title']

def test_missing_payment_time_is_shown_separately(db):
    import_file(db,'orders',HEADER+'i1,o1,A,1,,unpaid,,none,0\n')
    r=calculate(db,1,'2026-10-01','2026-10-31')
    assert r['counts']['paid_orders']==0 and r['counts']['missing_paid_time_all_dates']==1

def test_empty_export_workbook_reports_actionable_error(monkeypatch):
    import zipfile
    from types import SimpleNamespace
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as archive:archive.writestr('empty.txt','')
    monkeypatch.setattr(importer,'load_workbook',lambda *a,**k:SimpleNamespace(active=None))
    with pytest.raises(importer.ImportProblem,match='没有工作表'):importer.read_table('empty.xlsx',buf.getvalue())

def test_source_snapshot_is_separate_from_verified_profit(db):
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,success,3\n',basis='unknown')
    r=calculate(db,1,'2026-10-01','2026-10-31')
    assert r['source_snapshot']['payment_fen']==1000
    assert r['source_snapshot']['successful_refund_fen']==300
    assert r['counts']['settled']==0

def test_parent_and_child_order_counts_are_distinct(db):
    text=HEADER+'i1,parent,A,1,2026-10-01,completed,10,none,0\ni2,parent,B,1,2026-10-01,completed,10,none,0\n'
    p=importer.preview(db,1,'orders','multi.csv',csv_bytes(text),'gross')
    importer.commit(db,1,p['batch_id'],confirm_item_amounts=True)
    r=calculate(db,1,'2026-10-01','2026-10-31')
    assert r['counts']['paid_orders']==1 and r['counts']['paid_items']==2 and r['counts']['needs_review']==2

def test_closed_refund_application_needs_history_review(db):
    db.add(CostVersion(store_id=1,sku='A',valid_from='2026-01-01 00:00:00',unit_cost_fen=300));db.commit()
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,退款关闭,0\ni2,o2,A,1,2026-10-02,closed,10,none,0\n')
    r=calculate(db,1,'2026-10-01','2026-10-31')
    assert r['counts']['settled']==0 and r['counts']['needs_review']==2
    assert any('历史退款' in x['reasons'][0] for x in r['items'])

def test_same_file_reprocessed_only_after_parser_version_changes(db,monkeypatch):
    text=HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\n'
    monkeypatch.setattr(importer,'PARSER_VERSION','1')
    _,first=import_file(db,'orders',text)
    assert importer.preview(db,1,'orders','x.csv',csv_bytes(text),'gross')['duplicate']
    monkeypatch.setattr(importer,'PARSER_VERSION','2')
    second=importer.preview(db,1,'orders','x.csv',csv_bytes(text),'gross')
    assert not second.get('duplicate') and second['batch_id']!=first['batch_id']
    result=importer.commit(db,1,second['batch_id'])
    assert result['inserted']==0 and result['updated']==0

def test_same_file_with_corrected_mapping_is_not_discarded(db):
    text=HEADER.strip()+',alternative_payment\ni1,o1,A,1,2026-10-01,completed,10,none,0,7\n'
    data=csv_bytes(text)
    first=importer.preview(db,1,'orders','orders.csv',data,'gross')
    importer.commit(db,1,first['batch_id'])
    changed=importer.preview(db,1,'orders','orders.csv',data,'gross',{'payment':'alternative_payment'})
    assert not changed.get('duplicate') and changed['batch_id'] != first['batch_id']
    conflict=importer.commit(db,1,changed['batch_id']);assert conflict['conflicts']==[2]
    importer.commit(db,1,changed['batch_id'],accept_changes=True,changes_token=conflict['changes_token'])
    assert db.scalar(select(OrderItem).where(OrderItem.item_external_id=='i1')).payment_fen==700
    assert importer.preview(db,1,'orders','orders.csv',data,'gross',{'payment':'alternative_payment'})['duplicate']

def test_refund_parent_and_child_must_match_and_failure_is_atomic(db):
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,success,3\n')
    data=csv_bytes('refund_id,item_id,order_id,refund_status,refund_amount\nr1,i1,o1,success,3\nr2,i1,wrong,success,2\n')
    preview=importer.preview(db,1,'refunds','refunds.csv',data,None)
    with pytest.raises(importer.ImportProblem,match='主订单与子订单不匹配'):
        importer.commit(db,1,preview['batch_id'])
    db.rollback()
    assert list(db.scalars(select(Refund)))==[]
    assert db.get(ImportBatch,preview['batch_id']).status=='preview'

    missing=csv_bytes('refund_id,item_id,order_id,refund_status,refund_amount\nr3,missing,o1,success,2\n')
    preview=importer.preview(db,1,'refunds','missing.csv',missing,None)
    with pytest.raises(importer.ImportProblem,match='主订单与子订单不匹配'):
        importer.commit(db,1,preview['batch_id'])
    db.rollback()

def test_concurrent_import_checks_current_data_after_other_writer_commits(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\n')
    data=csv_bytes(HEADER+'i1,o1,A,1,2026-10-01,completed,8,none,0\n')
    batch=importer.preview(db,1,'orders','changed.csv',data,'gross')['batch_id']
    engine=db.get_bind()
    started=Event()
    with engine.connect() as writer:
        writer.exec_driver_sql('BEGIN IMMEDIATE')
        writer.exec_driver_sql("UPDATE order_items SET payment_fen=800 WHERE item_external_id='i1'")
        def submit():
            with Session(engine) as other:
                started.set()
                return importer.commit(other,1,batch)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(submit)
            assert started.wait(2)
            assert not future.done()
            writer.commit()
            result=future.result(timeout=5)
    assert result['updated']==0 and result['inserted']==0

def test_profit_provenance_includes_cost_refund_and_adjustment_sources(db):
    _,cost=import_file(db,'costs','sku,valid_from,unit_cost,supplier\nA,2026-01-01,3,source\n')
    _,order=import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,success,2\n')
    _,refund=import_file(db,'refunds','refund_id,item_id,order_id,refund_status,refund_amount\nr1,i1,o1,success,2\n')
    adjustment=Adjustment(store_id=1,item_external_id='i1',kind='expense',amount_fen=50,note='replacement')
    db.add(adjustment);db.commit()
    verify_synthetic_refund(db,'i1',200)
    result=calculate(db,1,'2026-10-01','2026-10-31')
    item=result['items'][0]
    assert result['known']['profit_fen']==450
    assert set(result['batch_ids'])=={cost['batch_id'],order['batch_id'],refund['batch_id']}
    assert item['cost_version_id'] is not None and item['cost_batch_id']==cost['batch_id']
    assert item['refund_batch_ids']==[refund['batch_id']] and item['adjustment_ids']==[adjustment.id]

def test_manual_order_cost_replaces_base_but_keeps_other_adjustments(db):
    _,cost=import_file(db,'costs','sku,valid_from,unit_cost,supplier\nA,2026-01-01,8,source\n')
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,none,0\n')
    db.add_all([Adjustment(store_id=1,item_external_id='i1',kind='cost_set',amount_fen=300,note='receipt'),
                Adjustment(store_id=1,item_external_id='i1',kind='cost_delta',amount_fen=50,note='difference'),
                Adjustment(store_id=1,item_external_id='i1',kind='supplier_refund',amount_fen=100,note='credit')]);db.commit()
    r=calculate(db,1,'2026-10-01','2026-10-31')
    assert r['items'][0]['cost_fen']==250 and r['known']['profit_fen']==750
    assert cost['batch_id'] not in r['batch_ids'] and r['items'][0]['cost_override_id'] is not None

def test_cashflow_is_separate_from_profit_and_reimport_is_idempotent(db):
    import_file(db,'orders',HEADER+'i1,o1,A,1,2026-10-01,completed,10,success,3\n',basis='unknown')
    data=('bill_type,business_flow,order_id,item_id,refund_id,settled_at,bill_income,bill_refund\n'
          'goods_income,f1,o1,i1,,2026-10-10,10,0\n'
          'goods_income,f1,o1,i1,r1,2026-11-03,0,3\n')
    _,first=import_file(db,'cashflows',data)
    assert first['inserted']==2
    october=summarize(db,1,'2026-10-01','2026-10-31')
    november=summarize(db,1,'2026-11-01','2026-11-30')
    assert (october['income_fen'],october['refund_fen'],october['net_fen'])==(1000,0,1000)
    assert (november['income_fen'],november['refund_fen'],november['net_fen'])==(0,300,-300)
    assert october['items'][0]['matched_order'] and calculate(db,1,'2026-10-01','2026-10-31')['counts']['settled']==0
    duplicate=importer.preview(db,1,'cashflows','same.csv',csv_bytes(data),None)
    assert duplicate['duplicate'] and db.scalar(select(func.count(Cashflow.id)))==2

def test_cashflow_overlaps_are_deduplicated_and_bad_rows_rejected(db):
    header='bill_type,business_flow,order_id,item_id,refund_id,settled_at,bill_income,bill_refund\n'
    a='goods_income,f1,o1,i1,,2026-10-10,10,0\n'
    b='goods_income,f2,o2,i2,,2026-10-11,20,0\n'
    import_file(db,'cashflows',header+a)
    _,second=import_file(db,'cashflows',header+a+b)
    assert second['inserted']==1 and summarize(db,1,'2026-10-01','2026-10-31')['income_fen']==3000
    invalid=importer.preview(db,1,'cashflows','bad.csv',csv_bytes(header+'goods_income,f3,o3,i3,,2026-10-12,1,2\n'),None)
    assert invalid['batch_id'] is None and invalid['errors']
