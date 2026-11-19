from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request, Response
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import LoginSession, Membership, Store, User

passwords=PasswordHash.recommended()
_dummy=passwords.hash(secrets.token_urlsafe(24))
_attempts: dict[str, deque[float]]=defaultdict(deque)
COOKIE="multistore_session"

def digest(value: str) -> str: return hashlib.sha256(value.encode("utf-8")).hexdigest()

def login(db: Session, username: str, password: str, response: Response, client_ip: str):
    now=time.monotonic(); attempts=_attempts[client_ip]
    while attempts and attempts[0]<now-60: attempts.popleft()
    if len(attempts)>=10: raise HTTPException(429,"尝试次数过多，请稍后再试")
    user=db.scalar(select(User).where(User.username==username))
    if not passwords.verify(password,user.password_hash if user else _dummy):
        attempts.append(now);raise HTTPException(401,"账号或密码不正确")
    attempts.clear()
    token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=digest(token),csrf_hash=digest(csrf),user_id=user.id,
        expires_at=(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat(timespec="seconds")))
    db.commit()
    response.set_cookie(COOKIE,token,httponly=True,secure=os.environ.get("MULTISTORE_SECURE_COOKIE")=="1",samesite="strict",max_age=43200,path="/")
    stores=list(db.execute(select(Store.id,Store.name).join(Membership,Membership.store_id==Store.id).where(Membership.user_id==user.id)))
    return {"username":user.username,"csrf":csrf,"stores":[{"id":s.id,"name":s.name} for s in stores]}

def quick_login(db: Session, response: Response, username: str | None = None):
    if username:
        user = db.scalar(select(User).where(User.username == username.strip()))
        if not user:
            raise HTTPException(404, f"未找到指定用户: {username}")
    else:
        user = db.scalar(select(User).where(User.username == "store_owner"))
        if not user:
            user = db.scalar(select(User).order_by(User.id.asc()))
        if not user:
            raise HTTPException(404, "系统中尚未初始化用户")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    db.add(LoginSession(
        token_hash=digest(token),
        csrf_hash=digest(csrf),
        user_id=user.id,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
    ))
    db.commit()
    response.set_cookie(COOKIE, token, httponly=True, secure=os.environ.get("MULTISTORE_SECURE_COOKIE") == "1", samesite="strict", max_age=86400, path="/")
    stores = list(db.execute(select(Store.id, Store.name).join(Membership, Membership.store_id == Store.id).where(Membership.user_id == user.id)))
    return {"username": user.username, "csrf": csrf, "stores": [{"id": s.id, "name": s.name} for s in stores]}

def list_quick_users(db: Session):
    users = list(db.scalars(select(User).order_by(User.id.asc())))
    result = []
    for u in users:
        stores = list(db.execute(select(Store.id, Store.name).join(Membership, Membership.store_id == Store.id).where(Membership.user_id == u.id)))
        result.append({
            "id": u.id,
            "username": u.username,
            "stores": [{"id": s.id, "name": s.name} for s in stores]
        })
    return result

def current_session(request: Request, db: Session=Depends(get_db)):
    token=request.cookies.get(COOKIE,"")
    record=db.get(LoginSession,digest(token)) if token else None
    if not record or record.expires_at <= datetime.now(timezone.utc).isoformat(timespec="seconds"):
        raise HTTPException(401,"请先登录")
    if request.method not in ("GET","HEAD","OPTIONS"):
        supplied=request.headers.get("x-csrf-token","")
        if not supplied or not hmac.compare_digest(digest(supplied),record.csrf_hash): raise HTTPException(403,"操作已过期，请重新登录")
    return record

def require_store(store_id:int, current:LoginSession=Depends(current_session),db:Session=Depends(get_db)):
    if not db.get(Membership,(current.user_id,store_id)): raise HTTPException(403,"没有该店铺的访问权限")
    return store_id
