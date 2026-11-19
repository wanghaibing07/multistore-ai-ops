from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Adjustment, Cashflow, CostVersion, ImportBatch, ImportRow, OrderItem, Refund

MAX_FILE = 8 * 1024 * 1024
MAX_EXPANDED = 80 * 1024 * 1024
MAX_ROWS = 20000
PARSER_VERSION = "3"

ALIASES = {
    "item_id": ["子订单编号", "子订单号", "子订单ID", "item_id", "item_external_id"],
    "order_id": ["主订单编号", "订单编号", "订单号", "order_id", "order_external_id"],
    "sku": ["SKU", "sku", "商家编码", "规格编码"],
    "product_id": ["商品ID", "商品id", "product_id"],
    "property": ["商品属性", "商品sku信息", "SKU信息", "商品属性SKU", "property"],
    "title": ["商品标题", "宝贝标题", "title"],
    "quantity": ["购买数量", "数量", "宝贝总数量", "商品数量", "quantity"],
    "paid_at": ["订单付款时间", "付款时间", "paid_at"],
    "status": ["订单状态", "交易状态", "status"],
    "payment": ["买家应付货款", "买家实付金额", "买家实际支付金额", "实付金额", "总金额", "payment"],
    "refund_status": ["退款状态", "售后状态", "退款/售后状态", "订单关闭原因", "refund_status"],
    "refund_amount": ["退款金额", "退款总额", "退给买家金额", "refund_amount"],
    "refund_id": ["退款编号", "售后编号", "退款单号", "refund_id"],
    "completed_at": ["退款完结时间", "退款完成时间", "completed_at"],
    "valid_from": ["生效时间", "生效日期", "valid_from"],
    "unit_cost": ["单位成本", "采购单价", "unit_cost"],
    "supplier": ["供应商", "供货渠道", "supplier"],
    "bill_type": ["账单大类", "bill_type"],
    "business_flow": ["业务流水号", "business_flow"],
    "settled_at": ["打款时间", "settled_at"],
    "bill_income": ["订单实际金额（元）", "bill_income"],
    "bill_refund": ["退款金额（元）", "bill_refund"],
}
REQUIRED = {
    "orders": {"item_id", "order_id", "quantity", "paid_at", "status", "payment", "refund_status"},
    "refunds": {"refund_id", "order_id", "refund_status", "refund_amount"},
    "costs": {"sku", "valid_from", "unit_cost"},
    "cashflows": {"bill_type","business_flow","order_id","item_id","settled_at","bill_income","bill_refund"},
}

class ImportProblem(ValueError):
    pass

def serialize_sqlite_writes(db: Session) -> None:
    """Acquire SQLite's writer lock before inspecting data for an import."""
    if db.get_bind().dialect.name != "sqlite":
        return
    connection = db.connection()
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

def money(value: Any, *, optional=False) -> int | None:
    if value is None or str(value).strip() in ("", "-", "--", "无", "无退款申请", "None"):
        if optional:
            return None
        raise ImportProblem("金额为空")
    raw = str(value).strip().replace("¥", "").replace("￥", "").replace(",", "")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise ImportProblem("金额格式不正确") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("100000000") or amount * 100 != (amount * 100).to_integral_value():
        raise ImportProblem("金额须为非负数，最多两位小数")
    return int(amount * 100)

def timestamp(value: Any, *, optional=False) -> str | None:
    if value is None or str(value).strip() in ("", "-", "--", "无", "None"):
        if optional: return None
        raise ImportProblem("日期为空")
    if isinstance(value, datetime): return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date): return value.strftime("%Y-%m-%d 00:00:00")
    text = str(value).strip().replace("/", "-")
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ImportProblem("日期格式不正确") from None

def identifier(value: Any, label: str, *, optional=False) -> str:
    if value is None or str(value).strip() in ("", "-", "--", "None"):
        if optional: return ""
        raise ImportProblem(label + "为空")
    if isinstance(value, (int, float)) and abs(value) > 2**53 - 1:
        raise ImportProblem(label + "为超长数值，电子表格可能已丢失精度，请导出为文本")
    text = str(value).strip()
    if len(text) > 128 or not re.fullmatch(r"[\w.\-]+", text, re.UNICODE):
        raise ImportProblem(label + "格式不正确")
    return text

def read_table(name: str, data: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    if len(data) > MAX_FILE: raise ImportProblem("文件超过 8 MB")
    if name.lower().endswith(".csv"):
        for enc in ("utf-8-sig", "gb18030"):
            try: text = data.decode(enc); break
            except UnicodeDecodeError: continue
        else: raise ImportProblem("CSV 编码无法识别，请使用 UTF-8 或 GB18030")
        source = csv.reader(io.StringIO(text, newline=""))
        try: values = [r for r in source]
        except csv.Error: raise ImportProblem("CSV 格式无法解析") from None
    elif name.lower().endswith(".xlsx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(x.file_size for x in z.infolist()) > MAX_EXPANDED: raise ImportProblem("表格解压后过大")
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            sheet = workbook.active
            if sheet is None: raise ImportProblem("表格没有工作表；请检查导出结果是否为空")
            values = []
            for row in sheet.iter_rows():
                if len(values) > MAX_ROWS + 1: raise ImportProblem("单次最多导入 20000 行")
                if any(c.data_type == "f" for c in row): raise ImportProblem("文件包含公式，请先保存为纯数值表格")
                values.append([c.value for c in row])
            workbook.close()
        except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
            if isinstance(exc, ImportProblem): raise
            raise ImportProblem("XLSX 文件无法解析") from None
    else: raise ImportProblem("只支持 CSV 或 XLSX 文件")
    if not values: raise ImportProblem("文件为空")
    if len(values) > MAX_ROWS + 1: raise ImportProblem("单次最多导入 20000 行")
    headers = [str(v or "").strip() for v in values[0]]
    if not all(headers) or len(headers) != len(set(headers)): raise ImportProblem("表头为空或重复，请先整理表格")
    rows = [dict(zip(headers, r)) for r in values[1:] if any(v is not None and str(v).strip() for v in r)]
    if not rows: raise ImportProblem("文件没有数据行")
    return headers, rows

def map_headers(headers: list[str], kind: str, supplied: dict[str, str] | None = None) -> dict[str, str]:
    if kind not in REQUIRED: raise ImportProblem("导入类型无效")
    allowed = {"orders": {"item_id","order_id","sku","product_id","property","title","quantity","paid_at","status","payment","refund_status","refund_amount"},
               "refunds": {"refund_id","item_id","order_id","refund_status","refund_amount","completed_at"},
               "costs": {"sku","valid_from","unit_cost","supplier"},
               "cashflows": {"bill_type","business_flow","order_id","item_id","refund_id","settled_at","bill_income","bill_refund"}}[kind]
    supplied = supplied or {}
    if set(supplied) - allowed or any(v not in headers for v in supplied.values()): raise ImportProblem("字段映射无效")
    result = dict(supplied)
    for key in allowed - result.keys():
        result[key] = next((h for h in ALIASES[key] if h in headers), "")
    missing = sorted(k for k in REQUIRED[kind] if not result.get(k))
    # Qianniu's sold-order export is one row per main order and does not
    # provide a separate child-order column. The order number is a stable
    # fallback for this export and keeps repeated imports idempotent.
    if kind == "orders" and "item_id" in missing and result.get("order_id"):
        missing.remove("item_id")
    if kind == "orders" and not (result.get("sku") or result.get("product_id")): missing.append("sku 或 product_id")
    if missing: raise ImportProblem("缺少必要字段：" + ", ".join(missing))
    selected = [x for x in result.values() if x]
    if len(selected) != len(set(selected)): raise ImportProblem("同一表格列不能映射到多个字段")
    return {k: v for k,v in result.items() if v}

def scrub(value: Any) -> str:
    text=str(value or "")
    text=re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)","[手机号已隐藏]",text)
    text=re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}","[邮箱已隐藏]",text)
    text=re.sub(r"(?i)(密码|口令|密钥|password|token)\s*[:=：]\s*\S+",r"\1：[已隐藏]",text)
    return text

def get(row, mapping, key): return row.get(mapping.get(key, ""))

def order_state(value: Any) -> str:
    text = str(value or "").strip()
    lookup = {"交易成功":"completed", "已完成":"completed", "completed":"completed", "交易关闭":"closed", "已关闭":"closed", "closed":"closed", "等待买家确认收货":"pending", "买家已付款":"pending", "等待卖家发货":"pending", "已发货":"pending", "pending":"pending", "待付款":"unpaid", "等待买家付款":"unpaid", "unpaid":"unpaid"}
    if text in lookup:
        return lookup[text]
    if text.startswith(("卖家已发货", "买家已付款", "等待卖家发货", "等待买家确认")):
        return "pending"
    if text.startswith(("交易成功", "交易完成", "已完成")):
        return "completed"
    if text.startswith(("交易关闭", "已关闭")):
        return "closed"
    return "unknown"

def refund_state(value: Any) -> str:
    text = str(value or "").strip()
    lookup = {"没有申请退款":"none", "无退款":"none", "无退款申请":"none", "订单未关闭":"none", "退款关闭":"closed", "none":"none", "退款成功":"success", "成功":"success", "退款":"success", "success":"success", "退款中":"pending", "待处理":"pending", "退款待处理":"pending", "pending":"pending", "退款关闭/撤销":"closed", "closed":"closed"}
    if text in lookup:
        return lookup[text]
    if "退款" in text and "关闭" not in text:
        return "success"
    return "unknown"

def clean_row(raw: dict, m: dict, kind: str) -> dict:
    if kind == "orders":
        order_id = identifier(get(raw,m,"order_id"), "主订单号")
        paid = timestamp(get(raw,m,"paid_at"), optional=True)
        unit = str(get(raw,m,"sku") or "").strip()
        product = identifier(get(raw,m,"product_id"), "商品ID", optional=True)
        prop = scrub(get(raw,m,"property")).strip()
        title = scrub(get(raw,m,"title")).strip()
        if len(prop) > 300 or len(unit) > 500: raise ImportProblem("规格过长")
        sku = unit or ((product + " | " + prop) if product and prop else product or prop) or title
        if not sku: raise ImportProblem("商品编码、商品 ID 和商品标题均为空")
        try:
            qty = int(str(get(raw,m,"quantity")).strip())
            if qty < 1 or qty > 100000: raise ValueError
        except (ValueError, TypeError): raise ImportProblem("购买数量必须为正整数") from None
        amount = money(get(raw,m,"refund_amount"), optional=True)
        state = refund_state(get(raw,m,"refund_status"))
        if state == "none" and amount is None: amount = 0
        payment = money(get(raw,m,"payment"), optional=True)
        if paid and payment is None: raise ImportProblem("已付款订单缺少实付金额")
        item_id = identifier(get(raw,m,"item_id"), "子订单号", optional=True) or order_id
        return {"item_external_id":item_id, "order_external_id":order_id,
                "sku":sku, "product_title":title[:300], "quantity":qty, "paid_at":paid,
                "status":order_state(get(raw,m,"status")), "payment_fen":payment, "refund_status":state, "refund_snapshot_fen":amount}
    if kind == "refunds":
        return {"external_id":identifier(get(raw,m,"refund_id"),"退款编号"),
                "item_external_id":identifier(get(raw,m,"item_id"),"子订单号",optional=True),
                "order_external_id":identifier(get(raw,m,"order_id"),"订单号"), "status":refund_state(get(raw,m,"refund_status")),
                "amount_fen":money(get(raw,m,"refund_amount")), "completed_at":timestamp(get(raw,m,"completed_at"),optional=True)}
    if kind == "cashflows":
        if str(get(raw,m,"bill_type") or "").strip() not in ("货款收入","goods_income"):raise ImportProblem("仅支持货款收入账单")
        income=money(get(raw,m,"bill_income"),optional=True) or 0
        refund=money(get(raw,m,"bill_refund"),optional=True) or 0
        if bool(income)==bool(refund):raise ImportProblem("每条货款流水必须只有收款或退款之一且金额大于零")
        refund_id=identifier(get(raw,m,"refund_id"),"退款单号",optional=True)
        if refund and not refund_id:raise ImportProblem("退款流水缺少退款单号")
        return {"business_flow":identifier(get(raw,m,"business_flow"),"业务流水号"),
                "order_external_id":identifier(get(raw,m,"order_id"),"主订单号"),
                "item_external_id":identifier(get(raw,m,"item_id"),"子订单号"),"refund_external_id":refund_id,
                "kind":"refund" if refund else "income","amount_fen":refund or income,"settled_at":timestamp(get(raw,m,"settled_at"))}
    sku = str(get(raw,m,"sku") or "").strip()
    if not sku or len(sku) > 500: raise ImportProblem("SKU 为空或过长")
    return {"sku":sku, "valid_from":timestamp(get(raw,m,"valid_from")), "unit_cost_fen":money(get(raw,m,"unit_cost")), "supplier":str(get(raw,m,"supplier") or "")[:120]}

def preview(db: Session, store_id: int, kind: str, name: str, data: bytes, basis: str | None, mapping: dict | None = None) -> dict:
    if kind == "orders" and basis not in ("gross", "net", "unknown"): raise ImportProblem("请选择退款前实付、退款后净额或暂不确定")
    if kind != "orders": basis = None
    headers, raw_rows = read_table(name, data)
    mapped = map_headers(headers, kind, mapping)
    cleaned, errors = [], []
    for line, row in enumerate(raw_rows, 2):
        try: cleaned.append((line, clean_row(row, mapped, kind), row))
        except ImportProblem as exc:
            if len(errors) < 30: errors.append({"row":line, "message":str(exc)})
    seen = set()
    for line, row, _raw in cleaned:
        key = (row["sku"], row["valid_from"]) if kind == "costs" else row["item_external_id"] if kind == "orders" else (row["business_flow"],row["order_external_id"],row["item_external_id"],row["refund_external_id"],row["kind"]) if kind == "cashflows" else row["external_id"]
        if key in seen and len(errors) < 30: errors.append({"row":line, "message":"文件内存在重复业务编号或成本版本"})
        seen.add(key)
    if errors: return {"headers":headers,"mapping":mapped,"row_count":len(raw_rows),"errors":errors,"batch_id":None}
    digest = hashlib.sha256(data).hexdigest()
    serialize_sqlite_writes(db)
    # A file can be remapped: byte identity alone does not imply the same data.
    # Compare cleaned rows as well, without persisting unmapped personal columns.
    candidates = list(db.scalars(select(ImportBatch).where(ImportBatch.store_id==store_id,ImportBatch.kind==kind,ImportBatch.file_hash==digest,ImportBatch.basis==basis,ImportBatch.parser_version==PARSER_VERSION).order_by(ImportBatch.id.desc())))
    draft = None
    for candidate in candidates:
        previous = [(r.row_number, r.data) for r in db.scalars(select(ImportRow).where(ImportRow.batch_id==candidate.id).order_by(ImportRow.row_number))]
        normalized = [(line, row) for line, row, _raw in cleaned]
        if previous != normalized: continue
        if candidate.status == "committed":
            return {"headers":headers,"mapping":mapped,"row_count":len(raw_rows),"duplicate":True,"batch_id":candidate.id,"errors":[]}
        if candidate.status == "preview" and draft is None: draft = candidate
    if draft:
        for r in db.scalars(select(ImportRow).where(ImportRow.batch_id==draft.id)): db.delete(r)
        db.flush(); draft.basis=basis
    else:
        draft=ImportBatch(store_id=store_id,kind=kind,file_hash=digest,basis=basis,parser_version=PARSER_VERSION,row_count=len(cleaned))
        db.add(draft); db.flush()
    for line, row, raw in cleaned:
        db.add(ImportRow(batch_id=draft.id,row_number=line,data=row,raw_data=raw))
    db.commit()
    multi_count=multi_parent_count(db,store_id,[x[1] for x in cleaned]) if kind=="orders" else 0
    time_field={"orders":"paid_at","refunds":"completed_at","costs":"valid_from","cashflows":"settled_at"}[kind]
    times=sorted(row[time_field] for _,row,_raw in cleaned if row.get(time_field))
    differences=preview_differences(db,store_id,kind,cleaned,basis)
    return {"headers":headers,"mapping":mapped,"row_count":len(cleaned),"batch_id":draft.id,"errors":[],"multi_parent_count":multi_count,"preview": [x[1] for x in cleaned[:5]],
            "time_field":time_field,"time_start":times[0] if times else None,"time_end":times[-1] if times else None,
            "time_missing":len(cleaned)-len(times),"differences":differences}

def changed_fields(old: dict, new: dict) -> dict:
    return {key:{"old":old.get(key),"new":value} for key,value in new.items() if old.get(key)!=value}

def preview_differences(db: Session, store_id: int, kind: str, rows: list, basis: str | None) -> list:
    """Show changes to existing business records using only normalized fields."""
    differences=[]
    for line, raw, _source in rows:
        d=dict(raw)
        if kind=='orders':
            existing=db.scalar(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.item_external_id==d['item_external_id']))
            if existing:
                old={k:getattr(existing,k) for k in d};old['payment_basis']=existing.payment_basis;d['payment_basis']=basis
            else: continue
        elif kind=='refunds':
            order_id=d.pop('order_external_id')
            if not d['item_external_id']:
                matches=list(db.scalars(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.order_external_id==order_id)))
                if len(matches)!=1: continue
                d['item_external_id']=matches[0].item_external_id
            existing=db.scalar(select(Refund).where(Refund.store_id==store_id,Refund.external_id==d['external_id']))
            if not existing:continue
            old={k:getattr(existing,k) for k in d}
        elif kind=='cashflows':
            existing=db.scalar(select(Cashflow).where(Cashflow.store_id==store_id,Cashflow.business_flow==d['business_flow'],Cashflow.order_external_id==d['order_external_id'],Cashflow.item_external_id==d['item_external_id'],Cashflow.refund_external_id==d['refund_external_id'],Cashflow.kind==d['kind']))
            if not existing:continue
            old={k:getattr(existing,k) for k in d}
        else:
            existing=db.scalar(select(CostVersion).where(CostVersion.store_id==store_id,CostVersion.sku==d['sku'],CostVersion.valid_from==d['valid_from']))
            if not existing:continue
            old={k:getattr(existing,k) for k in d}
        fields=changed_fields(old,d)
        if fields:differences.append({'row':line,'fields':fields})
    return differences

def changes_fingerprint(differences: list, store_id: int, batch_id: int) -> str:
    data={'store_id':store_id,'batch_id':batch_id,'differences':differences}
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()

def lookup_cost(db: Session, store_id: int, sku: str, paid_at: str | None):
    if not paid_at: return None
    return db.scalar(select(CostVersion).where(CostVersion.store_id==store_id, CostVersion.sku==sku, CostVersion.valid_from<=paid_at).order_by(CostVersion.valid_from.desc()).limit(1))

def multi_parent_count(db: Session, store_id: int, rows: list[dict]) -> int:
    parents={r["order_external_id"] for r in rows}
    children={parent:set() for parent in parents}
    for row in rows:children[row["order_external_id"]].add(row["item_external_id"])
    for order in db.scalars(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.order_external_id.in_(parents))):
        children[order.order_external_id].add(order.item_external_id)
    return sum(len(items)>1 for items in children.values())

def commit(db: Session, store_id: int, batch_id: int, accept_changes: bool = False, confirm_item_amounts: bool = False, changes_token: str | None = None) -> dict:
    serialize_sqlite_writes(db)
    batch=db.scalar(select(ImportBatch).where(ImportBatch.id==batch_id,ImportBatch.store_id==store_id))
    if not batch: raise ImportProblem("导入批次不存在")
    if batch.status == "committed": return {"duplicate":True,"batch_id":batch.id}
    rows=list(db.scalars(select(ImportRow).where(ImportRow.batch_id==batch.id).order_by(ImportRow.row_number)))
    multi=multi_parent_count(db,store_id,[r.data for r in rows]) if batch.kind=="orders" else 0
    if multi and not confirm_item_amounts:
        return {"requires_item_amounts_confirmation":True,"multi_parent_count":multi}
    differences=preview_differences(db,store_id,batch.kind,[(r.row_number,r.data,r.raw_data) for r in rows],batch.basis)
    # Cost versions are immutable: a changed existing version must be rejected,
    # rather than presented as an overwrite the user can confirm.
    if batch.kind!='costs' and differences:
        token=changes_fingerprint(differences,store_id,batch_id)
        if not accept_changes or changes_token!=token:
            db.rollback()
            return {'conflicts':[d['row'] for d in differences], 'differences':differences, 'changes_token':token,
                    'message':'请核对变更前后的数据；如果数据再次变化，需要重新确认'}
    batch.item_amounts_confirmed=bool(multi and confirm_item_amounts)
    changes=[]; inserted=0
    # All writes occur within one transaction. A conflict rolls the whole batch back.
    for row in rows:
        d=dict(row.data)
        if batch.kind=="orders":
            existing=db.scalar(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.item_external_id==d["item_external_id"]))
            if existing:
                compare={k:getattr(existing,k) for k in d}
                if compare != d or existing.payment_basis != batch.basis:
                    changes.append(row.row_number)
                    if not accept_changes: continue
                    old_sku, old_paid, old_qty = existing.sku, existing.paid_at, existing.quantity
                    for k,v in d.items(): setattr(existing,k,v)
                    existing.payment_basis=batch.basis; existing.batch_id=batch.id
                    if (old_sku, old_paid, old_qty)!=(existing.sku, existing.paid_at, existing.quantity):
                        prior_adjustment=db.scalar(select(Adjustment.id).where(Adjustment.store_id==store_id,Adjustment.item_external_id==existing.item_external_id).limit(1))
                        needs_review=existing.cost_fen is not None or prior_adjustment is not None
                        previous_cost=existing.cost_fen
                        previous_version=existing.cost_version_id
                        existing.cost_fen=None; existing.cost_version_id=None
                        if needs_review:
                            note=f"订单规格、付款时间或数量变化；原成本版本 {previous_version or '无'}，请核对原成本及全部调整后重新设置逐单成本"
                            db.add(Adjustment(store_id=store_id,item_external_id=existing.item_external_id,kind='cost_invalidate',amount_fen=previous_cost or 0,note=note))
                        else:
                            cost=lookup_cost(db,store_id,existing.sku,existing.paid_at)
                            if cost: existing.cost_fen=cost.unit_cost_fen*existing.quantity; existing.cost_version_id=cost.id
            else:
                cost=lookup_cost(db,store_id,d["sku"],d["paid_at"])
                db.add(OrderItem(store_id=store_id,**d,payment_basis=batch.basis,cost_fen=cost.unit_cost_fen*d["quantity"] if cost else None,cost_version_id=cost.id if cost else None,batch_id=batch.id));inserted+=1
        elif batch.kind=="costs":
            existing=db.scalar(select(CostVersion).where(CostVersion.store_id==store_id,CostVersion.sku==d["sku"],CostVersion.valid_from==d["valid_from"]))
            if existing:
                if existing.unit_cost_fen!=d["unit_cost_fen"] or existing.supplier!=d["supplier"]: raise ImportProblem("相同 SKU 和生效日期已有不同成本，请使用新的生效日期或逐单调整")
            else: db.add(CostVersion(store_id=store_id,**d,batch_id=batch.id));inserted+=1
        elif batch.kind=="cashflows":
            existing=db.scalar(select(Cashflow).where(Cashflow.store_id==store_id,Cashflow.business_flow==d["business_flow"],Cashflow.order_external_id==d["order_external_id"],Cashflow.item_external_id==d["item_external_id"],Cashflow.refund_external_id==d["refund_external_id"],Cashflow.kind==d["kind"]))
            if existing:
                if any(getattr(existing,k)!=v for k,v in d.items()):
                    changes.append(row.row_number)
                    if not accept_changes: continue
                    for k,v in d.items():setattr(existing,k,v)
                    existing.batch_id=batch.id
            else:db.add(Cashflow(store_id=store_id,**d,batch_id=batch.id));inserted+=1
        else:
            order_id=d.pop("order_external_id")
            if not d["item_external_id"]:
                matches=list(db.scalars(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.order_external_id==order_id)))
                if len(matches)!=1: raise ImportProblem(f"第 {row.row_number} 行的订单无法唯一匹配子订单")
                d["item_external_id"]=matches[0].item_external_id
            else:
                order=db.scalar(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.item_external_id==d["item_external_id"]))
                if not order or order.order_external_id!=order_id:
                    raise ImportProblem(f"第 {row.row_number} 行的主订单与子订单不匹配，请先导入对应订单并核对编号")
            existing=db.scalar(select(Refund).where(Refund.store_id==store_id,Refund.external_id==d["external_id"]))
            if existing:
                if any(getattr(existing,k)!=v for k,v in d.items()):
                    changes.append(row.row_number)
                    if not accept_changes: continue
                    for k,v in d.items(): setattr(existing,k,v)
                    existing.batch_id=batch.id
            else: db.add(Refund(store_id=store_id,**d,batch_id=batch.id));inserted+=1
    if changes and not accept_changes:
        db.rollback(); return {"conflicts":changes[:30],"message":"存在变化的数据，请核对后确认更新"}
    db.flush()
    if batch.kind == "costs":
        invalidated=set(db.scalars(select(Adjustment.item_external_id).where(Adjustment.store_id==store_id,Adjustment.kind=='cost_invalidate')))
        for order in db.scalars(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.cost_fen.is_(None))):
            if order.item_external_id in invalidated:continue
            cost=lookup_cost(db,store_id,order.sku,order.paid_at)
            if cost: order.cost_fen=cost.unit_cost_fen*order.quantity; order.cost_version_id=cost.id
    from .models import now_iso
    batch.status="committed";batch.committed_at=now_iso()
    db.commit()
    return {"batch_id":batch.id,"inserted":inserted,"updated":len(changes),"duplicate":False}
