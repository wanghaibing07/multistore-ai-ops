from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import csv
from datetime import date, datetime, timezone
import io
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.routing import Match

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
    Channel16688RestockRequest,
    IssueRequest,
    RedeemRequest,
    RetryRequest,
    RevokeRequest,
    SkuPatchRequest,
    UpstreamRefundRequest,
    WarrantyAckRequest,
    WarrantyForm,
)
from .service import (
    CANBOSO_CANCELLED_STATUSES,
    COMPLETED_ADMIN_SQL,
    DISPATCHED_UNUSED_SQL,
    IN_STOCK_SQL,
    MANUAL_ATTENTION_SQL,
    NON_IN_STOCK_SQL,
    PENDING_WARRANTY_ACK_SQL,
    Mailer,
    SKUS,
    Service,
)


logger = logging.getLogger(__name__)

RECONCILIATION_CSV_COLUMNS = [
    "采购单号",
    "来源",
    "商品名称",
    "商家名称",
    "数量",
    "原币币种",
    "原币金额",
    "付款时间",
    "状态",
    "关联渠道",
    "关联销售单号",
    "关联子单号",
    "兑换码",
    "SKU",
]


def _parse_iso_date_param(raw: Optional[str], field_name: str) -> Optional[str]:
    if raw is None:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trimmed):
        raise ValueError(f"{field_name} 必须为 YYYY-MM-DD 格式")
    try:
        date.fromisoformat(trimmed)
    except ValueError as exc:
        raise ValueError(f"{field_name} 不是合法日期") from exc
    return trimmed


def error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error_code": code, "message": message})


def status_for(code: str) -> int:
    return {
        "CODE_NOT_FOUND": 404,
        "WARRANTY_SAME_AS_STORE_EMAIL": 400,
        "CODE_REVOKED": 409,
        "CONCURRENT_IN_PROGRESS": 409,
        "ORDER_NOT_ELIGIBLE": 409,
        "CANNOT_REVOKE_COMPLETED": 409,
        "RETRY_NOT_AVAILABLE": 409,
        "CANBOSO_ORDER_CANCELLED": 409,
        "UPSTREAM_ORDER_ACTIVE": 409,
        "NO_UPSTREAM_ORDER": 409,
        "WARRANTY_LOCKED": 409,
        "WARRANTY_NOT_APPLICABLE": 409,
        "WARRANTY_NOT_ALLOWED": 409,
        "INVALID_EMAIL": 422,
        "EMAIL_MISMATCH": 422,
        "MERCHANT_RECIPIENT_CONFIRMATION_REQUIRED": 422,
        "PREFLIGHT_REQUIRED": 422,
        "INVALID_SCREENSHOT_TYPE": 422,
        "SCREENSHOT_TOO_LARGE": 422,
        "SCREENSHOT_EMPTY": 422,
        "NOTE_TOO_LONG": 422,
        "INVALID_CHANNEL": 422,
        "GOODS_NO_REQUIRED": 422,
        "TG_NOT_CONFIGURED": 503,
        "TG_FORWARD_FAILED": 503,
    }.get(code, 503)


def create_app(
    settings: Optional[Settings] = None,
    canboso: Any = None,
    channel: Any = None,
    mailer: Optional[Mailer] = None,
    agiso: Any = None,
    tg_forwarder: Any = None,
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
    service = Service(ledger, settings, canboso, channel, mailer, agiso=agiso, tg_forwarder=tg_forwarder)

    async def poller() -> None:
        await asyncio.sleep(10)
        while True:
            try:
                recovered = await asyncio.to_thread(service.recover_stuck_purchasing)
                if recovered:
                    logger.warning("recovered stuck purchasing codes=%s", ",".join(recovered))
            except Exception as exc:
                logger.warning("stuck purchasing recovery failed error=%s", type(exc).__name__)
            rows, _ = ledger.list_codes("status='waiting_fulfillment'", (), 500, 0)
            for row in rows:
                try:
                    await asyncio.to_thread(service.poll, row["code"])
                except Exception as exc:
                    logger.warning("background poll failed code=%s error=%s", row["code"], type(exc).__name__)
                    service.alert(row["code"], "UPSTREAM_ERROR")
            await asyncio.sleep(max(10, settings.poll_interval_seconds))

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

    async def channel16688_restocker() -> None:
        while True:
            await asyncio.sleep(max(30, settings.channel16688_restock_interval_seconds))
            if not settings.channel16688_restock_enabled or not service._channel16688_ready():
                continue
            try:
                await asyncio.to_thread(service.check_and_restock_channel16688_cards, trigger="scheduler")
            except Exception as exc:
                logger.warning("background channel16688 restock failed error=%s", type(exc).__name__)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = [asyncio.create_task(poller())]
        if settings.agiso_poll_enabled:
            tasks.append(asyncio.create_task(agiso_poller()))
        if settings.agiso_restock_enabled:
            tasks.append(asyncio.create_task(agiso_restocker()))
        if settings.channel16688_restock_enabled:
            tasks.append(asyncio.create_task(channel16688_restocker()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Plan 2 Redeem Service", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.ledger, app.state.service, app.state.mailer, app.state.agiso = settings, ledger, service, mailer, agiso
    app.state.tg_forwarder = service.tg_forwarder

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error_code" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"error_code": "HTTP_ERROR", "message": str(exc.detail)})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_, exc: RequestValidationError) -> JSONResponse:
        def _sanitize(obj: Any) -> Any:
            if isinstance(obj, float) and not math.isfinite(obj):
                return str(obj)
            if isinstance(obj, dict):
                return {key: _sanitize(val) for key, val in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_sanitize(item) for item in obj]
            return obj

        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(_sanitize(exc.errors()))})

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
        result, err_code = svc.redeem(
            code, body.buyer_email, body.buyer_email_confirm, body.preflight_confirmed,
        )
        if err_code:
            messages = {
                "CODE_NOT_FOUND": "没找到这个兑换码，请核对后重试",
                "CODE_REVOKED": "这个兑换码已作废，有问题请联系客服",
                "CONCURRENT_IN_PROGRESS": "正在处理中，请等几秒刷新看看",
                "ORDER_NOT_ELIGIBLE": "这个兑换码关联的交易暂不可提货，请联系客服核对",
                "CHANNEL16688_VERIFICATION_FAILED": "这个兑换码暂时无法确认可用，请稍后重试或联系客服",
                "CHANNEL16688_STOCK_IMPORT_PENDING": "这个兑换码还在核对入库结果，请联系客服",
                "INVALID_EMAIL": "请填正确的邮箱地址",
                "EMAIL_MISMATCH": "两次填的邮箱不一样，请核对后再提交",
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
    async def public_warranty(code: str, request: Request, svc: Service = Depends(get_service)) -> Any:
        content_type_header = (request.headers.get("content-type") or "").lower()
        content: Optional[bytes] = None
        upload_content_type = ""
        if "application/json" in content_type_header:
            try:
                raw_json = await request.json()
            except Exception:
                return error("INVALID_REQUEST", "请求格式不对，请刷新页面后重试", 422)
            if not isinstance(raw_json, dict):
                return error("INVALID_REQUEST", "请求格式不对，请刷新页面后重试", 422)
            try:
                payload = WarrantyForm.model_validate(raw_json)
            except ValidationError:
                return error("INVALID_EMAIL", "请填正确的邮箱地址", 422)
            admin_email = payload.admin_email
            admin_email_confirm = payload.admin_email_confirm
            note = payload.note or ""
        else:
            try:
                form = await request.form()
            except Exception:
                return error("INVALID_REQUEST", "请求格式不对，请刷新页面后重试", 422)
            raw_email = form.get("admin_email")
            if not isinstance(raw_email, str):
                return error("INVALID_EMAIL", "请填正确的邮箱地址", 422)
            admin_email = raw_email
            raw_confirm = form.get("admin_email_confirm")
            admin_email_confirm = raw_confirm if isinstance(raw_confirm, str) else None
            raw_note = form.get("note")
            note = raw_note if isinstance(raw_note, str) else ""
            screenshot = form.get("screenshot")
            if isinstance(screenshot, StarletteUploadFile):
                try:
                    content = await _read_upload_limited(screenshot, 5 * 1024 * 1024)
                    upload_content_type = screenshot.content_type or ""
                finally:
                    await screenshot.close()
                if content is None:
                    return error("SCREENSHOT_TOO_LARGE", "截图太大了，请传 5MB 以内的图片", 422)
            elif screenshot is not None and screenshot != "":
                return error("INVALID_SCREENSHOT_TYPE", "截图只支持 JPG、PNG 或 WEBP 格式", 422)

        if len(note) > 300:
            return error("NOTE_TOO_LONG", "备注最多写 300 字", 422)
        result, err_code = await asyncio.to_thread(
            svc.warranty,
            code,
            admin_email,
            admin_email_confirm,
            note,
            content,
            upload_content_type,
        )
        if err_code:
            messages = {
                "CODE_NOT_FOUND": "没找到这个兑换码，请核对后重试",
                "WARRANTY_LOCKED": "保修已经登记完，不能再改了，有变化请联系客服",
                "INVALID_EMAIL": "请填正确的邮箱地址",
                "EMAIL_MISMATCH": "两次填的邮箱不一样，请核对后再提交",
                "WARRANTY_SAME_AS_STORE_EMAIL": "这里请填你自己的常用邮箱（家庭组组长邮箱），不要填我们发给你的商店会员邮箱",
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
    def list_codes(
        status: Optional[str] = None,
        channel: Optional[str] = None,
        sku_key: Optional[str] = None,
        q: Optional[str] = None,
        error_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        svc: Service = Depends(get_service),
    ) -> Any:
        try:
            parsed_start = _parse_iso_date_param(start_date, "start_date")
            parsed_end = _parse_iso_date_param(end_date, "end_date")
        except ValueError as exc:
            return error("INVALID_DATE", str(exc), 400)
        normalized_status = (status or "").strip()
        clauses, params = [], []
        if normalized_status == "in_stock":
            clauses.append(IN_STOCK_SQL)
            order_by = "created_at_desc"
        else:
            order_by = "used_at_desc"
            if normalized_status == "dispatched_unused":
                clauses.append(DISPATCHED_UNUSED_SQL)
            elif normalized_status == "manual_attention":
                clauses.append(MANUAL_ATTENTION_SQL)
            elif normalized_status == "pending_warranty_ack":
                clauses.append(PENDING_WARRANTY_ACK_SQL)
            elif normalized_status == "completed":
                clauses.append(COMPLETED_ADMIN_SQL)
            elif normalized_status:
                clauses.append("status=?")
                params.append(normalized_status)
            else:
                clauses.append(NON_IN_STOCK_SQL)
        for field, value in (("channel", channel), ("sku_key", sku_key)):
            if value:
                clauses.append(field + "=?"); params.append(value)
        if error_code and error_code.strip():
            clauses.append("last_error_code=?")
            params.append(error_code.strip())
        if parsed_start:
            clauses.append("substr(COALESCE(NULLIF(submitted_at, ''), created_at), 1, 10) >= ?")
            params.append(parsed_start)
        if parsed_end:
            clauses.append("substr(COALESCE(NULLIF(submitted_at, ''), created_at), 1, 10) <= ?")
            params.append(parsed_end)
        if q and q.strip():
            pattern = f"%{q.strip()}%"
            clauses.append("(code LIKE ? OR external_order_id LIKE ? OR buyer_email LIKE ? OR upstream_order_code LIKE ? OR remark LIKE ? OR warranty_json LIKE ? OR delivery_json LIKE ?)")
            params.extend([pattern] * 7)
        rows, total = ledger.list_codes(
            " AND ".join(clauses),
            tuple(params),
            page_size,
            (page - 1) * page_size,
            order_by=order_by,
        )
        sku_map = ledger.get_all_skus()
        review_codes = [row["code"] for row in rows if row["status"] == "needs_manual_review"]
        disputed_codes = ledger.list_disputed_16688_codes(review_codes)
        return {
            "items": [svc.admin_summary(row, sku_map=sku_map, disputed_codes=disputed_codes) for row in rows],
            "total": total, "page": page, "page_size": page_size,
        }

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
        svc.poll(code, allow_manual_review=True)
        return svc.admin_detail(ledger.get_code(code))

    @app.post("/api/v1/admin/codes/{code}/revoke", dependencies=[Depends(require_admin)])
    def revoke(code: str, body: RevokeRequest, svc: Service = Depends(get_service)) -> Any:
        outcome = svc.ledger.revoke_code(
            code,
            body.reason,
            force=body.force,
            cancelled_statuses=CANBOSO_CANCELLED_STATUSES,
        )
        if outcome == "CODE_NOT_FOUND":
            return error("CODE_NOT_FOUND", "没找到这个兑换码", 404)
        if outcome == "CANNOT_REVOKE_COMPLETED":
            return error("CANNOT_REVOKE_COMPLETED", "这笔单已经完成了，不能作废", 409)
        if outcome == "UPSTREAM_ORDER_ACTIVE":
            return error(
                "UPSTREAM_ORDER_ACTIVE",
                "这笔单已在上游下单扣款且未取消，默认禁止直接作废；如已核对上游退款或确认要强制本地作废，请开启强制确认",
                409,
            )
        if outcome != "OK":
            return error("CONCURRENT_IN_PROGRESS", "订单正在处理，请等下单结果落账后再作废", 409)
        svc.ledger.mark_agiso_delivery_revoked(code)
        return svc.admin_detail(ledger.get_code(code))

    @app.post("/api/v1/admin/codes/{code}/retry", dependencies=[Depends(require_admin)])
    def retry(code: str, body: RetryRequest, svc: Service = Depends(get_service)) -> Any:
        row = ledger.get_code(code)
        if not row:
            return error("CODE_NOT_FOUND", "没找到这个兑换码", 404)
        if row["status"] != "needs_manual_review":
            return svc.admin_detail(row)
        detail, err_code = svc.replay(
            code, body.override_price_limit, body.note, body.merchant_verified_order_no,
            body.merchant_verified_recipient_email,
            force_new_order=body.force_new_order,
            new_buyer_email=body.new_buyer_email,
        )
        if err_code:
            messages = {
                "RETRY_NOT_AVAILABLE": "这笔单已有上游订单、正在处理，或没有可重试的原始请求，请核对详情",
                "UPSTREAM_ORDER_ACTIVE": "原上游订单尚未取消，禁止重下新单（防止重复扣款）；请先点“同步上游状态”确认已取消",
                "INVALID_EMAIL": "新的收件邮箱格式不对，请检查后再试",
                "ORDER_NOT_ELIGIBLE": "16688 单号或订单状态不符合领货条件，已拦截重试",
                "CHANNEL16688_OPENAPI_PAUSED": "16688 暂停 OpenAPI，请在商家后台核验订单后再试",
                "MERCHANT_RECIPIENT_CONFIRMATION_REQUIRED": "请和买家核对收邀请邮箱，并确认它与原请求一致。邮箱不一致时请停止续单。",
                "PRICE_LIMIT_EXCEEDED": "上游现价还是高于上限，确认接受再勾选放行重试",
                "LOW_BALANCE": "Canboso 余额不够，充值后再试",
                "UPSTREAM_OUT_OF_STOCK": "上游暂时没货，等补货后再试",
                "UPSTREAM_ERROR": "上游暂时连不上，请稍后再试",
                "UPSTREAM_TIMEOUT": "Canboso 下单超时，请在订单详情核对状态。重试会沿用原请求。",
                "CANBOSO_PURCHASE_UNCONFIRMED": "Canboso 未确认下单，请查看订单详情。重试会沿用原请求。",
                "CANBOSO_ORDER_CANCELLED": "Canboso 返回的订单已终止，请核对钱包退款和买家交付。",
            }
            return error(err_code, messages.get(err_code, "重试没成功，点开详情看看原因"), status_for(err_code))
        return detail

    @app.post("/api/v1/admin/codes/{code}/upstream-refund", dependencies=[Depends(require_admin)])
    def upstream_refund(code: str, body: UpstreamRefundRequest, svc: Service = Depends(get_service)) -> Any:
        detail, err_code = svc.record_upstream_refund(code, body.upstream_refund_usd, body.note)
        if err_code:
            messages = {
                "CODE_NOT_FOUND": "没找到这个兑换码",
                "NO_UPSTREAM_ORDER": "这笔单还没有关联上游采购单号，无法登记上游退款金额",
            }
            return error(err_code, messages.get(err_code, "登记上游退款失败"), status_for(err_code))
        return detail

    @app.post("/api/v1/admin/codes/{code}/warranty-ack", dependencies=[Depends(require_admin)])
    def warranty_ack(code: str, body: WarrantyAckRequest, svc: Service = Depends(get_service)) -> Any:
        detail, err_code = svc.warranty_ack(code, body.note)
        if err_code:
            messages = {
                "CODE_NOT_FOUND": "没找到这个兑换码",
                "WARRANTY_NOT_APPLICABLE": "这个商品不用登记保修报备",
                "WARRANTY_NOT_ALLOWED": "买家还没提交保修邮箱或订单状态不符合，暂时不能确认已报备",
            }
            return error(err_code, messages.get(err_code, "确认保修报备失败"), status_for(err_code))
        return detail

    @app.post("/api/v1/admin/codes/{code}/warranty-forward", dependencies=[Depends(require_admin)])
    async def warranty_forward(code: str, svc: Service = Depends(get_service)) -> Any:
        result, err_code, err_detail = await asyncio.to_thread(svc.warranty_forward, code)
        if err_code:
            messages = {
                "CODE_NOT_FOUND": "没找到这个兑换码",
                "WARRANTY_NOT_APPLICABLE": "这个商品不用登记保修报备",
                "WARRANTY_NOT_ALLOWED": "买家还没提交保修邮箱，暂时不能报备",
                "WARRANTY_LOCKED": "保修已经完成上游报备，不能重复发送",
                "TG_NOT_CONFIGURED": "Telegram 自动报备未启用或配置不完整",
                "TG_FORWARD_FAILED": f"Telegram 报备发送失败：{err_detail}" if err_detail else "Telegram 报备发送失败，请稍后重试或改用手动复制话术",
            }
            return error(err_code, messages.get(err_code, "Telegram 报备发送失败，请稍后重试"), status_for(err_code))
        return result

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

    @app.get("/api/v1/admin/channel16688/stock", dependencies=[Depends(require_admin)])
    def admin_channel16688_stock(
        sku_keys: Optional[str] = Query(default=None),
        svc: Service = Depends(get_service),
    ) -> Any:
        parsed_skus = [item.strip() for item in sku_keys.split(",") if item.strip()] if sku_keys else None
        try:
            return svc.get_channel16688_stock(sku_keys=parsed_skus)
        except UpstreamTimeout as exc:
            return error("CHANNEL_16688_TIMEOUT", str(exc) or "16688 查询库存超时", 504)
        except UpstreamError as exc:
            return error("CHANNEL_16688_ERROR", exc.message or "16688 查询库存失败", 503)

    @app.post("/api/v1/admin/channel16688/restock", dependencies=[Depends(require_admin)])
    def admin_channel16688_restock(
        body: Optional[Channel16688RestockRequest] = None,
        svc: Service = Depends(get_service),
    ) -> Any:
        req = body or Channel16688RestockRequest()
        try:
            return svc.check_and_restock_channel16688_cards(
                sku_keys=req.sku_keys,
                dry_check=req.dry_check,
                sync_sold=req.sync_sold,
                trigger="manual",
            )
        except UpstreamTimeout as exc:
            return error("CHANNEL_16688_TIMEOUT", str(exc) or "16688 自动补卡超时", 504)
        except UpstreamError as exc:
            return error("CHANNEL_16688_ERROR", exc.message or "16688 自动补卡失败", 503)

    @app.get("/api/v1/admin/reconciliation-export", dependencies=[Depends(require_admin)])
    def admin_reconciliation_export(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        channel: Optional[str] = None,
        format: str = "json",
        exclude_inactive: bool = False,
    ) -> Any:
        try:
            parsed_start = _parse_iso_date_param(start_date, "start_date")
            parsed_end = _parse_iso_date_param(end_date, "end_date")
        except ValueError as exc:
            return error("INVALID_DATE", str(exc), 400)

        export_format = (format or "").strip().lower()
        if export_format not in ("json", "csv"):
            return error("INVALID_FORMAT", "format 仅支持 json 或 csv", 400)

        items = ledger.list_reconciliation_rows(
            start_date=parsed_start,
            end_date=parsed_end,
            channel=channel,
            exclude_inactive=exclude_inactive,
        )
        if export_format == "json":
            return {"source": "tg_fatbunny", "count": len(items), "items": items}

        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.DictWriter(buf, fieldnames=RECONCILIATION_CSV_COLUMNS)
        writer.writeheader()

        def _csv_safe(value: Any, *, allow_numeric_sign: bool = False) -> str:
            text = "" if value is None else str(value)
            if not text:
                return ""
            if allow_numeric_sign:
                stripped = text.lstrip()
                if stripped and stripped[0] in ("+", "-"):
                    try:
                        float(stripped)
                        return text
                    except ValueError:
                        pass
            if text[0] in ("=", "+", "-", "@", "\t", "\r"):
                return f"'{text}"
            return text

        for item in items:
            cost_usd = item["cost_usd"]
            writer.writerow(
                {
                    "采购单号": _csv_safe(item["upstream_order_code"]),
                    "来源": _csv_safe(item["source"]),
                    "商品名称": _csv_safe(item["sku_title"]),
                    "商家名称": _csv_safe(item["seller"]),
                    "数量": _csv_safe(item["quantity"], allow_numeric_sign=True),
                    "原币币种": "USD",
                    "原币金额": _csv_safe(f"{cost_usd:.4f}" if cost_usd is not None else "", allow_numeric_sign=True),
                    "付款时间": _csv_safe(item["submitted_at"] or item["created_at"]),
                    "状态": _csv_safe(item["status"]),
                    "关联渠道": _csv_safe(item["channel"]),
                    "关联销售单号": _csv_safe(item["external_order_id"]),
                    "关联子单号": _csv_safe(item["agiso_oid"]),
                    "兑换码": _csv_safe(item["code"]),
                    "SKU": _csv_safe(item["sku_key"]),
                }
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="plan2-reconciliation.csv"'},
        )

    async def api_fallback(request: Request, path: str = "") -> Response:
        allowed_methods: set[str] = set()
        for route in app.routes:
            if getattr(route, "endpoint", None) is api_fallback:
                continue
            if not str(getattr(route, "path", "")).startswith("/api"):
                continue
            match, _ = route.matches(request.scope)
            if match == Match.PARTIAL:
                allowed_methods.update(getattr(route, "methods", set()) or set())
        if allowed_methods:
            return JSONResponse(
                status_code=405,
                content={"error_code": "METHOD_NOT_ALLOWED", "message": "该接口不支持当前请求方法"},
                headers={"Allow": ", ".join(sorted(allowed_methods))},
            )
        return error("NOT_FOUND", "没有这个接口", 404)

    _api_fallback_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    app.add_api_route("/api", api_fallback, methods=_api_fallback_methods, include_in_schema=False)
    app.add_api_route("/api/{path:path}", api_fallback, methods=_api_fallback_methods, include_in_schema=False)

    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if dist.is_dir():
        async def spa(request: Request, path: str = "") -> Response:
            if path == "api" or path.startswith("api/"):
                return error("NOT_FOUND", "没有这个接口", 404)
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

