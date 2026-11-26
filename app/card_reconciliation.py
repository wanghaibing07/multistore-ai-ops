from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from itertools import combinations
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from .accounting import CHANNEL_LABELS, normalize_channel_filter
from .importer import ImportProblem, money, read_table, timestamp
from .models import Adjustment, CardMatch, CardPurchase, CostVersion, ImportBatch, OrderItem, StoreSetting


DEFAULT_USD_CNY_RATE = Decimal("7.30")
DEFAULT_VND_CNY_RATE = Decimal("0.00028")

_SENSITIVE_EXACT_KEYS = frozenset({
    "email",
    "contact_email",
    "buyer_email",
    "buyer_email_masked",
    "user_email",
    "customer_email",
    "mail",
    "phone",
    "buyer_phone",
    "mobile",
    "tel",
    "telephone",
    "contact",
    "contacts",
    "password",
    "passwd",
    "pwd",
    "pass",
    "card_pwd",
    "card_password",
    "secret",
    "secrets",
    "secret_code",
    "secret_codes",
    "secret_list",
    "stock_secrets",
    "card_no",
    "card_secret",
    "cards",
    "card_list",
    "card_content",
    "content",
    "kami",
    "carmi",
    "cdk",
    "account",
    "accounts",
    "credential",
    "credentials",
    "delivery",
    "deliveries",
    "delivery_json",
    "delivery_data",
    "delivery_info",
    "delivery_content",
    "request_payload_json",
    "warranty_json",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "cookie",
    "cookies",
    "authorization",
    "buyer_nick",
    "buyer_name",
    "receiver",
    "receiver_name",
    "address",
    "shipping_address",
})

_SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "card_pwd",
    "secret",
    "credential",
    "token",
    "cookie",
    "email",
    "phone",
    "mobile",
    "address",
    "account",
    "buyer_nick",
    "buyer_name",
    "card_no",
    "card_list",
    "card_content",
    "kami",
    "carmi",
    "delivery_json",
    "delivery_data",
    "delivery_info",
    "delivery_content",
    "卡密",
    "卡号",
    "密码",
    "口令",
    "密钥",
    "邮箱",
    "手机号",
    "电话",
    "联系方式",
    "旺旺",
    "买家",
    "收货",
    "地址",
    "账号",
    "帐号",
)

_SAFE_IDENTIFIER_KEYS = frozenset({
    "agiso_oid",
    "agiso_tid",
    "external_order_id",
    "order_id",
    "ordercode",
    "orderid",
    "order_sn",
    "trade_no",
    "item_external_id",
    "order_external_id",
    "purchase_external_id",
    "upstream_order_code",
    "code",
    "goods_no",
    "sku_key",
    "sku_id",
    "channel",
    "source",
})

_INLINE_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_INLINE_SECRET_RE = re.compile(r"(?i)(密码|口令|密钥|password|passwd|token|secret)\s*[:=：]\s*\S+")


def _is_sensitive_raw_key(key: str) -> bool:
    cleaned = str(key or "").strip()
    if not cleaned:
        return False
    low = cleaned.lower()
    if low in _SAFE_IDENTIFIER_KEYS:
        return False
    if low in _SENSITIVE_EXACT_KEYS:
        return True
    return any(sub in low for sub in _SENSITIVE_KEY_SUBSTRINGS)


def sanitize_raw_payload(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= 8:
        return None
    if isinstance(value, dict):
        cleaned_dict: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_sensitive_raw_key(key_str):
                continue
            if key_str.strip().lower() in _SAFE_IDENTIFIER_KEYS:
                cleaned_dict[key_str] = item if not isinstance(item, (dict, list, tuple)) else sanitize_raw_payload(item, _depth=_depth + 1)
            else:
                cleaned_dict[key_str] = sanitize_raw_payload(item, _depth=_depth + 1)
        return cleaned_dict
    if isinstance(value, (list, tuple)):
        return [sanitize_raw_payload(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        text = _INLINE_EMAIL_RE.sub("[邮箱已隐藏]", value)
        text = _INLINE_SECRET_RE.sub(r"\1：[已隐藏]", text)
        return text
    return value

SOURCE_LABELS = {
    "16688": "16688",
    "16688页面": "16688",
    "链动小铺": "链动小铺",
    "tg_fatbunny": "TG · FatBunny Hub",
    "tg_chugax": "TG · ChugAx Global",
    "tg_canboso": "TG · Canboso",
    "thatim": "Thatim (Zalo 2号)",
}

SOURCE_DEFAULT_SELLER = {
    "链动小铺": "链动小铺",
    "tg_fatbunny": "FatBunny Hub",
    "tg_chugax": "ChugAx Global",
    "tg_canboso": "Canboso",
    "thatim": "Thatim (2号)",
}


def source_label(source: str | None) -> str:
    raw = str(source or "").strip()
    return SOURCE_LABELS.get(raw, raw or "16688")


def normalize_purchase_source(raw: Any, *, default: str = "16688", ext_id: str = "") -> str:
    text = str(raw or "").strip()
    if not text:
        return "链动小铺" if ext_id.startswith("LD") else default
    if text in ("16688", "16688页面", "链动小铺", "tg_fatbunny", "tg_chugax", "tg_canboso", "thatim"):
        return text
    low = text.lower()
    if any(tok in low for tok in ("thatim", "zalo", "2号", "二号")):
        return "thatim"
    if low in ("fatbunny", "fatbunny_hub", "tg_primary", "primary", "plan2", "plan2_fatbunny") or "fatbunny" in low or "兔子" in low:
        return "tg_fatbunny"
    if low in ("chugax", "chugax_global", "tg_secondary", "secondary") or "chugax" in low:
        return "tg_chugax"
    if low in ("canboso", "tg", "telegram") or "canboso" in low:
        return "tg_canboso"
    if low in ("wzyp", "liandong") or "链动" in low:
        return "链动小铺"
    if "16688" in low:
        return "16688"
    return text[:80]


def get_store_fx_rates(db: Session, store_id: int) -> tuple[Decimal, Decimal]:
    setting = db.scalar(select(StoreSetting).where(StoreSetting.store_id == store_id))
    usd_rate = DEFAULT_USD_CNY_RATE
    vnd_rate = DEFAULT_VND_CNY_RATE
    if setting is not None:
        for attr_name, is_usd in (("usd_cny_rate", True), ("vnd_cny_rate", False)):
            raw_val = str(getattr(setting, attr_name, "") or "").strip()
            if not raw_val:
                continue
            try:
                parsed = Decimal(raw_val)
                if parsed > 0:
                    if is_usd:
                        usd_rate = parsed
                    else:
                        vnd_rate = parsed
            except InvalidOperation:
                continue
    return usd_rate, vnd_rate


def _format_decimal_clean(val: Decimal) -> str:
    normalized = val.normalize()
    if normalized == 0:
        return "0"
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _parse_decimal_amount(value: Any, *, currency_hint: str = "") -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text:
        return None
    low = text.lower()
    is_vnd_hint = currency_hint.upper() == "VND" or "₫" in text or "vnd" in low or "đ" in low
    raw_num = re.sub(r"(?:us\$|usd|vnd|cny|rmb|[¥￥$₫đĐ元\s])", "", text, flags=re.IGNORECASE)
    if not raw_num:
        return None
    if re.search(r",\d+\s*\$", text):
        num_str = raw_num.replace(".", "").replace(",", ".")
    elif "," in raw_num and "." in raw_num:
        if raw_num.rfind(",") > raw_num.rfind("."):
            num_str = raw_num.replace(".", "").replace(",", ".")
        else:
            num_str = raw_num.replace(",", "")
    elif "," in raw_num:
        if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", raw_num):
            num_str = raw_num.replace(",", "")
        else:
            num_str = raw_num.replace(",", ".")
    elif "." in raw_num:
        if re.fullmatch(r"-?\d{1,3}(?:\.\d{3}){2,}", raw_num) or (is_vnd_hint and re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", raw_num)):
            num_str = raw_num.replace(".", "")
        else:
            num_str = raw_num
    else:
        num_str = raw_num
    try:
        return Decimal(num_str)
    except InvalidOperation:
        return None


def _detect_currency(raw_value: Any, explicit_currency: Any = "", *, source: str = "") -> str:
    exp = str(explicit_currency or "").strip().upper()
    if exp in ("USD", "US$", "$"):
        return "USD"
    if exp in ("VND", "₫", "Đ", "DONG"):
        return "VND"
    if exp in ("CNY", "RMB", "¥", "￥", "元"):
        return "CNY"
    text = str(raw_value or "")
    low = text.lower()
    if "₫" in text or "vnd" in low or "đ" in low:
        return "VND"
    if "$" in text or "usd" in low:
        return "USD"
    if "¥" in text or "￥" in text or "cny" in low or "rmb" in low or "元" in text:
        return "CNY"
    return "CNY"


def _convert_to_cny_fen(
    amount: Decimal,
    currency: str,
    usd_cny_rate: Decimal = DEFAULT_USD_CNY_RATE,
    vnd_cny_rate: Decimal = DEFAULT_VND_CNY_RATE,
) -> tuple[int, str, str]:
    cur = (currency or "CNY").upper()
    if cur == "USD":
        fx = usd_cny_rate
    elif cur == "VND":
        fx = vnd_cny_rate
    else:
        cur = "CNY"
        fx = Decimal("1")
    fen = int((amount * fx * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return fen, _format_decimal_clean(fx), _format_decimal_clean(amount)


def _normalize_upstream_status(raw_status: Any, *, badge_text: str = "") -> str:
    combined = f"{raw_status or ''} {badge_text or ''}".strip()
    if not combined:
        return ""
    low = combined.lower()
    if any(k in low for k in ("cancel", "cancelled", "canceled", "void", "取消", "已取消", "đã hủy", "hủy")):
        return "已取消"
    if any(k in low for k in ("refund", "已退款", "退款", "hoàn tiền")):
        return "已退款"
    if any(k in low for k in ("failed", "fail", "error", "失败", "已失败", "thất bại")):
        return "已失败"
    if any(k in low for k in ("completed", "delivered", "fulfilled", "done", "success", "issued", "发表", "已完成", "hoàn thành")):
        return "已完成"
    if any(k in low for k in ("unpaid", "waiting_payment", "pending_payment", "待付款", "待支付", "未付款", "未支付", "chờ thanh toán")):
        return "待付款"
    if any(k in low for k in ("waiting_fulfillment", "pending", "waiting", "processing", "submitted", "处理中", "已提交", "待发货")):
        return "处理中"
    if any(k in low for k in ("paid", "confirmed", "已付款", "已支付", "đã thanh toán")):
        return "已付款"
    return str(raw_status or badge_text).strip()[:40]


CARD_HEADERS = {
    "order_id": (
        "order_id",
        "purchase_order",
        "upstream_order_code",
        "orderCode",
        "orderId",
        "订单号",
        "订单编号",
        "卡网订单号",
        "卡网订单编号",
        "采购单号",
        "采购订单号",
        "采购订单编号",
        "订单 ID",
        "订单ID",
    ),
    "product": ("product", "title", "productName", "sku_title", "variantName", "商品", "商品名", "商品名称", "产品", "商品信息"),
    "seller": ("seller", "merchant", "supplier_label", "店铺", "商家", "卖家", "商家名称"),
    "quantity": ("qty", "quantity", "数量"),
    "amount": ("实付金额", "amount_cny", "amount", "payment", "金额", "原币金额", "original_amount", "cost_usd", "amountVnd", "amountUsd", "amountDisplay"),
    "refund": ("payment_refund", "refund", "退款", "退款金额"),
    "paid_at": ("paid_at", "paidAt", "submitted_at", "completed_at", "deliveredAt", "createdAt", "created_at", "付款时间", "下单时间"),
    "status": ("status", "upstream_status", "badgeText", "状态"),
    "currency": ("_amount_currency", "original_currency", "currency", "原币币种", "币种"),
    "original_amount_usd": ("原币金额USD", "cost_usd", "amountUsd"),
    "fx_rate": ("fx_rate", "汇率"),
    "source": ("_source", "source", "来源"),
    "linked_order_id": ("_linked_order_external_id", "linked_order_external_id", "external_order_id", "关联销售单号", "关联订单号", "agiso_tid"),
    "linked_item_id": ("_linked_item_external_id", "linked_item_external_id", "agiso_oid", "关联子单号", "关联子订单号"),
    "linked_channel": ("_linked_channel", "linked_channel", "关联渠道", "channel"),
    "linked_redeem_code": ("_linked_redeem_code", "linked_redeem_code", "兑换码", "code"),
}

CATEGORY_LABELS = {
    "gemini_finished": "Gemini 成品号",
    "activation": "激活/开通",
    "muse_finished": "Muse 成品号",
    "gemini_family": "Gemini 家庭组",
    "other": "其他",
}

CATEGORY_GROUP_LABELS = {
    "finished": "成品号",
    "activation": "激活",
    "family": "家庭组",
    "other": "其他",
}


def category_group(category: str) -> str:
    if category in ("gemini_finished", "muse_finished"):
        return "finished"
    if category == "gemini_family":
        return "family"
    if category == "activation":
        return "activation"
    return "other"


def category_matches(category: str, requested: str) -> bool:
    if requested in ("", "all"):
        return True
    if requested in CATEGORY_GROUP_LABELS:
        return category_group(category) == requested
    return category == requested

FORM_LABELS = {
    "finished_account": "成品号",
    "activation_link": "激活/开通",
    "family_slot": "家庭组席位",
    "invite_boost": "邀请助力/代刷",
    "plain_gmail": "Gmail 白号",
    "unknown": "通用规格",
}

_LOCAL_TZ = timezone(timedelta(hours=8))
_TS_RE = re.compile(r"\b(\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2})\b")
_PRICE_RE = re.compile(r"[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)")
_QTY_RE = re.compile(r"(?:共\s*(\d+)\s*件|(?<![A-Za-z0-9])[×xX]\s*(\d+)\b)")
_LD_ID_RE = re.compile(r"\b(LD[A-Za-z0-9]{6,})\b")
_FF_ANCHOR_RE = re.compile(r"订单号\s+([A-Za-z0-9_-]{6,})\s*[·•\s]+(\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2})")
_WZYP_STATUSES = ("已付款", "已支付", "已退款", "已关闭", "待付款", "待支付", "已完成", "已发货", "待发货", "已取消")
_NOISE_16688 = {
    "16688您好，欢迎访问16688交易平台！",
    "开店指引",
    "卖家中心",
    "免费开店",
    "举报中心",
    "查询结果",
    "全部",
    "联系卖家",
    "查看详情",
    "再次购买",
    "申请售后",
    "订单查询",
}
_NOISE_WZYP = {
    "网站首页",
    "订单查询/投诉",
    "禁售目录",
    "常见问题",
    "开店教学",
    "举报违规商家",
    "svg 订单列表",
    "订单列表",
    "全部",
    "待付款",
    "已支付",
    "已付款",
    "已退款",
    "获取卡密",
    "订单详情",
    "申请售后",
    "查看记录",
    "售后完成",
    "查看权益",
    "操作",
    "商品名",
    "订单号",
    "金额",
    "状态",
}


class CardPurchaseImportError(ImportProblem):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__(
            f"有 {len(issues)} 条采购记录无法识别。请确认复制范围包含商品、金额、时间等完整列。"
        )


def _first(record: dict, keys: tuple[str, ...], default: Any = "") -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return default


def _first_with_key(record: dict, keys: tuple[str, ...], default: Any = "") -> tuple[Any, str]:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key], key
    return default, ""


def _clean_md_cell(value: str) -> str:
    text = value.strip()
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _format_local_ts(ts: str | None) -> str:
    if not ts:
        return ""
    text = str(ts).strip()
    if len(text) == 19 and "T" not in text:
        return text
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(_LOCAL_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def _normalize_product(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def _similarity(left: str, right: str) -> float:
    a, b = _normalize_product(left), _normalize_product(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    grams = lambda text: {text[i:i + 2] for i in range(max(1, len(text) - 1))}
    aa, bb = grams(a), grams(b)
    dice = (2 * len(aa & bb) / (len(aa) + len(bb))) if aa and bb else 0.0
    return max(dice, SequenceMatcher(None, a, b).ratio())


def _clean_match_title(title: str) -> str:
    cleaned = _normalize_promo_title(title)
    cleaned = re.sub(r"对接\s*[-:：]?\s*[a-z0-9_-]*", " ", cleaned)
    cleaned = re.sub(r"(?:考虑|卡密|首登)?售后[^|｜\n]*", " ", cleaned)
    cleaned = re.sub(r"(?:批量合作|免手续费|无需绑卡|直接激活|直接领取|自动发货|极高质量渠道|高质量链接|无客服|不会用勿下单|链接当天用|包首登)", " ", cleaned)
    cleaned = re.sub(r"购买后\s*\d+\s*个?小时内[^|｜\s]*", " ", cleaned)
    cleaned = cleaned.replace("|", " ").replace("｜", " ")
    return _collapse_ws(cleaned)


def _normalize_plan2_item(raw: dict, *, source_hint: str = "") -> dict:
    order_id = str(raw.get("upstream_order_code") or raw.get("order_id") or "").strip()
    src = normalize_purchase_source(raw.get("source") or source_hint or "tg_fatbunny", default="tg_fatbunny")
    seller = str(raw.get("seller") or "FatBunny Hub (方案二)").strip()
    title = str(raw.get("sku_title") or raw.get("product") or raw.get("sku_key") or "").strip()
    cost_usd = raw.get("cost_usd")
    paid_at = raw.get("submitted_at") or raw.get("completed_at") or raw.get("created_at") or raw.get("updated_at") or ""
    status = _normalize_upstream_status(raw.get("upstream_status") or raw.get("status") or "")
    linked_order_id = str(raw.get("external_order_id") or raw.get("agiso_tid") or "").strip()
    linked_item_id = str(raw.get("agiso_oid") or "").strip()
    linked_channel = str(raw.get("channel") or "").strip()
    linked_code = str(raw.get("code") or "").strip()
    return {
        "order_id": order_id,
        "product": title,
        "seller": seller,
        "quantity": raw.get("quantity") or 1,
        "amount": cost_usd if cost_usd not in (None, "") else "0",
        "_amount_currency": "USD",
        "_allow_zero_amount": True,
        "paid_at": paid_at,
        "status": status,
        "_source": src,
        "_linked_order_external_id": linked_order_id,
        "_linked_item_external_id": linked_item_id,
        "_linked_channel": linked_channel,
        "_linked_redeem_code": linked_code,
        "_raw": raw,
    }


def _normalize_thatim_item(raw: dict, *, top_vnd_per_usd: Any = None) -> dict:
    order_id = str(raw.get("orderId") or raw.get("order_id") or "").strip()
    product_name = str(raw.get("productName") or raw.get("product") or "").strip()
    variant_name = str(raw.get("variantName") or "").strip()
    if variant_name and variant_name not in product_name:
        title = f"{product_name} {variant_name}".strip()
    else:
        title = product_name or variant_name
    amount_vnd = raw.get("amountVnd")
    amount_usd = raw.get("amountUsd")
    amount_display = raw.get("amountDisplay")
    amount_val: Any = ""
    currency = "VND"
    if amount_vnd not in (None, "") and (_parse_decimal_amount(amount_vnd, currency_hint="VND") or Decimal("0")) > 0:
        amount_val = amount_vnd
        currency = "VND"
    elif amount_usd not in (None, "") and (_parse_decimal_amount(amount_usd, currency_hint="USD") or Decimal("0")) > 0:
        amount_val = amount_usd
        currency = "USD"
    elif amount_display not in (None, ""):
        amount_val = amount_display
        currency = _detect_currency(amount_display, source="thatim")
    elif amount_vnd == 0 or amount_usd == 0:
        amount_val = 0
        currency = "VND" if amount_vnd == 0 else "USD"
    status = _normalize_upstream_status(raw.get("status") or "", badge_text=str(raw.get("badgeText") or ""))
    raw_copy = dict(raw)
    if top_vnd_per_usd not in (None, "") and "vndPerUsd" not in raw_copy:
        raw_copy["vndPerUsd"] = top_vnd_per_usd
    return {
        "order_id": order_id,
        "product": title,
        "seller": "Thatim (2号)",
        "quantity": raw.get("quantity") or 1,
        "amount": amount_val,
        "_amount_currency": currency,
        "_allow_zero_amount": status in ("已取消", "已退款", "已失败"),
        "paid_at": raw.get("createdAt") or raw.get("paid_at") or "",
        "status": status,
        "_source": "thatim",
        "_raw": raw_copy,
    }


def _normalize_canboso_item(raw: dict, *, source_hint: str = "") -> dict:
    order_id = str(raw.get("orderCode") or raw.get("order_id") or "").strip()
    src = normalize_purchase_source(raw.get("_source") or raw.get("source") or source_hint or "tg_canboso", default="tg_canboso")
    seller = str(raw.get("seller") or SOURCE_DEFAULT_SELLER.get(src, "Canboso")).strip()
    product_name = str(raw.get("productName") or raw.get("product") or "").strip()
    payment = raw.get("payment") if isinstance(raw.get("payment"), dict) else {}
    amount_usd = payment.get("amountUsd")
    amount_vnd = payment.get("amountVnd")
    usd_dec = _parse_decimal_amount(amount_usd, currency_hint="USD") if amount_usd not in (None, "") else None
    vnd_dec = _parse_decimal_amount(amount_vnd, currency_hint="VND") if amount_vnd not in (None, "") else None
    amount_val: Any = ""
    refund_val: Any = 0
    currency = "USD"
    is_negative_refund = False
    if usd_dec is not None and usd_dec > 0:
        amount_val = amount_usd
        currency = "USD"
    elif vnd_dec is not None and vnd_dec > 0:
        amount_val = amount_vnd
        currency = "VND"
    elif usd_dec is not None and usd_dec < 0:
        amount_val = "0"
        refund_val = str(abs(usd_dec))
        currency = "USD"
        is_negative_refund = True
    elif vnd_dec is not None and vnd_dec < 0:
        amount_val = "0"
        refund_val = str(abs(vnd_dec))
        currency = "VND"
        is_negative_refund = True
    else:
        fallback_amt = payment.get("amount") if payment.get("amount") not in (None, "") else raw.get("amount")
        if fallback_amt not in (None, ""):
            fb_cur = _detect_currency(fallback_amt, payment.get("currency") or raw.get("currency") or "", source=src)
            fb_dec = _parse_decimal_amount(fallback_amt, currency_hint=fb_cur)
            if fb_dec is not None and fb_dec < 0:
                amount_val = "0"
                refund_val = str(abs(fb_dec))
                currency = fb_cur
                is_negative_refund = True
            else:
                amount_val = fallback_amt
                currency = fb_cur
        elif usd_dec == 0 or vnd_dec == 0:
            amount_val = 0
            currency = "USD" if usd_dec == 0 else "VND"
    status = _normalize_upstream_status(raw.get("status") or "")
    if is_negative_refund or product_name.lower().startswith("refund wallet"):
        status = "已退款"
    paid_at = raw.get("paidAt") or raw.get("deliveredAt") or raw.get("createdAt") or raw.get("paid_at") or ""
    return {
        "order_id": order_id,
        "product": product_name,
        "seller": seller,
        "quantity": raw.get("quantity") or 1,
        "amount": amount_val,
        "refund": refund_val,
        "_amount_currency": currency,
        "_allow_zero_amount": status in ("已取消", "已退款", "已失败"),
        "paid_at": paid_at,
        "status": status,
        "_source": src,
        "_raw": raw,
    }


def _dispatch_json_item(raw: dict, *, top_source: str = "", captured: Any = None, top_vnd_per_usd: Any = None) -> dict:
    top_norm = normalize_purchase_source(top_source, default="") if top_source else ""
    if "upstream_order_code" in raw or ("code" in raw and ("sku_key" in raw or "external_order_id" in raw)):
        return _normalize_plan2_item(raw, source_hint=top_norm or top_source)
    if top_norm == "thatim" or "orderId" in raw or "amountVnd" in raw or "amountDisplay" in raw:
        return _normalize_thatim_item(raw, top_vnd_per_usd=top_vnd_per_usd)
    payment = raw.get("payment") if isinstance(raw.get("payment"), dict) else {}
    if "orderCode" in raw or "amountUsd" in payment or "amountVnd" in payment or top_norm.startswith("tg_"):
        return _normalize_canboso_item(raw, source_hint=top_norm or top_source)
    if isinstance(raw.get("card_order"), dict):
        merged = {**raw["card_order"], **({"payment": raw.get("payment")} if raw.get("payment") else {})}
        if top_source and "_source" not in merged:
            merged["_source"] = top_source
        return merged
    out = dict(raw)
    if top_source and "_source" not in out:
        out["_source"] = top_source
    if captured is not None and "_captured_at" not in out:
        out["_captured_at"] = captured
    return out


def _parse_json(data: bytes, *, source_hint: str = "") -> list[dict]:
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportProblem("卡网文件不是有效的 UTF-8 JSON") from None
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        top_source = str(parsed.get("source") or source_hint or "tg_fatbunny")
        rows = []
        for raw in parsed["items"]:
            if isinstance(raw, dict):
                rows.append(_dispatch_json_item(raw, top_source=top_source))
        return rows
    if isinstance(parsed, dict) and isinstance(parsed.get("orders"), list):
        raw_top_source = str(parsed.get("source") or source_hint or "")
        source = raw_top_source or ("tg_canboso" if parsed.get("success") is True else "16688页面")
        captured = parsed.get("captured_at") or parsed.get("fetchedAt")
        top_vnd = parsed.get("vndPerUsd")
        rows = []
        for raw in parsed["orders"]:
            if isinstance(raw, dict):
                rows.append(_dispatch_json_item(raw, top_source=source, captured=captured, top_vnd_per_usd=top_vnd))
        return rows
    if isinstance(parsed, dict) and ("orders16688" in parsed or "wzyp" in parsed):
        rows = []
        o16688 = parsed.get("orders16688") or {}
        for card in o16688.get("cards") or []:
            if not isinstance(card, dict):
                continue
            meta = str(card.get("meta") or "")
            m = re.search(r"([A-Za-z0-9_-]{6,})\s*[·•\s]+(\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2})", meta)
            order_id = card.get("order_id") or (m.group(1) if m else "")
            paid_at = card.get("paid_at") or (m.group(2) if m else "")
            status = str(card.get("status") or "").strip()
            if card.get("refunded") and "退款" not in status:
                status = "已退款"
            rows.append({
                "order_id": order_id,
                "product": card.get("title") or card.get("product") or "",
                "seller": card.get("store") or card.get("seller") or "",
                "quantity": card.get("quantity") or 1,
                "amount": card.get("price") or card.get("amount") or "",
                "paid_at": paid_at,
                "status": status,
                "_source": "16688",
                "_raw": card,
            })
        wzyp = parsed.get("wzyp") or {}
        for page in wzyp.get("pages") or []:
            for cells in page.get("rows") or []:
                if not isinstance(cells, list) or len(cells) < 3:
                    continue
                parsed_row = _parse_wzyp_cells(cells)
                if parsed_row:
                    rows.append(parsed_row)
        return rows
    if isinstance(parsed, list):
        rows = []
        for raw in parsed:
            if not isinstance(raw, dict):
                continue
            rows.append(_dispatch_json_item(raw, top_source=source_hint))
        return rows
    raise ImportProblem("卡网 JSON 需为订单数组，或包含 orders / items 数组")


def _parse_wzyp_cells(cells: list[str]) -> dict | None:
    joined = " | ".join(str(c or "") for c in cells)
    order_match = _LD_ID_RE.search(joined)
    if not order_match:
        return None
    order_id = order_match.group(1)
    ts_match = _TS_RE.search(joined)
    paid_at = ts_match.group(1) if ts_match else ""
    price_match = _PRICE_RE.search(joined)
    amount = price_match.group(1) if price_match else ""
    qty = 1
    qty_cells = [str(c or "").replace(order_id, " ") for c in cells]
    for cell_text in qty_cells:
        m_jian = re.search(r"共\s*(\d+)\s*件", cell_text)
        if m_jian:
            qty = int(m_jian.group(1))
            break
    else:
        for cell_text in qty_cells:
            if _PRICE_RE.search(cell_text) or _QTY_RE.fullmatch(_clean_md_cell(cell_text)):
                qm = _QTY_RE.search(cell_text)
                if qm:
                    qty = int(qm.group(1) or qm.group(2))
                    break
    status = ""
    for cell in cells:
        cleaned = _clean_md_cell(str(cell))
        if cleaned in _WZYP_STATUSES:
            status = cleaned
            break
    if not status:
        for candidate_status in _WZYP_STATUSES:
            if candidate_status in joined:
                status = candidate_status
                break
    title = ""
    for cell in cells:
        lines = [_clean_md_cell(line) for line in str(cell or "").splitlines() if _clean_md_cell(line)]
        for line in lines:
            if order_id in line or _TS_RE.fullmatch(line) or _PRICE_RE.search(line) or _QTY_RE.fullmatch(line) or line in _NOISE_WZYP:
                continue
            if len(line) >= 2:
                title = line
                break
        if title:
            break
    return {
        "order_id": order_id,
        "product": title,
        "seller": "链动小铺",
        "quantity": qty,
        "amount": amount,
        "paid_at": paid_at,
        "status": status,
        "_source": "链动小铺",
        "_raw_cells": cells,
    }


def _parse_markdown_table(text: str) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|") and line.strip().endswith("|")]
    if len(lines) < 2:
        return []
    rows_cells: list[list[str]] = []
    for line in lines:
        raw_cells = [c.strip() for c in line.strip("|").split("|")]
        if any(c.strip() for c in raw_cells) and all(re.fullmatch(r":?-{1,}:?", c.strip()) or not c.strip() for c in raw_cells):
            continue
        rows_cells.append([_clean_md_cell(c) for c in raw_cells])
    if len(rows_cells) < 2:
        return []
    header = rows_cells[0]
    data_rows = rows_cells[1:]
    parsed: list[dict] = []
    header_keys = set()
    for h in header:
        for field, aliases in CARD_HEADERS.items():
            if h in aliases:
                header_keys.add(field)
    for cells in data_rows:
        wzyp_row = _parse_wzyp_cells(cells)
        if wzyp_row:
            parsed.append(wzyp_row)
            continue
        if header_keys and len(cells) >= len(header_keys):
            record = {header[idx]: cells[idx] for idx in range(min(len(header), len(cells))) if header[idx]}
            if any(record.values()):
                parsed.append(record)
    return parsed


def _parse_16688_text(text: str) -> list[dict]:
    lines = [_collapse_ws(line) for line in text.splitlines()]
    anchors: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        m = _FF_ANCHOR_RE.search(line)
        if m:
            anchors.append((idx, m.group(1), m.group(2)))
    if not anchors:
        return []
    rows: list[dict] = []
    prev_tail = 0
    filter_tabs = {"全部", "待支付", "待发货", "已发货", "已完成", "已退款"}
    for idx, line in enumerate(lines[:30]):
        if line == "全部":
            cursor = idx + 1
            while cursor < len(lines) and (not lines[cursor] or lines[cursor] in filter_tabs):
                cursor += 1
            prev_tail = cursor
            break
    for pos, (anchor_idx, order_id, paid_at) in enumerate(anchors):
        next_anchor_idx = anchors[pos + 1][0] if pos + 1 < len(anchors) else len(lines)
        pre_lines = [
            line for line in lines[prev_tail:anchor_idx]
            if line and line not in _NOISE_16688 and not line.startswith("邮箱 ") and not line.startswith("http")
        ]
        dedup_pre: list[str] = []
        for line in pre_lines:
            if not dedup_pre or dedup_pre[-1] != line:
                dedup_pre.append(line)
        status = ""
        seller = ""
        title = ""
        status_candidates = {"已发货", "已完成", "已退款", "待支付", "待发货", "已关闭", "已取消", "交易成功", "已付款"}
        status_idx = None
        for idx, line in enumerate(dedup_pre):
            if line in status_candidates:
                status = line
                status_idx = idx
        if status_idx is not None:
            before_status = dedup_pre[:status_idx]
            after_status = dedup_pre[status_idx + 1:]
            if before_status:
                seller = before_status[-1]
            if after_status:
                title = " ".join(after_status)
        elif len(dedup_pre) >= 2:
            seller = dedup_pre[0]
            title = " ".join(dedup_pre[1:])
        elif len(dedup_pre) == 1:
            title = dedup_pre[0]

        amount = ""
        quantity = 1
        tail_end = anchor_idx + 1
        for idx in range(anchor_idx + 1, next_anchor_idx):
            line = lines[idx]
            if not line:
                continue
            if line in ("联系卖家", "查看详情", "再次购买", "申请售后"):
                tail_end = idx + 1
                continue
            pm = _PRICE_RE.search(line)
            if pm and not amount:
                amount = pm.group(1)
                qm = _QTY_RE.search(line)
                if qm:
                    quantity = int(qm.group(1) or qm.group(2))
                tail_end = idx + 1
                continue
            qm = _QTY_RE.fullmatch(line)
            if qm and amount:
                quantity = int(qm.group(1) or qm.group(2))
                tail_end = idx + 1
                continue
            if line in ("已退款", "已关闭", "已完成") and amount:
                status = line
                tail_end = idx + 1
                continue
            if amount:
                break
        prev_tail = tail_end
        rows.append({
            "order_id": order_id,
            "product": title,
            "seller": seller,
            "quantity": quantity,
            "amount": amount,
            "paid_at": paid_at,
            "status": status,
            "_source": "16688",
            "_raw_text": "\n".join(x for x in lines[max(0, anchor_idx - 6):tail_end] if x),
        })
    return rows


def _parse_wzyp_text(text: str) -> list[dict]:
    tokens = [_collapse_ws(_clean_md_cell(t)) for t in re.split(r"[\t\r\n]+", text)]
    tokens = [t for t in tokens if t]
    ld_indices = [i for i, t in enumerate(tokens) if _LD_ID_RE.fullmatch(t)]
    if not ld_indices:
        return []
    rows: list[dict] = []
    prev_end = 0
    for pos, ld_idx in enumerate(ld_indices):
        order_id = tokens[ld_idx]
        next_ld = ld_indices[pos + 1] if pos + 1 < len(ld_indices) else len(tokens)
        pre = [t for t in tokens[prev_end:ld_idx] if t not in _NOISE_WZYP and not t.startswith("邮箱")]
        paid_at = ""
        title = ""
        for idx in range(len(pre) - 1, -1, -1):
            if _TS_RE.search(pre[idx]):
                m = _TS_RE.search(pre[idx])
                paid_at = m.group(1)
                title_candidates = [x for x in pre[:idx] if x not in _WZYP_STATUSES and not _PRICE_RE.search(x)]
                if title_candidates:
                    title = title_candidates[-1]
                break
        if not title and pre:
            title_candidates = [x for x in pre if x not in _WZYP_STATUSES and not _TS_RE.search(x)]
            if title_candidates:
                title = title_candidates[-1]
        amount = ""
        quantity = 1
        status = ""
        end_idx = ld_idx + 1
        for idx in range(ld_idx + 1, next_ld):
            tok = tokens[idx]
            if tok in ("获取卡密", "订单详情", "申请售后", "查看记录", "售后完成"):
                end_idx = idx + 1
                continue
            if not amount:
                pm = _PRICE_RE.search(tok)
                if pm:
                    amount = pm.group(1)
                    end_idx = idx + 1
                    continue
            qm = _QTY_RE.search(tok)
            if qm and amount:
                quantity = int(qm.group(1) or qm.group(2))
                end_idx = idx + 1
                continue
            if tok in _WZYP_STATUSES and not status:
                status = tok
                end_idx = idx + 1
                continue
            if amount and status:
                break
        prev_end = end_idx
        rows.append({
            "order_id": order_id,
            "product": title,
            "seller": "链动小铺",
            "quantity": quantity,
            "amount": amount,
            "paid_at": paid_at,
            "status": status,
            "_source": "链动小铺",
            "_raw_tokens": tokens[max(0, ld_idx - 2):end_idx],
        })
    return rows


_THATIM_ORDER_ID_RE = re.compile(r"(?:^|\s)(?:#|Mã\s*đơn\s*[:：]?\s*|订单号\s*[:：]?\s*)([A-Za-z0-9_-]{4,24})\b", re.IGNORECASE)
_THATIM_PURE_ID_RE = re.compile(r"^#?(\d{5,12})$")
_THATIM_VND_RE = re.compile(r"(\d{1,3}(?:[.,]\d{3})+)\s*(?:₫|(?:đ|vnd)\b)", re.IGNORECASE)
_THATIM_USD_RE = re.compile(r"(?:\$\s*(\d+(?:[.,]\d{1,6})?)|(\d+(?:[.,]\d{1,6})?)\s*(?:\$|usd\b))", re.IGNORECASE)
_THATIM_DMY_TS_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\s+(\d{2}:\d{2}:\d{2})\b")
_THATIM_STATUS_WORDS = ("发表", "取消", "已完成", "已取消", "已退款", "hoàn thành", "đã hủy", "hủy", "completed", "cancelled", "canceled")


def _parse_thatim_text(text: str, *, force: bool = False) -> list[dict]:
    low = text.lower()
    has_signal = (
        "thatim" in low
        or "₫" in text
        or "vnd" in low
        or "hoàn thành" in low
        or "đã hủy" in low
        or "mã đơn" in low
        or bool(re.search(r"\d+,\d{2,4}\$", text))
        or ("发表" in text and ("$" in text or "₫" in text))
    )
    if not force and not has_signal:
        return []
    lines = [_collapse_ws(ln) for ln in text.splitlines() if _collapse_ws(ln)]
    if not lines:
        return []

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        is_new = bool(_THATIM_PURE_ID_RE.match(line)) or bool(re.match(r"^(?:#\d{4,}|Mã\s*đơn|订单号\s*[:：]?\s*#?\d{4,})", line, re.IGNORECASE))
        if is_new and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    rows: list[dict] = []
    for block in blocks:
        joined = " | ".join(block)
        order_id = ""
        for ln in block:
            pm = _THATIM_PURE_ID_RE.match(ln)
            if pm:
                order_id = pm.group(1)
                break
            om = _THATIM_ORDER_ID_RE.search(ln)
            if om:
                order_id = om.group(1)
                break
        if not order_id:
            continue
        paid_at = ""
        ts_m = _TS_RE.search(joined)
        if ts_m:
            paid_at = ts_m.group(1).replace("/", "-")
        else:
            dmy = _THATIM_DMY_TS_RE.search(joined)
            if dmy:
                paid_at = f"{dmy.group(3)}-{dmy.group(2)}-{dmy.group(1)} {dmy.group(4)}"
        amount_val: Any = ""
        currency = "VND"
        vnd_m = _THATIM_VND_RE.search(joined)
        usd_m = _THATIM_USD_RE.search(joined)
        if vnd_m:
            amount_val = f"{vnd_m.group(1)}₫"
            currency = "VND"
        elif usd_m:
            amount_val = f"{usd_m.group(1) or usd_m.group(2)}$"
            currency = "USD"
        if amount_val == "":
            continue
        status = ""
        for ln in block:
            ln_low = ln.lower()
            if any(w in ln_low for w in _THATIM_STATUS_WORDS):
                status = _normalize_upstream_status(ln)
                break
        title_parts: list[str] = []
        for ln in block:
            if _THATIM_PURE_ID_RE.match(ln):
                continue
            if _TS_RE.fullmatch(ln) or _THATIM_DMY_TS_RE.fullmatch(ln):
                continue
            if _THATIM_VND_RE.fullmatch(ln) or _THATIM_USD_RE.fullmatch(ln):
                continue
            if ln.lower() in _THATIM_STATUS_WORDS:
                continue
            cleaned_ln = _THATIM_ORDER_ID_RE.sub("", ln).strip(" :-|·•")
            cleaned_ln = _TS_RE.sub("", cleaned_ln).strip(" :-|·•")
            cleaned_ln = _THATIM_VND_RE.sub("", cleaned_ln).strip(" :-|·•")
            cleaned_ln = _THATIM_USD_RE.sub("", cleaned_ln).strip(" :-|·•")
            if cleaned_ln and cleaned_ln.lower() not in _THATIM_STATUS_WORDS:
                title_parts.append(cleaned_ln)
        product = " ".join(title_parts[:2]).strip() or f"Thatim 订单 #{order_id}"
        rows.append({
            "order_id": order_id,
            "product": product,
            "seller": "Thatim (2号)",
            "quantity": 1,
            "amount": amount_val,
            "_amount_currency": currency,
            "_allow_zero_amount": status in ("已取消", "已退款", "已失败"),
            "paid_at": paid_at,
            "status": status or "已完成",
            "_source": "thatim",
            "_raw_lines": block,
        })
    return rows


def _has_card_header(first_line: str) -> bool:
    parts = {p.strip().strip('"').strip("'") for p in re.split(r"[\t,]", first_line)}
    all_aliases = {alias for aliases in CARD_HEADERS.values() for alias in aliases if not alias.startswith("_")}
    return len(parts & all_aliases) >= 2


def _is_shifted_wzyp_table(decoded: str, dict_rows: list[dict]) -> bool:
    if not dict_rows or not _LD_ID_RE.search(decoded):
        return False
    first = dict_rows[0]
    oid_val = str(_first(first, CARD_HEADERS["order_id"]) or "").strip()
    amt_val = str(_first(first, CARD_HEADERS["amount"]) or "").strip()
    return bool(_TS_RE.search(oid_val) or _LD_ID_RE.search(amt_val))


def parse_purchases(
    name: str,
    data: bytes,
    *,
    source_hint: str = "",
    usd_cny_rate: Decimal = DEFAULT_USD_CNY_RATE,
    vnd_cny_rate: Decimal = DEFAULT_VND_CNY_RATE,
) -> list[dict]:
    lower_name = name.lower()
    norm_hint = normalize_purchase_source(source_hint, default="") if source_hint else ""
    raw_rows: list[dict] = []
    if lower_name.endswith(".json"):
        raw_rows = _parse_json(data, source_hint=norm_hint)
    elif lower_name.endswith((".tsv", ".txt", ".md", ".paste")) or (lower_name.endswith(".csv") and b"\t" in data[:4096]):
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ImportProblem("粘贴内容不是 UTF-8 文本") from None
        stripped = decoded.strip()
        if stripped.startswith("{") or re.match(r"^\[\s*[\{\"]", stripped):
            try:
                raw_rows = _parse_json(stripped.encode("utf-8"), source_hint=norm_hint)
            except ImportProblem:
                raw_rows = []
        else:
            first_line = next((line for line in decoded.splitlines() if line.strip()), "")
            if _has_card_header(first_line):
                delimiter = "\t" if "\t" in decoded else ","
                dict_rows = [dict(row) for row in csv.DictReader(io.StringIO(decoded), delimiter=delimiter)]
                if _is_shifted_wzyp_table(decoded, dict_rows):
                    raw_rows = _parse_wzyp_text(decoded)
                else:
                    raw_rows = dict_rows
            else:
                # Try Markdown table, 16688 copied page text, and 链动小铺 copied text (can even be combined!)
                if norm_hint == "thatim":
                    raw_rows = _parse_thatim_text(decoded, force=True)
                if not raw_rows:
                    md_rows = _parse_markdown_table(decoded)
                    ff_rows = _parse_16688_text(decoded)
                    ld_rows = _parse_wzyp_text(decoded) if not md_rows else []
                    th_rows = _parse_thatim_text(decoded, force=False) if not (md_rows or ff_rows or ld_rows) else []
                    raw_rows = md_rows + ff_rows + ld_rows + th_rows
                if not raw_rows and ("\t" in decoded or "," in decoded):
                    delimiter = "\t" if "\t" in decoded else ","
                    raw_rows = [dict(row) for row in csv.DictReader(io.StringIO(decoded), delimiter=delimiter)]
    else:
        headers, raw_rows = read_table(name, data)
        if len(headers) == 1:
            decoded = data.decode("utf-8-sig", errors="replace")
            if "\t" in decoded:
                raw_rows = list(csv.DictReader(io.StringIO(decoded), delimiter="\t"))
        raw_rows = [dict(row) for row in raw_rows]
    if not raw_rows:
        raise ImportProblem("卡网文件中没有采购订单")
    result = []
    errors = []
    seen_ids: dict[tuple[str, str], int] = {}
    for index, raw in enumerate(raw_rows, 1):
        try:
            payment = raw.get("payment") if isinstance(raw.get("payment"), dict) else {}
            merged = {**payment, **raw}
            order_id = _first(merged, CARD_HEADERS["order_id"])
            if not order_id:
                raise ImportProblem("缺少卡网订单号")
            ext_id = str(order_id).strip()[:128]
            raw_source = _first(merged, CARD_HEADERS["source"], "")
            if raw_source:
                resolved_source = normalize_purchase_source(raw_source, ext_id=ext_id)
            elif norm_hint:
                resolved_source = norm_hint
            else:
                resolved_source = "链动小铺" if ext_id.startswith("LD") else "16688"

            linked_order_id = str(_first(merged, CARD_HEADERS["linked_order_id"], "") or "").strip()[:128]
            linked_item_id = str(_first(merged, CARD_HEADERS["linked_item_id"], "") or "").strip()[:128]
            linked_channel = str(_first(merged, CARD_HEADERS["linked_channel"], "") or "").strip()[:20]
            linked_redeem_code = str(_first(merged, CARD_HEADERS["linked_redeem_code"], "") or "").strip()[:64]
            raw_status = _first(merged, CARD_HEADERS["status"], "")
            status_str = _normalize_upstream_status(raw_status) if (resolved_source.startswith("tg_") or resolved_source == "thatim") else str(raw_status)[:40]
            allow_zero_amount = bool(merged.get("_allow_zero_amount")) or bool(linked_order_id or linked_redeem_code) or status_str in ("已取消", "已退款", "已失败")

            amount_val, amount_key = _first_with_key(merged, CARD_HEADERS["amount"])
            if amount_val in (None, ""):
                if allow_zero_amount:
                    amount_val = "0"
                    amount_key = "_zero"
                else:
                    raise ImportProblem("缺少采购金额")
            explicit_currency = _first(merged, CARD_HEADERS["currency"], "")
            if amount_key in ("实付金额", "amount_cny"):
                amount_currency = "CNY"
            elif amount_key in ("cost_usd", "amountUsd"):
                amount_currency = "USD"
            elif amount_key == "amountVnd":
                amount_currency = "VND"
            else:
                amount_currency = _detect_currency(amount_val, explicit_currency, source=resolved_source)
            amount_dec = _parse_decimal_amount(amount_val, currency_hint=amount_currency)
            if amount_dec is None:
                raise ImportProblem("缺少采购金额")
            if amount_dec < 0:
                raise ImportProblem("采购金额不能为负数")
            if amount_dec == 0 and not allow_zero_amount:
                raise ImportProblem("缺少采购金额")

            if amount_key in ("实付金额", "amount_cny"):
                amount_fen, fx_rate_str, orig_amt_str = _convert_to_cny_fen(amount_dec, "CNY", usd_cny_rate, vnd_cny_rate)
                orig_currency = "CNY"
                plan1_usd = merged.get("原币金额USD")
                if plan1_usd not in (None, ""):
                    usd_dec = _parse_decimal_amount(plan1_usd, currency_hint="USD")
                    if usd_dec is not None and usd_dec > 0:
                        orig_currency = "USD"
                        orig_amt_str = _format_decimal_clean(usd_dec)
                        csv_fx = _first(merged, CARD_HEADERS["fx_rate"], "")
                        csv_fx_dec = _parse_decimal_amount(csv_fx) if csv_fx not in (None, "") else None
                        if csv_fx_dec and csv_fx_dec > 0:
                            fx_rate_str = _format_decimal_clean(csv_fx_dec)
                        elif amount_dec > 0:
                            implied_fx = (amount_dec / usd_dec).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                            fx_rate_str = _format_decimal_clean(implied_fx)
                        else:
                            fx_rate_str = _format_decimal_clean(usd_cny_rate)
            else:
                amount_fen, fx_rate_str, orig_amt_str = _convert_to_cny_fen(amount_dec, amount_currency, usd_cny_rate, vnd_cny_rate)
                orig_currency = amount_currency

            qty_raw = _first(merged, CARD_HEADERS["quantity"], 1)
            if isinstance(qty_raw, str):
                qm = _QTY_RE.search(qty_raw)
                if qm:
                    qty_raw = qm.group(1) or qm.group(2)
            try:
                quantity = int(str(qty_raw).strip())
            except ValueError:
                raise ImportProblem("采购数量不是整数") from None
            if quantity < 1 or quantity > 100000:
                raise ImportProblem("采购数量超出范围")

            refund_raw = _first(merged, CARD_HEADERS["refund"], 0)
            refund_fen = 0
            if refund_raw not in (None, "", 0, "0", "0.00"):
                ref_cur = _detect_currency(refund_raw, explicit_currency if amount_currency != "CNY" else "CNY", source=resolved_source)
                ref_dec = _parse_decimal_amount(refund_raw, currency_hint=ref_cur)
                if ref_dec is not None and ref_dec > 0:
                    refund_fen, _, _ = _convert_to_cny_fen(ref_dec, ref_cur, usd_cny_rate, vnd_cny_rate)

            seller_val = str(_first(merged, CARD_HEADERS["seller"]) or SOURCE_DEFAULT_SELLER.get(resolved_source, ""))[:200]
            raw_source_payload = dict(raw.get("_raw")) if isinstance(raw.get("_raw"), dict) else dict(raw)
            sanitized_payload = sanitize_raw_payload(raw_source_payload)
            raw_payload = sanitized_payload if isinstance(sanitized_payload, dict) else {}
            if linked_item_id and not raw_payload.get("agiso_oid"):
                raw_payload["agiso_oid"] = linked_item_id
            if linked_order_id and not raw_payload.get("external_order_id"):
                raw_payload["external_order_id"] = linked_order_id
            if linked_redeem_code and not raw_payload.get("code"):
                raw_payload["code"] = linked_redeem_code
            if linked_channel and not raw_payload.get("channel"):
                raw_payload["channel"] = linked_channel
            row = {
                "purchase_external_id": ext_id,
                "product_title": str(_first(merged, CARD_HEADERS["product"]))[:500],
                "seller": seller_val,
                "quantity": quantity,
                "amount_fen": amount_fen,
                "refund_fen": refund_fen,
                "paid_at": timestamp(_first(merged, CARD_HEADERS["paid_at"]), optional=True),
                "status": status_str[:40],
                "source": resolved_source[:80],
                "original_currency": orig_currency[:12],
                "original_amount": orig_amt_str[:32],
                "fx_rate": fx_rate_str[:20],
                "linked_order_external_id": linked_order_id,
                "linked_channel": linked_channel,
                "linked_redeem_code": linked_redeem_code,
                "raw_data": raw_payload,
            }
            dedupe_key = (row["source"], ext_id)
            if dedupe_key in seen_ids:
                prev_idx = seen_ids[dedupe_key]
                prev = result[prev_idx]
                result[prev_idx] = _merge_purchase_dicts(prev, row)
            else:
                seen_ids[dedupe_key] = len(result)
                result.append(row)
        except ImportProblem as exc:
            if len(errors) < 20:
                errors.append(f"第 {index} 条：{exc}")
    if errors:
        raise CardPurchaseImportError(errors)
    return result


_BUSINESS_KEYS = (
    "product_title",
    "seller",
    "quantity",
    "amount_fen",
    "refund_fen",
    "paid_at",
    "status",
    "source",
    "original_currency",
    "original_amount",
    "fx_rate",
    "linked_order_external_id",
    "linked_channel",
    "linked_redeem_code",
)
_RAW_LINK_KEYS = ("external_order_id", "agiso_oid", "agiso_tid", "code", "sku_key", "sku_id", "channel", "goods_no")
_TERMINAL_NEGATIVE_STATUSES = ("已退款", "已取消", "已失败", "已关闭")
_WEAK_STATUSES = ("", "处理中", "待付款")


def _resolve_merged_status(existing_status: str, incoming_status: str, merged_refund_fen: int) -> str:
    prev_s = str(existing_status or "").strip()
    inc_s = str(incoming_status or "").strip()
    if merged_refund_fen > 0 or prev_s == "已退款" or inc_s == "已退款":
        return "已退款"
    if inc_s in _TERMINAL_NEGATIVE_STATUSES:
        return inc_s
    if prev_s in _TERMINAL_NEGATIVE_STATUSES and inc_s in _WEAK_STATUSES:
        return prev_s
    if not inc_s and prev_s:
        return prev_s
    if prev_s in ("已完成", "已付款") and inc_s in _WEAK_STATUSES:
        return prev_s
    if prev_s == "已完成" and inc_s == "已付款":
        return prev_s
    return inc_s or prev_s


def _merge_purchase_dicts(prev: dict, incoming: dict) -> dict:
    merged = dict(incoming)
    merged_refund_fen = max(int(prev.get("refund_fen") or 0), int(merged.get("refund_fen") or 0))
    merged["refund_fen"] = merged_refund_fen
    if int(merged.get("amount_fen") or 0) <= 0 and int(prev.get("amount_fen") or 0) > 0:
        merged["amount_fen"] = prev["amount_fen"]
        merged["original_currency"] = prev.get("original_currency") or merged.get("original_currency") or "CNY"
        merged["original_amount"] = prev.get("original_amount") or merged.get("original_amount") or ""
        merged["fx_rate"] = prev.get("fx_rate") or merged.get("fx_rate") or "1"
        if prev.get("product_title") and (
            not merged.get("product_title")
            or str(merged.get("product_title") or "").strip().lower().startswith("refund wallet")
        ):
            merged["product_title"] = prev["product_title"]
    if (not merged.get("original_amount") or merged.get("original_amount") == "0") and prev.get("original_amount") and prev.get("original_amount") != "0":
        merged["original_currency"] = prev.get("original_currency") or merged.get("original_currency") or "CNY"
        merged["original_amount"] = prev["original_amount"]
        merged["fx_rate"] = prev.get("fx_rate") or merged.get("fx_rate") or "1"
    if not merged.get("seller") and prev.get("seller"):
        merged["seller"] = prev["seller"]
    elif prev.get("seller") == "FatBunny Hub (方案二)" and merged.get("seller") in ("", "FatBunny Hub", "Canboso"):
        merged["seller"] = prev["seller"]
    if not merged.get("product_title") and prev.get("product_title"):
        merged["product_title"] = prev["product_title"]
    if not merged.get("paid_at") and prev.get("paid_at"):
        merged["paid_at"] = prev["paid_at"]
    merged["status"] = _resolve_merged_status(prev.get("status") or "", merged.get("status") or "", merged_refund_fen)
    if int(merged.get("quantity") or 1) == 1 and int(prev.get("quantity") or 1) > 1 and int(prev.get("amount_fen") or 0) == int(merged.get("amount_fen") or 0):
        merged["quantity"] = prev["quantity"]
    if not merged.get("linked_order_external_id") and prev.get("linked_order_external_id"):
        merged["linked_order_external_id"] = prev["linked_order_external_id"]
    if not merged.get("linked_channel") and prev.get("linked_channel"):
        merged["linked_channel"] = prev["linked_channel"]
    if not merged.get("linked_redeem_code") and prev.get("linked_redeem_code"):
        merged["linked_redeem_code"] = prev["linked_redeem_code"]
    if prev.get("source") in ("16688", "16688页面") and merged.get("source") in ("16688", "16688页面"):
        merged["source"] = prev["source"]
    if prev.get("source") in ("tg_fatbunny", "tg_chugax") and merged.get("source") == "tg_canboso":
        merged["source"] = prev["source"]
    prev_raw = sanitize_raw_payload(prev.get("raw_data")) if isinstance(prev.get("raw_data"), dict) else {}
    inc_raw = sanitize_raw_payload(merged.get("raw_data")) if isinstance(merged.get("raw_data"), dict) else {}
    combined_raw = dict(prev_raw if isinstance(prev_raw, dict) else {})
    for rk, rv in (inc_raw.items() if isinstance(inc_raw, dict) else ()):
        if rv not in (None, "") or rk not in combined_raw:
            combined_raw[rk] = rv
    merged["raw_data"] = combined_raw
    return merged


def _merge_purchase_row(existing: CardPurchase, incoming: dict) -> tuple[dict, bool]:
    existing_raw = existing.raw_data if isinstance(existing.raw_data, dict) else {}
    sanitized_existing_raw = sanitize_raw_payload(existing_raw)
    prev_dict = {key: getattr(existing, key) for key in _BUSINESS_KEYS}
    prev_dict["purchase_external_id"] = existing.purchase_external_id
    prev_dict["raw_data"] = sanitized_existing_raw if isinstance(sanitized_existing_raw, dict) else {}
    merged = _merge_purchase_dicts(prev_dict, incoming)
    combined_raw = merged["raw_data"]
    raw_link_changed = any(combined_raw.get(rkey) != existing_raw.get(rkey) for rkey in _RAW_LINK_KEYS)
    raw_sanitized_changed = sanitized_existing_raw != existing_raw
    changed = raw_link_changed or raw_sanitized_changed or any(getattr(existing, key) != merged[key] for key in _BUSINESS_KEYS)
    return merged, changed


def import_purchases(db: Session, store_id: int, name: str, data: bytes, *, source_hint: str = "") -> dict:
    usd_rate, vnd_rate = get_store_fx_rates(db, store_id)
    rows = parse_purchases(name, data, source_hint=source_hint, usd_cny_rate=usd_rate, vnd_cny_rate=vnd_rate)
    digest_source = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(digest_source).hexdigest()
    batch = db.scalar(select(ImportBatch).where(
        ImportBatch.store_id == store_id,
        ImportBatch.kind == "card_purchases",
        ImportBatch.file_hash == digest,
        ImportBatch.status == "committed",
    ))
    if batch:
        return {"duplicate": True, "batch_id": batch.id, "inserted": 0, "updated": 0, "skipped": len(rows), "row_count": batch.row_count}
    batch = ImportBatch(store_id=store_id, kind="card_purchases", file_hash=digest, basis=None, row_count=len(rows), status="committed")
    from .models import now_iso
    batch.committed_at = now_iso()
    db.add(batch)
    db.flush()
    inserted = updated = skipped = 0
    for row in rows:
        existing = db.scalar(select(CardPurchase).where(
            CardPurchase.store_id == store_id,
            CardPurchase.source == row["source"],
            CardPurchase.purchase_external_id == row["purchase_external_id"],
        ))
        if existing is None and row["source"] in ("16688", "16688页面"):
            existing = db.scalar(select(CardPurchase).where(
                CardPurchase.store_id == store_id,
                CardPurchase.source.in_(("16688", "16688页面")),
                CardPurchase.purchase_external_id == row["purchase_external_id"],
            ))
        if existing is None and row["source"] in ("tg_fatbunny", "tg_chugax"):
            existing = db.scalar(select(CardPurchase).where(
                CardPurchase.store_id == store_id,
                CardPurchase.source == "tg_canboso",
                CardPurchase.purchase_external_id == row["purchase_external_id"],
            ))
        if existing is None and row["source"] == "tg_canboso":
            canboso_matches = list(db.scalars(select(CardPurchase).where(
                CardPurchase.store_id == store_id,
                CardPurchase.source.in_(("tg_fatbunny", "tg_chugax")),
                CardPurchase.purchase_external_id == row["purchase_external_id"],
            )))
            if len(canboso_matches) == 1:
                existing = canboso_matches[0]
        if existing:
            merged_row, changed = _merge_purchase_row(existing, row)
            if changed:
                for key, value in merged_row.items():
                    setattr(existing, key, value)
                existing.batch_id = batch.id
                updated += 1
            else:
                skipped += 1
        else:
            created = CardPurchase(store_id=store_id, batch_id=batch.id, **row)
            db.add(created)
            inserted += 1
    if inserted == 0 and updated == 0:
        db.rollback()
        return {
            "duplicate": True,
            "batch_id": None,
            "inserted": 0,
            "updated": 0,
            "skipped": skipped,
            "row_count": len(rows),
        }
    db.commit()
    return {"duplicate": False, "batch_id": batch.id, "inserted": inserted, "updated": updated, "skipped": skipped, "row_count": len(rows)}


@dataclass(frozen=True)
class ProductFeatures:
    brand: str          # gemini | muse | gmail_blank | chatgpt_plus | claude | fee_diff | other
    tier: str           # ultra | pro | free | none
    form: str           # finished_account | activation_link | family_slot | invite_boost | plain_gmail | unknown
    durations: frozenset[int]
    category: str       # gemini_finished | activation | muse_finished | gemini_family | other
    is_non_product: bool
    spec_text: str


def _extract_spec(sku: str) -> str:
    raw = str(sku or "").strip()
    if not raw:
        return ""
    if "|" in raw:
        tail = "|".join(part.strip() for part in raw.split("|")[1:]).strip()
        tail = re.sub(r"^(?:颜色分类|套餐类型|规格|版本)\s*[:：]\s*", "", tail)
        return tail
    if re.fullmatch(r"\d{8,}", raw):
        return ""
    return raw


def _extract_durations(text: str) -> frozenset[int]:
    if not text:
        return frozenset()
    normalized = text
    for cn, num in (("十八", "18"), ("十二", "12"), ("六", "6"), ("三", "3"), ("一", "1")):
        normalized = normalized.replace(f"{cn}个月", f"{num}个月").replace(f"{cn}月", f"{num}月")
    months: set[int] = set()
    for match in re.finditer(r"(\d{1,2})\s*个?月", normalized):
        val = int(match.group(1))
        if val in (1, 3, 6, 12, 18, 24):
            months.add(val)
    for match in re.finditer(r"(?<![A-Za-z0-9])(\d{1,2})\s*(?:months?|mos?|tháng|thang|m)\b", normalized, re.IGNORECASE):
        val = int(match.group(1))
        if val in (1, 3, 6, 12, 18, 24):
            months.add(val)
    low = normalized.lower()
    if "月卡" in normalized or "30天" in normalized or "monthly" in low or "30 days" in low:
        months.add(1)
    if "季卡" in normalized:
        months.add(3)
    if "年卡" in normalized or re.search(r"\b1\s*(?:year|yr|năm|nam)\b", low) or "yearly" in low or "annual" in low:
        months.add(12)
    return frozenset(months)


def _detect_form_from_text(text: str, brand: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    if brand == "gmail_blank" or "无会员白号" in text or ("白号" in text and ("gmail" in lower or "谷歌" in text or "邮箱" in text)):
        return "plain_gmail"
    if brand == "muse":
        if any(k in text for k in ("代刷", "帮刷", "帮填", "助力", "邀请人数", "带刷", "邀请码")) and "成品号" not in text:
            return "invite_boost"
        if any(k in text for k in ("成品号", "成品账号", "成品")):
            return "finished_account"
        return None
    if brand == "gemini":
        if any(k in text for k in ("家庭组", "拼车", "车位", "席位", "家庭邀请", "共享车位", "独享整组", "整组", "成员席位")) or any(
            k in lower for k in ("member seat", "admin full fam", "full fam", "family")
        ):
            return "family_slot"
        if any(k in text for k in ("激活资格", "激活链接", "兑换链接", "充到自己号", "一键领取", "无需账密", "直接激活", "代充", "开通", "链接", "升级至", "升级到")) or any(
            k in lower for k in ("upgrade", "invite link", "activation")
        ):
            if not any(k in text for k in ("成品号", "成品pro", "成品会员号", "带邮箱", "送邮", "老谷歌邮箱订阅号", "pixel成品", "年邮箱")):
                return "activation_link"
        if any(k in text for k in ("成品号", "成品pro", "成品会员号", "带邮箱", "送邮", "老谷歌邮箱订阅号", "pixel成品", "年邮箱")):
            return "finished_account"
        if any(k in text for k in ("激活资格", "激活链接", "兑换链接", "充到自己号", "一键领取", "无需账密", "直接激活", "代充", "开通", "链接")):
            return "activation_link"
        return None
    if any(k in text for k in ("家庭组", "拼车", "车位", "席位", "共享车位", "独享整组")) or any(
        k in lower for k in ("member seat", "admin full fam", "full fam", "family")
    ):
        return "family_slot"
    if any(k in text for k in ("成品号", "成品账号", "成品")):
        return "finished_account"
    if any(k in text for k in ("激活", "兑换链接", "订阅", "代充", "开通", "升级至")) or "upgrade" in lower:
        return "activation_link"
    return None


def _is_umbrella_multi_form_title(title: str) -> bool:
    if not title:
        return False
    has_fin = any(k in title for k in ("成品号", "成品pro", "带邮箱", "送邮"))
    has_act = any(k in title for k in ("激活", "兑换链接", "充到自己号", "代充", "链接", "家庭组"))
    return has_fin and has_act


def extract_features(sku: str, title: str, *, amount_fen: int | None = None, is_sale: bool = False) -> ProductFeatures:
    spec = _extract_spec(sku)
    combined = f"{spec} {title}".strip()
    lower = combined.lower()
    spec_lower = spec.lower()

    if "补差价" in combined:
        return ProductFeatures(
            brand="fee_diff",
            tier="none",
            form="unknown",
            durations=frozenset(),
            category="other",
            is_non_product=True,
            spec_text=spec,
        )

    spec_is_pure_gmail = (
        bool(spec)
        and any(k in spec_lower for k in ("无会员", "白号", "gmail", "谷歌邮箱", "谷歌账号", "老谷歌", "个人谷歌"))
        and not any(k in spec_lower for k in ("gemini", "pro", "ultra", "会员", "激活", "家庭", "18个月", "12个月", "6个月", "3个月", "1个月"))
    )

    if "muse" in lower:
        brand = "muse"
    elif "capcut" in lower or "剪映" in combined:
        brand = "capcut"
    elif "claude" in lower:
        brand = "claude"
    elif spec_is_pure_gmail or ("无会员" in combined and ("gmail" in lower or "谷歌" in combined)) or (
        ("gmail" in lower or "谷歌邮箱" in combined)
        and "gemini" not in lower
        and "pro" not in lower
        and "ultra" not in lower
        and "会员" not in combined
    ):
        brand = "gmail_blank"
    elif any(k in lower for k in ("gemini", "argon", "agron", "antigravity", "antiravity", "veo3", "veo 3", "ai ultra", "google ultra", "反重力", "google ai", "jio", "20倍")):
        brand = "gemini"
    elif any(k in lower for k in ("chatgpt", "plus", "codex", "cpa", "已接")):
        brand = "chatgpt_plus"
    else:
        brand = "other"

    if brand == "gmail_blank":
        tier = "none"
    elif any(k in lower for k in ("ultra", "20x", "x20", "5x", "20倍", "veo3", "veo 3")):
        tier = "ultra"
    elif "free" in lower:
        tier = "free"
    elif any(k in lower for k in ("pro", "会员", "argon", "agron")):
        tier = "pro"
    else:
        tier = "none"

    durations = frozenset() if brand == "gmail_blank" else (_extract_durations(spec) or _extract_durations(title))

    # Prioritize SKU spec over generic listing title!
    form = _detect_form_from_text(spec, brand)
    if form is None:
        if is_sale and _is_umbrella_multi_form_title(title):
            if brand == "gemini" and amount_fen is not None and (18 in durations or 12 in durations):
                if amount_fen >= 4200:
                    form = "finished_account"
                elif 0 < amount_fen <= 2300:
                    form = "activation_link"
                else:
                    form = "unknown"
            else:
                form = "unknown"
        else:
            form = _detect_form_from_text(title, brand) or "unknown"

    if brand == "muse":
        category = "activation" if form == "invite_boost" else "muse_finished"
    elif brand == "gemini":
        if form == "unknown" and is_sale and amount_fen is not None and 18 in durations and amount_fen >= 4000:
            form = "finished_account"
        if form == "family_slot":
            category = "gemini_family"
        elif form == "finished_account":
            category = "gemini_finished"
        elif form == "activation_link":
            category = "activation"
        elif tier == "ultra" and ("家庭" in combined or "车位" in combined):
            category = "gemini_family"
        elif is_sale and amount_fen is not None and amount_fen >= 4000 and 18 in durations:
            category = "gemini_finished"
        else:
            category = "activation"
    else:
        category = "other"

    return ProductFeatures(
        brand=brand,
        tier=tier,
        form=form,
        durations=durations,
        category=category,
        is_non_product=False,
        spec_text=spec,
    )


def classify_product(sku: str, title: str, *, amount_fen: int | None = None, is_sale: bool = False) -> str:
    return extract_features(sku, title, amount_fen=amount_fen, is_sale=is_sale).category


def _is_inactive_purchase(purchase: CardPurchase) -> bool:
    status = str(purchase.status or "")
    low = status.lower()
    if any(bad in low for bad in ("退款", "关闭", "待付", "待支", "取消", "失败", "撤销", "cancel", "failed", "refund", "hủy", "thất bại", "unpaid", "void", "closed")):
        return True
    return (purchase.amount_fen - purchase.refund_fen) <= 0


def _compatible(sale: ProductFeatures, buy: ProductFeatures) -> tuple[bool, float]:
    if sale.is_non_product:
        return False, 0.0
    if sale.brand != "other" and buy.brand != "other" and sale.brand != buy.brand:
        return False, 0.0
    if sale.tier in ("ultra", "pro") and buy.tier in ("ultra", "pro") and sale.tier != buy.tier:
        return False, 0.0
    if sale.form == "plain_gmail" or buy.form == "plain_gmail":
        if sale.form != buy.form:
            return False, 0.0
    if sale.durations and buy.durations and not (sale.durations & buy.durations):
        return False, 0.0

    if sale.form != "unknown" and buy.form != "unknown":
        if sale.form == buy.form:
            form_compat = 1.0
        elif {sale.form, buy.form} == {"activation_link", "family_slot"} and (1 in sale.durations or 1 in buy.durations):
            # 1-month "充到自己号" is frequently fulfilled via 1-month family-group slot invites
            form_compat = 0.95
        else:
            return False, 0.0
    elif sale.form == "unknown" or buy.form == "unknown":
        form_compat = 0.78
    else:
        form_compat = 0.5
    return True, form_compat


def _is_duration_fallback_spec(sale: ProductFeatures, buy: ProductFeatures) -> bool:
    if sale.brand != "gemini" or buy.brand != "gemini" or sale.is_non_product:
        return False
    tier_ok = (
        sale.tier != "ultra"
        and buy.tier != "ultra"
        and (sale.tier == buy.tier or "none" in (sale.tier, buy.tier))
    )
    return bool(
        tier_ok
        and sale.form == "activation_link"
        and buy.form == "activation_link"
        and sale.durations == frozenset({1})
        and buy.durations
        and min(buy.durations) >= 12
    )


def _signed_gap_minutes(sale_paid_at: str | None, buy_paid_at: str | None) -> int | None:
    if not sale_paid_at or not buy_paid_at:
        return None
    try:
        sale_dt = datetime.fromisoformat(sale_paid_at)
        buy_dt = datetime.fromisoformat(buy_paid_at)
        return int(round((buy_dt - sale_dt).total_seconds() / 60))
    except ValueError:
        return None


def _time_score(signed_minutes: int | None, purchase_qty: int, *, buy_form: str | None = None) -> tuple[float, list[str]]:
    if signed_minutes is None:
        return 0.2, ["缺少下单或采购时间"]
    warnings: list[str] = []
    if signed_minutes >= 0:
        if purchase_qty >= 2 and signed_minutes > 90:
            if buy_form in ("finished_account", "plain_gmail"):
                if signed_minutes <= 210:
                    return 0.42 - 0.16 * ((signed_minutes - 90) / 120), [f"采购晚于下单 {signed_minutes} 分钟，请核对是否同单"]
                if signed_minutes <= 60 * 72:
                    hours = max(1, round(signed_minutes / 60))
                    return max(0.14, 0.26 - 0.12 * ((signed_minutes - 210) / (60 * 72 - 210))), [
                        f"批量采购晚于下单约 {hours} 小时（{signed_minutes} 分钟），请核对是否先发后采或补货"
                    ]
            return -1.0, []
        if signed_minutes > 210:
            return -1.0, []
        if signed_minutes <= 5:
            return 1.0, warnings
        if signed_minutes <= 15:
            return 0.94, warnings
        if signed_minutes <= 60:
            return 0.88 - 0.16 * ((signed_minutes - 15) / 45), warnings
        if signed_minutes <= 90:
            return 0.72 - 0.18 * ((signed_minutes - 60) / 30), warnings
        return 0.42 - 0.16 * ((signed_minutes - 90) / 120), [f"采购晚于下单 {signed_minutes} 分钟，请核对是否同单"]

    early_mins = -signed_minutes
    if purchase_qty >= 2:
        # Pre-stocked bulk inventory used after purchase
        if early_mins <= 60 * 24:
            return 0.86, warnings
        if early_mins <= 60 * 72:
            return 0.78, warnings
        if early_mins <= 60 * 24 * 7:
            return 0.66, warnings
        if early_mins <= 60 * 24 * 14:
            warnings.append("批量预采超过 7 天")
            return 0.54, warnings
        return -1.0, []

    if early_mins <= 3:
        return 0.95, warnings
    if early_mins <= 5:
        return 0.90, warnings
    if buy_form in ("finished_account", "plain_gmail"):
        if early_mins <= 15:
            return 0.84, warnings
        if early_mins <= 60:
            warnings.append(f"提前备货 {early_mins} 分钟")
            return 0.74, warnings
        if early_mins <= 60 * 24:
            hours = max(1, round(early_mins / 60))
            warnings.append(f"提前备货（早于下单约 {hours} 小时）")
            return 0.58, warnings
        if early_mins <= 60 * 48:
            hours = max(1, round(early_mins / 60))
            warnings.append(f"提前备货（早于下单约 {hours} 小时）")
            return 0.44, warnings
        return -1.0, []
    if early_mins <= 30:
        warnings.append(f"采购早于下单 {early_mins} 分钟")
        return 0.52, warnings
    if early_mins <= 180:
        warnings.append(f"采购早于下单 {early_mins} 分钟")
        return 0.24, warnings
    return -1.0, []


def _price_score(sale_pay_fen: int | None, allocated_cost_fen: int, *, same_form: bool = False) -> tuple[float, list[str]]:
    if allocated_cost_fen <= 0:
        return 0.0, ["采购净额为 0"]
    if not sale_pay_fen or sale_pay_fen <= 0:
        return 0.55, []
    ratio = allocated_cost_fen / sale_pay_fen
    low_full = 0.12 if same_form else 0.20
    low_warn = 0.08 if same_form else 0.14
    if low_full <= ratio <= 0.75:
        return 1.0, []
    if low_warn <= ratio < low_full:
        span = max(0.01, low_full - low_warn)
        return 0.74 + ((ratio - low_warn) / span) * 0.26, []
    if 0.05 <= ratio < low_warn:
        span = max(0.01, low_warn - 0.05)
        return 0.30 + ((ratio - 0.05) / span) * 0.44, ["采购价偏低，请核对规格"]
    if 0.75 < ratio <= 0.90:
        return 0.68 + ((0.90 - ratio) / 0.18) * 0.32, ([] if ratio <= 0.82 else ["利润率偏低"])
    if 0.90 < ratio <= 1.00:
        return 0.55, ["利润率偏低"]
    if ratio > 1.00:
        return 0.12, ["采购成本高于买家实付"]
    return 0.10, ["采购价与售价差距过大"]


def _normalize_promo_title(title: str) -> str:
    cleaned = _collapse_ws(title).lower()
    cleaned = re.sub(r"[（(\[【]?\s*(?:活动)?倒计时\s*\d+\s*[天小时分秒]+\s*[）)\]】]?", "", cleaned)
    cleaned = re.sub(r"[（(\[【]?\s*限时(?:特惠|秒杀|促销|活动)[^）)\]】]*[）)\]】]?", "", cleaned)
    return _collapse_ws(cleaned)


def _infer_16688_multi_units(purchases: list[CardPurchase]) -> dict[int, tuple[int, int]]:
    """Infer effective multi-unit quantity for 16688 purchases whose list text omits 'xN'."""
    groups: dict[tuple[str, str], list[CardPurchase]] = defaultdict(list)
    for purchase in purchases:
        if _is_inactive_purchase(purchase):
            continue
        seller_key = _collapse_ws(purchase.seller).lower()
        title_key = _normalize_promo_title(purchase.product_title)
        if title_key:
            groups[(seller_key, title_key)].append(purchase)

    inferred: dict[int, tuple[int, int]] = {}
    for purchase in purchases:
        if _is_inactive_purchase(purchase) or purchase.quantity != 1:
            continue
        src = str(purchase.source or "")
        if src.startswith("tg_") or src == "thatim":
            continue
        is_16688 = "16688" in src or (purchase.purchase_external_id or "").startswith("FF")
        if not is_16688:
            continue
        net_fen = purchase.amount_fen - purchase.refund_fen
        if net_fen <= 0:
            continue
        key = (_collapse_ws(purchase.seller).lower(), _normalize_promo_title(purchase.product_title))
        siblings = groups.get(key, [])
        best: tuple[float, int, int, int] | None = None
        for other in siblings:
            if other.id == purchase.id:
                continue
            other_net = other.amount_fen - other.refund_fen
            if other_net <= 0:
                continue
            base_fen = round(other_net / max(1, other.quantity))
            if base_fen < 100 or base_fen >= net_fen:
                continue
            gap = _signed_gap_minutes(purchase.paid_at, other.paid_at)
            if gap is None or abs(gap) > 60 * 48:
                continue
            k = round(net_fen / base_fen)
            if 2 <= k <= 50:
                err = abs(net_fen - k * base_fen)
                if err <= max(3, round(k * base_fen * 0.015)):
                    score_tuple = (err / net_fen, abs(gap), k, base_fen)
                    if best is None or score_tuple < best:
                        best = score_tuple
        if best is not None:
            inferred[purchase.id] = (best[2], best[3])
    return inferred


def _active_confirmed_matches(db: Session, store_id: int, adjustments: list[Adjustment]) -> list[CardMatch]:
    latest_invalidate_ts: dict[str, str] = {}
    for row in adjustments:
        if row.kind == "cost_invalidate":
            latest_invalidate_ts[row.item_external_id] = row.created_at or ""
        elif row.kind == "cost_set":
            latest_invalidate_ts.pop(row.item_external_id, None)
    raw_matches = list(db.scalars(select(CardMatch).where(CardMatch.store_id == store_id, CardMatch.status == "confirmed")))
    active: list[CardMatch] = []
    for m in raw_matches:
        inv_ts = latest_invalidate_ts.get(m.item_external_id)
        if inv_ts and (m.created_at or "") <= inv_ts:
            continue
        active.append(m)
    return active


_AI_RERANK_CACHE: dict[tuple[str, str, str], int] = {}


def _optional_ai_rerank(ambiguous_pairs: list[dict]) -> dict[str, int]:
    """Optional lightweight semantic check via configured LLM for borderline/other SKU pairs."""
    base = os.environ.get("MULTISTORE_MODEL_URL", "").strip().rstrip("/")
    key = os.environ.get("MULTISTORE_MODEL_API_KEY", "").strip()
    model = os.environ.get("MULTISTORE_MODEL_NAME", "").strip()
    if not (base and model and ambiguous_pairs):
        return {}
    cached_results: dict[str, int] = {}
    pending_pairs: list[dict] = []
    for p in ambiguous_pairs:
        cache_key = (p["sale_sku"], p["sale_title"], p["buy_title"])
        if cache_key in _AI_RERANK_CACHE:
            cached_results[p["pair_id"]] = _AI_RERANK_CACHE[cache_key]
        else:
            pending_pairs.append(p)
    if not pending_pairs:
        return cached_results
    prompt_items = [
        {
            "pair_id": p["pair_id"],
            "sale_sku": p["sale_sku"][:120],
            "sale_title": p["sale_title"][:100],
            "buy_title": p["buy_title"][:120],
            "qty": p["qty"],
            "sale_yuan": p.get("sale_yuan"),
            "buy_yuan": p.get("buy_yuan"),
        }
        for p in pending_pairs[:8]
    ]
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 1200,
        "messages": [
            {
                "role": "system",
                "content": "你是虚拟商品电商规格核对器。注意：虚拟商品正常毛利高，进货成本(buy_yuan)占售价(sale_yuan)15%-75%完全正常，不要因为进价便宜而扣分；如果千牛SKU明确写了子规格(如3个月/白号)，以SKU规格为准而非主标题。重点核对品牌、形态(成品号/激活链接/白号/家庭组)、周期月数是否一致。只输出JSON对象，键为pair_id，值为整数调整分(-8到+8，高度同款给+4到+8，明显不同规格或形态给-8，不确定给0)。",
            },
            {"role": "user", "content": json.dumps(prompt_items, ensure_ascii=False)},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = (((body.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            return cached_results
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            return cached_results
        fresh = {str(k): max(-8, min(8, int(v))) for k, v in parsed.items() if isinstance(v, (int, float))}
        pid_to_item = {p["pair_id"]: p for p in pending_pairs[:8]}
        if len(_AI_RERANK_CACHE) > 256:
            _AI_RERANK_CACHE.clear()
        for pid, val in fresh.items():
            item = pid_to_item.get(pid)
            if item:
                _AI_RERANK_CACHE[(item["sale_sku"], item["sale_title"], item["buy_title"])] = val
        cached_results.update(fresh)
        return cached_results
    except Exception:
        return cached_results


def _is_equivalent_batch_candidate(primary: dict, other: dict) -> bool:
    if primary.get("is_bundle") or other.get("is_bundle"):
        return False
    if primary.get("category") != other.get("category"):
        return False
    s1 = _collapse_ws(primary.get("seller") or "").lower()
    s2 = _collapse_ws(other.get("seller") or "").lower()
    if not s1 or s1 != s2:
        return False
    u1 = int(primary.get("unit_cost_fen") or 0)
    u2 = int(other.get("unit_cost_fen") or 0)
    if u1 <= 0 or u2 <= 0:
        return False
    return abs(u1 - u2) <= max(30, round(u1 * 0.05))


def _extract_cited_purchase_ids(note: str, purchase_by_ext: dict[str, CardPurchase]) -> list[str]:
    if not note:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_-]{4,}", note):
        if token in purchase_by_ext and token not in seen:
            seen.add(token)
            found.append(token)
    if not found:
        for token in re.findall(r"\b(?:FF|LD)[A-Za-z0-9]{6,}\b", note):
            if token not in seen:
                seen.add(token)
                found.append(token)
    return found


def suggestions(
    db: Session,
    store_id: int,
    start: str = "",
    end: str = "",
    limit: int = 100,
    *,
    use_ai: bool = False,
    channel: str = "",
) -> dict:
    stmt = select(OrderItem).where(OrderItem.store_id == store_id, OrderItem.paid_at.is_not(None))
    channel_filter = normalize_channel_filter(channel)
    if channel_filter:
        stmt = stmt.where(OrderItem.channel == channel_filter)
    if start and start != "all":
        stmt = stmt.where(OrderItem.paid_at >= start)
    if end and end != "all":
        stmt = stmt.where(OrderItem.paid_at < end + " 23:59:59.999999")
    orders = list(db.scalars(stmt.order_by(OrderItem.paid_at.desc())))
    purchases = list(db.scalars(select(CardPurchase).where(CardPurchase.store_id == store_id).order_by(CardPurchase.paid_at.desc())))
    adjustments = list(db.scalars(select(Adjustment).where(Adjustment.store_id == store_id).order_by(Adjustment.id.asc())))
    latest_cost: dict[str, str] = {}
    latest_cost_adj: dict[str, Adjustment] = {}
    for row in adjustments:
        if row.kind in ("cost_set", "cost_invalidate"):
            latest_cost[row.item_external_id] = row.kind
            if row.kind == "cost_set":
                latest_cost_adj[row.item_external_id] = row
            else:
                latest_cost_adj.pop(row.item_external_id, None)

    confirmed_matches = _active_confirmed_matches(db, store_id, adjustments)
    matched_confirmed = {row.item_external_id for row in confirmed_matches}

    all_orders_by_item = {
        o.item_external_id: o
        for o in db.scalars(select(OrderItem).where(OrderItem.store_id == store_id))
    }
    purchase_by_id = {p.id: p for p in purchases}
    purchase_by_ext = {p.purchase_external_id: p for p in purchases}

    used_qty_by_purchase: dict[int, int] = defaultdict(int)
    for row in confirmed_matches:
        used_qty_by_purchase[row.purchase_id] += max(1, row.allocated_quantity or 1)

    # Learn confirmed SKU -> seller / typical unit cost affinity
    sku_sellers: dict[str, set[str]] = defaultdict(set)
    sku_unit_costs: dict[str, list[int]] = defaultdict(list)
    for row in confirmed_matches:
        ord_obj = all_orders_by_item.get(row.item_external_id)
        pur_obj = purchase_by_id.get(row.purchase_id)
        if ord_obj and pur_obj:
            if pur_obj.seller:
                sku_sellers[ord_obj.sku].add(pur_obj.seller)
            net_buy = max(0, pur_obj.amount_fen - pur_obj.refund_fen)
            if net_buy > 0 and pur_obj.quantity > 0:
                sku_unit_costs[ord_obj.sku].append(round(net_buy / pur_obj.quantity))

    for item_id, adj in latest_cost_adj.items():
        if item_id in matched_confirmed:
            continue
        cited_ids = _extract_cited_purchase_ids(adj.note or "", purchase_by_ext)
        valid_cited = [purchase_by_ext[cid] for cid in cited_ids if cid in purchase_by_ext]
        if len(valid_cited) == 1:
            cited_purchase = valid_cited[0]
            ord_obj = all_orders_by_item.get(item_id)
            alloc_qty = max(1, ord_obj.quantity if ord_obj else 1)
            used_qty_by_purchase[cited_purchase.id] += alloc_qty
            if ord_obj:
                if cited_purchase.seller:
                    sku_sellers[ord_obj.sku].add(cited_purchase.seller)
                if adj.amount_fen and adj.amount_fen > 0:
                    sku_unit_costs[ord_obj.sku].append(round(adj.amount_fen / alloc_qty))

    inferred_multi = _infer_16688_multi_units(purchases)
    effective_qty_by_purchase = {
        p.id: max(
            used_qty_by_purchase.get(p.id, 0),
            inferred_multi[p.id][0] if p.id in inferred_multi else max(1, p.quantity),
        )
        for p in purchases
    }

    purchase_features = {
        p.id: extract_features("", p.product_title, amount_fen=p.amount_fen - p.refund_fen, is_sale=False)
        for p in purchases
    }

    active_purchases = [p for p in purchases if not _is_inactive_purchase(p)]
    exact_by_item_id: dict[str, list[CardPurchase]] = defaultdict(list)
    exact_by_order_id: dict[str, list[CardPurchase]] = defaultdict(list)
    for p in active_purchases:
        p_raw = p.raw_data if isinstance(p.raw_data, dict) else {}
        agiso_oid = str(p_raw.get("agiso_oid") or "").strip()
        linked_oid = str(p.linked_order_external_id or p_raw.get("external_order_id") or p_raw.get("agiso_tid") or "").strip()
        if agiso_oid:
            exact_by_item_id[agiso_oid].append(p)
        if linked_oid:
            exact_by_order_id[linked_oid].append(p)

    earliest_purchase_ts = min((p.paid_at for p in active_purchases if p.paid_at), default=None)
    window_floor_ts: str | None = None
    if earliest_purchase_ts:
        try:
            window_floor_ts = (datetime.fromisoformat(earliest_purchase_ts) - timedelta(hours=2)).isoformat(sep=" ", timespec="seconds")
        except ValueError:
            window_floor_ts = earliest_purchase_ts

    active_items_per_parent: dict[tuple[str, str], int] = defaultdict(int)
    for order_row in orders:
        if order_row.status == "closed" or order_row.refund_status == "success":
            continue
        active_items_per_parent[(getattr(order_row, "channel", None) or "qianniu", order_row.order_external_id)] += 1

    candidates_by_item: dict[str, list[dict]] = defaultdict(list)
    order_payload: dict[str, dict] = {}
    order_features_map: dict[str, ProductFeatures] = {}
    refunded_orders: list[OrderItem] = []
    missing_cost_count = 0
    pending_sales_fen = 0
    already_reconciled_count = 0
    already_reconciled_sales_fen = 0
    skipped_refunded_count = 0

    for order in orders:
        unit_pay_fen = (order.payment_fen // max(1, order.quantity)) if order.payment_fen is not None else None
        sale_feat = extract_features(order.sku, order.product_title, amount_fen=unit_pay_fen, is_sale=True)
        if sale_feat.is_non_product:
            continue
        cost_invalidated = latest_cost.get(order.item_external_id) == "cost_invalidate"
        has_cost = (
            (order.cost_fen is not None and not cost_invalidated)
            or latest_cost.get(order.item_external_id) == "cost_set"
            or order.item_external_id in matched_confirmed
        )
        is_refunded_or_closed = (order.status == "closed" or order.refund_status == "success")
        if has_cost:
            if not is_refunded_or_closed:
                already_reconciled_count += 1
                already_reconciled_sales_fen += max(0, order.payment_fen or 0)
            continue

        if is_refunded_or_closed:
            skipped_refunded_count += 1
            refunded_orders.append(order)
            continue

        missing_cost_count += 1
        pending_sales_fen += max(0, order.payment_fen or 0)
        order_features_map[order.item_external_id] = sale_feat

        order_channel = getattr(order, "channel", None) or "qianniu"
        order_payload[order.item_external_id] = {
            "item_id": order.item_external_id,
            "order_id": order.order_external_id,
            "channel": order_channel,
            "channel_label": CHANNEL_LABELS.get(order_channel, order_channel),
            "sku": order.sku,
            "product_title": order.product_title,
            "title": order.product_title,
            "quantity": order.quantity,
            "payment_fen": order.payment_fen,
            "paid_at": order.paid_at,
            "status": order.status,
            "refund_status": order.refund_status,
            "category": sale_feat.category,
            "category_label": CATEGORY_LABELS[sale_feat.category],
        }

        # Stage 0: Deterministic exact match via Plan 2 / upstream order link
        parent_active_count = active_items_per_parent.get((order_channel, order.order_external_id), 1)
        linked_seen: set[int] = set()
        linked_available: list[tuple[CardPurchase, int, int, str, bool]] = []
        for link_label, pool_list, require_no_other_oid, is_parent_fallback in (
            ("子订单号精确关联", exact_by_item_id.get(order.item_external_id, ()), False, False),
            ("主订单号精确关联", exact_by_order_id.get(order.order_external_id, ()), True, True),
            ("子单号精确关联", exact_by_order_id.get(order.item_external_id, ()), True, False),
        ):
            for lp in pool_list:
                if lp.id in linked_seen:
                    continue
                lp_raw = lp.raw_data if isinstance(lp.raw_data, dict) else {}
                lp_oid = str(lp_raw.get("agiso_oid") or "").strip()
                if require_no_other_oid:
                    if lp_oid and lp_oid != order.item_external_id:
                        continue
                eff_q = effective_qty_by_purchase.get(lp.id, max(1, lp.quantity))
                rem_q = eff_q - used_qty_by_purchase.get(lp.id, 0)
                if rem_q <= 0:
                    continue
                linked_seen.add(lp.id)
                ambiguous_parent = bool(is_parent_fallback and not lp_oid and parent_active_count > 1)
                linked_available.append((lp, eff_q, rem_q, link_label, ambiguous_parent))

        if linked_available:
            exact_added = False
            for lp, eff_q, rem_q, link_label, ambiguous_parent in linked_available:
                if rem_q < order.quantity:
                    continue
                net_buy_fen = max(0, lp.amount_fen - lp.refund_fen)
                unit_cost_fen = round(net_buy_fen / eff_q)
                effective_cost_fen = round(net_buy_fen * order.quantity / eff_q)
                signed_gap = _signed_gap_minutes(order.paid_at, lp.paid_at)
                abs_gap = abs(signed_gap) if signed_gap is not None else None
                buy_feat = purchase_features[lp.id]
                code_hint = f"（兑换码 {lp.linked_redeem_code}）" if lp.linked_redeem_code else ""
                exact_reasons = [f"方案二{link_label}{code_hint}", f"来源：{source_label(lp.source)}"]
                if order.payment_fen and effective_cost_fen <= order.payment_fen:
                    exact_reasons.append(f"成本占比 {round(effective_cost_fen * 100 / order.payment_fen)}%")
                alloc_entry = {
                    "purchase_id": lp.id,
                    "purchase_external_id": lp.purchase_external_id,
                    "allocated_quantity": order.quantity,
                    "purchase_quantity": lp.quantity,
                    "cost_fen": effective_cost_fen,
                    "paid_at": lp.paid_at,
                    "product_title": lp.product_title,
                }
                candidates_by_item[order.item_external_id].append({
                    "purchase_id": lp.id,
                    "purchase_external_id": lp.purchase_external_id,
                    "product_title": lp.product_title,
                    "seller": lp.seller,
                    "source": lp.source,
                    "source_label": source_label(lp.source),
                    "original_currency": lp.original_currency or "CNY",
                    "original_amount": lp.original_amount or "",
                    "fx_rate": lp.fx_rate or "1",
                    "linked_order_external_id": lp.linked_order_external_id or "",
                    "linked_redeem_code": lp.linked_redeem_code or "",
                    "is_exact_link": True,
                    "quantity": eff_q,
                    "purchase_quantity": lp.quantity,
                    "inferred_quantity": None,
                    "remaining_quantity": rem_q,
                    "allocated_quantity": order.quantity,
                    "amount_fen": lp.amount_fen,
                    "refund_fen": lp.refund_fen,
                    "unit_cost_fen": unit_cost_fen,
                    "effective_cost_fen": effective_cost_fen,
                    "margin_fen": (order.payment_fen - effective_cost_fen) if order.payment_fen is not None else None,
                    "paid_at": lp.paid_at,
                    "status": lp.status,
                    "score": 100,
                    "confidence_level": "high",
                    "category": buy_feat.category,
                    "category_label": CATEGORY_LABELS[buy_feat.category],
                    "time_gap_minutes": abs_gap,
                    "signed_gap_minutes": signed_gap,
                    "same_quantity": order.quantity == eff_q,
                    "is_bundle": False,
                    "bundle_kind": None,
                    "is_duration_fallback": False,
                    "allocations": [alloc_entry],
                    "reasons": exact_reasons,
                    "warnings": ["同一主订单含多个子单且未提供子单号，请人工核对"] if ambiguous_parent else [],
                })
                exact_added = True

            if not exact_added and order.quantity > 1 and sum(x[2] for x in linked_available) >= order.quantity:
                need = order.quantity
                allocs = []
                total_cost_fen = 0
                gaps: list[int] = []
                codes: list[str] = []
                bundle_ambiguous = False
                for lp, eff_q, rem_q, _lbl, ambiguous_parent in linked_available:
                    take = min(need, rem_q)
                    if take <= 0:
                        continue
                    if ambiguous_parent:
                        bundle_ambiguous = True
                    net_b = max(0, lp.amount_fen - lp.refund_fen)
                    part_cost = round(net_b * take / eff_q)
                    total_cost_fen += part_cost
                    sg = _signed_gap_minutes(order.paid_at, lp.paid_at)
                    if sg is not None:
                        gaps.append(sg)
                    if lp.linked_redeem_code:
                        codes.append(lp.linked_redeem_code)
                    allocs.append({
                        "purchase_id": lp.id,
                        "purchase_external_id": lp.purchase_external_id,
                        "allocated_quantity": take,
                        "purchase_quantity": lp.quantity,
                        "cost_fen": part_cost,
                        "paid_at": lp.paid_at,
                        "product_title": lp.product_title,
                    })
                    need -= take
                    if need == 0:
                        break
                if need == 0 and len(allocs) >= 2:
                    first_lp = linked_available[0][0]
                    ext_joined = " + ".join(a["purchase_external_id"] for a in allocs)
                    max_abs_gap = max((abs(g) for g in gaps), default=None)
                    max_sg = max(gaps) if gaps else None
                    code_str = ",".join(codes[:4])
                    candidates_by_item[order.item_external_id].append({
                        "purchase_id": allocs[0]["purchase_id"],
                        "purchase_external_id": ext_joined,
                        "product_title": f"方案二精确关联（{len(allocs)}笔）：{first_lp.product_title}",
                        "seller": first_lp.seller,
                        "source": first_lp.source,
                        "source_label": source_label(first_lp.source),
                        "original_currency": first_lp.original_currency or "CNY",
                        "original_amount": "",
                        "fx_rate": first_lp.fx_rate or "1",
                        "linked_order_external_id": first_lp.linked_order_external_id or order.order_external_id,
                        "linked_redeem_code": code_str,
                        "is_exact_link": True,
                        "quantity": order.quantity,
                        "purchase_quantity": order.quantity,
                        "inferred_quantity": None,
                        "remaining_quantity": sum(x[2] for x in linked_available),
                        "allocated_quantity": order.quantity,
                        "amount_fen": total_cost_fen,
                        "refund_fen": 0,
                        "unit_cost_fen": round(total_cost_fen / order.quantity),
                        "effective_cost_fen": total_cost_fen,
                        "margin_fen": (order.payment_fen - total_cost_fen) if order.payment_fen is not None else None,
                        "paid_at": first_lp.paid_at,
                        "status": "精确关联",
                        "score": 100,
                        "confidence_level": "high",
                        "category": sale_feat.category,
                        "category_label": CATEGORY_LABELS[sale_feat.category],
                        "time_gap_minutes": max_abs_gap,
                        "signed_gap_minutes": max_sg,
                        "same_quantity": True,
                        "is_bundle": True,
                        "bundle_kind": "exact_link",
                        "is_duration_fallback": False,
                        "allocations": allocs,
                        "reasons": [
                            f"方案二精确关联 {len(allocs)} 笔上游采购（共 {order.quantity} 件）",
                            f"来源：{source_label(first_lp.source)}",
                        ],
                        "warnings": ["同一主订单含多个子单且未提供子单号，请人工核对"] if bundle_ambiguous else [],
                    })
                    exact_added = True
            if exact_added:
                continue

        sale_text_for_sim = f"{sale_feat.spec_text} {order.product_title}".strip()
        cleaned_sale_text = _clean_match_title(sale_text_for_sim)

        partial_pool: list[tuple[int, CardPurchase, int, int | None, float]] = []
        for purchase in active_purchases:
            eff_qty = effective_qty_by_purchase.get(purchase.id, max(1, purchase.quantity))
            inferred_qty = inferred_multi[purchase.id][0] if purchase.id in inferred_multi else None
            remaining_qty = eff_qty - used_qty_by_purchase.get(purchase.id, 0)
            if remaining_qty <= 0:
                continue
            buy_feat = purchase_features[purchase.id]
            ok, form_compat = _compatible(sale_feat, buy_feat)
            is_duration_fallback = False
            fallback_buy_duration: int | None = None
            if not ok and remaining_qty >= order.quantity and _is_duration_fallback_spec(sale_feat, buy_feat):
                fgap = _signed_gap_minutes(order.paid_at, purchase.paid_at)
                net_buy_check = max(0, purchase.amount_fen - purchase.refund_fen)
                eff_cost_check = round(net_buy_check * order.quantity / eff_qty)
                if (
                    fgap is not None
                    and 0 <= fgap <= 30
                    and (not order.payment_fen or eff_cost_check <= order.payment_fen)
                ):
                    ok = True
                    form_compat = 0.72
                    is_duration_fallback = True
                    fallback_buy_duration = min(buy_feat.durations)
            if not ok:
                continue
            signed_gap = _signed_gap_minutes(order.paid_at, purchase.paid_at)
            if remaining_qty < order.quantity:
                if order.quantity > 1 and signed_gap is not None and -45 <= signed_gap <= 90:
                    partial_pool.append((signed_gap, purchase, remaining_qty, inferred_qty, form_compat))
                continue

            # 1. Spec & Title semantic match (weight 0.36)
            cleaned_buy_title = _clean_match_title(purchase.product_title)
            text_sim = max(
                _similarity(sale_text_for_sim, purchase.product_title),
                _similarity(cleaned_sale_text, cleaned_buy_title),
            )
            if sale_feat.spec_text:
                text_sim = max(
                    text_sim,
                    _similarity(sale_feat.spec_text, purchase.product_title),
                    _similarity(_clean_match_title(sale_feat.spec_text), cleaned_buy_title),
                )
            same_form = (sale_feat.form == buy_feat.form and sale_feat.form != "unknown")
            if is_duration_fallback:
                dur_bonus = 0.04
            else:
                dur_bonus = 0.25 if (sale_feat.durations and sale_feat.durations == buy_feat.durations) else (
                    0.18 if (sale_feat.durations & buy_feat.durations) else (
                        0.22 if (not sale_feat.durations and not buy_feat.durations and same_form) else 0.10
                    )
                )
            brand_bonus = 0.08 if (sale_feat.brand != "other" and sale_feat.brand == buy_feat.brand) else 0.0
            tier_bonus = 0.04 if (sale_feat.tier in ("pro", "ultra") and sale_feat.tier == buy_feat.tier) else 0.0
            spec_score = min(1.0, text_sim * 0.36 + form_compat * 0.34 + dur_bonus + brand_bonus + tier_bonus)

            # 2. Directional time proximity (weight 0.34)
            abs_gap = abs(signed_gap) if signed_gap is not None else None
            time_score, time_warnings = _time_score(signed_gap, eff_qty, buy_form=buy_feat.form)
            if time_score < 0:
                continue

            # 3. Unit cost allocation & price sanity (weight 0.18)
            net_buy_fen = max(0, purchase.amount_fen - purchase.refund_fen)
            unit_cost_fen = round(net_buy_fen / eff_qty)
            effective_cost_fen = round(net_buy_fen * order.quantity / eff_qty)
            price_score, price_warnings = _price_score(order.payment_fen, effective_cost_fen, same_form=(same_form or form_compat >= 0.9))

            # 4. Quantity fit (weight 0.06)
            if order.quantity == eff_qty:
                qty_score = 1.0
            elif eff_qty > order.quantity:
                qty_score = 0.78
            else:
                qty_score = 0.25

            # 5. Historical confirmed SKU affinity (weight 0.06)
            known_sellers = sku_sellers.get(order.sku)
            known_costs = sku_unit_costs.get(order.sku)
            if not known_sellers and not known_costs:
                history_score = 0.50
            else:
                history_score = 0.0
                if known_sellers and purchase.seller in known_sellers:
                    history_score += 0.50
                if known_costs:
                    median_cost = sorted(known_costs)[len(known_costs) // 2]
                    if median_cost > 0 and abs(unit_cost_fen - median_cost) / median_cost <= 0.25:
                        history_score += 0.50

            raw_score = round(100 * (
                spec_score * 0.36
                + time_score * 0.34
                + price_score * 0.18
                + qty_score * 0.06
                + history_score * 0.06
            ))
            if is_duration_fallback:
                raw_score -= 10
            score = min(99, max(0, raw_score))
            if signed_gap is not None and signed_gap > 90:
                score = min(score, 71)
            if score < 58:
                continue

            reasons: list[str] = []
            if sale_feat.form == buy_feat.form and sale_feat.form != "unknown":
                reasons.append(f"{FORM_LABELS[sale_feat.form]}一致")
            elif form_compat >= 0.90:
                reasons.append("1个月充自己号由家庭组席位履约")
            if is_duration_fallback and fallback_buy_duration is not None:
                reasons.append(f"1个月订单应急使用{fallback_buy_duration}个月激活链接履约")
            elif sale_feat.durations & buy_feat.durations:
                d_str = "/".join(str(d) for d in sorted(sale_feat.durations & buy_feat.durations))
                reasons.append(f"{d_str}个月周期匹配")
            if signed_gap is not None:
                if 0 <= signed_gap <= 15:
                    reasons.append(f"下单后 {signed_gap} 分钟采购")
                elif 15 < signed_gap <= 120:
                    reasons.append(f"下单后 {signed_gap} 分钟采购")
                elif signed_gap > 120:
                    reasons.append(f"下单后约 {max(1, round(signed_gap / 60))} 小时批量采购回补")
                elif signed_gap < 0 and eff_qty >= 2:
                    reasons.append(f"批量库存预采（共{eff_qty}件/余{remaining_qty}件）")
                elif -5 <= signed_gap < 0:
                    reasons.append(f"邻近下单时间（早于下单 {-signed_gap} 分钟）")
                elif -15 <= signed_gap < -5 and buy_feat.form in ("finished_account", "plain_gmail"):
                    reasons.append(f"小幅提前备货（早于下单 {-signed_gap} 分钟）")
            if inferred_qty and inferred_qty > 1:
                reasons.append(f"按同款单价推算约 {inferred_qty} 件（单件约 ¥{unit_cost_fen / 100:.2f}）")
            if order.payment_fen and 0.15 <= (effective_cost_fen / order.payment_fen) <= 0.85:
                pct = round(effective_cost_fen * 100 / order.payment_fen)
                reasons.append(f"成本占比 {pct}%")
            if known_sellers and purchase.seller in known_sellers:
                reasons.append("历史常用供货商")

            warnings = time_warnings + price_warnings
            if is_duration_fallback and fallback_buy_duration is not None:
                warnings.append(f"1个月订单由{fallback_buy_duration}个月激活链接履约，周期不一致，请人工核对")
            if inferred_qty and inferred_qty > 1:
                warnings.append(f"16688未展示件数，按同款单价推测为 {inferred_qty} 件，确认前请核实")
            has_hard_warn = any("高于买家实付" in w or "差距过大" in w or "晚于下单" in w or "周期不一致" in w for w in warnings)
            confidence_level = "high" if (score >= 85 and not has_hard_warn) else ("medium" if score >= 72 and not has_hard_warn else "review")
            alloc_entry = {
                "purchase_id": purchase.id,
                "purchase_external_id": purchase.purchase_external_id,
                "allocated_quantity": order.quantity,
                "purchase_quantity": inferred_qty or purchase.quantity,
                "cost_fen": effective_cost_fen,
                "paid_at": purchase.paid_at,
                "product_title": purchase.product_title,
            }
            candidate = {
                "purchase_id": purchase.id,
                "purchase_external_id": purchase.purchase_external_id,
                "product_title": purchase.product_title,
                "seller": purchase.seller,
                "source": purchase.source,
                "source_label": source_label(purchase.source),
                "original_currency": purchase.original_currency or "CNY",
                "original_amount": purchase.original_amount or "",
                "fx_rate": purchase.fx_rate or "1",
                "linked_order_external_id": purchase.linked_order_external_id or "",
                "linked_redeem_code": purchase.linked_redeem_code or "",
                "is_exact_link": False,
                "quantity": eff_qty,
                "purchase_quantity": purchase.quantity,
                "inferred_quantity": inferred_qty,
                "remaining_quantity": remaining_qty,
                "allocated_quantity": order.quantity,
                "amount_fen": purchase.amount_fen,
                "refund_fen": purchase.refund_fen,
                "unit_cost_fen": unit_cost_fen,
                "effective_cost_fen": effective_cost_fen,
                "margin_fen": (order.payment_fen - effective_cost_fen) if order.payment_fen is not None else None,
                "paid_at": purchase.paid_at,
                "status": purchase.status,
                "score": score,
                "confidence_level": confidence_level,
                "category": buy_feat.category,
                "category_label": CATEGORY_LABELS[buy_feat.category],
                "time_gap_minutes": abs_gap,
                "signed_gap_minutes": signed_gap,
                "same_quantity": order.quantity == eff_qty,
                "is_bundle": False,
                "bundle_kind": None,
                "is_duration_fallback": is_duration_fallback,
                "allocations": [alloc_entry],
                "reasons": reasons,
                "warnings": warnings,
            }
            candidates_by_item[order.item_external_id].append(candidate)

        # Multi-purchase split bundle ("多单凑一单") when order.quantity > 1
        if order.quantity > 1 and len(partial_pool) >= 2:
            partial_pool.sort(key=lambda item: (0 if item[0] >= 0 else 1, abs(item[0])))
            pool = partial_pool[:6]
            best_bundle: dict | None = None
            for r_len in (2, 3, 4):
                if len(pool) < r_len:
                    continue
                for combo in combinations(pool, r_len):
                    total_avail = sum(x[2] for x in combo)
                    if total_avail < order.quantity:
                        continue
                    need = order.quantity
                    allocs = []
                    total_cost_fen = 0
                    gaps = []
                    for s_gap, p_obj, rem_q, inf_q, _fc in combo:
                        take = min(need, rem_q)
                        if take <= 0:
                            continue
                        eff_q = effective_qty_by_purchase[p_obj.id]
                        net_b = max(0, p_obj.amount_fen - p_obj.refund_fen)
                        part_cost = round(net_b * take / eff_q)
                        total_cost_fen += part_cost
                        gaps.append(s_gap)
                        allocs.append({
                            "purchase_id": p_obj.id,
                            "purchase_external_id": p_obj.purchase_external_id,
                            "allocated_quantity": take,
                            "purchase_quantity": inf_q or p_obj.quantity,
                            "cost_fen": part_cost,
                            "paid_at": p_obj.paid_at,
                            "product_title": p_obj.product_title,
                        })
                        need -= take
                        if need == 0:
                            break
                    if need > 0 or len(allocs) < 2:
                        continue
                    p_score, p_warns = _price_score(order.payment_fen, total_cost_fen, same_form=True)
                    if p_score <= 0.2:
                        continue
                    max_abs_gap = max(abs(g) for g in gaps)
                    all_after = all(0 <= g <= 45 for g in gaps)
                    exact_sum = (total_avail == order.quantity)
                    t_component = 0.96 if (all_after and max_abs_gap <= 30) else (0.84 if max_abs_gap <= 60 else 0.72)
                    q_component = 1.0 if exact_sum else 0.82
                    b_score = min(97, round(100 * (0.90 * 0.36 + t_component * 0.34 + p_score * 0.18 + q_component * 0.12)))
                    if b_score < 68:
                        continue
                    lead = max(allocs, key=lambda a: a["allocated_quantity"])
                    parts_desc = "+".join(f"{a['allocated_quantity']}件" for a in allocs)
                    ext_joined = " + ".join(a["purchase_external_id"] for a in allocs)
                    b_reasons = [
                        f"多笔采购凑单履约（{parts_desc}=共{order.quantity}件）",
                        f"下单后 {min(gaps)}~{max(gaps)} 分钟连续采购" if min(gaps) >= 0 else f"邻近时段组合采购（最大时差 {max_abs_gap} 分钟）",
                    ]
                    if order.payment_fen and total_cost_fen < order.payment_fen:
                        b_reasons.append(f"合计成本占比 {round(total_cost_fen * 100 / order.payment_fen)}%")
                    b_cand = {
                        "purchase_id": lead["purchase_id"],
                        "purchase_external_id": ext_joined,
                        "product_title": f"组合凑单（{len(allocs)}笔）：{lead['product_title']}",
                        "seller": combo[0][1].seller,
                        "source": combo[0][1].source,
                        "source_label": source_label(combo[0][1].source),
                        "original_currency": combo[0][1].original_currency or "CNY",
                        "original_amount": "",
                        "fx_rate": combo[0][1].fx_rate or "1",
                        "linked_order_external_id": "",
                        "linked_redeem_code": "",
                        "is_exact_link": False,
                        "quantity": total_avail,
                        "purchase_quantity": order.quantity,
                        "inferred_quantity": None,
                        "remaining_quantity": total_avail,
                        "allocated_quantity": order.quantity,
                        "amount_fen": total_cost_fen,
                        "refund_fen": 0,
                        "unit_cost_fen": round(total_cost_fen / order.quantity),
                        "effective_cost_fen": total_cost_fen,
                        "margin_fen": (order.payment_fen - total_cost_fen) if order.payment_fen is not None else None,
                        "paid_at": lead["paid_at"],
                        "status": "组合已付",
                        "score": b_score,
                        "confidence_level": "high" if (b_score >= 85 and not p_warns) else "medium",
                        "category": sale_feat.category,
                        "category_label": CATEGORY_LABELS[sale_feat.category],
                        "time_gap_minutes": max_abs_gap,
                        "signed_gap_minutes": max(gaps),
                        "same_quantity": exact_sum,
                        "is_bundle": True,
                        "bundle_kind": "split_qty",
                        "allocations": allocs,
                        "reasons": b_reasons,
                        "warnings": p_warns,
                    }
                    if best_bundle is None or b_cand["score"] > best_bundle["score"]:
                        best_bundle = b_cand
            if best_bundle is not None:
                candidates_by_item[order.item_external_id].append(best_bundle)

        # Composite assembly ("Gmail白号 + 激活链接 = 带邮箱成品号") when no tight finished account exists
        if (
            order.quantity == 1
            and sale_feat.brand == "gemini"
            and sale_feat.form == "finished_account"
            and not any(c["score"] >= 82 for c in candidates_by_item.get(order.item_external_id, ()))
        ):
            near_blanks = []
            near_acts = []
            for p_obj in active_purchases:
                rem_q = effective_qty_by_purchase[p_obj.id] - used_qty_by_purchase.get(p_obj.id, 0)
                if rem_q < 1:
                    continue
                sg = _signed_gap_minutes(order.paid_at, p_obj.paid_at)
                if sg is None or not (-25 <= sg <= 45):
                    continue
                pf = purchase_features[p_obj.id]
                if pf.form == "plain_gmail":
                    near_blanks.append((abs(sg), sg, p_obj))
                elif pf.brand == "gemini" and pf.form == "activation_link" and (not sale_feat.durations or not pf.durations or (sale_feat.durations & pf.durations)):
                    near_acts.append((abs(sg), sg, p_obj))
            if near_blanks and near_acts:
                near_blanks.sort(key=lambda x: x[0])
                near_acts.sort(key=lambda x: x[0])
                b_abs, b_sg, p_blank = near_blanks[0]
                a_abs, a_sg, p_act = near_acts[0]
                c_blank = round(max(0, p_blank.amount_fen - p_blank.refund_fen) / effective_qty_by_purchase[p_blank.id])
                c_act = round(max(0, p_act.amount_fen - p_act.refund_fen) / effective_qty_by_purchase[p_act.id])
                combo_cost = c_blank + c_act
                if not order.payment_fen or combo_cost <= order.payment_fen * 0.88:
                    max_g = max(b_abs, a_abs)
                    c_score = 84 if max_g <= 15 else 76
                    allocs = [
                        {
                            "purchase_id": p_act.id,
                            "purchase_external_id": p_act.purchase_external_id,
                            "allocated_quantity": 1,
                            "purchase_quantity": inferred_multi[p_act.id][0] if p_act.id in inferred_multi else p_act.quantity,
                            "cost_fen": c_act,
                            "paid_at": p_act.paid_at,
                            "product_title": p_act.product_title,
                        },
                        {
                            "purchase_id": p_blank.id,
                            "purchase_external_id": p_blank.purchase_external_id,
                            "allocated_quantity": 1,
                            "purchase_quantity": inferred_multi[p_blank.id][0] if p_blank.id in inferred_multi else p_blank.quantity,
                            "cost_fen": c_blank,
                            "paid_at": p_blank.paid_at,
                            "product_title": p_blank.product_title,
                        },
                    ]
                    candidates_by_item[order.item_external_id].append({
                        "purchase_id": p_act.id,
                        "purchase_external_id": f"{p_act.purchase_external_id} + {p_blank.purchase_external_id}",
                        "product_title": f"组合组装：{p_act.product_title} + Gmail白号",
                        "seller": f"{p_act.seller}+{p_blank.seller}",
                        "source": p_act.source,
                        "source_label": source_label(p_act.source),
                        "original_currency": p_act.original_currency or "CNY",
                        "original_amount": "",
                        "fx_rate": p_act.fx_rate or "1",
                        "linked_order_external_id": "",
                        "linked_redeem_code": "",
                        "is_exact_link": False,
                        "quantity": 1,
                        "purchase_quantity": 1,
                        "inferred_quantity": None,
                        "remaining_quantity": 1,
                        "allocated_quantity": 1,
                        "amount_fen": combo_cost,
                        "refund_fen": 0,
                        "unit_cost_fen": combo_cost,
                        "effective_cost_fen": combo_cost,
                        "margin_fen": (order.payment_fen - combo_cost) if order.payment_fen is not None else None,
                        "paid_at": p_act.paid_at,
                        "status": "组合履约",
                        "score": c_score,
                        "confidence_level": "medium" if c_score >= 75 else "review",
                        "category": sale_feat.category,
                        "category_label": CATEGORY_LABELS[sale_feat.category],
                        "time_gap_minutes": max_g,
                        "signed_gap_minutes": max(b_sg, a_sg),
                        "same_quantity": True,
                        "is_bundle": True,
                        "bundle_kind": "composite",
                        "allocations": allocs,
                        "reasons": [f"Gmail白号(¥{c_blank/100:.2f}) + 激活链接(¥{c_act/100:.2f})组合履约", f"邻近 {max_g} 分钟内配齐"],
                        "warnings": ["组合拆单建议，确认前请核实是否自制成品号"],
                    })

    ai_stats = {"enabled": bool(use_ai), "unique_pairs_checked": 0, "adjusted_candidates": 0}
    if use_ai:
        sig_to_req: dict[tuple[str, str, str], dict] = {}
        for iid, clist in candidates_by_item.items():
            if not clist:
                continue
            clist.sort(key=lambda c: -c["score"])
            top = clist[0]
            if top.get("is_exact_link"):
                continue
            second = clist[1] if len(clist) > 1 else None
            has_close_tie = (
                second is not None
                and (top["score"] - second["score"]) <= 6
                and not _is_equivalent_batch_candidate(top, second)
            )
            needs_ai = (
                top["category"] == "other"
                or 58 <= top["score"] <= 84
                or has_close_tie
            )
            if not needs_ai:
                continue
            check_cands = [top, second] if (has_close_tie and second is not None) else [top]
            for cand in check_cands:
                sig = (order_payload[iid]["sku"], order_payload[iid]["title"], cand["product_title"])
                if sig not in sig_to_req:
                    sig_to_req[sig] = {
                        "pair_id": f"sig_{len(sig_to_req)}",
                        "sale_sku": order_payload[iid]["sku"],
                        "sale_title": order_payload[iid]["title"],
                        "buy_title": cand["product_title"],
                        "qty": order_payload[iid]["quantity"],
                        "gap_min": cand["signed_gap_minutes"],
                        "sale_yuan": round((order_payload[iid]["payment_fen"] or 0) / 100, 2) if order_payload[iid]["payment_fen"] is not None else None,
                        "buy_yuan": round((cand["effective_cost_fen"] or 0) / 100, 2),
                    }
        unique_reqs = list(sig_to_req.values())
        ai_stats["unique_pairs_checked"] = min(8, len(unique_reqs))
        deltas_by_pid = _optional_ai_rerank(unique_reqs)
        if deltas_by_pid:
            sig_deltas = {
                sig: deltas_by_pid[req["pair_id"]]
                for sig, req in sig_to_req.items()
                if req["pair_id"] in deltas_by_pid and deltas_by_pid[req["pair_id"]] != 0
            }
            for iid, clist in candidates_by_item.items():
                for c in clist:
                    if c.get("is_exact_link"):
                        continue
                    sig = (order_payload[iid]["sku"], order_payload[iid]["title"], c["product_title"])
                    delta = sig_deltas.get(sig, 0)
                    if delta != 0:
                        ai_stats["adjusted_candidates"] += 1
                        c["score"] = max(0, min(99, c["score"] + delta))
                        if delta > 0:
                            c["reasons"].append("AI语义复核规格吻合")
                        else:
                            c["warnings"].append("AI语义提示规格可能存疑")
                        has_hw = any("高于买家实付" in w or "差距过大" in w or "晚于下单" in w or "存疑" in w or "周期不一致" in w for w in c["warnings"])
                        c["confidence_level"] = "high" if (c["score"] >= 85 and not has_hw) else ("medium" if c["score"] >= 72 and not has_hw else "review")

    # Regret-aware global capacity assignment: lock immediate tight matches & bundles first, then by regret + score
    sim_remaining = {p.id: effective_qty_by_purchase[p.id] - used_qty_by_purchase.get(p.id, 0) for p in purchases}

    def _cand_feasible(cand: dict) -> bool:
        return all(sim_remaining.get(a["purchase_id"], 0) >= a["allocated_quantity"] for a in cand["allocations"])

    def _commit_cand(cand: dict) -> None:
        for a in cand["allocations"]:
            sim_remaining[a["purchase_id"]] = sim_remaining.get(a["purchase_id"], 0) - a["allocated_quantity"]

    def _cand_remaining(cand: dict) -> int:
        allocs = cand.get("allocations") or []
        if not allocs:
            return 0
        if cand.get("bundle_kind") == "composite":
            return min(sim_remaining.get(a["purchase_id"], 0) // max(1, a["allocated_quantity"]) for a in allocs)
        return sum(sim_remaining.get(a["purchase_id"], 0) for a in allocs)

    for clist in candidates_by_item.values():
        clist.sort(key=lambda c: (-c["score"], c["time_gap_minutes"] if c["time_gap_minutes"] is not None else 10**9))

    primary_by_order: dict[str, dict] = {}
    unassigned = set(candidates_by_item.keys())
    while unassigned:
        best_choice: tuple[tuple, str, dict] | None = None
        dead_items: list[str] = []
        for iid in unassigned:
            avail = [c for c in candidates_by_item[iid] if _cand_feasible(c)]
            if not avail:
                dead_items.append(iid)
                continue
            top = avail[0]
            second_score = avail[1]["score"] if len(avail) > 1 else 45
            regret = top["score"] - second_score
            sg = top["signed_gap_minutes"]
            is_immediate = 1 if (sg is not None and 0 <= sg <= 25 and (top["same_quantity"] or top.get("is_bundle")) and top["score"] >= 82) else 0
            priority = (
                1 if top.get("is_exact_link") else 0,
                is_immediate,
                1 if top.get("bundle_kind") == "split_qty" and top["same_quantity"] else 0,
                top["score"] if top["score"] >= 85 else top["score"] - 5,
                regret,
                -(top["time_gap_minutes"] if top["time_gap_minutes"] is not None else 10**9),
            )
            if best_choice is None or priority > best_choice[0]:
                best_choice = (priority, iid, top)
        for d in dead_items:
            unassigned.discard(d)
        if best_choice is None:
            break
        _prio, chosen_iid, chosen_cand = best_choice
        chosen_cand["remaining_quantity"] = _cand_remaining(chosen_cand)
        _commit_cand(chosen_cand)
        primary_by_order[chosen_iid] = chosen_cand
        unassigned.discard(chosen_iid)

    # Post-pass: detect potential refund-loss candidates on refunded buyer orders without stealing active order stock
    refund_loss_candidates: list[dict] = []
    for r_ord in refunded_orders:
        unit_pay = (r_ord.payment_fen // max(1, r_ord.quantity)) if r_ord.payment_fen is not None else None
        rf = extract_features(r_ord.sku, r_ord.product_title, amount_fen=unit_pay, is_sale=True)
        for p_obj in active_purchases:
            if effective_qty_by_purchase[p_obj.id] != 1 or sim_remaining.get(p_obj.id, 0) < 1:
                continue
            sg = _signed_gap_minutes(r_ord.paid_at, p_obj.paid_at)
            if sg is None or not (0 <= sg <= 20):
                continue
            ok, fc = _compatible(rf, purchase_features[p_obj.id])
            if ok and fc >= 0.9:
                net_b = max(0, p_obj.amount_fen - p_obj.refund_fen)
                refund_loss_candidates.append({
                    "item_id": r_ord.item_external_id,
                    "order_id": r_ord.order_external_id,
                    "sku": r_ord.sku,
                    "title": r_ord.product_title,
                    "paid_at": r_ord.paid_at,
                    "purchase_id": p_obj.id,
                    "purchase_external_id": p_obj.purchase_external_id,
                    "purchase_title": p_obj.product_title,
                    "purchase_paid_at": p_obj.paid_at,
                    "loss_cost_fen": net_b,
                    "signed_gap_minutes": sg,
                    "note": f"买家已退款，但下单后 {sg} 分钟有未退同款采购单，建议核实是否为进货损耗",
                })
                break

    suggestions_out = []
    ordered_items = sorted(
        primary_by_order.keys(),
        key=lambda iid: (
            -primary_by_order[iid]["score"],
            order_payload[iid]["paid_at"] or "",
        ),
    )
    auto_confirmable_count = 0
    auto_confirmable_sales_fen = 0
    matched_sales_fen = 0
    for item_id in ordered_items:
        primary = primary_by_order[item_id]
        all_cands = candidates_by_item[item_id]
        primary["current_remaining_quantity"] = _cand_remaining(primary)
        feasible_cands = [c for c in all_cands if _cand_feasible(c)]
        distinct_runner_up = next(
            (
                c
                for c in feasible_cands
                if c["purchase_external_id"] != primary["purchase_external_id"]
                and not _is_equivalent_batch_candidate(primary, c)
            ),
            None,
        )
        margin_lead = max(0, primary["score"] - distinct_runner_up["score"]) if distinct_runner_up else 20
        equiv_pool_count = 1 + sum(
            1
            for c in feasible_cands
            if c["purchase_external_id"] != primary["purchase_external_id"]
            and _is_equivalent_batch_candidate(primary, c)
        )
        sg = primary["signed_gap_minutes"]
        is_auto = bool(primary.get("is_exact_link") and not primary["warnings"]) or (
            primary["confidence_level"] == "high"
            and not primary["warnings"]
            and primary.get("bundle_kind") != "composite"
            and (
                margin_lead >= 6
                or (sg is not None and -5 <= sg <= 20)
                or (primary["quantity"] >= 2 and primary["score"] >= 85 and margin_lead >= 4)
            )
        )
        primary["auto_confirmable"] = is_auto
        primary["margin_lead"] = margin_lead
        primary["equivalent_pool_count"] = equiv_pool_count
        item_pay_fen = max(0, order_payload[item_id]["payment_fen"] or 0)
        matched_sales_fen += item_pay_fen
        if is_auto:
            auto_confirmable_count += 1
            auto_confirmable_sales_fen += item_pay_fen

    for item_id in ordered_items[:limit]:
        primary = primary_by_order[item_id]
        backups_raw = sorted(
            [
                c
                for c in candidates_by_item[item_id]
                if c["purchase_external_id"] != primary["purchase_external_id"] and _cand_feasible(c)
            ],
            key=lambda c: c["score"],
            reverse=True,
        )[:2]
        backups = []
        for b_cand in backups_raw:
            b_copy = dict(b_cand)
            rem_now = _cand_remaining(b_copy)
            b_copy["remaining_quantity"] = rem_now
            b_copy["current_remaining_quantity"] = rem_now
            backups.append(b_copy)
        suggestions_out.append({**order_payload[item_id], "candidates": [primary, *backups]})

    confidence_counts = {"high": 0, "medium": 0, "review": 0}
    exact_link_count = 0
    for iid in ordered_items:
        lvl = primary_by_order[iid]["confidence_level"]
        confidence_counts[lvl] = confidence_counts.get(lvl, 0) + 1
        if primary_by_order[iid].get("is_exact_link"):
            exact_link_count += 1

    matched_total = len(ordered_items)
    window_missing = 0
    window_matched = 0
    window_manual_unmatched = 0
    before_window_unmatched = 0
    likely_manual_unmatched = 0
    supply_shortage_unmatched = 0
    no_compatible_stock = 0
    inventory_exhausted = 0
    time_window_mismatch = 0
    spec_mismatch = 0
    low_confidence = 0
    shortage_counter: Counter[tuple[str, str]] = Counter()
    true_shortage_counter: Counter[tuple[str, str]] = Counter()
    time_mismatch_counter: Counter[tuple[str, str]] = Counter()
    spec_mismatch_counter: Counter[tuple[str, str]] = Counter()

    for iid, o_info in order_payload.items():
        in_win = (window_floor_ts is None) or ((o_info["paid_at"] or "") >= window_floor_ts)
        if in_win:
            window_missing += 1
            if iid in primary_by_order:
                window_matched += 1
        if iid not in primary_by_order:
            ofeat = order_features_map[iid]
            if not in_win:
                before_window_unmatched += 1
            elif ofeat.form == "invite_boost":
                likely_manual_unmatched += 1
                if in_win:
                    window_manual_unmatched += 1
            else:
                supply_shortage_unmatched += 1
                spec_label = _extract_spec(o_info["sku"]) or o_info["title"][:24]
                spec_key = (CATEGORY_LABELS[ofeat.category], spec_label[:26])
                shortage_counter[spec_key] += 1
                compat_entries: list[tuple[CardPurchase, bool]] = []
                for p in active_purchases:
                    bf = purchase_features[p.id]
                    if _compatible(ofeat, bf)[0]:
                        compat_entries.append((p, False))
                    elif _is_duration_fallback_spec(ofeat, bf):
                        compat_entries.append((p, True))
                if not compat_entries:
                    if candidates_by_item[iid]:
                        inventory_exhausted += 1
                        true_shortage_counter[spec_key] += 1
                    elif ofeat.brand != "other" and any(purchase_features[p.id].brand == ofeat.brand for p in active_purchases):
                        spec_mismatch += 1
                        spec_mismatch_counter[spec_key] += 1
                    else:
                        no_compatible_stock += 1
                        true_shortage_counter[spec_key] += 1
                else:
                    qty_need = o_info["quantity"]
                    rem_entries = [(p, is_fb) for p, is_fb in compat_entries if sim_remaining.get(p.id, 0) >= qty_need]
                    if not rem_entries and (
                        qty_need == 1 or sum(sim_remaining.get(p.id, 0) for p, _ in compat_entries) < qty_need
                    ):
                        inventory_exhausted += 1
                        true_shortage_counter[spec_key] += 1
                    else:
                        check_entries = rem_entries or [
                            (p, is_fb) for p, is_fb in compat_entries if sim_remaining.get(p.id, 0) > 0
                        ]
                        has_time_ok = False
                        for p, is_fb in check_entries:
                            sg_chk = _signed_gap_minutes(o_info["paid_at"], p.paid_at)
                            if is_fb:
                                eff_q_chk = effective_qty_by_purchase.get(p.id, max(1, p.quantity))
                                net_buy_chk = max(0, p.amount_fen - p.refund_fen)
                                eff_cost_chk = round(net_buy_chk * qty_need / eff_q_chk)
                                if (
                                    sim_remaining.get(p.id, 0) >= qty_need
                                    and sg_chk is not None
                                    and 0 <= sg_chk <= 30
                                    and (not o_info["payment_fen"] or eff_cost_chk <= o_info["payment_fen"])
                                ):
                                    has_time_ok = True
                                    break
                            elif qty_need > 1 and sim_remaining.get(p.id, 0) < qty_need:
                                if sg_chk is not None and -45 <= sg_chk <= 90:
                                    has_time_ok = True
                                    break
                            else:
                                eff_q_chk = effective_qty_by_purchase.get(p.id, max(1, p.quantity))
                                ts_chk, _ = _time_score(sg_chk, eff_q_chk, buy_form=purchase_features[p.id].form)
                                if ts_chk >= 0:
                                    has_time_ok = True
                                    break
                        if not has_time_ok:
                            time_window_mismatch += 1
                            time_mismatch_counter[spec_key] += 1
                        else:
                            low_confidence += 1

    overall_pct = round(matched_total * 100 / missing_cost_count) if missing_cost_count > 0 else 100
    window_pct = round(window_matched * 100 / window_missing) if window_missing > 0 else overall_pct
    effective_window_missing = max(0, window_missing - window_manual_unmatched)
    effective_window_pct = round(window_matched * 100 / effective_window_missing) if effective_window_missing > 0 else window_pct
    auto_pct = round(auto_confirmable_count * 100 / matched_total) if matched_total > 0 else 0
    auto_sales_pct = round(auto_confirmable_sales_fen * 100 / pending_sales_fen) if pending_sales_fen > 0 else 0
    matched_sales_pct = round(matched_sales_fen * 100 / pending_sales_fen) if pending_sales_fen > 0 else 100
    total_active_orders = already_reconciled_count + missing_cost_count
    reconciled_progress_pct = round(already_reconciled_count * 100 / total_active_orders) if total_active_orders > 0 else 100
    projected_coverage_pct = round((already_reconciled_count + auto_confirmable_count) * 100 / total_active_orders) if total_active_orders > 0 else 100
    projected_all_suggestions_pct = round((already_reconciled_count + matched_total) * 100 / total_active_orders) if total_active_orders > 0 else 100
    shortage_breakdown = [
        {"category_label": cat_lbl, "spec": spec_lbl, "count": cnt}
        for (cat_lbl, spec_lbl), cnt in shortage_counter.most_common(3)
    ]
    true_shortage_breakdown = [
        {"category_label": cat_lbl, "spec": spec_lbl, "count": cnt}
        for (cat_lbl, spec_lbl), cnt in true_shortage_counter.most_common(3)
    ]
    time_mismatch_breakdown = [
        {"category_label": cat_lbl, "spec": spec_lbl, "count": cnt}
        for (cat_lbl, spec_lbl), cnt in time_mismatch_counter.most_common(3)
    ]
    spec_mismatch_breakdown = [
        {"category_label": cat_lbl, "spec": spec_lbl, "count": cnt}
        for (cat_lbl, spec_lbl), cnt in spec_mismatch_counter.most_common(3)
    ]

    tips: list[str] = []
    if exact_link_count > 0:
        tips.append(
            f"有 {exact_link_count} 笔通过方案二兑换码/上游单号实现 100% 精确关联，可直接「一键确认精确关联」。"
        )
    if auto_confirmable_count > 0:
        tips.append(
            f"有 {auto_confirmable_count} 笔（占建议 {auto_pct}%，对应销售额 {auto_sales_pct}%）时间与规格高度吻合，点「一键确认高匹配」后全店成本核清率将从 {reconciled_progress_pct}% 升至 {projected_coverage_pct}%。"
        )
    if before_window_unmatched > 0 and earliest_purchase_ts:
        tips.append(f"有 {before_window_unmatched} 笔千牛订单早于最早采购单（{earliest_purchase_ts[:10]}），未计入可配对缺口；剔除后可对账时段命中率为 {effective_window_pct}%。")
    true_shortage_count = no_compatible_stock + inventory_exhausted
    if true_shortage_count > 0:
        top_short_desc = "、".join(f"{item['spec']}({item['count']}笔)" for item in true_shortage_breakdown)
        tips.append(
            f"有 {true_shortage_count} 笔确实缺采购库存（无同款库存 {no_compatible_stock} 笔、库存已被占满 {inventory_exhausted} 笔；主要集中在：{top_short_desc}），可补导历史采购或设 SKU 默认成本。"
        )
    if time_window_mismatch > 0:
        top_time_desc = "、".join(f"{item['spec']}({item['count']}笔)" for item in time_mismatch_breakdown)
        tips.append(f"有 {time_window_mismatch} 笔有同款余量但下单与采购时间差超出匹配窗口（集中在：{top_time_desc}），建议在订单明细手动核对。")
    if spec_mismatch > 0:
        top_spec_desc = "、".join(f"{item['spec']}({item['count']}笔)" for item in spec_mismatch_breakdown)
        tips.append(f"有 {spec_mismatch} 笔同品牌但周期或履约形态不一致（集中在：{top_spec_desc}），需核对是否跨规格发货或单独设成本。")
    if low_confidence > 0:
        tips.append(f"有 {low_confidence} 笔存在候选但文本/价格匹配分过低或倒挂（<58分），已拦截未推荐。")
    if likely_manual_unmatched > 0:
        tips.append(f"有 {likely_manual_unmatched} 笔邀请助力/代刷类订单疑似人工履约，建议直接设置固定单位成本。")
    if refund_loss_candidates:
        tips.append(f"检测到 {len(refund_loss_candidates)} 笔买家已退款但采购单未退款的疑似损耗单，建议下方核实。")
    if skipped_refunded_count > 0:
        tips.append(f"已自动隔离 {skipped_refunded_count} 笔买家全额退款/关单订单，防止误占正常采购库存。")

    smart_summary = {
        "actionable_missing_count": missing_cost_count,
        "window_missing_count": window_missing,
        "effective_window_missing_count": effective_window_missing,
        "matched_count": matched_total,
        "exact_link_count": exact_link_count,
        "window_matched_count": window_matched,
        "overall_coverage_pct": overall_pct,
        "window_coverage_pct": window_pct,
        "effective_window_coverage_pct": effective_window_pct,
        "already_reconciled_count": already_reconciled_count,
        "total_active_orders": total_active_orders,
        "reconciled_progress_pct": reconciled_progress_pct,
        "projected_coverage_pct": projected_coverage_pct,
        "projected_all_suggestions_pct": projected_all_suggestions_pct,
        "auto_confirmable_count": auto_confirmable_count,
        "auto_confirmable_pct": auto_pct,
        "pending_sales_fen": pending_sales_fen,
        "matched_sales_fen": matched_sales_fen,
        "matched_sales_pct": matched_sales_pct,
        "auto_confirmable_sales_fen": auto_confirmable_sales_fen,
        "auto_confirmable_sales_pct": auto_sales_pct,
        "earliest_purchase_at": earliest_purchase_ts or "",
        "tier_breakdown": {
            "high": confidence_counts["high"],
            "medium": confidence_counts["medium"],
            "review": confidence_counts["review"],
            "high_pct": round(confidence_counts["high"] * 100 / matched_total) if matched_total else 0,
            "medium_pct": round(confidence_counts["medium"] * 100 / matched_total) if matched_total else 0,
            "review_pct": round(confidence_counts["review"] * 100 / matched_total) if matched_total else 0,
        },
        "unmatched_reasons": {
            "before_purchase_window": before_window_unmatched,
            "supply_exhausted": supply_shortage_unmatched,
            "no_compatible_stock": no_compatible_stock,
            "inventory_exhausted": inventory_exhausted,
            "time_window_mismatch": time_window_mismatch,
            "spec_mismatch": spec_mismatch,
            "low_confidence": low_confidence,
            "likely_manual_fulfillment": likely_manual_unmatched,
            "skipped_refunded": skipped_refunded_count,
        },
        "shortage_breakdown": shortage_breakdown,
        "true_shortage_breakdown": true_shortage_breakdown,
        "time_mismatch_breakdown": time_mismatch_breakdown,
        "spec_mismatch_breakdown": spec_mismatch_breakdown,
        "diagnostic_tips": tips,
    }

    return {
        "channel": channel_filter or "all",
        "purchase_count": len(purchases),
        "missing_cost_count": missing_cost_count,
        "skipped_refunded_count": skipped_refunded_count,
        "suggestion_count": len(suggestions_out),
        "total_matched_candidates": len(ordered_items),
        "confidence_counts": confidence_counts,
        "ai_stats": ai_stats,
        "smart_summary": smart_summary,
        "refund_loss_candidates": refund_loss_candidates,
        "suggestions": suggestions_out,
    }


def confirm_match(db: Session, store_id: int, body, reviewer_id: int) -> dict:
    req_key = str(body.request_id)
    existing = db.scalar(select(CardMatch).where(CardMatch.store_id == store_id, CardMatch.request_key == req_key))
    if existing:
        if existing.item_external_id != body.item_id or existing.purchase_id != body.purchase_id:
            raise ImportProblem("同一操作编号不能用于不同成本匹配")
        return {"id": existing.id, "duplicate": True, "adjustment_id": existing.metadata_json.get("adjustment_id")}

    order = db.scalar(select(OrderItem).where(OrderItem.store_id == store_id, OrderItem.item_external_id == body.item_id))
    if not order:
        raise ImportProblem("订单或卡网采购单不存在")

    adjustments = list(db.scalars(select(Adjustment).where(Adjustment.store_id == store_id, Adjustment.item_external_id == body.item_id).order_by(Adjustment.id.asc())))
    latest_invalidate_ts = ""
    for a in adjustments:
        if a.kind == "cost_invalidate":
            latest_invalidate_ts = a.created_at or ""
        elif a.kind == "cost_set":
            latest_invalidate_ts = ""

    prior_item_matches = list(db.scalars(select(CardMatch).where(
        CardMatch.store_id == store_id,
        CardMatch.item_external_id == body.item_id,
        CardMatch.status == "confirmed",
    )))
    active_item_matches: list[CardMatch] = []
    for pm in prior_item_matches:
        if latest_invalidate_ts and (pm.created_at or "") <= latest_invalidate_ts:
            pm.status = "superseded"
        else:
            active_item_matches.append(pm)

    raw_allocs = getattr(body, "allocations", None)
    if raw_allocs and len(raw_allocs) > 1:
        if active_item_matches:
            existing_pids = {m.purchase_id for m in active_item_matches}
            target_pids = {int(a["purchase_id"] if isinstance(a, dict) else a.purchase_id) for a in raw_allocs}
            if existing_pids == target_pids:
                m0 = active_item_matches[0]
                return {"id": m0.id, "duplicate": True, "adjustment_id": m0.metadata_json.get("adjustment_id")}
            raise ImportProblem("这笔千牛订单已经核对过其他卡网采购单，如需修改请在订单明细调整")

        all_adj = list(db.scalars(select(Adjustment).where(Adjustment.store_id == store_id).order_by(Adjustment.id.asc())))
        all_active_matches = _active_confirmed_matches(db, store_id, all_adj)
        used_by_pid: dict[int, int] = defaultdict(int)
        for m in all_active_matches:
            used_by_pid[m.purchase_id] += max(1, m.allocated_quantity or 1)

        resolved_parts: list[tuple[CardPurchase, int, int]] = []
        total_cost_fen = 0
        for entry in raw_allocs:
            pid = int(entry["purchase_id"] if isinstance(entry, dict) else entry.purchase_id)
            alloc_q = max(1, int((entry.get("allocated_quantity") if isinstance(entry, dict) else getattr(entry, "allocated_quantity", 1)) or 1))
            req_pq = entry.get("purchase_quantity") if isinstance(entry, dict) else getattr(entry, "purchase_quantity", None)
            p_obj = db.scalar(select(CardPurchase).where(CardPurchase.store_id == store_id, CardPurchase.id == pid))
            if not p_obj:
                raise ImportProblem("订单或卡网采购单不存在")
            if req_pq and int(req_pq) > p_obj.quantity and int(req_pq) <= 100:
                p_obj.quantity = int(req_pq)
            used_q = used_by_pid.get(p_obj.id, 0)
            if used_q + alloc_q > p_obj.quantity:
                rem = max(0, p_obj.quantity - used_q)
                raise ImportProblem(f"卡网采购单 {p_obj.purchase_external_id} 剩余可分配数量为 {rem} 件，无法再分配 {alloc_q} 件")
            net_p = p_obj.amount_fen - p_obj.refund_fen
            if net_p < 0:
                raise ImportProblem("采购退款不能超过采购金额")
            part_fen = round(net_p * alloc_q / max(1, p_obj.quantity))
            total_cost_fen += part_fen
            used_by_pid[p_obj.id] = used_q + alloc_q
            resolved_parts.append((p_obj, alloc_q, part_fen))

        final_amount = money(body.cost) if body.cost.strip() else total_cost_fen
        ext_list = "+".join(p.purchase_external_id for p, _, _ in resolved_parts)
        note = f"卡网组合采购 {ext_list}；{body.note.strip() or 'AI组合核对确认'}"[:200]
        adjustment = Adjustment(
            store_id=store_id,
            item_external_id=body.item_id,
            kind="cost_set",
            amount_fen=final_amount,
            note=note,
            request_key=req_key,
        )
        db.add(adjustment)
        db.flush()
        created_matches: list[CardMatch] = []
        for idx, (p_obj, alloc_q, part_fen) in enumerate(resolved_parts):
            sub_key = req_key if idx == 0 else str(uuid5(NAMESPACE_URL, f"{req_key}:{idx}:{p_obj.id}"))
            m_row = db.scalar(select(CardMatch).where(
                CardMatch.store_id == store_id,
                CardMatch.item_external_id == body.item_id,
                CardMatch.purchase_id == p_obj.id,
            ))
            ev_dict = {
                "sale_paid_at": order.paid_at,
                "purchase_paid_at": p_obj.paid_at,
                "sale_title": order.product_title,
                "purchase_title": p_obj.product_title,
                "seller": p_obj.seller,
                "allocated_quantity": alloc_q,
                "allocated_cost_fen": part_fen,
                "bundle": True,
            }
            if m_row is not None:
                m_row.score = body.score
                m_row.allocated_quantity = alloc_q
                m_row.status = "confirmed"
                m_row.evidence = ev_dict
                m_row.metadata_json = {"adjustment_id": adjustment.id, "reviewer_id": reviewer_id}
                m_row.request_key = sub_key
            else:
                m_row = CardMatch(
                    store_id=store_id,
                    item_external_id=body.item_id,
                    purchase_id=p_obj.id,
                    score=body.score,
                    allocated_quantity=alloc_q,
                    status="confirmed",
                    evidence=ev_dict,
                    metadata_json={"adjustment_id": adjustment.id, "reviewer_id": reviewer_id},
                    request_key=sub_key,
                )
                db.add(m_row)
            created_matches.append(m_row)
        db.commit()
        return {
            "id": created_matches[0].id,
            "adjustment_id": adjustment.id,
            "duplicate": False,
            "allocated_quantity": sum(q for _, q, _ in resolved_parts),
            "amount_fen": final_amount,
            "bundle_count": len(created_matches),
        }

    purchase = db.scalar(select(CardPurchase).where(CardPurchase.store_id == store_id, CardPurchase.id == body.purchase_id))
    if not purchase:
        raise ImportProblem("订单或卡网采购单不存在")
    existing_for_item = active_item_matches[0] if active_item_matches else None
    if existing_for_item and existing_for_item.purchase_id != purchase.id:
        raise ImportProblem("这笔千牛订单已经核对过其他卡网采购单，如需修改请在订单明细调整")
    all_adj = list(db.scalars(select(Adjustment).where(Adjustment.store_id == store_id).order_by(Adjustment.id.asc())))
    existing_for_purchase = [
        m for m in _active_confirmed_matches(db, store_id, all_adj) if m.purchase_id == purchase.id
    ]
    duplicate = next((m for m in existing_for_purchase if m.item_external_id == body.item_id), None)
    if duplicate:
        return {"id": duplicate.id, "duplicate": True, "adjustment_id": duplicate.metadata_json.get("adjustment_id")}
    used_qty = sum(max(1, m.allocated_quantity or 1) for m in existing_for_purchase)
    requested_purchase_qty = getattr(body, "purchase_quantity", None)
    if requested_purchase_qty and int(requested_purchase_qty) > purchase.quantity:
        expanded_qty = int(requested_purchase_qty)
        if expanded_qty <= 100:
            purchase.quantity = expanded_qty
    alloc_qty = max(1, order.quantity)
    if purchase.quantity == 1:
        if used_qty >= 1:
            raise ImportProblem("这张卡网采购单已经匹配给另一笔订单，请先核对原匹配")
        alloc_qty = 1
    elif used_qty + alloc_qty > purchase.quantity:
        remaining = max(0, purchase.quantity - used_qty)
        raise ImportProblem(f"这张卡网采购单剩余可分配数量为 {remaining} 件，无法再分配 {alloc_qty} 件")

    net_fen = purchase.amount_fen - purchase.refund_fen
    if net_fen < 0:
        raise ImportProblem("采购退款不能超过采购金额")
    if body.cost.strip():
        amount = money(body.cost)
    elif purchase.quantity > 1:
        amount = round(net_fen * alloc_qty / purchase.quantity)
    else:
        amount = net_fen
    if amount is not None and amount < 0:
        raise ImportProblem("采购退款不能超过采购金额")
    note = f"卡网采购单 {purchase.purchase_external_id}；{body.note.strip() or '人工确认成本匹配'}"[:200]
    adjustment = Adjustment(
        store_id=store_id,
        item_external_id=body.item_id,
        kind="cost_set",
        amount_fen=amount,
        note=note,
        request_key=req_key,
    )
    db.add(adjustment)
    db.flush()
    superseded_same_pair = db.scalar(select(CardMatch).where(
        CardMatch.store_id == store_id,
        CardMatch.item_external_id == body.item_id,
        CardMatch.purchase_id == purchase.id,
    ))
    ev_payload = {
        "sale_paid_at": order.paid_at,
        "purchase_paid_at": purchase.paid_at,
        "sale_title": order.product_title,
        "purchase_title": purchase.product_title,
        "seller": purchase.seller,
        "allocated_quantity": alloc_qty,
        "allocated_cost_fen": amount,
    }
    if superseded_same_pair is not None:
        superseded_same_pair.score = body.score
        superseded_same_pair.allocated_quantity = alloc_qty
        superseded_same_pair.status = "confirmed"
        superseded_same_pair.evidence = ev_payload
        superseded_same_pair.metadata_json = {"adjustment_id": adjustment.id, "reviewer_id": reviewer_id}
        superseded_same_pair.request_key = req_key
        match = superseded_same_pair
    else:
        match = CardMatch(
            store_id=store_id,
            item_external_id=body.item_id,
            purchase_id=purchase.id,
            score=body.score,
            allocated_quantity=alloc_qty,
            status="confirmed",
            evidence=ev_payload,
            metadata_json={"adjustment_id": adjustment.id, "reviewer_id": reviewer_id},
            request_key=req_key,
        )
        db.add(match)
    db.commit()
    return {"id": match.id, "adjustment_id": adjustment.id, "duplicate": False, "allocated_quantity": alloc_qty, "amount_fen": amount}


def batch_confirm_matches(db: Session, store_id: int, items: list, reviewer_id: int) -> dict:
    confirmed_count = 0
    duplicate_count = 0
    skipped_count = 0
    total_cost_fen = 0
    errors: list[str] = []
    results: list[dict] = []
    for entry in items:
        try:
            res = confirm_match(db, store_id, entry, reviewer_id=reviewer_id)
            if res.get("duplicate"):
                duplicate_count += 1
            else:
                confirmed_count += 1
                total_cost_fen += int(res.get("amount_fen") or 0)
            results.append({"item_id": entry.item_id, **res})
        except ImportProblem as exc:
            db.rollback()
            skipped_count += 1
            errors.append(f"{entry.item_id}: {exc}")
    return {
        "confirmed_count": confirmed_count,
        "duplicate_count": duplicate_count,
        "skipped_count": skipped_count,
        "total_cost_fen": total_cost_fen,
        "errors": errors[:10],
        "items": results,
    }


def auto_confirm_exact_matches(
    db: Session,
    store_id: int,
    reviewer_id: int,
    *,
    start: str = "",
    end: str = "",
    channel: str = "",
) -> dict:
    rep = suggestions(db, store_id, start=start, end=end, limit=5000, use_ai=False, channel=channel)
    bodies: list[SimpleNamespace] = []
    for entry in rep.get("suggestions") or []:
        cands = entry.get("candidates") or []
        if not cands:
            continue
        primary = cands[0]
        if not primary.get("is_exact_link") or not primary.get("auto_confirmable"):
            continue
        req_id = str(uuid5(NAMESPACE_URL, f"exact:{store_id}:{entry['item_id']}:{primary['purchase_external_id']}"))
        allocs = primary.get("allocations") or []
        code_hint = primary.get("linked_redeem_code") or primary["purchase_external_id"]
        bodies.append(
            SimpleNamespace(
                request_id=req_id,
                item_id=entry["item_id"],
                purchase_id=primary["purchase_id"],
                cost="",
                score=primary["score"],
                allocated_quantity=primary["allocated_quantity"],
                purchase_quantity=primary.get("purchase_quantity"),
                allocations=allocs if len(allocs) > 1 else None,
                note=f"方案二精确关联自动确认({code_hint})",
            )
        )
    if not bodies:
        return {
            "matched_exact_candidates": 0,
            "confirmed_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
            "total_cost_fen": 0,
            "errors": [],
            "items": [],
        }
    res = batch_confirm_matches(db, store_id, bodies, reviewer_id=reviewer_id)
    res["matched_exact_candidates"] = len(bodies)
    return res


def ledger(
    db: Session,
    store_id: int,
    *,
    category: str = "",
    status: str = "",
    source: str = "",
    channel: str = "",
    start: str = "",
    end: str = "",
    q: str = "",
    limit: int = 200,
) -> dict:
    purchases = list(db.scalars(select(CardPurchase).where(CardPurchase.store_id == store_id).order_by(CardPurchase.paid_at.desc(), CardPurchase.id.desc())))
    all_orders = list(db.scalars(select(OrderItem).where(OrderItem.store_id == store_id).order_by(OrderItem.paid_at.desc(), OrderItem.id.desc())))
    channel_filter = normalize_channel_filter(channel)
    channel_counts: dict[str, int] = {"qianniu": 0, "16688": 0}
    for o in all_orders:
        ch = getattr(o, "channel", None) or "qianniu"
        channel_counts[ch] = channel_counts.get(ch, 0) + 1
    orders = [o for o in all_orders if not channel_filter or (getattr(o, "channel", None) or "qianniu") == channel_filter]
    adjustments = list(db.scalars(select(Adjustment).where(Adjustment.store_id == store_id).order_by(Adjustment.id.asc())))
    matches = sorted(_active_confirmed_matches(db, store_id, adjustments), key=lambda m: m.id, reverse=True)
    cost_versions = {c.id: c for c in db.scalars(select(CostVersion).where(CostVersion.store_id == store_id))}
    inferred_multi = _infer_16688_multi_units(purchases)

    order_by_item = {o.item_external_id: o for o in all_orders}
    purchase_by_id = {p.id: p for p in purchases}

    latest_cost_adj: dict[str, Adjustment] = {}
    invalidated_items: set[str] = set()
    for a in adjustments:
        if a.kind == "cost_set":
            latest_cost_adj[a.item_external_id] = a
            invalidated_items.discard(a.item_external_id)
        elif a.kind == "cost_invalidate":
            latest_cost_adj.pop(a.item_external_id, None)
            invalidated_items.add(a.item_external_id)

    matches_by_purchase: dict[int, list[CardMatch]] = defaultdict(list)
    matches_by_item: dict[str, list[CardMatch]] = defaultdict(list)
    for m in matches:
        matches_by_purchase[m.purchase_id].append(m)
        matches_by_item[m.item_external_id].append(m)

    purchase_by_ext = {p.purchase_external_id: p for p in purchases}
    adj_links_by_purchase: dict[int, list[tuple[Adjustment, OrderItem | None]]] = defaultdict(list)
    for item_id, adj in latest_cost_adj.items():
        if item_id in matches_by_item:
            continue
        cited_ids = _extract_cited_purchase_ids(adj.note or "", purchase_by_ext)
        if len(cited_ids) == 1 and cited_ids[0] in purchase_by_ext:
            adj_links_by_purchase[purchase_by_ext[cited_ids[0]].id].append((adj, order_by_item.get(item_id)))

    cat_filter = "" if category in ("", "all") else category
    status_filter = "" if status in ("", "all") else status
    source_filter = "" if source in ("", "all") else source
    query_text = q.strip().lower()

    def _in_date(ts: str | None) -> bool:
        if not start and not end:
            return True
        if not ts:
            return False
        day_str = ts[:10]
        if start and day_str < start:
            return False
        if end and day_str > end:
            return False
        return True

    category_counts: dict[str, int] = {k: 0 for k in CATEGORY_LABELS}
    category_group_counts: dict[str, int] = {k: 0 for k in CATEGORY_GROUP_LABELS}
    source_counts: dict[str, int] = {
        "16688": 0,
        "wzyp": 0,
        "tg_fatbunny": 0,
        "tg_chugax": 0,
        "tg_canboso": 0,
        "tg": 0,
        "thatim": 0,
    }
    purchases_out = []
    p_pending = p_matched = p_partial = p_excluded = 0

    for p in purchases:
        src_raw = str(p.source or "")
        if "链动" in src_raw or p.purchase_external_id.startswith("LD"):
            source_counts["wzyp"] += 1
        elif src_raw == "tg_fatbunny":
            source_counts["tg_fatbunny"] += 1
            source_counts["tg"] += 1
        elif src_raw == "tg_chugax":
            source_counts["tg_chugax"] += 1
            source_counts["tg"] += 1
        elif src_raw.startswith("tg_"):
            source_counts["tg_canboso"] += 1
            source_counts["tg"] += 1
        elif src_raw == "thatim":
            source_counts["thatim"] += 1
        elif "16688" in src_raw or p.purchase_external_id.startswith("FF"):
            source_counts["16688"] += 1
        net_fen = max(0, p.amount_fen - p.refund_fen)
        cat = classify_product("", p.product_title, amount_fen=net_fen, is_sale=False)
        category_counts[cat] = category_counts.get(cat, 0) + 1
        category_group_counts[category_group(cat)] += 1
        p_matches = matches_by_purchase.get(p.id, [])
        p_adj_links = adj_links_by_purchase.get(p.id, [])
        allocated_qty = sum(max(1, m.allocated_quantity or 1) for m in p_matches) + sum(
            max(1, o.quantity if o else 1) for _, o in p_adj_links
        )
        inferred_qty = inferred_multi[p.id][0] if p.id in inferred_multi else None
        eff_qty = max(allocated_qty, inferred_qty if inferred_qty else max(1, p.quantity))
        remaining_qty = max(0, eff_qty - allocated_qty)
        inactive = _is_inactive_purchase(p)
        if p_matches or p_adj_links:
            if remaining_qty > 0 and eff_qty > 1:
                rec_status = "partial"
                rec_label = f"部分核对 ({allocated_qty}/{eff_qty})"
                p_partial += 1
            else:
                rec_status = "matched"
                rec_label = "已核对"
            p_matched += 1
        elif inactive:
            rec_status = "excluded"
            rec_label = "已退款/关闭"
            p_excluded += 1
        else:
            rec_status = "pending"
            rec_label = "待核对"
            p_pending += 1

        matched_details = []
        for m in p_matches:
            o = order_by_item.get(m.item_external_id)
            matched_details.append({
                "match_id": m.id,
                "item_id": m.item_external_id,
                "order_id": o.order_external_id if o else m.item_external_id,
                "sale_title": o.product_title if o else (m.evidence or {}).get("sale_title", ""),
                "sku": o.sku if o else "",
                "allocated_quantity": max(1, m.allocated_quantity or 1),
                "score": m.score,
                "matched_at": _format_local_ts(m.created_at),
            })
        for adj, o in p_adj_links:
            matched_details.append({
                "match_id": None,
                "item_id": adj.item_external_id,
                "order_id": o.order_external_id if o else adj.item_external_id,
                "sale_title": o.product_title if o else "",
                "sku": o.sku if o else "",
                "allocated_quantity": max(1, o.quantity if o else 1),
                "score": 100,
                "matched_at": _format_local_ts(adj.created_at),
            })
        matched_order_ids = list(dict.fromkeys(d["order_id"] for d in matched_details))
        matched_at = matched_details[0]["matched_at"] if matched_details else ""

        if cat_filter and not category_matches(cat, cat_filter):
            continue
        if status_filter:
            if status_filter == "matched" and rec_status not in ("matched", "partial"):
                continue
            elif status_filter != "matched" and rec_status != status_filter:
                continue
        if source_filter:
            if source_filter == "16688" and "16688" not in p.source and not p.purchase_external_id.startswith("FF"):
                continue
            elif source_filter == "wzyp" and "链动" not in p.source and not p.purchase_external_id.startswith("LD"):
                continue
            elif source_filter == "tg" and not str(p.source or "").startswith("tg_"):
                continue
            elif source_filter in ("tg_fatbunny", "tg_chugax", "tg_canboso", "thatim") and p.source != source_filter:
                continue
            elif source_filter not in ("16688", "wzyp", "tg", "tg_fatbunny", "tg_chugax", "tg_canboso", "thatim") and p.source != source_filter:
                continue
        if not _in_date(p.paid_at):
            continue
        if query_text:
            haystack = " ".join([
                p.purchase_external_id,
                p.product_title,
                p.seller,
                p.source,
                source_label(p.source),
                p.linked_order_external_id or "",
                p.linked_redeem_code or "",
                *matched_order_ids,
                *(d["item_id"] for d in matched_details),
            ]).lower()
            if query_text not in haystack:
                continue
        purchases_out.append({
            "id": p.id,
            "purchase_external_id": p.purchase_external_id,
            "product_title": p.product_title,
            "seller": p.seller,
            "quantity": p.quantity,
            "effective_quantity": eff_qty,
            "inferred_quantity": inferred_qty,
            "allocated_quantity": allocated_qty,
            "remaining_quantity": remaining_qty,
            "amount_fen": p.amount_fen,
            "refund_fen": p.refund_fen,
            "net_fen": net_fen,
            "unit_cost_fen": round(net_fen / eff_qty) if eff_qty else net_fen,
            "paid_at": p.paid_at,
            "status": p.status,
            "source": p.source,
            "source_label": source_label(p.source),
            "original_currency": p.original_currency or "CNY",
            "original_amount": p.original_amount or "",
            "fx_rate": p.fx_rate or "1",
            "linked_order_external_id": p.linked_order_external_id or "",
            "linked_channel": p.linked_channel or "",
            "linked_redeem_code": p.linked_redeem_code or "",
            "category": cat,
            "category_label": CATEGORY_LABELS[cat],
            "category_group": category_group(cat),
            "category_group_label": CATEGORY_GROUP_LABELS[category_group(cat)],
            "reconcile_status": rec_status,
            "reconcile_label": rec_label,
            "matched_at": matched_at,
            "matched_order_ids": matched_order_ids,
            "matches": matched_details,
        })

    orders_out = []
    orders_category_counts: dict[str, int] = {k: 0 for k in CATEGORY_LABELS}
    orders_category_group_counts: dict[str, int] = {k: 0 for k in CATEGORY_GROUP_LABELS}
    o_pending = o_matched = o_excluded = 0
    for o in orders:
        unit_pay_fen = (o.payment_fen // max(1, o.quantity)) if o.payment_fen is not None else None
        feat = extract_features(o.sku, o.product_title, amount_fen=unit_pay_fen, is_sale=True)
        cat = feat.category
        orders_category_counts[cat] = orders_category_counts.get(cat, 0) + 1
        orders_category_group_counts[category_group(cat)] += 1
        o_matches = matches_by_item.get(o.item_external_id, [])
        adj = latest_cost_adj.get(o.item_external_id)
        has_version_cost = o.cost_fen is not None and o.item_external_id not in invalidated_items
        cv = cost_versions.get(o.cost_version_id) if o.cost_version_id else None
        if feat.is_non_product:
            rec_status = "excluded"
            rec_label = "无需核对"
            o_excluded += 1
        elif o_matches or adj is not None or has_version_cost:
            rec_status = "matched"
            rec_label = "已核对"
            o_matched += 1
        elif o.paid_at is None or o.status == "closed" or o.refund_status == "success":
            rec_status = "excluded"
            rec_label = "已退/无需核对" if o.paid_at else "未付款关单"
            o_excluded += 1
        else:
            rec_status = "pending"
            rec_label = "待核对"
            o_pending += 1

        matched_details = []
        for m in o_matches:
            p = purchase_by_id.get(m.purchase_id)
            matched_details.append({
                "match_id": m.id,
                "purchase_id": m.purchase_id,
                "purchase_external_id": p.purchase_external_id if p else "",
                "purchase_title": p.product_title if p else (m.evidence or {}).get("purchase_title", ""),
                "seller": p.seller if p else (m.evidence or {}).get("seller", ""),
                "score": m.score,
                "allocated_quantity": max(1, m.allocated_quantity or 1),
                "matched_at": _format_local_ts(m.created_at),
            })
        matched_purchase_ids = list(dict.fromkeys(d["purchase_external_id"] for d in matched_details if d["purchase_external_id"]))
        if matched_details:
            matched_at = matched_details[0]["matched_at"]
        elif adj is not None:
            matched_at = _format_local_ts(adj.created_at)
            extracted_ids = _extract_cited_purchase_ids(adj.note or "", purchase_by_ext)
            matched_purchase_ids = extracted_ids or (["手工设成本"] if adj.note else [])
        elif cv is not None and has_version_cost:
            matched_at = _format_local_ts(cv.created_at)
            matched_purchase_ids = [f"成本版本#{cv.id}"]
        else:
            matched_at = ""

        effective_cost_fen = adj.amount_fen if adj is not None else (o.cost_fen if has_version_cost else None)

        if cat_filter and not category_matches(cat, cat_filter):
            continue
        if status_filter and rec_status != status_filter:
            continue
        if not _in_date(o.paid_at):
            continue
        if query_text:
            haystack = " ".join([
                o.order_external_id,
                o.item_external_id,
                o.product_title,
                o.sku,
                *matched_purchase_ids,
            ]).lower()
            if query_text not in haystack:
                continue
        order_channel = getattr(o, "channel", None) or "qianniu"
        orders_out.append({
            "item_id": o.item_external_id,
            "order_id": o.order_external_id,
            "channel": order_channel,
            "channel_label": CHANNEL_LABELS.get(order_channel, order_channel),
            "sku": o.sku,
            "product_title": o.product_title,
            "quantity": o.quantity,
            "payment_fen": o.payment_fen,
            "cost_fen": effective_cost_fen,
            "paid_at": o.paid_at,
            "status": o.status,
            "refund_status": o.refund_status,
            "category": cat,
            "category_label": CATEGORY_LABELS[cat],
            "category_group": category_group(cat),
            "category_group_label": CATEGORY_GROUP_LABELS[category_group(cat)],
            "reconcile_status": rec_status,
            "reconcile_label": rec_label,
            "matched_at": matched_at,
            "matched_purchase_ids": matched_purchase_ids,
            "matches": matched_details,
        })

    return {
        "counts": {
            "purchases_total": len(purchases),
            "purchases_pending": p_pending,
            "purchases_matched": p_matched,
            "purchases_partial": p_partial,
            "purchases_excluded": p_excluded,
            "purchases_filtered": len(purchases_out),
            "orders_total": len(orders),
            "orders_pending": o_pending,
            "orders_matched": o_matched,
            "orders_excluded": o_excluded,
            "orders_filtered": len(orders_out),
            "category_counts": category_counts,
            "orders_category_counts": orders_category_counts,
            "category_group_counts": category_group_counts,
            "orders_category_group_counts": orders_category_group_counts,
            "source_counts": source_counts,
            "channel_counts": channel_counts,
        },
        "purchases": purchases_out[:limit],
        "orders": orders_out[:limit],
    }
