from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .agiso import (
    AOPIC_AUTO_SEND_SUCCESS,
    AgisoClient,
    AgisoError,
    DryRunAgisoClient,
    build_redeem_url,
    format_delivery_message,
    verify_agiso_push_signature,
)
from .config import Settings
from .db import Ledger
from .gateways import CanbosoClient, Channel16688Client, DryRun16688, DryRunCanboso, UpstreamError, UpstreamTimeout
from .models import (
    AgisoOrderRetryRequest,
    AgisoPushCardsRequest,
    AgisoRestockRequest,
    AgisoSkuMappingRequest,
    AgisoSyncOrdersRequest,
    BatchRequest,
    IssueRequest,
    RedeemRequest,
    RetryRequest,
    RevokeRequest,
    SkuPatchRequest,
    WarrantyAckRequest,
)
from .service import Mailer, SKUS, Service


logger = logging.getLogger(__name__)


def error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error_code": code, "message": message})


def status_for(code: str) -> int:
    return {"CODE_NOT_FOUND": 404, "CODE_REVOKED": 409, "CONCURRENT_IN_PROGRESS": 409, "ORDER_NOT_ELIGIBLE": 409, "CANNOT_REVOKE_COMPLETED": 409, "RETRY_NOT_AVAILABLE": 409, "WARRANTY_LOCKED": 409, "WARRANTY_NOT_APPLICABLE": 409, "WARRANTY_NOT_ALLOWED": 409, "INVALID_EMAIL": 422, "EMAIL_MISMATCH": 422, "PREFLIGHT_REQUIRED": 422, "INVALID_SCREENSHOT_TYPE": 422, "SCREENSHOT_TOO_LARGE": 422, "SCREENSHOT_EMPTY": 422, "NOTE_TOO_LONG": 422, "INVALID_CHANNEL": 422, "GOODS_NO_REQUIRED": 422}.get(code, 503)


def create_app(
    settings: Optional[Settings] = None,
    canboso: Any = None,
    channel: Any = None,
    mailer: Optional[Mailer] = None,
    agiso: Any = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.prepare()
    ledger = Ledger(settings.db_path)
    canboso = canboso or (
        DryRunCanboso()
        if settings.execution_mode == "dry_run"
        else CanbosoClient(
            settings.canboso_base_url,
            settings.canboso_api_secret,
            settings.canboso_timeout_seconds,
            settings.canboso_products_cache_ttl_seconds,
            settings.canboso_get_retries,
        )
    )
    channel = channel or (DryRun16688() if settings.execution_mode == "dry_run" else Channel16688Client(settings.channel16688_base_url, settings.channel16688_api_key, settings.channel16688_api_secret, settings.canboso_timeout_seconds))
    agiso = agiso or (
        DryRunAgisoClient()
        if settings.execution_mode == "dry_run"
        else AgisoClient(
            settings.agiso_api_base_url,
            settings.agiso_app_secret,
            settings.agiso_access_token,
            settings.agiso_timeout_seconds,
        )
    )
    mailer = mailer or Mailer(settings)
    service = Service(ledger, settings, canboso, channel, mailer, agiso=agiso)

    async def poller() -> None:
        while True:
            await asyncio.sleep(max(10, settings.poll_interval_seconds))
            rows, _ = ledger.list_codes("status='waiting_fulfillment'", (), 500, 0)
            for row in rows:
                try:
                    await asyncio.to_thread(service.poll, row["code"])
                except Exception as exc:
                    logger.warning("background poll failed code=%s error=%s", row["code"], type(exc).__name__)
                    service.alert(row["code"], "UPSTREAM_ERROR")

    async def agiso_poller() -> None:
        while True:
            await asyncio.sleep(max(15, settings.agiso_poll_interval_seconds))
            if not settings.agiso_poll_enabled or not getattr(agiso, "is_ready", False):
                continue
            try:
                await asyncio.to_thread(service.sync_agiso_orders)
            except Exception as exc:
                logger.warning("background agiso sync failed error=%s", type(exc).__name__)
                hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
                detail = str(exc)[:300]

                def _record_poll_error() -> None:
                    ledger.record_agiso_event(
                        source="poll",
                        from_platform="TbAlds",
                        aopic=None,
                        tid="",
                        oid="",
                        status="poll_error",
                        detail=detail,
                    )
                    service._agiso_alert(
                        f"poll-error:{hour_bucket}",
                        "阿奇索定时补漏巡检失败（需排查）",
                        f"后台定时同步阿奇索订单失败（{type(exc).__name__}）：{detail}",
                    )

                try:
                    await asyncio.to_thread(_record_poll_error)
                except Exception:
                    logger.warning("recording agiso poll error failed")

    async def agiso_restocker() -> None:
        while True:
            await asyncio.sleep(max(30, settings.agiso_restock_interval_seconds))
            if not settings.agiso_restock_enabled or not getattr(agiso, "is_ready", False):
                continue
            try:
                await asyncio.to_thread(service.check_and_restock_agiso_cards, trigger="scheduler")
            except Exception as exc:
                logger.warning("background agiso restock failed error=%s", type(exc).__name__)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = [asyncio.create_task(poller())]
        if settings.agiso_poll_enabled:
            tasks.append(asyncio.create_task(agiso_poller()))
        if settings.agiso_restock_enabled:
            tasks.append(asyncio.create_task(agiso_restocker()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Plan 2 Redeem Service", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.ledger, app.state.service, app.state.mailer, app.state.agiso = settings, ledger, service, mailer, agiso

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
            raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "message": "后台密钥不对，请重新登录"})

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "execution_mode": settings.execution_mode}

    @app.post("/api/v1/webhooks/agiso")
    async def agiso_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        svc: Service = Depends(get_service),
    ) -> PlainTextResponse:
        if not settings.agiso_app_secret.strip():
            return PlainTextResponse("服务未配置", status_code=503)
        try:
            form = await request.form()
        except Exception:
            form = {}
        from_platform = (request.query_params.get("fromPlatform") or str(form.get("fromPlatform") or "")).strip()
        timestamp_raw = (request.query_params.get("timestamp") or str(form.get("timestamp") or "")).strip()
        aopic_raw = (request.query_params.get("aopic") or str(form.get("aopic") or "")).strip()
        sign = (request.query_params.get("sign") or str(form.get("sign") or "")).strip()
        raw_json_value = form.get("json")
        if raw_json_value is None:
            raw_json_value = request.query_params.get("json")
        raw_json = str(raw_json_value) if raw_json_value is not None else ""

        try:
            ts_int = int(timestamp_raw)
        except ValueError:
            ledger.record_agiso_event(
                source="webhook",
                from_platform=from_platform,
                aopic=None,
                tid="",
                oid="",
                status="invalid_timestamp",
                detail="缺少或非法 timestamp",
            )
            return PlainTextResponse("时间戳无效", status_code=403)

        if settings.agiso_timestamp_tolerance_seconds > 0:
            skew = abs(int(time.time()) - ts_int)
            if skew > settings.agiso_timestamp_tolerance_seconds:
                ledger.record_agiso_event(
                    source="webhook",
                    from_platform=from_platform,
                    aopic=int(aopic_raw) if aopic_raw.isdigit() else None,
                    tid="",
                    oid="",
                    status="timestamp_expired",
                    detail=f"时间戳偏差 {skew}s 超出允许范围 {settings.agiso_timestamp_tolerance_seconds}s",
                )
                return PlainTextResponse("时间戳无效", status_code=403)

        if not verify_agiso_push_signature(raw_json, timestamp_raw, sign, settings.agiso_app_secret.strip()):
            ledger.record_agiso_event(
                source="webhook",
                from_platform=from_platform,
                aopic=int(aopic_raw) if aopic_raw.isdigit() else None,
                tid="",
                oid="",
                status="invalid_signature",
                detail="推送签名校验未通过",
            )
            return PlainTextResponse("验签失败", status_code=403)

        try:
            aopic = int(aopic_raw)
        except ValueError:
            ledger.record_agiso_event(
                source="webhook",
                from_platform=from_platform,
                aopic=None,
                tid="",
                oid="",
                status="invalid_aopic",
                detail=f"非法 aopic: {aopic_raw[:32]}",
            )
            return PlainTextResponse("参数错误", status_code=400)

        try:
            payload = json.loads(raw_json)
        except ValueError:
            ledger.record_agiso_event(
                source="webhook",
                from_platform=from_platform,
                aopic=aopic,
                tid="",
                oid="",
                status="invalid_json",
                detail="json 参数解析失败",
            )
            return PlainTextResponse("参数错误", status_code=400)
        if not isinstance(payload, dict):
            ledger.record_agiso_event(
                source="webhook",
                from_platform=from_platform,
                aopic=aopic,
                tid="",
                oid="",
                status="invalid_json",
                detail="json 参数必须为对象",
            )
            return PlainTextResponse("参数错误", status_code=400)

        status_code, body_text, outcome = await asyncio.to_thread(
            svc.handle_agiso_webhook,
            from_platform=from_platform,
            aopic=aopic,
            payload=payload,
            timestamp=timestamp_raw,
            sign=sign,
        )
        if (
            status_code == 200
            and aopic == AOPIC_AUTO_SEND_SUCCESS
            and settings.agiso_restock_enabled
            and getattr(agiso, "is_ready", False)
            and isinstance(outcome, dict)
            and outcome.get("bound_sku_keys")
        ):
            bound_sku_keys = list(outcome["bound_sku_keys"])

            def _run_webhook_restock() -> None:
                try:
                    svc.check_and_restock_agiso_cards(sku_keys=bound_sku_keys, trigger="webhook_2048")
                except Exception as exc:
                    logger.warning(
                        "webhook-triggered agiso restock failed skus=%s error=%s",
                        bound_sku_keys,
                        type(exc).__name__,
                    )

            background_tasks.add_task(_run_webhook_restock)
        return PlainTextResponse(body_text, status_code=status_code)

    @app.get("/api/v1/public/codes/{code}")
    def public_code(code: str, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        return svc.public_row(row) if row else error("CODE_NOT_FOUND", "没找到这个兑换码，请核对后重试", 404)

    @app.post("/api/v1/public/codes/{code}/redeem")
    def public_redeem(code: str, body: RedeemRequest, svc: Service = Depends(get_service)) -> Any:
        result, err_code = svc.redeem(code, body.buyer_email, body.buyer_email_confirm, body.preflight_confirmed)
        if err_code:
            messages = {
                "CODE_NOT_FOUND": "没找到这个兑换码，请核对后重试",
                "CODE_REVOKED": "这个兑换码已作废，有问题请联系客服",
                "CONCURRENT_IN_PROGRESS": "正在处理中，请等几秒刷新看看",
                "ORDER_NOT_ELIGIBLE": "这笔订单暂时领不了，请确认已付款且没退款",
                "INVALID_EMAIL": "请填正确的 Gmail 邮箱（如 xxx@gmail.com）",
                "EMAIL_MISMATCH": "两次填的 Gmail 不一样，请核对后再提交",
                "PREFLIGHT_REQUIRED": "请先勾选上面的确认项再提交",
            }
            return error(err_code, messages.get(err_code, "现在暂时处理不了，请稍后再试或联系客服"), status_for(err_code))
        return result

    @app.post("/api/v1/public/codes/{code}/refresh")
    def public_refresh(code: str, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "没找到这个兑换码，请核对后重试", 404)
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
    async def public_warranty(code: str, admin_email: str = Form(...), admin_email_confirm: Optional[str] = Form(default=None), note: str = Form(""), screenshot: UploadFile = File(...), svc: Service = Depends(get_service)) -> Any:
        if len(note) > 300:
            return error("NOTE_TOO_LONG", "备注最多写 300 字", 422)
        content = await _read_upload_limited(screenshot, 5 * 1024 * 1024)
        if content is None:
            return error("SCREENSHOT_TOO_LARGE", "截图太大了，请传 5MB 以内的图片", 422)
        result, err_code = svc.warranty(code, admin_email, admin_email_confirm, note, content, screenshot.content_type or "")
        if err_code:
            messages = {
                "CODE_NOT_FOUND": "没找到这个兑换码，请核对后重试",
                "WARRANTY_LOCKED": "保修已经登记完，不能再改了，有变化请联系客服",
                "INVALID_EMAIL": "请填正确的 Gmail 邮箱（如 xxx@gmail.com）",
                "EMAIL_MISMATCH": "两次填的 Gmail 不一样，请核对后再提交",
                "INVALID_SCREENSHOT_TYPE": "截图只支持 JPG、PNG 或 WEBP 格式",
                "SCREENSHOT_TOO_LARGE": "截图太大了，请传 5MB 以内的图片",
                "SCREENSHOT_EMPTY": "截图文件是空的，请重新上传",
                "WARRANTY_NOT_APPLICABLE": "这个商品不用在这里登记保修",
                "WARRANTY_NOT_ALLOWED": "还没到登记保修这一步，请先领账号",
            }
            return error(err_code, messages.get(err_code, "现在提交不了保修，请刷新页面后再试"), status_for(err_code))
        return result

    @app.get("/api/v1/admin/overview", dependencies=[Depends(require_admin)])
    def overview(svc: Service = Depends(get_service)) -> Any:
        try:
            return svc.overview()
        except Exception as exc:
            logger.warning("admin overview failed error_type=%s detail=%s", type(exc).__name__, exc)
            return error("UPSTREAM_ERROR", "上游暂时连不上，请稍后再试", 503)

    @app.post("/api/v1/admin/upstream/refresh", dependencies=[Depends(require_admin)])
    def refresh(svc: Service = Depends(get_service)) -> Any:
        try:
            return svc.overview(force_refresh=True)
        except Exception as exc:
            logger.warning("admin upstream refresh failed error_type=%s detail=%s", type(exc).__name__, exc)
            return error("UPSTREAM_ERROR", "上游暂时连不上，请稍后再试", 503)

    @app.post("/api/v1/admin/codes/issue", status_code=201, dependencies=[Depends(require_admin)])
    def issue(body: IssueRequest, svc: Service = Depends(get_service)) -> Any:
        if body.channel not in {"taobao", "16688"}:
            return error("INVALID_CHANNEL", "渠道不对，只能选 taobao 或 16688", 422)
        try:
            row = svc.issue(body.sku_key, body.channel, body.external_order_id, body.remark)
        except ValueError as e:
            return error(str(e), "找不到这个商品 SKU", 422)
        sku = SKUS[body.sku_key]
        url = build_redeem_url(settings.public_base_url, row["code"])
        wangwang_message = format_delivery_message(sku["title"], sku["product_type"], row["code"], url)
        return {"code": row["code"], "redeem_url": url, "wangwang_message": wangwang_message, "item": svc.admin_summary(row)}

    @app.post("/api/v1/admin/codes/batch", status_code=201, dependencies=[Depends(require_admin)])
    def batch(body: BatchRequest, svc: Service = Depends(get_service)) -> Any:
        if body.channel not in {"taobao", "16688"}:
            return error("INVALID_CHANNEL", "渠道不对，只能选 taobao 或 16688", 422)
        try:
            return svc.batch(body.sku_key, body.channel, body.count, body.goods_no, body.batch_label)
        except ValueError as e:
            batch_messages = {
                "COUNT_OUT_OF_RANGE": "数量要在 1 到 500 之间",
                "UNKNOWN_SKU": "找不到这个商品 SKU",
                "GOODS_NO_REQUIRED": "16688 批量制码需要先填商品编号",
            }
            return error(str(e), batch_messages.get(str(e), "批量制码参数不对，请检查后再试"), 422)
        except UpstreamTimeout as exc:
            logger.warning(
                "admin batch 16688 timeout sku=%s goods_no=%s detail=%s",
                body.sku_key,
                body.goods_no or "",
                exc,
            )
            return error(
                "CHANNEL_16688_TIMEOUT",
                "16688 卡密导入超时，本地已先保留本次兑换码（备注待核对库存），请先去 16688 后台确认是否已导入成功，避免重复制码",
                504,
            )
        except UpstreamError as exc:
            logger.warning(
                "admin batch 16688 failed sku=%s goods_no=%s status=%s rejected=%s detail=%s",
                body.sku_key,
                body.goods_no or "",
                exc.status_code,
                exc.rejected,
                exc.message,
            )
            detail = exc.message or "16688 接口异常"
            if exc.rejected:
                status = 422 if 400 <= exc.status_code < 500 else 502
                hint = ""
                if "卡密" in detail or "自动发货" in detail:
                    hint = "。请确认该 16688 商品的发货方式为「卡密/自动发货」，或改选「淘宝提前备码」"
                return error(
                    "CHANNEL_16688_REJECTED",
                    f"16688 拒绝导入卡密：{detail}{hint}",
                    status,
                )
            return error(
                "CHANNEL_16688_ERROR",
                f"16688 卡密导入结果异常（{detail}），本地已先保留本次兑换码（备注待核对库存），请先去 16688 后台核对是否已导入",
                502,
            )

    @app.get("/api/v1/admin/codes", dependencies=[Depends(require_admin)])
    def list_codes(status: Optional[str] = None, channel: Optional[str] = None, sku_key: Optional[str] = None, q: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), svc: Service = Depends(get_service)) -> Any:
        clauses, params = [], []
        if status == "in_stock":
            clauses.append("status='unused' AND COALESCE(TRIM(external_order_id), '') = ''")
        elif status == "dispatched_unused":
            clauses.append("status='unused' AND COALESCE(TRIM(external_order_id), '') <> ''")
        elif status:
            clauses.append("status=?")
            params.append(status)
        for field, value in (("channel", channel), ("sku_key", sku_key)):
            if value:
                clauses.append(field + "=?"); params.append(value)
        if q and q.strip():
            pattern = f"%{q.strip()}%"
            clauses.append("(code LIKE ? OR external_order_id LIKE ? OR buyer_email LIKE ? OR upstream_order_code LIKE ? OR remark LIKE ? OR warranty_json LIKE ? OR delivery_json LIKE ?)")
            params.extend([pattern] * 7)
        rows, total = ledger.list_codes(" AND ".join(clauses), tuple(params), page_size, (page - 1) * page_size)
        return {"items": [svc.admin_summary(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    @app.get("/api/v1/admin/codes/{code}", dependencies=[Depends(require_admin)])
    def admin_code(code: str, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        return svc.admin_detail(row) if row else error("CODE_NOT_FOUND", "没找到这个兑换码", 404)

    @app.get("/api/v1/admin/codes/{code}/warranty-screenshot", dependencies=[Depends(require_admin)])
    def screenshot(code: str) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "没找到这个兑换码", 404)
        path = row.get("screenshot_path")
        if not path or not _is_private_path(path, settings.storage_dir) or not Path(path).is_file():
            return error("SCREENSHOT_NOT_FOUND", "没找到这张保修截图", 404)
        return FileResponse(path, media_type=row.get("screenshot_content_type") or "application/octet-stream")

    @app.post("/api/v1/admin/codes/{code}/poll", dependencies=[Depends(require_admin)])
    def admin_poll(code: str, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "没找到这个兑换码", 404)
        svc.poll(code)
        return svc.admin_detail(ledger.get_code(code))

    @app.post("/api/v1/admin/codes/{code}/revoke", dependencies=[Depends(require_admin)])
    def revoke(code: str, body: RevokeRequest, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "没找到这个兑换码", 404)
        if row["status"] == "completed":
            return error("CANNOT_REVOKE_COMPLETED", "这笔单已经完成了，不能作废", 409)
        if svc.ledger.transition(code, "revoked", body.reason, {"last_error_code": "REVOKED"}):
            svc.ledger.mark_agiso_delivery_revoked(code)
        return svc.admin_detail(ledger.get_code(code))

    @app.post("/api/v1/admin/codes/{code}/retry", dependencies=[Depends(require_admin)])
    def retry(code: str, body: RetryRequest, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "没找到这个兑换码", 404)
        if row["status"] != "needs_manual_review":
            return svc.admin_detail(row)
        _, err_code = svc.replay(code, body.override_price_limit, body.note)
        if err_code:
            messages = {
                "RETRY_NOT_AVAILABLE": "这笔单没有可重试的原始请求，请先核对详情",
                "PRICE_LIMIT_EXCEEDED": "上游现价还是高于上限，确认接受再勾选放行重试",
                "LOW_BALANCE": "Canboso 余额不够，充值后再试",
                "UPSTREAM_OUT_OF_STOCK": "上游暂时没货，等补货后再试",
                "UPSTREAM_ERROR": "上游暂时连不上，请稍后再试",
            }
            return error(err_code, messages.get(err_code, "重试没成功，点开详情看看原因"), status_for(err_code))
        return svc.admin_detail(ledger.get_code(code))

    @app.post("/api/v1/admin/codes/{code}/warranty-ack", dependencies=[Depends(require_admin)])
    def warranty_ack(code: str, body: WarrantyAckRequest, svc: Service = Depends(get_service)) -> Any:
        if not ledger.get_code(code):
            return error("CODE_NOT_FOUND", "没找到这个兑换码", 404)
        return svc.warranty_ack(code, body.note)

    @app.patch("/api/v1/admin/settings/skus/{sku_key}", dependencies=[Depends(require_admin)])
    def patch_sku(sku_key: str, body: SkuPatchRequest, svc: Service = Depends(get_service)) -> Any:
        current = ledger.get_sku(sku_key)
        if not current:
            return error("UNKNOWN_SKU", "找不到这个商品 SKU", 404)
        goods_no = current.get("goods_no")
        if "goods_no" in body.model_fields_set:
            goods_no = body.goods_no.strip() if body.goods_no and body.goods_no.strip() else None
        with ledger.transaction(immediate=True) as c:
            c.execute("UPDATE sku_settings SET max_cost_usd=?, goods_no=? WHERE sku_key=?", (body.max_cost_usd, goods_no, sku_key))
        try:
            return svc.sku_status(sku_key)
        except Exception as exc:
            logger.warning("admin patch_sku status lookup failed sku=%s error_type=%s detail=%s", sku_key, type(exc).__name__, exc)
            return error("UPSTREAM_ERROR", "上游暂时连不上，请稍后再试", 503)

    @app.post("/api/v1/admin/notifications/test-email", dependencies=[Depends(require_admin)])
    def test_email() -> Any:
        try:
            mailer.send("Plan 2 测试邮件", "这是一封 Plan 2 自助提货服务测试邮件。")
            return {"ok": True, "message": "测试邮件已发出，请去收件箱看看"}
        except Exception:
            return error("SMTP_ERROR", "测试邮件没发出去，请检查邮箱配置", 503)

    @app.get("/api/v1/admin/agiso/status", dependencies=[Depends(require_admin)])
    def admin_agiso_status(svc: Service = Depends(get_service)) -> Any:
        return svc.agiso_status()

    @app.get("/api/v1/admin/agiso/sku-mappings", dependencies=[Depends(require_admin)])
    def admin_agiso_list_sku_mappings(svc: Service = Depends(get_service)) -> Any:
        all_rules = svc.agiso_sku_rules(enabled_only=False)
        db_items = [rule for rule in all_rules if rule.get("source") == "db"]
        env_items = [rule for rule in all_rules if rule.get("source") == "env"]
        return {"items": db_items, "env_rules": env_items}

    @app.post("/api/v1/admin/agiso/sku-mappings", status_code=201, dependencies=[Depends(require_admin)])
    def admin_agiso_create_sku_mapping(body: AgisoSkuMappingRequest, svc: Service = Depends(get_service)) -> Any:
        try:
            return svc.create_agiso_sku_mapping(body.model_dump())
        except ValueError as exc:
            messages = {
                "UNKNOWN_SKU": "找不到这个商品 SKU",
                "MATCHER_REQUIRED": "至少需要填写一个匹配条件（num_iid / sku_id / outer_iid / outer_sku_id / sku_props_contains）",
            }
            return error(str(exc), messages.get(str(exc), "SKU 映射参数不对"), 422)

    @app.delete("/api/v1/admin/agiso/sku-mappings/{mapping_id}", dependencies=[Depends(require_admin)])
    def admin_agiso_delete_sku_mapping(mapping_id: int) -> Any:
        deleted = ledger.delete_agiso_sku_mapping(mapping_id)
        if not deleted:
            return error("MAPPING_NOT_FOUND", "没找到这条 SKU 映射规则", 404)
        return {"ok": True, "mapping_id": mapping_id}

    @app.get("/api/v1/admin/agiso/events", dependencies=[Depends(require_admin)])
    def admin_agiso_events(
        tid: Optional[str] = None,
        aopic: Optional[int] = None,
        status: Optional[str] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ) -> Any:
        clauses: list[str] = []
        params: list[Any] = []
        if tid and tid.strip():
            clauses.append("tid=?")
            params.append(tid.strip())
        if aopic is not None:
            clauses.append("aopic=?")
            params.append(aopic)
        if status and status.strip():
            clauses.append("status=?")
            params.append(status.strip())
        rows, total = ledger.list_agiso_events(" AND ".join(clauses), tuple(params), page_size, (page - 1) * page_size)
        response: dict[str, Any] = {"items": rows, "total": total, "page": page, "page_size": page_size}
        if tid and tid.strip():
            response["deliveries"] = ledger.list_agiso_deliveries_by_tid(tid.strip())
        return response

    @app.post("/api/v1/admin/agiso/orders/{tid}/retry", dependencies=[Depends(require_admin)])
    def admin_agiso_retry_order(
        tid: str,
        body: Optional[AgisoOrderRetryRequest] = None,
        svc: Service = Depends(get_service),
    ) -> Any:
        req = body or AgisoOrderRetryRequest()
        try:
            result = svc.retry_agiso_order(tid, fetch_from_agiso=req.fetch_from_agiso)
        except AgisoError as exc:
            return error("AGISO_ERROR", exc.message, 503)
        if result is None:
            return error("ORDER_NOT_FOUND", "没找到这笔阿奇索发货记录", 404)
        return result

    @app.post("/api/v1/admin/agiso/sync-orders", dependencies=[Depends(require_admin)])
    def admin_agiso_sync_orders(
        body: Optional[AgisoSyncOrdersRequest] = None,
        svc: Service = Depends(get_service),
    ) -> Any:
        req = body or AgisoSyncOrdersRequest()
        try:
            return svc.sync_agiso_orders(
                lookback_minutes=req.lookback_minutes,
                page_size=req.page_size,
                max_pages=req.max_pages,
            )
        except AgisoError as exc:
            return error("AGISO_ERROR", exc.message, 503)

    @app.get("/api/v1/admin/agiso/remote-skus", dependencies=[Depends(require_admin)])
    def admin_agiso_remote_skus(num_iids: str = Query(..., min_length=1), svc: Service = Depends(get_service)) -> Any:
        parsed = [item.strip() for item in num_iids.split(",") if item.strip()]
        if not parsed:
            return error("NUM_IIDS_REQUIRED", "请提供至少一个宝贝 ID（num_iids）", 422)
        if len(parsed) > 40:
            return error("TOO_MANY_NUM_IIDS", "单次最多查询 40 个宝贝 ID", 422)
        try:
            return {"items": svc.get_agiso_remote_skus(parsed)}
        except AgisoError as exc:
            return error("AGISO_ERROR", exc.message, 503)

    @app.post("/api/v1/admin/agiso/push-cards", status_code=201, dependencies=[Depends(require_admin)])
    def admin_agiso_push_cards(body: AgisoPushCardsRequest, svc: Service = Depends(get_service)) -> Any:
        try:
            return svc.push_agiso_cards(
                sku_key=body.sku_key,
                cpc_id=body.cpc_id,
                count=body.count,
                batch_label=body.batch_label,
                is_use_first=body.is_use_first,
                allow_replace=body.allow_replace,
            )
        except ValueError as exc:
            messages = {
                "COUNT_OUT_OF_RANGE": "数量要在 1 到 500 之间",
                "UNKNOWN_SKU": "找不到这个商品 SKU",
            }
            return error(str(exc), messages.get(str(exc), "推送卡密参数不对"), 422)
        except AgisoError as exc:
            return error("AGISO_ERROR", exc.message, 503)

    @app.get("/api/v1/admin/agiso/stock", dependencies=[Depends(require_admin)])
    def admin_agiso_stock(
        sku_keys: Optional[str] = Query(default=None),
        svc: Service = Depends(get_service),
    ) -> Any:
        parsed_skus = [item.strip() for item in sku_keys.split(",") if item.strip()] if sku_keys else None
        try:
            return svc.get_agiso_card_stock(sku_keys=parsed_skus)
        except AgisoError as exc:
            return error("AGISO_ERROR", exc.message, 503)

    @app.post("/api/v1/admin/agiso/restock", dependencies=[Depends(require_admin)])
    def admin_agiso_restock(
        body: Optional[AgisoRestockRequest] = None,
        svc: Service = Depends(get_service),
    ) -> Any:
        req = body or AgisoRestockRequest()
        try:
            return svc.check_and_restock_agiso_cards(
                sku_keys=req.sku_keys,
                dry_check=req.dry_check,
                trigger="manual",
            )
        except AgisoError as exc:
            return error("AGISO_ERROR", exc.message, 503)

    @app.get("/api/{path:path}")
    def api_not_found(path: str) -> JSONResponse:
        return error("NOT_FOUND", "没有这个接口", 404)

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

