from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .config import Settings
from .db import Ledger
from .gateways import CanbosoClient, Channel16688Client, DryRun16688, DryRunCanboso
from .models import BatchRequest, IssueRequest, RedeemRequest, RetryRequest, RevokeRequest, SkuPatchRequest, WarrantyAckRequest
from .service import Mailer, SKUS, Service


def error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error_code": code, "message": message})


def status_for(code: str) -> int:
    return {"CODE_NOT_FOUND": 404, "CODE_REVOKED": 409, "CONCURRENT_IN_PROGRESS": 409, "ORDER_NOT_ELIGIBLE": 409, "CANNOT_REVOKE_COMPLETED": 409, "RETRY_NOT_AVAILABLE": 409, "WARRANTY_LOCKED": 409, "WARRANTY_NOT_APPLICABLE": 409, "WARRANTY_NOT_ALLOWED": 409, "INVALID_EMAIL": 422, "EMAIL_MISMATCH": 422, "PREFLIGHT_REQUIRED": 422, "INVALID_SCREENSHOT_TYPE": 422, "SCREENSHOT_TOO_LARGE": 422, "SCREENSHOT_EMPTY": 422, "NOTE_TOO_LONG": 422, "INVALID_CHANNEL": 422, "GOODS_NO_REQUIRED": 422}.get(code, 503)


def create_app(settings: Optional[Settings] = None, canboso: Any = None, channel: Any = None, mailer: Optional[Mailer] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.prepare()
    ledger = Ledger(settings.db_path)
    canboso = canboso or (DryRunCanboso() if settings.execution_mode == "dry_run" else CanbosoClient(settings.canboso_base_url, settings.canboso_api_secret, settings.canboso_timeout_seconds))
    channel = channel or (DryRun16688() if settings.execution_mode == "dry_run" else Channel16688Client(settings.channel16688_base_url, settings.channel16688_api_key, settings.channel16688_api_secret, settings.canboso_timeout_seconds))
    mailer = mailer or Mailer(settings)
    service = Service(ledger, settings, canboso, channel, mailer)

    async def poller() -> None:
        while True:
            await asyncio.sleep(max(10, settings.poll_interval_seconds))
            rows, _ = ledger.list_codes("status='waiting_fulfillment'", (), 500, 0)
            for row in rows:
                try:
                    service.poll(row["code"])
                except Exception:
                    service.alert(row["code"], "UPSTREAM_ERROR")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(poller())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Plan 2 Redeem Service", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.ledger, app.state.service, app.state.mailer = settings, ledger, service, mailer

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error_code" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"error_code": "HTTP_ERROR", "message": str(exc.detail)})

    def get_service() -> Service:
        return service

    def require_admin(authorization: Optional[str] = Header(default=None)) -> None:
        expected = f"Bearer {settings.admin_token}"
        if not authorization or authorization != expected:
            raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "message": "未授权"})

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "execution_mode": settings.execution_mode}

    @app.get("/api/v1/public/codes/{code}")
    def public_code(code: str, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        return svc.public_row(row) if row else error("CODE_NOT_FOUND", "兑换码不存在", 404)

    @app.post("/api/v1/public/codes/{code}/redeem")
    def public_redeem(code: str, body: RedeemRequest, svc: Service = Depends(get_service)) -> Any:
        result, err_code = svc.redeem(code, body.buyer_email, body.buyer_email_confirm, body.preflight_confirmed)
        if err_code:
            messages = {"CODE_NOT_FOUND": "兑换码不存在", "CODE_REVOKED": "兑换码已作废", "CONCURRENT_IN_PROGRESS": "该兑换码正在处理中，请稍后刷新", "ORDER_NOT_ELIGIBLE": "当前订单暂不满足核销条件", "INVALID_EMAIL": "请输入有效的 Gmail 地址", "EMAIL_MISMATCH": "两次 Gmail 地址不一致", "PREFLIGHT_REQUIRED": "请先确认使用前检查项"}
            return error(err_code, messages.get(err_code, "暂时无法处理，请稍后重试"), status_for(err_code))
        return result

    @app.post("/api/v1/public/codes/{code}/refresh")
    def public_refresh(code: str, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "兑换码不存在", 404)
        if row["status"] != "waiting_fulfillment":
            return svc.public_row(row)
        if row.get("last_polled_at"):
            from datetime import datetime, timezone
            try:
                elapsed = (datetime.now(timezone.utc).astimezone() - datetime.fromisoformat(row["last_polled_at"])).total_seconds()
                if elapsed < 15:
                    result = svc.public_row(row)
                    result["cooldown_seconds"] = int(15 - elapsed)
                    return result
            except ValueError:
                pass
        result = svc.poll(code)
        result["cooldown_seconds"] = 0
        return result

    @app.post("/api/v1/public/codes/{code}/warranty")
    async def public_warranty(code: str, admin_email: str = Form(...), admin_email_confirm: str = Form(...), note: str = Form(""), screenshot: UploadFile = File(...), svc: Service = Depends(get_service)) -> Any:
        if len(note) > 300:
            return error("NOTE_TOO_LONG", "备注不能超过 300 字", 422)
        content = await _read_upload_limited(screenshot, 5 * 1024 * 1024)
        if content is None:
            return error("SCREENSHOT_TOO_LARGE", "截图不能超过 5MB", 422)
        result, err_code = svc.warranty(code, admin_email, admin_email_confirm, note, content, screenshot.content_type or "")
        if err_code:
            messages = {"CODE_NOT_FOUND": "兑换码不存在", "WARRANTY_LOCKED": "保修信息已报备，不能再修改", "INVALID_EMAIL": "请输入有效的 Gmail 地址", "EMAIL_MISMATCH": "两次 Gmail 地址不一致", "INVALID_SCREENSHOT_TYPE": "仅支持 JPG、PNG 或 WEBP 图片", "SCREENSHOT_TOO_LARGE": "截图不能超过 5MB", "SCREENSHOT_EMPTY": "请上传有效的截图文件", "WARRANTY_NOT_APPLICABLE": "该兑换码不支持保修登记", "WARRANTY_NOT_ALLOWED": "当前状态不能提交保修登记"}
            return error(err_code, messages.get(err_code, "当前状态不能提交保修登记"), status_for(err_code))
        return result

    @app.get("/api/v1/admin/overview", dependencies=[Depends(require_admin)])
    def overview(svc: Service = Depends(get_service)) -> Any:
        try:
            return svc.overview()
        except Exception:
            return error("UPSTREAM_ERROR", "上游状态暂时不可用", 503)

    @app.post("/api/v1/admin/upstream/refresh", dependencies=[Depends(require_admin)])
    def refresh(svc: Service = Depends(get_service)) -> Any:
        return overview(svc)

    @app.post("/api/v1/admin/codes/issue", status_code=201, dependencies=[Depends(require_admin)])
    def issue(body: IssueRequest, svc: Service = Depends(get_service)) -> Any:
        if body.channel not in {"taobao", "16688"}:
            return error("INVALID_CHANNEL", "渠道不合法", 422)
        try:
            row = svc.issue(body.sku_key, body.channel, body.external_order_id, body.remark)
        except ValueError as e:
            return error(str(e), "SKU 不存在", 422)
        url = f"{settings.public_base_url}/r/{row['code']}"
        return {"code": row["code"], "redeem_url": url, "wangwang_message": f"您好，感谢购买 {SKUS[body.sku_key]['title']}！\n请点击专属提货链接：\n{url}\n兑换码：{row['code']}", "item": svc.admin_summary(row)}

    @app.post("/api/v1/admin/codes/batch", status_code=201, dependencies=[Depends(require_admin)])
    def batch(body: BatchRequest, svc: Service = Depends(get_service)) -> Any:
        if body.channel not in {"taobao", "16688"}:
            return error("INVALID_CHANNEL", "渠道不合法", 422)
        try:
            return svc.batch(body.sku_key, body.channel, body.count, body.goods_no, body.batch_label)
        except ValueError as e:
            return error(str(e), "批量制码参数不合法", 422)

    @app.get("/api/v1/admin/codes", dependencies=[Depends(require_admin)])
    def list_codes(status: Optional[str] = None, channel: Optional[str] = None, sku_key: Optional[str] = None, q: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), svc: Service = Depends(get_service)) -> Any:
        clauses, params = [], []
        for field, value in (("status", status), ("channel", channel), ("sku_key", sku_key)):
            if value:
                clauses.append(field + "=?"); params.append(value)
        if q:
            clauses.append("(code LIKE ? OR external_order_id LIKE ? OR buyer_email LIKE ? OR upstream_order_code LIKE ? OR remark LIKE ?)"); params.extend([f"%{q}%"] * 5)
        rows, total = ledger.list_codes(" AND ".join(clauses), tuple(params), page_size, (page - 1) * page_size)
        return {"items": [svc.admin_summary(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    @app.get("/api/v1/admin/codes/{code}", dependencies=[Depends(require_admin)])
    def admin_code(code: str, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        return svc.admin_detail(row) if row else error("CODE_NOT_FOUND", "兑换码不存在", 404)

    @app.get("/api/v1/admin/codes/{code}/warranty-screenshot", dependencies=[Depends(require_admin)])
    def screenshot(code: str) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "兑换码不存在", 404)
        path = row.get("screenshot_path")
        if not path or not _is_private_path(path, settings.storage_dir) or not Path(path).is_file():
            return error("SCREENSHOT_NOT_FOUND", "截图不存在", 404)
        return FileResponse(path, media_type=row.get("screenshot_content_type") or "application/octet-stream")

    @app.post("/api/v1/admin/codes/{code}/poll", dependencies=[Depends(require_admin)])
    def admin_poll(code: str, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "兑换码不存在", 404)
        svc.poll(code)
        return svc.admin_detail(ledger.get_code(code))

    @app.post("/api/v1/admin/codes/{code}/revoke", dependencies=[Depends(require_admin)])
    def revoke(code: str, body: RevokeRequest, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "兑换码不存在", 404)
        if row["status"] == "completed":
            return error("CANNOT_REVOKE_COMPLETED", "已完成订单不能作废", 409)
        svc.ledger.transition(code, "revoked", body.reason, {"last_error_code": "REVOKED"})
        return svc.admin_detail(ledger.get_code(code))

    @app.post("/api/v1/admin/codes/{code}/retry", dependencies=[Depends(require_admin)])
    def retry(code: str, body: RetryRequest, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "兑换码不存在", 404)
        if row["status"] != "needs_manual_review":
            return svc.admin_detail(row)
        _, err_code = svc.replay(code, body.override_price_limit, body.note)
        if err_code:
            messages = {"RETRY_NOT_AVAILABLE": "当前订单没有可安全重放的原始请求", "PRICE_LIMIT_EXCEEDED": "当前价格仍超过熔断上限", "LOW_BALANCE": "Canboso 余额不足", "UPSTREAM_OUT_OF_STOCK": "上游商品暂时缺货", "UPSTREAM_ERROR": "上游暂时不可用"}
            return error(err_code, messages.get(err_code, "重试未成功，请检查订单详情"), status_for(err_code))
        return svc.admin_detail(ledger.get_code(code))

    @app.post("/api/v1/admin/codes/{code}/warranty-ack", dependencies=[Depends(require_admin)])
    def warranty_ack(code: str, body: WarrantyAckRequest, svc: Service = Depends(get_service)) -> Any:
        if not ledger.get_code(code):
            return error("CODE_NOT_FOUND", "兑换码不存在", 404)
        return svc.warranty_ack(code, body.note)

    @app.patch("/api/v1/admin/settings/skus/{sku_key}", dependencies=[Depends(require_admin)])
    def patch_sku(sku_key: str, body: SkuPatchRequest, svc: Service = Depends(get_service)) -> Any:
        current = ledger.get_sku(sku_key)
        if not current:
            return error("UNKNOWN_SKU", "SKU 不存在", 404)
        goods_no = current.get("goods_no")
        if "goods_no" in body.model_fields_set:
            goods_no = body.goods_no.strip() if body.goods_no and body.goods_no.strip() else None
        with ledger.transaction(immediate=True) as c:
            c.execute("UPDATE sku_settings SET max_cost_usd=?, goods_no=? WHERE sku_key=?", (body.max_cost_usd, goods_no, sku_key))
        try:
            return svc.sku_status(sku_key)
        except Exception:
            return error("UPSTREAM_ERROR", "上游状态暂时不可用", 503)

    @app.post("/api/v1/admin/notifications/test-email", dependencies=[Depends(require_admin)])
    def test_email() -> Any:
        try:
            mailer.send("Plan 2 测试邮件", "这是一封 Plan 2 自助提货服务测试邮件。")
            return {"ok": True, "message": "测试邮件已发送"}
        except Exception:
            return error("SMTP_ERROR", "测试邮件发送失败", 503)

    @app.get("/api/{path:path}")
    def api_not_found(path: str) -> JSONResponse:
        return error("NOT_FOUND", "接口不存在", 404)

    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if dist.is_dir():
        async def spa(path: str = "") -> FileResponse:
            requested = (dist / path).resolve()
            try:
                requested.relative_to(dist.resolve())
            except ValueError:
                requested = dist / "index.html"
            if not requested.is_file():
                requested = dist / "index.html"
            return FileResponse(str(requested))

        app.add_api_route("/{path:path}", spa, methods=["GET"])
    return app


app = create_app()


async def _read_upload_limited(upload: UploadFile, limit: int) -> Optional[bytes]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _is_private_path(path: str, storage_dir: str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(storage_dir).expanduser().resolve())
        return True
    except ValueError:
        return False

