from __future__ import annotations

import ipaddress
import json
import os
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from . import agent, card_reconciliation, card_query_ai, cashflow, importer, payment_basis_review, qianniu_chats, qianniu_orders, refund_review, upstream_sync
from .accounting import calculate
from .auth import COOKIE, current_session, login, quick_login, list_quick_users, require_store
from .db import get_db
from .models import Adjustment, CardMatch, CardPurchase, Cashflow, CostVersion, ImportBatch, LoginSession, Membership, OrderItem, Refund, RefundReview, PaymentBasisReview, Store, StoreSetting, User, now_iso

app=FastAPI(title="MultiStore AI Ops",docs_url=None,redoc_url=None,openapi_url=None)
STATIC=Path(__file__).parent/"static"
app.mount("/static",StaticFiles(directory=STATIC),name="static")

class LoginIn(BaseModel):
    username: str=Field(min_length=1,max_length=100)
    password: str=Field(min_length=1,max_length=200)
class QuickLoginIn(BaseModel):
    username: str | None=Field(default=None,max_length=100)
class CostIn(BaseModel):
    sku: str=Field(min_length=1,max_length=500)
    valid_from: str
    unit_cost: str
    supplier: str=Field(default="",max_length=120)
class AdjustmentIn(BaseModel):
    request_id: UUID
    item_id: str=Field(min_length=1,max_length=128)
    kind: str
    amount: str
    note: str=Field(default="",max_length=200)
class QueryIn(BaseModel):
    question: str=Field(min_length=1,max_length=500)
    start: str
    end: str
    sku: str | None=Field(default=None,max_length=500)
class CommitIn(BaseModel):
    accept_changes: bool=False
    confirm_item_amounts: bool=False
    changes_token: str | None=Field(default=None,pattern=r"^[0-9a-f]{64}$")
class RefundReviewIn(BaseModel):
    request_id: UUID
    item_id: str=Field(min_length=1,max_length=128)
    source_fingerprint: str=Field(pattern=r"^[0-9a-f]{64}$")
    refund_amount: str
    note: str=Field(min_length=1,max_length=200)

class PaymentBasisReviewIn(BaseModel):
    request_id: UUID
    item_id: str=Field(min_length=1,max_length=128)
    source_fingerprint: str=Field(pattern=r"^[0-9a-f]{64}$")
    basis: str
    note: str=Field(min_length=1,max_length=200)

class BulkBasisIn(BaseModel):
    note: str = Field(default="店主确认：订单导出的买家实付金额为退款前金额", min_length=1, max_length=200)

class LatestDownloadIn(BaseModel):
    kind: str = "orders"
    basis: str | None = "gross"

class QianniuAiImportIn(BaseModel):
    basis: str = "gross"

class CardAllocationIn(BaseModel):
    purchase_id: int
    allocated_quantity: int = Field(default=1, ge=1, le=100)
    purchase_quantity: int | None = Field(default=None, ge=1, le=100)

class CardMatchIn(BaseModel):
    request_id: UUID
    item_id: str = Field(min_length=1, max_length=128)
    purchase_id: int
    purchase_quantity: int | None = Field(default=None, ge=1, le=100)
    score: int = Field(ge=0, le=100)
    cost: str = ""
    note: str = Field(default="", max_length=160)
    allocations: list[CardAllocationIn] | None = None

class CardBatchMatchIn(BaseModel):
    items: list[CardMatchIn] = Field(min_length=1, max_length=300)

class DeleteCardPurchasesIn(BaseModel):
    purchase_ids: list[int] = Field(min_length=1, max_length=300)

class CardSettingsIn(BaseModel):
    contact_email: str | None = Field(default=None, max_length=200)
    usd_cny_rate: str | None = Field(default=None, max_length=32)
    vnd_cny_rate: str | None = Field(default=None, max_length=32)

class UpstreamSyncTgIn(BaseModel):
    supplier: str = Field(default="all", max_length=32)

class UpstreamSyncPlan2In(BaseModel):
    start_date: str = Field(default="", max_length=20)
    end_date: str = Field(default="", max_length=20)
    channel: str = Field(default="", max_length=32)
    auto_confirm_exact: bool = False

class UpstreamSync16688In(BaseModel):
    max_pages: int = Field(default=5, ge=1, le=50)
    page_size: int = Field(default=50, ge=1, le=100)
    start_time: int | None = None
    end_time: int | None = None

class AutoExactMatchIn(BaseModel):
    start: str = Field(default="", max_length=20)
    end: str = Field(default="", max_length=20)
    channel: str = Field(default="", max_length=32)

class CardTextImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    source_hint: str = Field(default="", max_length=40)
    filename: str = Field(default="card-paste.txt", max_length=120)

class DeleteOrdersIn(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=300)

class CardAiQueryIn(BaseModel):
    site: str = Field(default="all", pattern=r"^(all|16688|wzyp)$")
    contact_email: str | None = Field(default=None, max_length=200)
    captcha_code: str | None = Field(default=None, max_length=20)
    captcha_session_id: str | None = Field(default=None, max_length=64)

class ChatTextImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    filename: str = Field(default="chat-paste.txt", max_length=120)
    source: str = Field(default="paste", max_length=40)
    use_ai: bool = False

class ChatKnowledgeIn(BaseModel):
    id: int | None = None
    topic: str = Field(default="general", max_length=50)
    product_category: str = Field(default="all", max_length=40)
    title: str = Field(min_length=1, max_length=160)
    trigger_question: str = Field(default="", max_length=300)
    standard_reply: str = Field(min_length=1, max_length=5000)
    is_pinned: bool = False

class ChatResolveIn(BaseModel):
    resolved: bool | None = None

class ChatQueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    start: str = ""
    end: str = ""
    use_ai: bool = True

@app.get("/")
def index(): return FileResponse(STATIC/"index.html",headers={"Cache-Control":"no-store"})
@app.get("/health")
def health(): return {"status":"ok"}

_LOOPBACK_PROXY_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_LOOPBACK_CLIENT_HOSTS = _LOOPBACK_PROXY_HOSTS | frozenset({"testclient"})


def _is_loopback_host(host: str | None, *, allow_testclient: bool = False) -> bool:
    if not host:
        return False
    val = host.strip().strip('"').strip("'").strip().lower()
    if not val:
        return False
    if val.startswith("["):
        end = val.find("]")
        if end == -1:
            return False
        rest = val[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
        val = val[1:end]
    elif val.count(":") == 1:
        host_part, port_part = val.split(":", 1)
        if port_part.isdigit():
            val = host_part
    allowed_names = _LOOPBACK_CLIENT_HOSTS if allow_testclient else _LOOPBACK_PROXY_HOSTS
    if val in allowed_names:
        return True
    try:
        ip = ipaddress.ip_address(val)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None and ip.ipv4_mapped.is_loopback


def _is_loopback_request(request: Request) -> bool:
    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host, allow_testclient=True):
        return False
    for header_val in request.headers.getlist("x-forwarded-for"):
        text = header_val.strip()
        if not text:
            continue
        hops = [part.strip() for part in text.split(",")]
        if any(not hop or not _is_loopback_host(hop) for hop in hops):
            return False
    for header_val in request.headers.getlist("forwarded"):
        text = header_val.strip()
        if not text:
            continue
        elements = [elem.strip() for elem in text.split(",")]
        for elem in elements:
            if not elem:
                return False
            for_values = []
            for param in elem.split(";"):
                key, sep, val = param.partition("=")
                if sep and key.strip().lower() == "for":
                    for_values.append(val.strip())
            if not for_values or any(not _is_loopback_host(val) for val in for_values):
                return False
    for header_val in request.headers.getlist("x-real-ip"):
        text = header_val.strip()
        if text and not _is_loopback_host(text):
            return False
    return True


def _is_public_mode() -> bool:
    return os.environ.get("MULTISTORE_PUBLIC_MODE") == "1"


def _quick_login_allowed(request: Request) -> bool:
    if _is_public_mode():
        return False
    if os.environ.get("MULTISTORE_ALLOW_QUICK_LOGIN") == "1":
        return True
    return _is_loopback_request(request)


def _require_quick_login(request: Request, *, public_detail: str) -> None:
    if _is_public_mode():
        raise HTTPException(404, public_detail)
    if not _quick_login_allowed(request):
        raise HTTPException(403, "仅本机回环访问或显式开启 MULTISTORE_ALLOW_QUICK_LOGIN=1 时允许免密进入")


@app.get("/api/config")
def config(request: Request):
    public_mode = _is_public_mode()
    return {
        "public_mode": public_mode,
        "quick_login_enabled": _quick_login_allowed(request),
        "qianniu_url": (os.environ.get("MULTISTORE_QIANNIU_URL") or "https://myseller.taobao.com/home.htm/trade-platform/tp/sold").strip(),
        "card_site_url": os.environ.get("MULTISTORE_CARD_SITE_URL", "https://www.16688.com.cn/"),
        "card_sites": {
            "16688": {
                "name": "16688",
                "home_url": os.environ.get("MULTISTORE_CARD_16688_HOME_URL", "https://www.16688.com.cn/"),
                "order_url": os.environ.get("MULTISTORE_CARD_16688_URL", "https://www.16688.com.cn/order"),
            },
            "wzyp": {
                "name": "链动小铺",
                "home_url": os.environ.get("MULTISTORE_CARD_WZYP_HOME_URL", "https://wzyp.cn/"),
                "order_url": os.environ.get("MULTISTORE_CARD_WZYP_URL", "https://wzyp.cn/order"),
            },
        },
    }
@app.post("/api/login")
def api_login(body:LoginIn,request:Request,response:Response,db:Session=Depends(get_db)):
    return login(db,body.username,body.password,response,request.client.host if request.client else "unknown")
@app.post("/api/quick-login")
@app.post("/api/auth/quick-login")
def api_quick_login(request:Request,response:Response,body:QuickLoginIn|None=None,db:Session=Depends(get_db)):
    _require_quick_login(request, public_detail="远程模式已关闭免密进入，请使用账号密码登录")
    return quick_login(db,response,username=body.username if body else None)
@app.get("/api/auth/users")
def api_auth_users(request:Request,db:Session=Depends(get_db)):
    _require_quick_login(request, public_detail="远程模式不提供用户列表")
    return list_quick_users(db)
@app.get("/api/me")
def me(current:LoginSession=Depends(current_session),db:Session=Depends(get_db)):
    user=db.get(User,current.user_id)
    stores=list(db.execute(select(Store.id,Store.name).join(Membership,Membership.store_id==Store.id).where(Membership.user_id==user.id)))
    return {"username":user.username,"stores":[{"id":x.id,"name":x.name} for x in stores]}
@app.post("/api/logout")
def logout(response:Response,current:LoginSession=Depends(current_session),db:Session=Depends(get_db)):
    db.delete(current);db.commit();response.delete_cookie(COOKIE,path="/");return {"ok":True}

@app.get("/api/stores/{store_id}/report")
def report(store_id:int=Depends(require_store),start:str="",end:str="",sku:str|None=None,channel:str|None=None,db:Session=Depends(get_db)):
    today=date.today();start=start or (today-timedelta(days=29)).isoformat();end=end or today.isoformat()
    try:return calculate(db,store_id,start,end,sku,channel=channel)
    except ValueError as exc: raise HTTPException(400,str(exc)) from None

@app.delete("/api/stores/{store_id}/orders")
def delete_orders(body:DeleteOrdersIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    item_ids=list(dict.fromkeys(item_id.strip() for item_id in body.item_ids if item_id.strip()))
    if not item_ids:
        raise HTTPException(400,"请至少选择一笔订单")
    importer.serialize_sqlite_writes(db)
    orders=list(db.scalars(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.item_external_id.in_(item_ids))))
    if not orders:
        return {"deleted":0,"missing":len(item_ids),"item_ids":[]}
    existing_ids=[order.item_external_id for order in orders]
    def item_filter(column):
        return column.store_id==store_id, column.item_external_id.in_(existing_ids)
    db.execute(delete(CardMatch).where(*item_filter(CardMatch)))
    db.execute(delete(RefundReview).where(*item_filter(RefundReview)))
    db.execute(delete(PaymentBasisReview).where(*item_filter(PaymentBasisReview)))
    db.execute(delete(Adjustment).where(*item_filter(Adjustment)))
    db.execute(delete(Refund).where(*item_filter(Refund)))
    db.execute(delete(Cashflow).where(*item_filter(Cashflow)))
    db.execute(delete(OrderItem).where(*item_filter(OrderItem)))
    db.commit()
    return {"deleted":len(existing_ids),"missing":len(item_ids)-len(existing_ids),"item_ids":existing_ids}

@app.get("/api/stores/{store_id}/date-range")
def store_date_range(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    first=db.scalar(select(func.min(OrderItem.paid_at)).where(OrderItem.store_id==store_id,OrderItem.paid_at.is_not(None)))
    last=db.scalar(select(func.max(OrderItem.paid_at)).where(OrderItem.store_id==store_id,OrderItem.paid_at.is_not(None)))
    latest=db.scalar(select(func.max(ImportBatch.committed_at)).where(ImportBatch.store_id==store_id,ImportBatch.status=="committed"))
    return {"start": first[:10] if first else None, "end": last[:10] if last else None, "as_of": latest, "has_orders": bool(first)}

@app.get("/api/stores/{store_id}/cashflows")
def cashflows(store_id:int=Depends(require_store),start:str="",end:str="",db:Session=Depends(get_db)):
    today=date.today();start=start or (today-timedelta(days=29)).isoformat();end=end or today.isoformat()
    try:return cashflow.summarize(db,store_id,start,end)
    except ValueError as exc:raise HTTPException(400,str(exc)) from None

@app.get("/api/stores/{store_id}/costs")
def costs(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    entries=db.scalars(select(CostVersion).where(CostVersion.store_id==store_id).order_by(CostVersion.sku,CostVersion.valid_from.desc()))
    return [{"id":x.id,"sku":x.sku,"valid_from":x.valid_from,"unit_cost_fen":x.unit_cost_fen,"supplier":x.supplier} for x in entries]

@app.post("/api/stores/{store_id}/costs")
def add_cost(body:CostIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:
        if not body.sku.strip():raise importer.ImportProblem("商品规格不能为空")
        effective=importer.timestamp(body.valid_from);cost=importer.money(body.unit_cost)
        entry=CostVersion(store_id=store_id,sku=body.sku.strip(),valid_from=effective,unit_cost_fen=cost,supplier=body.supplier.strip())
        db.add(entry);db.flush();matched=0
        invalidated=set(db.scalars(select(Adjustment.item_external_id).where(Adjustment.store_id==store_id,Adjustment.kind=='cost_invalidate')))
        for order in db.scalars(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.sku==body.sku.strip(),OrderItem.cost_fen.is_(None))):
            if order.item_external_id in invalidated:continue
            selected=importer.lookup_cost(db,store_id,order.sku,order.paid_at)
            if selected:order.cost_fen=selected.unit_cost_fen*order.quantity;order.cost_version_id=selected.id;matched+=1
        db.commit();return {"id":entry.id,"matched_orders":matched}
    except importer.ImportProblem as e: db.rollback();raise HTTPException(400,str(e)) from None
    except IntegrityError: db.rollback();raise HTTPException(409,"该规格在此时间已有成本版本") from None

@app.post("/api/stores/{store_id}/adjustments")
def add_adjustment(body:AdjustmentIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    if body.kind not in ("cost_set","cost_delta","expense","supplier_refund"): raise HTTPException(400,"调整类型无效")
    if body.kind=="cost_set" and not body.note.strip(): raise HTTPException(400,"请填写逐单成本的依据")
    order=db.scalar(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.item_external_id==body.item_id))
    if not order:raise HTTPException(404,"未找到该订单明细")
    try:
        negative=body.kind=="cost_delta" and body.amount.strip().startswith("-")
        amount=importer.money(body.amount.strip()[1:] if negative else body.amount)
        value=-amount if negative else amount
    except importer.ImportProblem as e:raise HTTPException(400,str(e)) from None
    key=str(body.request_id)
    def existing_result():
        previous=db.scalar(select(Adjustment).where(Adjustment.store_id==store_id,Adjustment.request_key==key))
        if not previous:return None
        if (previous.item_external_id,previous.kind,previous.amount_fen,previous.note)!=(body.item_id,body.kind,value,body.note.strip()):
            raise HTTPException(409,"同一操作编号不能用于不同调整")
        return {"id":previous.id,"duplicate":True}
    previous=existing_result()
    if previous:return previous
    entry=Adjustment(store_id=store_id,item_external_id=body.item_id,kind=body.kind,amount_fen=value,note=body.note.strip(),request_key=key)
    db.add(entry)
    try:db.commit();return {"id":entry.id,"duplicate":False}
    except IntegrityError:
        db.rollback();previous=existing_result()
        if previous:return previous
        raise HTTPException(409,"调整发生冲突，请检查记录后重试") from None

@app.get("/api/stores/{store_id}/adjustments/{item_id}")
def adjustments(item_id:str,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    rows=db.scalars(select(Adjustment).where(Adjustment.store_id==store_id,Adjustment.item_external_id==item_id).order_by(Adjustment.id.desc()))
    return [{"id":x.id,"kind":x.kind,"amount_fen":x.amount_fen,"note":x.note,"created_at":x.created_at} for x in rows]

@app.post("/api/stores/{store_id}/payment-basis-reviews")
def record_payment_basis_review(body:PaymentBasisReviewIn,store_id:int=Depends(require_store),current:LoginSession=Depends(current_session),db:Session=Depends(get_db)):
    if body.basis not in ('gross','net') or not body.note.strip():raise HTTPException(400,'请选择金额口径并填写核对依据')
    importer.serialize_sqlite_writes(db)
    previous=db.scalar(select(PaymentBasisReview).where(PaymentBasisReview.store_id==store_id,PaymentBasisReview.request_key==str(body.request_id)))
    if previous:
        if (previous.item_external_id,previous.source_fingerprint,previous.basis,previous.note,previous.reviewer_id)!=(body.item_id,body.source_fingerprint,body.basis,body.note.strip(),current.user_id):raise HTTPException(409,'同一操作编号不能用于不同核对')
        return {'id':previous.id,'duplicate':True}
    order=db.scalar(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.item_external_id==body.item_id))
    if not order:raise HTTPException(404,'未找到该订单明细')
    refunds=list(db.scalars(select(Refund).where(Refund.store_id==store_id,Refund.item_external_id==body.item_id)))
    flows=list(db.scalars(select(Cashflow).where(Cashflow.store_id==store_id,Cashflow.order_external_id==order.order_external_id,Cashflow.item_external_id==body.item_id)))
    review=db.scalar(select(RefundReview).where(RefundReview.store_id==store_id,RefundReview.item_external_id==body.item_id).order_by(RefundReview.id.desc()))
    refund_flows=[flow for flow in flows if flow.kind=='refund']
    if not review or not refund_review.eligible(order,refund_flows) or review.source_fingerprint!=refund_review.fingerprint(order,refunds,refund_flows):review=None
    if payment_basis_review.fingerprint(order,refunds,flows,review)!=body.source_fingerprint:raise HTTPException(409,'来源数据已变化，请刷新后重新核对')
    entry=PaymentBasisReview(store_id=store_id,item_external_id=body.item_id,reviewer_id=current.user_id,source_fingerprint=body.source_fingerprint,basis=body.basis,note=body.note.strip(),request_key=str(body.request_id))
    db.add(entry)
    try:db.commit();return {'id':entry.id,'duplicate':False}
    except IntegrityError:db.rollback();raise HTTPException(409,'核对记录发生冲突，请刷新后重试') from None

@app.post("/api/stores/{store_id}/payment-basis-reviews/apply-gross")
def apply_gross_basis(body:BulkBasisIn,store_id:int=Depends(require_store),current:LoginSession=Depends(current_session),db:Session=Depends(get_db)):
    importer.serialize_sqlite_writes(db)
    orders=list(db.scalars(select(OrderItem).where(OrderItem.store_id==store_id)))
    refunds=list(db.scalars(select(Refund).where(Refund.store_id==store_id)))
    flows=list(db.scalars(select(Cashflow).where(Cashflow.store_id==store_id)))
    reviews=list(db.scalars(select(RefundReview).where(RefundReview.store_id==store_id).order_by(RefundReview.id)))
    basis_reviews=list(db.scalars(select(PaymentBasisReview).where(PaymentBasisReview.store_id==store_id).order_by(PaymentBasisReview.id)))
    refunds_by_item={}
    for row in refunds: refunds_by_item.setdefault(row.item_external_id,[]).append(row)
    flows_by_item={}
    for row in flows: flows_by_item.setdefault(row.item_external_id,[]).append(row)
    review_by_item={row.item_external_id:row for row in reviews}
    basis_by_item={row.item_external_id:row for row in basis_reviews}
    created=skipped=0
    note=body.note.strip()
    for order in orders:
        item_refunds=refunds_by_item.get(order.item_external_id,[])
        item_flows=flows_by_item.get(order.item_external_id,[])
        refund_flows=[row for row in item_flows if row.kind=="refund"]
        refund_review_row=review_by_item.get(order.item_external_id)
        if refund_review_row and (
            not refund_review.eligible(order,refund_flows)
            or refund_review_row.source_fingerprint!=refund_review.fingerprint(order,item_refunds,refund_flows)
        ):
            refund_review_row=None
        source=payment_basis_review.fingerprint(order,item_refunds,item_flows,refund_review_row)
        previous=basis_by_item.get(order.item_external_id)
        if previous and previous.source_fingerprint==source and previous.basis=="gross":
            skipped+=1
            continue
        key=str(uuid5(NAMESPACE_URL,f"multistore:{store_id}:{order.item_external_id}:{source}:gross"))
        db.add(PaymentBasisReview(store_id=store_id,item_external_id=order.item_external_id,reviewer_id=current.user_id,
                                  source_fingerprint=source,basis="gross",note=note,request_key=key))
        created+=1
    try:
        db.commit()
        return {"created":created,"skipped":skipped}
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,"批量口径确认发生冲突，请刷新后重试") from None

@app.get("/api/stores/{store_id}/payment-basis-reviews/{item_id}")
def payment_basis_reviews(item_id:str,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    rows=db.scalars(select(PaymentBasisReview).where(PaymentBasisReview.store_id==store_id,PaymentBasisReview.item_external_id==item_id).order_by(PaymentBasisReview.id.desc()))
    return [{'id':row.id,'basis':row.basis,'note':row.note,'reviewer_id':row.reviewer_id,'created_at':row.created_at} for row in rows]

@app.post("/api/stores/{store_id}/refund-reviews")
def record_refund_review(body:RefundReviewIn,store_id:int=Depends(require_store),current:LoginSession=Depends(current_session),db:Session=Depends(get_db)):
    try: amount=importer.money(body.refund_amount)
    except importer.ImportProblem as e:raise HTTPException(400,str(e)) from None
    note=body.note.strip()
    if not note:raise HTTPException(400,"请填写核对依据")
    importer.serialize_sqlite_writes(db)
    previous=db.scalar(select(RefundReview).where(RefundReview.store_id==store_id,RefundReview.request_key==str(body.request_id)))
    if previous:
        if (previous.item_external_id,previous.source_fingerprint,previous.refund_fen,previous.note,previous.reviewer_id)!=(body.item_id,body.source_fingerprint,amount,note,current.user_id):
            raise HTTPException(409,"同一操作编号不能用于不同核对")
        return {"id":previous.id,"duplicate":True}
    order=db.scalar(select(OrderItem).where(OrderItem.store_id==store_id,OrderItem.item_external_id==body.item_id))
    if not order:raise HTTPException(404,"未找到该订单明细")
    rows=list(db.scalars(select(Refund).where(Refund.store_id==store_id,Refund.item_external_id==body.item_id)))
    cashflow_rows=list(db.scalars(select(Cashflow).where(Cashflow.store_id==store_id,Cashflow.order_external_id==order.order_external_id,Cashflow.item_external_id==order.item_external_id,Cashflow.kind=='refund')))
    if not refund_review.eligible(order,cashflow_rows):raise HTTPException(400,"该订单不属于可人工核对的退款场景")
    if refund_review.fingerprint(order,rows,cashflow_rows)!=body.source_fingerprint:raise HTTPException(409,"订单或退款数据已变化，请刷新后重新核对")
    if any(x.status in ("pending","unknown") for x in rows):raise HTTPException(400,"退款流水仍有未完成或未知状态，请先核对流水")
    success=[x for x in rows if x.status=="success"]
    if cashflow_rows and amount==0:raise HTTPException(400,"货款账单存在售后退款，不能确认成功退款为零；请核对退款明细")
    if order.refund_status=="success" and (amount==0 or (order.refund_snapshot_fen is not None and amount<order.refund_snapshot_fen)):
        raise HTTPException(400,"累计成功退款金额不能小于订单已显示的成功退款金额")
    if success and sum(x.amount_fen for x in success)!=amount:raise HTTPException(400,"核对金额与已导入的成功退款流水不一致")
    if order.payment_basis=="gross" and order.payment_fen is not None and amount>order.payment_fen:raise HTTPException(400,"成功退款金额超过退款前实付金额")
    review=RefundReview(store_id=store_id,item_external_id=body.item_id,reviewer_id=current.user_id,source_fingerprint=body.source_fingerprint,refund_fen=amount,note=note,request_key=str(body.request_id))
    db.add(review)
    try:db.commit();return {"id":review.id,"duplicate":False}
    except IntegrityError:db.rollback();raise HTTPException(409,"核对记录发生冲突，请刷新后重试") from None

@app.get("/api/stores/{store_id}/refund-reviews/{item_id}")
def refund_reviews(item_id:str,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    rows=db.scalars(select(RefundReview).where(RefundReview.store_id==store_id,RefundReview.item_external_id==item_id).order_by(RefundReview.id.desc()))
    return [{"id":r.id,"refund_fen":r.refund_fen,"note":r.note,"reviewer_id":r.reviewer_id,"source_fingerprint":r.source_fingerprint,"created_at":r.created_at} for r in rows]

@app.post("/api/stores/{store_id}/imports/preview")
async def import_preview(store_id:int=Depends(require_store),kind:str=Form(),basis:str=Form("unknown"),mapping:str=Form("{}"),channel:str=Form("qianniu"),file:UploadFile=File(),db:Session=Depends(get_db)):
    data=await file.read(importer.MAX_FILE+1)
    try:
        parsed=json.loads(mapping)
        if not isinstance(parsed,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in parsed.items()):raise ValueError
        return importer.preview(db,store_id,kind,file.filename or "",data,basis,parsed,channel=channel)
    except (ValueError,importer.ImportProblem) as e:db.rollback();raise HTTPException(400,str(e) or "字段映射格式无效") from None
    except IntegrityError:db.rollback();raise HTTPException(409,"同时处理了相同文件，请重新预览") from None
    finally:await file.close()

@app.post("/api/stores/{store_id}/imports/{batch_id}/commit")
def import_commit(batch_id:int,body:CommitIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:return importer.commit(db,store_id,batch_id,body.accept_changes,body.confirm_item_amounts,body.changes_token)
    except importer.ImportProblem as e:db.rollback();raise HTTPException(400,str(e)) from None
    except IntegrityError:db.rollback();raise HTTPException(409,"导入发生冲突，请重新预览") from None

@app.get("/api/stores/{store_id}/imports")
def batches(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    rows=db.scalars(select(ImportBatch).where(ImportBatch.store_id==store_id,ImportBatch.status=="committed").order_by(ImportBatch.id.desc()).limit(100))
    return [{"id":b.id,"kind":b.kind,"row_count":b.row_count,"committed_at":b.committed_at,"basis":b.basis} for b in rows]

def latest_download_file(kind: str) -> Path:
    if kind not in ("orders", "refunds", "cashflows", "costs"):
        raise importer.ImportProblem("导入类型无效")
    configured = os.environ.get("MULTISTORE_DOWNLOADS_DIR", "").strip()
    directories = [Path(item).expanduser() for item in configured.split(os.pathsep) if item.strip()] if configured else [Path.home() / "Downloads", Path.home() / "下载", Path("D:/下载")]
    directories = [directory for directory in directories if directory.is_dir()]
    if not directories:
        raise importer.ImportProblem("没有找到下载目录")
    hints = {
        "orders": ("订单", "宝贝", "销售", "order", "exportorderlist"),
        "refunds": ("退款", "售后", "refund"),
        "cashflows": ("货款", "收入", "账单", "cash"),
        "costs": ("成本", "采购", "cost"),
    }[kind]
    candidates = []
    for directory in directories:
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in (".csv", ".xlsx"):
                continue
            if path.stat().st_size > importer.MAX_FILE:
                continue
            name = path.name.lower()
            score = sum(1 for hint in hints if hint.lower() in name)
            if score:
                candidates.append((score, path.stat().st_mtime_ns, path))
    if not candidates:
        raise importer.ImportProblem("下载目录里没有匹配当前导入类型的 CSV 或 XLSX 文件")
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]

@app.post("/api/stores/{store_id}/imports/latest-download")
def import_latest_download(body:LatestDownloadIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:
        path = latest_download_file(body.kind)
        result = importer.preview(db, store_id, body.kind, path.name, path.read_bytes(), body.basis if body.kind == "orders" else None)
        result["file_name"] = path.name
        return result
    except (OSError, importer.ImportProblem) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "读取下载文件时发生冲突，请重试") from None

@app.post("/api/stores/{store_id}/imports/qianniu-ai")
def import_qianniu_ai(body:QianniuAiImportIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:
        return qianniu_orders.preview_from_browser(db, store_id, body.basis)
    except (importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "AI 导入千牛订单时发生冲突，请重试") from None

@app.get("/api/stores/{store_id}/card-settings")
def get_card_settings(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    row = db.scalar(select(StoreSetting).where(StoreSetting.store_id==store_id))
    usd_rate, vnd_rate = card_reconciliation.get_store_fx_rates(db, store_id)
    return {
        "store_id": store_id,
        "contact_email": row.contact_email if row else "",
        "usd_cny_rate": card_reconciliation._format_decimal_clean(usd_rate),
        "vnd_cny_rate": card_reconciliation._format_decimal_clean(vnd_rate),
        "default_usd_cny_rate": card_reconciliation._format_decimal_clean(card_reconciliation.DEFAULT_USD_CNY_RATE),
        "default_vnd_cny_rate": card_reconciliation._format_decimal_clean(card_reconciliation.DEFAULT_VND_CNY_RATE),
        "updated_at": row.updated_at if row else "",
    }

def _parse_fx_rate_input(raw_value: str | None, label: str, upper_bound: Decimal) -> str | None:
    if raw_value is None:
        return None
    cleaned = raw_value.strip()
    if not cleaned:
        return ""
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        raise HTTPException(400, f"{label} 格式无效，请填写正数") from None
    if not parsed.is_finite() or parsed <= 0 or parsed > upper_bound:
        raise HTTPException(400, f"{label} 必须大于 0 且不超过 {upper_bound}")
    return card_reconciliation._format_decimal_clean(parsed)

@app.put("/api/stores/{store_id}/card-settings")
def update_card_settings(body:CardSettingsIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    has_fx = body.usd_cny_rate is not None or body.vnd_cny_rate is not None
    if body.contact_email is None and not has_fx:
        raise HTTPException(400, "请先填写联系方式或汇率设置")
    email_to_save: str | None = None
    if body.contact_email is not None:
        email = body.contact_email.strip()
        if not email:
            if not has_fx:
                raise HTTPException(400, "请先填写联系方式（邮箱）")
        else:
            if "@" in email:
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    raise HTTPException(400, "邮箱格式不正确，请重新检查")
            elif len(email) < 5 or any(ch.isspace() for ch in email):
                raise HTTPException(400, "请填写有效的邮箱或联系方式")
            email_to_save = email
    usd_to_save = _parse_fx_rate_input(body.usd_cny_rate, "USD 汇率", Decimal("1000"))
    vnd_to_save = _parse_fx_rate_input(body.vnd_cny_rate, "VND 汇率", Decimal("10"))
    importer.serialize_sqlite_writes(db)
    row = db.scalar(select(StoreSetting).where(StoreSetting.store_id==store_id))
    now = now_iso()
    if row is None:
        row = StoreSetting(
            store_id=store_id,
            contact_email=email_to_save or "",
            usd_cny_rate=usd_to_save or "",
            vnd_cny_rate=vnd_to_save or "",
            updated_at=now,
        )
        db.add(row)
    else:
        if email_to_save is not None:
            row.contact_email = email_to_save
        if usd_to_save is not None:
            row.usd_cny_rate = usd_to_save
        if vnd_to_save is not None:
            row.vnd_cny_rate = vnd_to_save
        row.updated_at = now
    db.commit()
    usd_rate, vnd_rate = card_reconciliation.get_store_fx_rates(db, store_id)
    return {
        "store_id": store_id,
        "contact_email": row.contact_email,
        "usd_cny_rate": card_reconciliation._format_decimal_clean(usd_rate),
        "vnd_cny_rate": card_reconciliation._format_decimal_clean(vnd_rate),
        "default_usd_cny_rate": card_reconciliation._format_decimal_clean(card_reconciliation.DEFAULT_USD_CNY_RATE),
        "default_vnd_cny_rate": card_reconciliation._format_decimal_clean(card_reconciliation.DEFAULT_VND_CNY_RATE),
        "updated_at": row.updated_at,
    }

@app.post("/api/stores/{store_id}/card-purchases/import-text")
def import_card_purchases_text(body:CardTextImportIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    safe_name = Path(body.filename or "card-paste.txt").name or "card-paste.txt"
    if not safe_name.lower().endswith((".txt", ".md", ".tsv", ".csv", ".json", ".paste")):
        safe_name = f"{safe_name}.txt"
    try:
        return card_reconciliation.import_purchases(db, store_id, safe_name, body.text.encode("utf-8"), source_hint=body.source_hint)
    except card_reconciliation.CardPurchaseImportError as exc:
        db.rollback()
        raise HTTPException(400, detail={"message": str(exc), "issues": exc.issues}) from None
    except importer.ImportProblem as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "卡网采购单发生重复或冲突，请重试") from None

@app.post("/api/stores/{store_id}/card-purchases/ai-query")
def ai_query_card_purchases(body:CardAiQueryIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    email = (body.contact_email or "").strip()
    if not email:
        setting = db.scalar(select(StoreSetting).where(StoreSetting.store_id==store_id))
        email = (setting.contact_email if setting else "").strip()
    if not email and not body.captcha_session_id:
        raise HTTPException(400, "请先填写并保存联系方式（邮箱）")
    try:
        res = card_query_ai.query_card_sites(
            store_id=store_id,
            site=body.site,
            contact_email=email,
            captcha_code=body.captcha_code,
            captcha_session_id=body.captcha_session_id,
        )
    except importer.ImportProblem as exc:
        raise HTTPException(400, str(exc)) from None
    orders = res.get("orders") or []
    import_result = None
    if orders:
        try:
            payload = json.dumps(orders, ensure_ascii=False).encode("utf-8")
            import_result = card_reconciliation.import_purchases(db, store_id, "ai-query.json", payload)
        except (importer.ImportProblem, IntegrityError) as exc:
            db.rollback()
            raise HTTPException(400, f"查询到订单但写入账本失败：{exc}") from None
    if res.get("status") == "error" and not orders and res.get("errors"):
        raise HTTPException(400, "；".join(res["errors"]))
    out = {k: v for k, v in res.items() if k != "orders"}
    out["queried_count"] = len(orders)
    out["import_result"] = import_result
    return out

@app.get("/api/stores/{store_id}/card-ledger")
def card_ledger(
    store_id:int=Depends(require_store),
    category:str="",
    status:str="",
    source:str="",
    channel:str="",
    start:str="",
    end:str="",
    q:str="",
    limit:int=200,
    db:Session=Depends(get_db),
):
    safe_limit = max(1, min(limit, 2000))
    return card_reconciliation.ledger(
        db, store_id, category=category, status=status, source=source, channel=channel, start=start, end=end, q=q, limit=safe_limit
    )

@app.post("/api/stores/{store_id}/card-purchases/import")
async def import_card_purchases(store_id:int=Depends(require_store),source_hint:str=Form(""),file:UploadFile=File(),db:Session=Depends(get_db)):
    data = await file.read(importer.MAX_FILE + 1)
    try:
        return card_reconciliation.import_purchases(db, store_id, file.filename or "", data, source_hint=source_hint)
    except card_reconciliation.CardPurchaseImportError as exc:
        db.rollback()
        raise HTTPException(
            400,
            detail={"message": str(exc), "issues": exc.issues},
        ) from None
    except importer.ImportProblem as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "卡网采购单发生重复或冲突，请重试") from None
    finally:
        await file.close()

@app.get("/api/stores/{store_id}/card-purchases")
def card_purchases(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    rows = db.scalars(select(CardPurchase).where(CardPurchase.store_id==store_id).order_by(CardPurchase.paid_at.desc(),CardPurchase.id.desc()).limit(500))
    return [{"id":x.id,"purchase_external_id":x.purchase_external_id,"product_title":x.product_title,"seller":x.seller,
             "quantity":x.quantity,"amount_fen":x.amount_fen,"refund_fen":x.refund_fen,"paid_at":x.paid_at,"status":x.status,
             "source":x.source,"source_label":card_reconciliation.source_label(x.source),"original_currency":x.original_currency,
             "original_amount":x.original_amount,"linked_order_external_id":x.linked_order_external_id,
             "linked_item_external_id":(x.raw_data.get("agiso_oid","") if isinstance(x.raw_data,dict) else ""),"linked_channel":x.linked_channel,
             "linked_redeem_code":x.linked_redeem_code,"batch_id":x.batch_id} for x in rows]

@app.delete("/api/stores/{store_id}/card-purchases")
def delete_card_purchases(body:DeleteCardPurchasesIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    purchase_ids=list(dict.fromkeys(purchase_id for purchase_id in body.purchase_ids if purchase_id > 0))
    if not purchase_ids:
        raise HTTPException(400,"请至少选择一笔卡网订单")
    importer.serialize_sqlite_writes(db)
    purchases=list(db.scalars(select(CardPurchase).where(CardPurchase.store_id==store_id,CardPurchase.id.in_(purchase_ids))))
    if not purchases:
        return {"deleted":0,"missing":len(purchase_ids),"matched_deleted":0,"purchase_ids":[]}
    existing_ids=[purchase.id for purchase in purchases]
    matched_deleted=db.scalar(
        select(func.count()).select_from(CardMatch).where(
            CardMatch.store_id==store_id,
            CardMatch.purchase_id.in_(existing_ids),
        )
    ) or 0
    db.execute(delete(CardMatch).where(CardMatch.store_id==store_id,CardMatch.purchase_id.in_(existing_ids)))
    db.execute(delete(CardPurchase).where(CardPurchase.store_id==store_id,CardPurchase.id.in_(existing_ids)))
    db.commit()
    return {
        "deleted":len(existing_ids),
        "missing":len(purchase_ids)-len(existing_ids),
        "matched_deleted":int(matched_deleted),
        "purchase_ids":existing_ids,
    }

@app.get("/api/stores/{store_id}/card-matches")
def card_matches(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    rows=db.execute(
        select(CardMatch,CardPurchase,OrderItem.order_external_id)
        .join(CardPurchase,CardPurchase.id==CardMatch.purchase_id)
        .outerjoin(
            OrderItem,
            (OrderItem.store_id==CardMatch.store_id) & (OrderItem.item_external_id==CardMatch.item_external_id),
        )
        .where(CardMatch.store_id==store_id, CardMatch.status=="confirmed")
        .order_by(CardMatch.id.desc())
    ).all()
    return [{
        "id":match.id,
        "item_external_id":match.item_external_id,
        "order_external_id":order_ext_id or "",
        "sale_title":(match.evidence or {}).get("sale_title",""),
        "purchase_external_id":purchase.purchase_external_id,
        "purchase_title":purchase.product_title,
        "seller":purchase.seller,
        "cost_fen":(match.evidence or {}).get("allocated_cost_fen") if (match.evidence or {}).get("allocated_cost_fen") is not None else round((purchase.amount_fen-purchase.refund_fen)*max(1,match.allocated_quantity or 1)/max(1,purchase.quantity)),
        "unit_cost_fen":round((purchase.amount_fen-purchase.refund_fen)/max(1,purchase.quantity)),
        "quantity":purchase.quantity,
        "allocated_quantity":max(1,match.allocated_quantity or 1),
        "purchase_paid_at":purchase.paid_at,
        "score":match.score,
        "status":match.status,
        "created_at":match.created_at,
    } for match,purchase,order_ext_id in rows]

@app.get("/api/stores/{store_id}/cost-match-suggestions")
def cost_match_suggestions(
    store_id:int=Depends(require_store),
    start:str="",
    end:str="",
    scope:str="",
    channel:str="",
    use_ai:bool=False,
    limit:int=300,
    db:Session=Depends(get_db),
):
    safe_limit=max(1, min(limit, 500))
    if scope == "all" or start == "all":
        q_start, q_end = "", ""
    else:
        today=date.today()
        q_start=start or (today-timedelta(days=29)).isoformat()
        q_end=end or today.isoformat()
    try:return card_reconciliation.suggestions(db,store_id,q_start,q_end,limit=safe_limit,use_ai=use_ai,channel=channel)
    except ValueError as exc:raise HTTPException(400,str(exc)) from None

@app.post("/api/stores/{store_id}/card-matches")
def confirm_card_match(body:CardMatchIn,store_id:int=Depends(require_store),current:LoginSession=Depends(current_session),db:Session=Depends(get_db)):
    try:return card_reconciliation.confirm_match(db,store_id,body,current.user_id)
    except importer.ImportProblem as exc:db.rollback();raise HTTPException(400,str(exc)) from None
    except IntegrityError:db.rollback();raise HTTPException(409,"成本匹配发生冲突，请刷新后重试") from None

@app.post("/api/stores/{store_id}/card-matches/batch")
def batch_confirm_card_matches(body:CardBatchMatchIn,store_id:int=Depends(require_store),current:LoginSession=Depends(current_session),db:Session=Depends(get_db)):
    try:return card_reconciliation.batch_confirm_matches(db,store_id,body.items,current.user_id)
    except importer.ImportProblem as exc:db.rollback();raise HTTPException(400,str(exc)) from None
    except IntegrityError:db.rollback();raise HTTPException(409,"批量成本匹配发生冲突，请刷新后重试") from None

@app.post("/api/stores/{store_id}/card-matches/auto-exact")
def auto_confirm_exact_card_matches(
    body:AutoExactMatchIn|None=None,
    store_id:int=Depends(require_store),
    current:LoginSession=Depends(current_session),
    db:Session=Depends(get_db),
):
    req = body or AutoExactMatchIn()
    try:
        return card_reconciliation.auto_confirm_exact_matches(
            db,
            store_id,
            current.user_id,
            start=req.start,
            end=req.end,
            channel=req.channel,
        )
    except (importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "自动确认精确关联时发生冲突，请刷新后重试") from None

@app.get("/api/stores/{store_id}/upstreams/status")
def get_upstreams_status(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    return upstream_sync.get_upstreams_status(db, store_id)

@app.post("/api/stores/{store_id}/upstreams/sync-tg")
def sync_tg_upstreams(body:UpstreamSyncTgIn|None=None,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    req = body or UpstreamSyncTgIn()
    try:
        return upstream_sync.sync_canboso_orders(db, store_id, supplier=req.supplier)
    except (upstream_sync.UpstreamSyncError, importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None

@app.post("/api/stores/{store_id}/upstreams/sync-plan2")
def sync_plan2_upstream(
    body:UpstreamSyncPlan2In|None=None,
    store_id:int=Depends(require_store),
    current:LoginSession=Depends(current_session),
    db:Session=Depends(get_db),
):
    req = body or UpstreamSyncPlan2In()
    try:
        return upstream_sync.sync_plan2_reconciliation(
            db,
            store_id,
            start_date=req.start_date,
            end_date=req.end_date,
            channel=req.channel,
            auto_confirm_exact=req.auto_confirm_exact,
            reviewer_id=current.user_id,
        )
    except (upstream_sync.UpstreamSyncError, importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None

@app.post("/api/stores/{store_id}/upstreams/sync-16688-orders")
def sync_16688_orders(body:UpstreamSync16688In|None=None,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    req = body or UpstreamSync16688In()
    try:
        return upstream_sync.sync_16688_sales_orders(
            db,
            store_id,
            max_pages=req.max_pages,
            page_size=req.page_size,
            start_time=req.start_time,
            end_time=req.end_time,
        )
    except (upstream_sync.UpstreamSyncError, importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None

@app.post("/api/stores/{store_id}/orders/import-16688")
async def import_16688_orders(store_id:int=Depends(require_store),basis:str=Form("gross"),file:UploadFile=File(),db:Session=Depends(get_db)):
    data = await file.read(importer.MAX_FILE + 1)
    try:
        return upstream_sync.import_16688_orders_payload(db, store_id, file.filename or "16688-orders.csv", data, basis=basis)
    except (upstream_sync.UpstreamSyncError, importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "16688 销售订单导入发生冲突，请重试") from None
    finally:
        await file.close()

@app.post("/api/stores/{store_id}/chats/import-text")
def import_chats_text(body:ChatTextImportIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:
        return qianniu_chats.import_chats(
            db, store_id, body.text, filename=body.filename or "chat-paste.txt", source=body.source or "paste", use_ai=body.use_ai
        )
    except (importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "千牛聊天记录导入冲突，请重试") from None

@app.post("/api/stores/{store_id}/chats/import-file")
async def import_chats_file(
    store_id:int=Depends(require_store),
    use_ai:bool=Form(False),
    file:UploadFile=File(),
    db:Session=Depends(get_db),
):
    data = await file.read(importer.MAX_FILE + 1)
    try:
        if len(data) > importer.MAX_FILE:
            raise importer.ImportProblem("聊天文件过大，单次请勿超过 8 MB")
        return qianniu_chats.import_chats(
            db, store_id, data, filename=file.filename or "chats.txt", source="file", use_ai=use_ai
        )
    except (importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "千牛聊天记录导入冲突，请重试") from None
    finally:
        await file.close()

@app.post("/api/stores/{store_id}/chats/sync-browser")
def sync_qianniu_chats_browser(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:
        return qianniu_chats.sync_chats_from_browser(db, store_id)
    except (importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None

@app.get("/api/stores/{store_id}/chats/overview")
def get_chats_overview(
    store_id:int=Depends(require_store),
    category:str="",
    stage:str="",
    topic:str="",
    resolved:str="",
    q:str="",
    start:str="",
    end:str="",
    limit:int=100,
    db:Session=Depends(get_db),
):
    safe_limit = max(1, min(limit, 500))
    return qianniu_chats.chat_overview(
        db, store_id, category=category, stage=stage, topic=topic, resolved=resolved, q=q, start=start, end=end, limit=safe_limit
    )

@app.post("/api/stores/{store_id}/chats/knowledge")
def save_chat_knowledge(body:ChatKnowledgeIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:
        return qianniu_chats.upsert_knowledge_item(db, store_id, body)
    except (importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None

@app.delete("/api/stores/{store_id}/chats/knowledge/{item_id}")
def remove_chat_knowledge(item_id:int,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:
        return qianniu_chats.delete_knowledge_item(db, store_id, item_id)
    except (importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from None

@app.post("/api/stores/{store_id}/chats/sessions/{session_id}/toggle-resolved")
def toggle_chat_session_resolved(
    session_id:int,
    body:ChatResolveIn|None=None,
    store_id:int=Depends(require_store),
    db:Session=Depends(get_db),
):
    try:
        return qianniu_chats.toggle_session_resolved(
            db, store_id, session_id, body.resolved if body else None
        )
    except (importer.ImportProblem, ValueError) as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from None

@app.post("/api/stores/{store_id}/chats/query")
def query_store_chats(body:ChatQueryIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:
        return qianniu_chats.query_chat_insights(
            db, store_id, body.question, start=body.start, end=body.end, use_ai=body.use_ai
        )
    except (importer.ImportProblem, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None

@app.post("/api/stores/{store_id}/agent/query")
def ask(body:QueryIn,store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    try:return agent.run(db,store_id,body.start,body.end,body.question,sku_filter=body.sku)
    except (agent.AgentProblem,ValueError) as e:raise HTTPException(400,str(e)) from None

@app.get("/api/templates/{kind}")
def template(kind:str,current:LoginSession=Depends(current_session)):
    samples={
      "costs":"sku,valid_from,unit_cost,supplier\n示例商品 | 标准规格,2026-01-01,5.00,示例渠道\n",
      "refunds":"refund_id,item_id,order_id,refund_status,refund_amount,completed_at\nR-001,I-001,O-001,success,3.00,2026-01-05 12:00:00\n",
      "cashflows":"bill_type,business_flow,order_id,item_id,refund_id,settled_at,bill_income,bill_refund\ngoods_income,F-001,O-001,I-001,,2026-01-05 12:00:00,10.00,0\ngoods_income,F-001,O-001,I-001,R-001,2026-02-01 12:00:00,0,3.00\n",
      "orders":"item_id,order_id,sku,quantity,paid_at,status,payment,refund_status,refund_amount\nI-001,O-001,示例商品,1,2026-01-01 12:00:00,completed,10.00,none,0.00\n"}
    if kind not in samples:raise HTTPException(404,"模板不存在")
    return Response("\ufeff"+samples[kind],media_type="text/csv; charset=utf-8",headers={"Content-Disposition":f"attachment; filename={kind}-example.csv","Cache-Control":"no-store"})

@app.post("/api/stores/{store_id}/imports/columns")
async def import_columns(store_id:int=Depends(require_store),kind:str=Form(),file:UploadFile=File()):
    data=await file.read(importer.MAX_FILE+1)
    try:
        if kind not in importer.REQUIRED:raise importer.ImportProblem("导入类型无效")
        headers,rows=importer.read_table(file.filename or "",data)
        fields={
          "orders":["item_id","order_id","sku","product_id","property","title","quantity","paid_at","status","payment","refund_status","refund_amount"],
          "refunds":["refund_id","item_id","order_id","refund_status","refund_amount","completed_at"],
          "cashflows":["bill_type","business_flow","order_id","item_id","refund_id","settled_at","bill_income","bill_refund"],
          "costs":["sku","valid_from","unit_cost","supplier"]}[kind]
        suggested={k:next((x for x in importer.ALIASES[k] if x in headers),"") for k in fields}
        return {"headers":headers,"row_count":len(rows),"fields":fields,"required":sorted(importer.REQUIRED[kind]),"suggested":suggested}
    except importer.ImportProblem as exc:raise HTTPException(400,str(exc)) from None
    finally:await file.close()

@app.get("/api/stores/{store_id}/skus")
def skus(store_id:int=Depends(require_store),db:Session=Depends(get_db)):
    result={}
    overrides={};invalidations={}
    for a in db.scalars(select(Adjustment).where(Adjustment.store_id==store_id,Adjustment.kind.in_(("cost_set","cost_invalidate")))):
        target=overrides if a.kind=="cost_set" else invalidations
        target[a.item_external_id]=max(target.get(a.item_external_id,0),a.id)
    for x in db.scalars(select(OrderItem).where(OrderItem.store_id==store_id)):
        row=result.setdefault(x.sku,{"sku":x.sku,"title":x.product_title,"orders":0,"missing_cost":0})
        row["orders"]+=1
        marker=invalidations.get(x.item_external_id,0)
        has_override=overrides.get(x.item_external_id,0)>marker
        row["missing_cost"]+=not has_override and (bool(marker) or x.cost_fen is None)
    return sorted(result.values(),key=lambda x:(-x["missing_cost"],x["sku"]))
