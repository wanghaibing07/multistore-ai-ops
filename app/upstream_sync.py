from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import string
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import card_reconciliation, importer
from .models import CardPurchase, OrderItem

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CANBOSO_BASE_URL = "https://canboso.com"
DEFAULT_16688_BASE_URL = "https://www.16688.com.cn/openApi"

NONCE_ALPHABET = string.ascii_lowercase + string.digits
NONCE_PATTERN = re.compile(r"^[A-Za-z0-9]{16,64}$")

PAUSE_16688_KEYWORDS = (
    "暂停openapi",
    "暂停 open",
    "openapi暂停",
    "openapi已暂停",
    "超负载",
    "服务暂停",
    "接口维护",
    "临时关闭",
    "platform paused",
)

SENSITIVE_ORDER_KEYS = frozenset({
    "contact_email",
    "email",
    "secret",
    "secret_code",
    "secret_codes",
    "card_no",
    "card_secret",
    "cards",
    "password",
    "buyer_email",
    "buyer_phone",
    "mobile",
    "phone",
})


class UpstreamSyncError(ValueError):
    pass


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def _load_dotenv_layers() -> dict[str, str]:
    if os.environ.get("MULTISTORE_DISABLE_ENV_FALLBACK") == "1":
        return {}
    merged: dict[str, str] = {}
    for rel in (
        REPO_ROOT / "plan2_redeem_service" / ".env",
        REPO_ROOT / "plan1_auto_restock" / ".env",
        REPO_ROOT / ".env",
    ):
        merged.update(_parse_dotenv(rel))
    return merged


def _resolve_setting(keys: tuple[str, ...], dotenv_map: dict[str, str], default: str = "") -> str:
    primary = keys[0]
    if primary in os.environ:
        return os.environ[primary].strip() or default
    for key in keys[1:]:
        if key in os.environ and os.environ[key].strip():
            return os.environ[key].strip()
    for key in keys:
        val = (dotenv_map.get(key) or "").strip()
        if val:
            return val
    return default


def load_upstream_config() -> dict[str, str]:
    dotenv_map = _load_dotenv_layers()
    return {
        "canboso_base_url": _resolve_setting(
            ("MULTISTORE_CANBOSO_BASE_URL", "PLAN2_CANBOSO_BASE_URL"),
            dotenv_map,
            DEFAULT_CANBOSO_BASE_URL,
        ).rstrip("/"),
        "fatbunny_api_key": _resolve_setting(
            ("MULTISTORE_FATBUNNY_API_KEY", "CANBOSO_KEY_PRIMARY", "PLAN2_CANBOSO_API_SECRET"),
            dotenv_map,
        ),
        "chugax_api_key": _resolve_setting(
            ("MULTISTORE_CHUGAX_API_KEY", "CANBOSO_KEY_SECONDARY"),
            dotenv_map,
        ),
        "plan2_base_url": _resolve_setting(
            ("MULTISTORE_PLAN2_BASE_URL", "PLAN2_PUBLIC_BASE_URL"),
            dotenv_map,
        ).rstrip("/"),
        "plan2_admin_token": _resolve_setting(
            ("MULTISTORE_PLAN2_ADMIN_TOKEN", "PLAN2_ADMIN_TOKEN"),
            dotenv_map,
        ),
        "openapi_16688_base_url": _resolve_setting(
            ("MULTISTORE_16688_BASE_URL", "PLAN2_CHANNEL16688_BASE_URL"),
            dotenv_map,
            DEFAULT_16688_BASE_URL,
        ).rstrip("/"),
        "openapi_16688_app_id": _resolve_setting(
            ("MULTISTORE_16688_APP_ID", "PLATFORM_16688_APP_ID", "PLAN2_CHANNEL16688_API_KEY"),
            dotenv_map,
        ),
        "openapi_16688_secret": _resolve_setting(
            ("MULTISTORE_16688_SECRET", "PLATFORM_16688_SECRET", "PLAN2_CHANNEL16688_API_SECRET"),
            dotenv_map,
        ),
    }


def _sanitize_text(text: str, *secret_values: str) -> str:
    cleaned = str(text or "").strip()
    for secret_val in secret_values:
        if secret_val and len(secret_val) >= 4:
            cleaned = cleaned.replace(secret_val, "***")
    return cleaned


def _sanitize_order_meta(raw: dict[str, Any]) -> dict[str, Any]:
    cleaned = card_reconciliation.sanitize_raw_payload(raw)
    return cleaned if isinstance(cleaned, dict) else {}


def _map_plan2_channel(channel: str) -> str:
    raw = str(channel or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low in ("qianniu", "taobao", "千牛", "淘宝"):
        return "taobao"
    if low in ("16688", "16688_store", "16688自营", "16688卖货"):
        return "16688"
    return raw


def _generate_nonce(length: int = 32) -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))


def _validate_nonce(nonce: str) -> str:
    if not isinstance(nonce, str) or not NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("16688 nonce 必须是 16~64 位纯字母或数字")
    return nonce


def _split_16688_controller_action(path: str) -> tuple[str, str, str]:
    cleaned = path.strip()
    if cleaned.lower().startswith("/openapi/"):
        suffix = cleaned[len("/openapi/"):]
    elif cleaned.lower().startswith("openapi/"):
        suffix = cleaned[len("openapi/"):]
    else:
        suffix = cleaned.lstrip("/")
    segments = [segment for segment in suffix.split("/") if segment]
    if len(segments) != 2:
        raise ValueError(f"非法的 16688 OpenAPI 路径: {path!r}")
    controller_raw, action_raw = segments
    return f"/{controller_raw}/{action_raw}", controller_raw.lower(), action_raw.lower()


def _decimal_to_php_number(value: Decimal) -> int | float:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)
    return float(normalized)


def _decimal_to_php_string(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(int(normalized))
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _normalize_for_php(value: Any, *, is_nested: bool = False) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return _decimal_to_php_number(value)
    if isinstance(value, float):
        return _decimal_to_php_number(Decimal(str(value)))
    if isinstance(value, (list, tuple)):
        return [_normalize_for_php(item, is_nested=True) for item in value]
    if isinstance(value, dict):
        if is_nested and not value:
            return []
        return {
            str(dict_key): _normalize_for_php(dict_value, is_nested=True)
            for dict_key, dict_value in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    return str(value)


def _canonical_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return _decimal_to_php_string(value)
    if isinstance(value, float):
        return _decimal_to_php_string(Decimal(str(value)))
    if isinstance(value, (list, tuple, dict)):
        normalized = _normalize_for_php(value, is_nested=True)
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def build_16688_signed_payload(
    *,
    app_id: str,
    secret: str,
    path: str,
    params: dict[str, Any] | None = None,
    method: str = "POST",
    timestamp: int | str | None = None,
    nonce: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    if not app_id:
        raise ValueError("16688 app_id 不能为空")
    if not secret:
        raise ValueError("16688 secret 不能为空")
    final_nonce = _validate_nonce(nonce if nonce is not None else _generate_nonce(32))
    final_timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    payload: dict[str, Any] = {}
    if params:
        for param_key, param_value in params.items():
            if param_key == "sign" or param_value is None:
                continue
            payload[str(param_key)] = _normalize_for_php(
                param_value,
                is_nested=isinstance(param_value, (dict, list)),
            )
    payload["app_id"] = str(app_id)
    payload["timestamp"] = final_timestamp
    payload["nonce"] = final_nonce

    _, controller_lower, action_lower = _split_16688_controller_action(path)
    sorted_items = sorted(payload.items(), key=lambda entry: entry[0])
    query_pairs = [f"{param_key}={_canonical_value(param_value)}" for param_key, param_value in sorted_items]
    plain = f"{method.upper()}\n{controller_lower}\n{action_lower}\n{'&'.join(query_pairs)}"
    signature = hmac.new(secret.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256).hexdigest()
    signed_payload = dict(payload)
    signed_payload["sign"] = signature
    return signed_payload, signature, plain


def is_16688_paused_message(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(keyword in lowered for keyword in PAUSE_16688_KEYWORDS)


def get_upstreams_status(db: Session, store_id: int) -> dict[str, Any]:
    cfg = load_upstream_config()
    usd_rate, vnd_rate = card_reconciliation.get_store_fx_rates(db, store_id)

    purchase_rows = db.execute(
        select(CardPurchase.source, func.count(CardPurchase.id))
        .where(CardPurchase.store_id == store_id)
        .group_by(CardPurchase.source)
    ).all()
    purchase_counts: dict[str, int] = {}
    for src, cnt in purchase_rows:
        key = str(src or "unknown")
        purchase_counts[key] = int(cnt or 0)

    order_rows = db.execute(
        select(OrderItem.channel, func.count(OrderItem.id))
        .where(OrderItem.store_id == store_id)
        .group_by(OrderItem.channel)
    ).all()
    order_counts: dict[str, int] = {"qianniu": 0, "16688": 0}
    for ch, cnt in order_rows:
        key = str(ch or "qianniu")
        order_counts[key] = order_counts.get(key, 0) + int(cnt or 0)

    return {
        "store_id": store_id,
        "configured": {
            "fatbunny": bool(cfg["fatbunny_api_key"]),
            "chugax": bool(cfg["chugax_api_key"]),
            "plan2": bool(cfg["plan2_base_url"] and cfg["plan2_admin_token"]),
            "openapi_16688": bool(cfg["openapi_16688_app_id"] and cfg["openapi_16688_secret"]),
        },
        "fx_rates": {
            "usd_cny_rate": card_reconciliation._format_decimal_clean(usd_rate),
            "vnd_cny_rate": card_reconciliation._format_decimal_clean(vnd_rate),
        },
        "purchase_counts": purchase_counts,
        "purchase_total": sum(purchase_counts.values()),
        "order_counts": order_counts,
        "order_total": sum(order_counts.values()),
    }


def _normalize_tg_supplier(supplier: str) -> str:
    raw = str(supplier or "all").strip().lower()
    if raw in ("", "all", "both", "全部"):
        return "all"
    if raw in ("fatbunny", "tg_fatbunny", "primary", "rabbit", "兔子"):
        return "fatbunny"
    if raw in ("chugax", "tg_chugax", "secondary"):
        return "chugax"
    raise UpstreamSyncError("TG 上游参数无效，仅支持 all、fatbunny 或 chugax")


def sync_canboso_orders(
    db: Session,
    store_id: int,
    supplier: str = "all",
    *,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    mode = _normalize_tg_supplier(supplier)
    cfg = load_upstream_config()
    base_url = cfg["canboso_base_url"] or DEFAULT_CANBOSO_BASE_URL
    url = f"{base_url.rstrip('/')}/api/v2/telegram-buyer/orders"

    catalog = [
        ("fatbunny", "tg_fatbunny", "FatBunny Hub", cfg["fatbunny_api_key"]),
        ("chugax", "tg_chugax", "ChugAx Global", cfg["chugax_api_key"]),
    ]
    if mode == "all":
        targets = [item for item in catalog if item[3]]
        if not targets:
            raise UpstreamSyncError("未配置 FatBunny Hub 或 ChugAx Global 的 Canboso API Key")
    else:
        targets = [item for item in catalog if item[0] == mode]
        if not targets or not targets[0][3]:
            label = targets[0][2] if targets else mode
            raise UpstreamSyncError(f"未配置 {label} 的 Canboso API Key")

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=25.0, follow_redirects=False)
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    total_fetched = total_inserted = total_updated = total_skipped = 0

    try:
        for key_name, source_code, label, api_key in targets:
            try:
                response = client.get(
                    url,
                    headers={"x-buyer-key": api_key, "Accept": "application/json"},
                )
                if response.status_code != 200:
                    detail = _sanitize_text(response.text[:200], api_key)
                    raise UpstreamSyncError(f"{label} 拉取失败 (HTTP {response.status_code}): {detail}")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise UpstreamSyncError(f"{label} 返回非 JSON 响应") from exc
                if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
                    raise UpstreamSyncError(f"{label} 响应缺少 orders 列表")

                orders = [
                    sanitized
                    for row in payload["orders"]
                    if isinstance(row, dict)
                    and isinstance((sanitized := card_reconciliation.sanitize_raw_payload(row)), dict)
                ]
                fetched = len(orders)
                total_fetched += fetched
                if not orders:
                    results.append({
                        "supplier": key_name,
                        "source": source_code,
                        "label": label,
                        "fetched": 0,
                        "inserted": 0,
                        "updated": 0,
                        "skipped": 0,
                        "duplicate": False,
                    })
                    continue

                import_body = json.dumps(
                    {"success": True, "source": source_code, "orders": orders},
                    ensure_ascii=False,
                ).encode("utf-8")
                imp = card_reconciliation.import_purchases(
                    db,
                    store_id,
                    f"{source_code}-sync.json",
                    import_body,
                    source_hint=source_code,
                )
                total_inserted += int(imp.get("inserted") or 0)
                total_updated += int(imp.get("updated") or 0)
                total_skipped += int(imp.get("skipped") or 0)
                results.append({
                    "supplier": key_name,
                    "source": source_code,
                    "label": label,
                    "fetched": fetched,
                    "inserted": int(imp.get("inserted") or 0),
                    "updated": int(imp.get("updated") or 0),
                    "skipped": int(imp.get("skipped") or 0),
                    "duplicate": bool(imp.get("duplicate")),
                })
            except (httpx.HTTPError, UpstreamSyncError, importer.ImportProblem) as exc:
                db.rollback()
                msg = _sanitize_text(str(exc), api_key)
                errors.append(f"{label}: {msg}")
    finally:
        if owns_client:
            client.close()

    if errors and not results:
        raise UpstreamSyncError("；".join(errors))
    return {
        "status": "partial" if errors else "ok",
        "supplier": mode,
        "fetched": total_fetched,
        "inserted": total_inserted,
        "updated": total_updated,
        "skipped": total_skipped,
        "suppliers": results,
        "errors": errors,
    }


def sync_plan2_reconciliation(
    db: Session,
    store_id: int,
    *,
    start_date: str = "",
    end_date: str = "",
    channel: str = "",
    auto_confirm_exact: bool = False,
    reviewer_id: int = 0,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    cfg = load_upstream_config()
    base_url = cfg["plan2_base_url"]
    admin_token = cfg["plan2_admin_token"]
    if not base_url or not admin_token:
        raise UpstreamSyncError("未配置方案二服务地址或管理员 Token")

    url = f"{base_url.rstrip('/')}/api/v1/admin/reconciliation-export"
    query_params: dict[str, str] = {"format": "json"}
    if start_date.strip():
        query_params["start_date"] = start_date.strip()
    if end_date.strip():
        query_params["end_date"] = end_date.strip()
    if channel.strip():
        mapped_channel = _map_plan2_channel(channel)
        if mapped_channel:
            query_params["channel"] = mapped_channel

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=25.0, follow_redirects=False)
    try:
        try:
            response = client.get(
                url,
                params=query_params,
                headers={"Authorization": f"Bearer {admin_token}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamSyncError(_sanitize_text(f"连接方案二对账导出接口失败: {exc}", admin_token)) from exc
        if response.status_code != 200:
            detail = _sanitize_text(response.text[:200], admin_token)
            raise UpstreamSyncError(f"方案二对账导出失败 (HTTP {response.status_code}): {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamSyncError("方案二对账导出返回非 JSON 响应") from exc
    finally:
        if owns_client:
            client.close()

    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise UpstreamSyncError("方案二对账导出缺少 items 列表")

    raw_items = [row for row in payload["items"] if isinstance(row, dict)]
    valid_items = [row for row in raw_items if str(row.get("upstream_order_code") or "").strip()]
    ignored_unlinked = len(raw_items) - len(valid_items)

    if not valid_items:
        auto_res = None
        if auto_confirm_exact:
            auto_res = card_reconciliation.auto_confirm_exact_matches(
                db,
                store_id,
                reviewer_id,
                start=start_date.strip(),
                end=end_date.strip(),
                channel=channel.strip(),
            )
        return {
            "status": "ok",
            "fetched": 0,
            "ignored_unlinked": ignored_unlinked,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "duplicate": False,
            "auto_confirm": auto_res,
        }

    export_body = json.dumps(
        {"source": str(payload.get("source") or "tg_fatbunny"), "count": len(valid_items), "items": valid_items},
        ensure_ascii=False,
    ).encode("utf-8")
    imp = card_reconciliation.import_purchases(
        db,
        store_id,
        "plan2-reconciliation.json",
        export_body,
        source_hint="tg_fatbunny",
    )
    auto_res = None
    if auto_confirm_exact:
        auto_res = card_reconciliation.auto_confirm_exact_matches(
            db,
            store_id,
            reviewer_id,
            start=start_date.strip(),
            end=end_date.strip(),
            channel=channel.strip(),
        )
    return {
        "status": "ok",
        "fetched": len(valid_items),
        "ignored_unlinked": ignored_unlinked,
        "inserted": int(imp.get("inserted") or 0),
        "updated": int(imp.get("updated") or 0),
        "skipped": int(imp.get("skipped") or 0),
        "duplicate": bool(imp.get("duplicate")),
        "auto_confirm": auto_res,
    }


def _first_present(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        val = raw.get(key)
        if val not in (None, ""):
            return val
    return None


def _normalize_16688_order_dict(raw: dict[str, Any], idx: int) -> dict[str, Any]:
    trade_no = _first_present(
        raw,
        ("trade_no", "order_no", "order_external_id", "order_id", "订单编号", "订单号", "交易单号", "平台单号"),
    )
    if trade_no is None:
        raise importer.ImportProblem(f"第 {idx} 行缺少 16688 订单号（trade_no）")
    order_id = importer.identifier(trade_no, f"第 {idx} 行订单号")
    item_id_raw = _first_present(raw, ("item_external_id", "item_id", "子订单编号", "子订单号"))
    item_id = importer.identifier(item_id_raw, f"第 {idx} 行子订单号") if item_id_raw is not None else order_id

    goods_no = str(_first_present(raw, ("goods_no", "sku", "商品编号", "商品货号", "商家编码")) or "").strip()
    goods_name = str(_first_present(raw, ("goods_name", "product_title", "title", "name", "商品名称", "商品标题")) or "").strip()
    sku = goods_no or goods_name
    if not sku:
        raise importer.ImportProblem(f"第 {idx} 行缺少商品编号或商品名称")
    title = goods_name or goods_no

    qty_raw = _first_present(raw, ("quantity", "num", "buy_num", "购买数量", "数量", "商品数量"))
    try:
        quantity = max(1, int(str(qty_raw).strip())) if qty_raw not in (None, "") else 1
    except ValueError:
        raise importer.ImportProblem(f"第 {idx} 行数量格式无效") from None

    raw_status = _first_present(raw, ("status", "order_status", "status_label", "订单状态", "交易状态"))
    status_label = str(raw.get("status_label") or "").strip()
    status = importer.order_state(raw_status if raw_status is not None else status_label, channel="16688")

    raw_refund_status = _first_present(raw, ("refund_status", "退款状态", "售后状态"))
    if raw_refund_status not in (None, ""):
        ref_status = importer.refund_state(raw_refund_status)
    elif str(raw_status).strip() == "3" or "退款" in status_label or "退款" in str(raw_status or ""):
        ref_status = "success"
    else:
        ref_status = "none"

    shipped_at = _first_present(raw, ("send_time", "delivery_time", "shipped_at", "shipping_time", "发货时间", "发卡时间"))
    if shipped_at in (0, "0"):
        shipped_at = None
    fulfillment = importer.fulfillment_state(
        raw_status if raw_status is not None else status_label,
        shipped_at,
        ref_status,
        channel="16688",
    )

    payment_time_keys = (
        "success_time",
        "pay_time",
        "paid_at",
        "paid_at_ts",
        "付款时间",
        "支付时间",
        "订单付款时间",
    )
    create_time_keys = (
        "create_time",
        "created_at",
        "created_at_ts",
        "下单时间",
        "创建时间",
    )
    if status == "unpaid":
        paid_raw = None
    elif status == "closed":
        paid_raw = None
        for key in payment_time_keys:
            candidate = raw.get(key)
            if candidate not in (None, "", 0, "0"):
                paid_raw = candidate
                break
    else:
        paid_raw = None
        for key in (*payment_time_keys, *create_time_keys):
            candidate = raw.get(key)
            if candidate not in (None, "", 0, "0"):
                paid_raw = candidate
                break
    paid_at = importer.timestamp(paid_raw, optional=True) if paid_raw is not None else None

    amount_raw = _first_present(
        raw,
        (
            "total_amount",
            "pay_price",
            "total_price",
            "goods_total_amount",
            "payment",
            "amount_cny",
            "amount",
            "实付金额",
            "买家实付金额",
            "支付金额",
            "订单金额",
            "总金额",
        ),
    )
    payment_fen = importer.money(amount_raw, optional=True) if amount_raw not in (None, "") else None

    refund_amount_raw = _first_present(raw, ("refund_amount", "退款金额", "退款总额"))
    refund_snapshot_fen = importer.money(refund_amount_raw, optional=True) if refund_amount_raw not in (None, "") else None
    if ref_status == "success" and refund_snapshot_fen is None and payment_fen is not None:
        refund_snapshot_fen = payment_fen

    return {
        "item_external_id": item_id,
        "order_external_id": order_id,
        "sku": sku,
        "product_title": title,
        "quantity": quantity,
        "paid_at": paid_at,
        "status": status,
        "fulfillment_status": fulfillment,
        "payment_fen": payment_fen,
        "refund_status": ref_status,
        "refund_snapshot_fen": refund_snapshot_fen,
        "raw_data": _sanitize_order_meta(raw),
    }


def sync_16688_sales_orders(
    db: Session,
    store_id: int,
    *,
    max_pages: int = 5,
    page_size: int = 50,
    start_time: int | None = None,
    end_time: int | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    cfg = load_upstream_config()
    app_id = cfg["openapi_16688_app_id"]
    secret = cfg["openapi_16688_secret"]
    if not app_id or not secret:
        raise UpstreamSyncError("未配置 16688 OpenAPI 凭证（APP_ID / SECRET）")

    gateway = (cfg["openapi_16688_base_url"] or DEFAULT_16688_BASE_URL).rstrip("/")
    if not gateway.lower().endswith("/openapi"):
        gateway = f"{gateway}/openApi"
    url = f"{gateway}/order/list"

    safe_max_pages = max(1, min(int(max_pages), 50))
    safe_page_size = max(1, min(int(page_size), 100))
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=20.0, follow_redirects=False)

    collected: list[dict[str, Any]] = []
    pages_fetched = 0
    try:
        for page_no in range(1, safe_max_pages + 1):
            params: dict[str, Any] = {"page_no": page_no, "page_size": safe_page_size}
            if start_time is not None:
                params["start_time"] = int(start_time)
            if end_time is not None:
                params["end_time"] = int(end_time)
            signed_payload, _, _ = build_16688_signed_payload(
                app_id=app_id,
                secret=secret,
                path="/openApi/order/list",
                params=params,
                method="POST",
            )
            try:
                response = client.post(
                    url,
                    json=signed_payload,
                    headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise UpstreamSyncError(_sanitize_text(f"16688 请求网络异常: {exc}", app_id, secret)) from exc

            raw_text = response.text or ""
            if response.status_code != 200:
                if is_16688_paused_message(raw_text):
                    paused_message = raw_text[:200]
                    try:
                        paused_body = response.json()
                        if isinstance(paused_body, dict):
                            extracted = str(paused_body.get("msg") or paused_body.get("message") or "").strip()
                            if extracted:
                                paused_message = extracted[:200]
                    except ValueError:
                        pass
                    return {
                        "status": "paused",
                        "paused": True,
                        "message": _sanitize_text(paused_message, app_id, secret) or "16688 平台当前已暂停 OpenAPI，待恢复后再同步",
                        "fetched": 0,
                        "inserted": 0,
                        "updated": 0,
                        "skipped": 0,
                    }
                detail = _sanitize_text(raw_text[:200], app_id, secret)
                raise UpstreamSyncError(f"16688 HTTP 状态异常 ({response.status_code}): {detail}")

            try:
                body = response.json()
            except ValueError as exc:
                if is_16688_paused_message(raw_text):
                    return {
                        "status": "paused",
                        "paused": True,
                        "message": _sanitize_text(raw_text[:200], app_id, secret) or "16688 平台当前已暂停 OpenAPI，待恢复后再同步",
                        "fetched": 0,
                        "inserted": 0,
                        "updated": 0,
                        "skipped": 0,
                    }
                raise UpstreamSyncError("16688 返回非 JSON 响应") from exc
            if not isinstance(body, dict):
                raise UpstreamSyncError("16688 响应格式异常")

            code = body.get("code")
            msg = _sanitize_text(str(body.get("msg") or body.get("message") or ""), app_id, secret)
            if code not in (1, "1"):
                if is_16688_paused_message(msg):
                    return {
                        "status": "paused",
                        "paused": True,
                        "message": msg or "16688 平台当前已暂停 OpenAPI，待恢复后再同步",
                        "fetched": 0,
                        "inserted": 0,
                        "updated": 0,
                        "skipped": 0,
                    }
                raise UpstreamSyncError(f"16688 OpenAPI 返回错误 (code={code}): {msg or '未知错误'}")

            data = body.get("data")
            if isinstance(data, list):
                raw_list = data
                total_count = None
            elif isinstance(data, dict):
                raw_list = data.get("list") or data.get("orders") or []
                total_count = data.get("total")
            else:
                raw_list = []
                total_count = 0

            pages_fetched += 1
            page_items = [row for row in raw_list if isinstance(row, dict)]
            collected.extend(page_items)
            if len(page_items) < safe_page_size:
                break
            if isinstance(total_count, int) and len(collected) >= total_count:
                break
    finally:
        if owns_client:
            client.close()

    merchant_rows = [
        row
        for row in collected
        if str(row.get("identity") or "").strip().lower() in ("", "merchant", "seller")
    ]
    skipped_non_merchant = len(collected) - len(merchant_rows)
    normalized_rows = [_normalize_16688_order_dict(row, idx) for idx, row in enumerate(merchant_rows, 1)]
    imp = importer.import_channel_orders(
        db,
        store_id,
        "16688",
        normalized_rows,
        basis="gross",
        source_name="16688-openapi",
    )
    return {
        "status": "ok",
        "paused": False,
        "message": f"已同步 {len(normalized_rows)} 笔 16688 销售订单",
        "pages_fetched": pages_fetched,
        "fetched": len(normalized_rows),
        "skipped_non_merchant": skipped_non_merchant,
        **imp,
    }


def _read_tsv_table(data: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    if len(data) > importer.MAX_FILE:
        raise importer.ImportProblem("文件超过 8 MB")
    text = ""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise importer.ImportProblem("TSV 编码无法识别，请使用 UTF-8 或 GB18030")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t")
    values = [row for row in reader]
    if not values:
        raise importer.ImportProblem("文件为空")
    if len(values) > importer.MAX_ROWS + 1:
        raise importer.ImportProblem("单次最多导入 20000 行")
    headers = [str(cell or "").strip() for cell in values[0]]
    if not all(headers) or len(headers) != len(set(headers)):
        raise importer.ImportProblem("表头为空或重复，请先整理表格")
    rows = [dict(zip(headers, row)) for row in values[1:] if any(cell is not None and str(cell).strip() for cell in row)]
    if not rows:
        raise importer.ImportProblem("文件没有数据行")
    return headers, rows


def import_16688_orders_payload(
    db: Session,
    store_id: int,
    filename: str,
    data: bytes,
    *,
    basis: str = "gross",
) -> dict[str, Any]:
    if len(data) > importer.MAX_FILE:
        raise importer.ImportProblem("文件超过 8 MB")
    safe_name = Path(filename or "16688-orders.csv").name or "16688-orders.csv"
    lower_name = safe_name.lower()
    stripped_head = data.lstrip()[:1]
    skipped_non_merchant = 0

    if lower_name.endswith(".json") or stripped_head in (b"{", b"["):
        try:
            parsed = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise importer.ImportProblem("16688 订单 JSON 格式无效") from None
        raw_list: list[Any] = []
        if isinstance(parsed, list):
            raw_list = parsed
        elif isinstance(parsed, dict):
            if isinstance(parsed.get("list"), list):
                raw_list = parsed["list"]
            elif isinstance(parsed.get("orders"), list):
                raw_list = parsed["orders"]
            elif isinstance(parsed.get("items"), list):
                raw_list = parsed["items"]
            elif isinstance(parsed.get("data"), dict):
                nested = parsed["data"]
                raw_list = nested.get("list") or nested.get("orders") or nested.get("items") or []
            elif isinstance(parsed.get("data"), list):
                raw_list = parsed["data"]
            else:
                raise importer.ImportProblem("JSON 中未找到订单列表（list / orders / items）")
        else:
            raise importer.ImportProblem("16688 订单 JSON 需为数组或包含 orders/list 字段的对象")
        dict_rows = [item for item in raw_list if isinstance(item, dict)]
        if not dict_rows:
            raise importer.ImportProblem("JSON 中没有可导入的订单记录")
        if len(dict_rows) > importer.MAX_ROWS:
            raise importer.ImportProblem(f"单次最多导入 {importer.MAX_ROWS} 行")
        merchant_rows = [
            item
            for item in dict_rows
            if str(item.get("identity") or "").strip().lower() in ("", "merchant", "seller")
        ]
        skipped_non_merchant = len(dict_rows) - len(merchant_rows)
        if not merchant_rows:
            return {
                "status": "ok",
                "filename": safe_name,
                "fetched": 0,
                "inserted": 0,
                "updated": 0,
                "skipped": 0,
                "skipped_non_merchant": skipped_non_merchant,
                "batch_id": None,
            }
        normalized_rows = [_normalize_16688_order_dict(item, idx) for idx, item in enumerate(merchant_rows, 1)]
    elif lower_name.endswith(".tsv"):
        headers, raw_rows = _read_tsv_table(data)
        mapping = importer.map_headers(headers, "orders", channel="16688")
        normalized_rows = []
        for raw in raw_rows:
            cleaned = importer.clean_row(raw, mapping, "orders", channel="16688")
            cleaned["raw_data"] = _sanitize_order_meta(raw)
            normalized_rows.append(cleaned)
    elif lower_name.endswith((".csv", ".xlsx")):
        headers, raw_rows = importer.read_table(safe_name, data)
        mapping = importer.map_headers(headers, "orders", channel="16688")
        normalized_rows = []
        for raw in raw_rows:
            cleaned = importer.clean_row(raw, mapping, "orders", channel="16688")
            cleaned["raw_data"] = _sanitize_order_meta(raw)
            normalized_rows.append(cleaned)
    else:
        raise importer.ImportProblem("16688 销售订单仅支持 CSV、TSV、XLSX 或 JSON 文件")

    res = importer.import_channel_orders(
        db,
        store_id,
        "16688",
        normalized_rows,
        basis=basis,
        source_name=safe_name,
    )
    return {
        "status": "ok",
        "filename": safe_name,
        "fetched": len(normalized_rows),
        "skipped_non_merchant": skipped_non_merchant,
        **res,
    }
