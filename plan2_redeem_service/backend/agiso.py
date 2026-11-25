from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any, Optional

import httpx


logger = logging.getLogger(__name__)

PLAN2_CODE_RE = re.compile(r"\bU20[SE]-[A-Z0-9]{4}-[A-Z0-9]{4}\b")

AOPIC_BUYER_CREATE = 1
AOPIC_BUYER_PAY = 2
AOPIC_BUYER_CONFIRM = 4
AOPIC_REFUND_CREATED = 256
AOPIC_AUTO_SEND_SUCCESS = 2048
AOPIC_REFUND_CLOSED = 32768
AOPIC_REFUND_SUCCESS = 65536
AOPIC_SELLER_SHIP = 1048576
AOPIC_BUYER_PAY_SIMPLE = 2097152

DEFAULT_SKUS_FIELDS = "sku_id,iid,num_iid,properties,properties_name,quantity,price,outer_id,status"


def build_redeem_url(public_base_url: str, code: str) -> str:
    return f"{public_base_url.rstrip('/')}/r/{code}"


SLOT_DELIVERY_GUIDE = (
    "【先看这里】\n"
    "淘宝显示已发货，就是兑换码和提货链接已经发给你了，直接点上面的链接去填 Gmail 就行。\n"
    "\n"
    "【填邮箱前先查这 3 件事（都要满足）】\n"
    "1. 先关掉 Google 付款资料：https://payments.google.com/gp/w/home/settings\n"
    "2. 先退出原来的 Google 家庭组：https://g.co/yourfamily\n"
    "3. 这个 Gmail 最近 12 个月里没加过、没退过、也没被重拉过任何 Google 家庭组（Google 规定每 12 个月只能换 1 次家庭组，哪怕只进出过 1 次也会被限制加不进新组）。\n"
    "\n"
    "【提交后等多久】\n"
    "提交 Gmail 后就会马上给你占好名额并开始排队，一般 2-6 小时内发家庭组邀请，最晚 24 小时，没超过 24 小时不用催；随时点提货链接都能看进度，收到 Google 邮件点“接受邀请”就开通了。\n"
    "\n"
    "【质保和退换说明】\n"
    "1. 质保 30 天会员，不保你的 Google 账号本身；自己违规使用或反代导致封号没法保修。\n"
    "2. 本款是 0 积分/点数车位，请不要使用积分或点数，用了会被系统自动踢出家庭组且不退款。\n"
    "3. 提交邮箱后就占用了名额，如果不是商品本身问题（比如 12 个月内换过组加不进、没退旧家庭组、没关付款资料、填错邮箱等），不支持全额退款；可以补 49.9 元手续费换新 Gmail 重新拉人，或扣除 49.9 元名额成本后退还余款。"
)

EXCLUSIVE_DELIVERY_GUIDE = (
    "【先看这里】\n"
    "淘宝显示已发货，就是兑换码和提货链接已经发给你了，直接点上面的链接就能秒领会员邮箱。\n"
    "\n"
    "【开通前先查这 3 件事】\n"
    "1. 先关掉 Google 付款资料：https://payments.google.com/gp/w/home/settings\n"
    "2. 先退出原来的 Google 家庭组：https://g.co/yourfamily\n"
    "3. 准备一个你自己常用的稳定 Gmail 当组长，且这个号最近 12 个月里没加过、没退过、也没被重拉过其他家庭组（Google 规定每 12 个月只能换 1 次家庭组）。\n"
    "\n"
    "【3 步开通（很简单）】\n"
    "1. 点上面的提货链接，立刻领到我们给的会员邮箱。\n"
    "2. 用你自己的常用 Gmail 建一个 Google 家庭组，把这个会员邮箱拉进去；对方同意后你就开通 Ultra 会员了，自己还能再拉 4 个人一起用。\n"
    "3. 发完邀请后，记得回到这个提货页上传一张「已发出邀请」的截图，用来登记 30 天质保（一般 2-6 小时内处理，最晚 24 小时，没超过 24 小时不用催）。\n"
    "\n"
    "【质保和退换说明】\n"
    "1. 千万别踢掉我们给的会员邮箱！踢掉就会立刻掉会员，而且没法保修。\n"
    "2. 质保 30 天（从领到邮箱那天开始算），如果不是你自己的原因掉会员，免费给你保修；领完邮箱后，不是商品本身问题不支持全额退款。\n"
    "3. 如果是你自己的 Gmail 被封或登不上，没法保修。\n"
    "4. 如果因为自己的号被封、登不上、12 个月换组限制，或不小心踢掉了我们给的会员邮箱导致用不了/掉会员，补救或换号要收 79.9 元手续费，而且不保证一定能恢复。"
)


def format_delivery_message(sku_title: str, product_type: str, code: str, redeem_url: str) -> str:
    if product_type == "slot":
        return (
            f"感谢下单 {sku_title}！\n"
            f"点链接自助填 Gmail 提货（不用在旺旺发邮箱）：{redeem_url}\n"
            f"兑换码：{code}\n"
            "\n"
            f"{SLOT_DELIVERY_GUIDE}"
        )
    return (
        f"感谢下单 {sku_title}！\n"
        f"点链接自助秒领会员邮箱：{redeem_url}\n"
        f"兑换码：{code}\n"
        "\n"
        f"{EXCLUSIVE_DELIVERY_GUIDE}"
    )


def format_trade_delivery_message(
    deliveries: list[dict[str, Any]],
    skus: dict[str, dict[str, Any]],
    public_base_url: str,
) -> str:
    if not deliveries:
        return ""
    if len(deliveries) == 1:
        item = deliveries[0]
        sku = skus[item["sku_key"]]
        url = build_redeem_url(public_base_url, item["code"])
        return format_delivery_message(sku["title"], sku["product_type"], item["code"], url)
    blocks: list[str] = []
    for index, item in enumerate(deliveries, start=1):
        sku = skus[item["sku_key"]]
        url = build_redeem_url(public_base_url, item["code"])
        link_label = (
            "点链接自助填 Gmail 提货（不用在旺旺发邮箱）："
            if sku["product_type"] == "slot"
            else "点链接自助秒领会员邮箱："
        )
        blocks.append(
            f"【第 {index} 份】{sku['title']}\n"
            f"{link_label}{url}\n"
            f"兑换码：{item['code']}"
        )
    has_slot = any(skus[item["sku_key"]]["product_type"] == "slot" for item in deliveries)
    has_account = any(skus[item["sku_key"]]["product_type"] != "slot" for item in deliveries)
    guides: list[str] = []
    if has_slot and has_account:
        guides.append(f"【共享车位·提货与使用须知】\n{SLOT_DELIVERY_GUIDE}")
        guides.append(f"【独享整组·开通与质保须知】\n{EXCLUSIVE_DELIVERY_GUIDE}")
    elif has_slot:
        guides.append(SLOT_DELIVERY_GUIDE)
    elif has_account:
        guides.append(EXCLUSIVE_DELIVERY_GUIDE)
    header = f"感谢下单！本次共为您发出 {len(deliveries)} 份提货码："
    text = "\n\n".join([header, *blocks, *guides])
    return text[:4950]


def build_create_order_result_content(
    deliveries: list[dict[str, Any]],
    skus: dict[str, dict[str, Any]],
    public_base_url: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in deliveries:
        oid_key = str(item["oid"])
        grouped.setdefault(oid_key, []).append(item)

    content: list[dict[str, Any]] = []
    for oid_str, group_items in grouped.items():
        oid_value: Any = int(oid_str) if oid_str.isdigit() else oid_str
        by_sku: dict[str, list[dict[str, Any]]] = {}
        for item in group_items:
            by_sku.setdefault(item["sku_key"], []).append(item)
        card_pwd_content: list[dict[str, Any]] = []
        for sku_key, sku_items in by_sku.items():
            sku = skus[sku_key]
            card_pwd_content.append(
                {
                    "CardNoShowType": 0,
                    "PwdShowType": 0,
                    "Title": sku["title"],
                    "PrefixCardNo": "提货链接：",
                    "PrefixPwd": "兑换码：",
                    "CardPwdArr": [
                        {
                            "c": build_redeem_url(public_base_url, entry["code"]),
                            "p": entry["code"],
                            "d": "",
                        }
                        for entry in sku_items
                    ],
                }
            )
        additional = format_trade_delivery_message(group_items, skus, public_base_url)
        content.append(
            {
                "Oid": oid_value,
                "Additional": additional,
                "CardPwdContent": card_pwd_content,
            }
        )
    return content


def _redact(text: str, secrets_to_hide: list[str]) -> str:
    cleaned = (text or "").strip()
    for secret in secrets_to_hide:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    cleaned = " ".join(cleaned.split())
    return cleaned[:240]


def sign_agiso_api_params(params: dict[str, Any], app_secret: str) -> str:
    items: dict[str, str] = {}
    for key, value in params.items():
        if key == "sign" or value is None or isinstance(value, (bytes, bytearray)):
            continue
        if isinstance(value, bool):
            items[key] = "1" if value else "0"
        else:
            items[key] = str(value)
    ordered_keys = sorted(items.keys())
    raw = app_secret + "".join(f"{key}{items[key]}" for key in ordered_keys) + app_secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().lower()


def verify_agiso_push_signature(raw_json: str, timestamp: str, sign: str, app_secret: str) -> bool:
    if not app_secret or not timestamp or not sign:
        return False
    payload = f"{app_secret}json{raw_json}timestamp{timestamp}{app_secret}"
    expected = hashlib.md5(payload.encode("utf-8")).hexdigest().lower()
    return hmac.compare_digest(expected, sign.strip().lower())


def _norm_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_int_field(entry: dict[str, Any], key: str, *, default: Optional[int] = None, minimum: Optional[int] = None, maximum: Optional[int] = None, idx: int = 0) -> int:
    raw = entry.get(key, default)
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"Restock rule at index {idx} has invalid integer field {key!r}")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Restock rule at index {idx} has invalid integer field {key!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"Restock rule at index {idx} field {key!r} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"Restock rule at index {idx} field {key!r} must be <= {maximum}")
    return value


def parse_env_restock_rules(raw_json: str) -> list[dict[str, Any]]:
    text = (raw_json or "").strip()
    if not text:
        return []
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("PLAN2_AGISO_RESTOCK_RULES_JSON must be a JSON array")
    normalized: list[dict[str, Any]] = []
    for idx, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ValueError(f"Restock rule at index {idx} must be an object")
        sku_key = _norm_id(entry.get("sku_key"))
        if not sku_key:
            raise ValueError(f"Restock rule at index {idx} requires sku_key")
        cpc_id = _parse_int_field(entry, "cpc_id", minimum=1, idx=idx)
        min_stock = _parse_int_field(entry, "min_stock", default=0, minimum=0, idx=idx)
        target_stock = _parse_int_field(entry, "target_stock", minimum=max(1, min_stock), idx=idx)
        max_push = _parse_int_field(entry, "max_push", default=20, minimum=1, maximum=500, idx=idx)
        normalized.append(
            {
                "sku_key": sku_key,
                "cpc_id": cpc_id,
                "min_stock": min_stock,
                "target_stock": target_stock,
                "max_push": max_push,
                "enabled": bool(entry.get("enabled", True)),
                "is_use_first": bool(entry.get("is_use_first", False)),
                "allow_replace": bool(entry.get("allow_replace", False)),
                "note": _norm_id(entry.get("note")),
            }
        )
    return normalized


def parse_env_sku_rules(raw_json: str) -> list[dict[str, Any]]:
    text = (raw_json or "").strip()
    if not text:
        return []
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("PLAN2_AGISO_SKU_RULES_JSON must be a JSON array")
    normalized: list[dict[str, Any]] = []
    for idx, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ValueError(f"SKU rule at index {idx} must be an object")
        normalized.append(
            {
                "mapping_id": None,
                "source": "env",
                "num_iid": _norm_id(entry.get("num_iid")),
                "sku_id": _norm_id(entry.get("sku_id")),
                "outer_iid": _norm_id(entry.get("outer_iid")),
                "outer_sku_id": _norm_id(entry.get("outer_sku_id")),
                "sku_props_contains": _norm_id(entry.get("sku_props_contains")),
                "sku_key": _norm_id(entry.get("sku_key")),
                "enabled": bool(entry.get("enabled", True)),
                "note": _norm_id(entry.get("note")),
            }
        )
    return normalized


def rule_has_matcher(rule: dict[str, Any]) -> bool:
    return any(
        _norm_id(rule.get(field))
        for field in ("outer_sku_id", "sku_id", "num_iid", "outer_iid", "sku_props_contains")
    )


def _rule_priority(rule: dict[str, Any]) -> int:
    if _norm_id(rule.get("outer_sku_id")):
        return 50
    if _norm_id(rule.get("sku_id")):
        return 40
    if _norm_id(rule.get("num_iid")) and _norm_id(rule.get("sku_props_contains")):
        return 30
    if _norm_id(rule.get("outer_iid")):
        return 20
    if _norm_id(rule.get("sku_props_contains")):
        return 15
    if _norm_id(rule.get("num_iid")):
        return 10
    return 0


def match_agiso_sku(
    order: dict[str, Any],
    rules: list[dict[str, Any]],
    valid_sku_keys: Optional[set[str]] = None,
) -> Optional[dict[str, Any]]:
    num_iid = _norm_id(order.get("NumIid") if "NumIid" in order else order.get("num_iid"))
    sku_id = _norm_id(order.get("SkuId") if "SkuId" in order else order.get("sku_id"))
    outer_iid = _norm_id(order.get("OuterIid") if "OuterIid" in order else order.get("outer_iid"))
    outer_sku_id = _norm_id(order.get("OuterSkuId") if "OuterSkuId" in order else order.get("outer_sku_id"))
    sku_props = str(
        order.get("SkuPropertiesName")
        if order.get("SkuPropertiesName") is not None
        else (order.get("sku_properties_name") or "")
    ).lower()

    best_rule: Optional[dict[str, Any]] = None
    best_rank: tuple[int, int] = (-1, 0)

    for index, rule in enumerate(rules):
        if not rule.get("enabled", True):
            continue
        sku_key = _norm_id(rule.get("sku_key"))
        if not sku_key:
            continue
        if valid_sku_keys is not None and sku_key not in valid_sku_keys:
            continue
        if not rule_has_matcher(rule):
            continue

        rule_outer_sku_id = _norm_id(rule.get("outer_sku_id"))
        rule_sku_id = _norm_id(rule.get("sku_id"))
        rule_outer_iid = _norm_id(rule.get("outer_iid"))
        rule_num_iid = _norm_id(rule.get("num_iid"))
        rule_props = _norm_id(rule.get("sku_props_contains")).lower()

        if rule_outer_sku_id and rule_outer_sku_id != outer_sku_id:
            continue
        if rule_sku_id and rule_sku_id != sku_id:
            continue
        if rule_outer_iid and rule_outer_iid != outer_iid:
            continue
        if rule_num_iid and rule_num_iid != num_iid:
            continue
        if rule_props and rule_props not in sku_props:
            continue

        priority = _rule_priority(rule)
        rank = (priority, -index)
        if rank > best_rank:
            best_rank = rank
            best_rule = rule

    return best_rule


def extract_sent_card_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    orders = payload.get("Orders")
    if not isinstance(orders, list):
        return found
    for order in orders:
        if not isinstance(order, dict):
            continue
        oid = _norm_id(order.get("Oid"))
        num_iid = _norm_id(order.get("NumIid"))
        sku_id = _norm_id(order.get("SkuId"))
        send_cards = order.get("SendCards")
        if not isinstance(send_cards, list):
            continue
        for group in send_cards:
            if not isinstance(group, dict):
                continue
            cpc_id = _norm_id(group.get("CpcId"))
            cards = group.get("Cards")
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                for field in ("Pwd", "Card"):
                    text = str(card.get(field) or "")
                    for match in PLAN2_CODE_RE.findall(text):
                        if match not in seen:
                            seen.add(match)
                            found.append(
                                {
                                    "oid": oid,
                                    "num_iid": num_iid,
                                    "sku_id": sku_id,
                                    "cpc_id": cpc_id,
                                    "code": match,
                                }
                            )
    return found


def extract_plan2_codes_from_sent_cards(payload: dict[str, Any]) -> list[tuple[str, str]]:
    return [(entry["oid"], entry["code"]) for entry in extract_sent_card_entries(payload)]


class AgisoError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: Optional[int] = None,
        status_code: Optional[int] = None,
        allow_retry: Optional[bool] = None,
        transient: bool = False,
        data: Any = None,
    ):
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.allow_retry = allow_retry
        self.transient = transient
        self.data = data
        super().__init__(message)


class AgisoClient:
    def __init__(
        self,
        base_url: str,
        app_secret: str,
        access_token: str,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.app_secret = app_secret.strip()
        self.access_token = access_token.strip()
        self.timeout = timeout

    @property
    def is_ready(self) -> bool:
        return bool(self.app_secret and self.access_token)

    def _post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.app_secret or not self.access_token:
            raise AgisoError("Agiso API credentials are not configured", status_code=503, transient=False)
        payload: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                payload[key] = "1" if value else "0"
            else:
                payload[key] = str(value)
        payload["timestamp"] = str(int(time.time()))
        payload["sign"] = sign_agiso_api_params(payload, self.app_secret)
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "ApiVersion": "1",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        secrets_to_hide = [self.app_secret, self.access_token]
        try:
            response = httpx.post(
                f"{self.base_url}{path}",
                data=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise AgisoError(
                f"Agiso {path} timed out after {self.timeout:g}s",
                status_code=504,
                transient=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise AgisoError(
                _redact(f"Agiso {path} network error ({type(exc).__name__})", secrets_to_hide),
                status_code=503,
                transient=True,
            ) from exc

        if response.status_code >= 400:
            detail = _redact(response.text, secrets_to_hide)
            raise AgisoError(
                f"Agiso HTTP {response.status_code}: {detail}" if detail else f"Agiso HTTP {response.status_code}",
                status_code=response.status_code,
                transient=response.status_code in {429, 500, 502, 503, 504},
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AgisoError(
                f"Agiso {path} returned invalid JSON",
                status_code=502,
                transient=True,
            ) from exc
        if not isinstance(body, dict):
            raise AgisoError(f"Agiso {path} returned unexpected payload", status_code=502, transient=True)
        if not body.get("IsSuccess"):
            error_code = body.get("Error_Code")
            error_msg = _redact(str(body.get("Error_Msg") or "Agiso API error"), secrets_to_hide)
            allow_retry = body.get("AllowRetry")
            transient = bool(allow_retry) or error_code in {2, 10, 13}
            raise AgisoError(
                f"Agiso error {error_code}: {error_msg}",
                error_code=error_code if isinstance(error_code, int) else None,
                status_code=200,
                allow_retry=allow_retry if isinstance(allow_retry, bool) else None,
                transient=transient,
                data=body.get("Data"),
            )
        return body

    def trade_info(self, tid: str) -> dict[str, Any]:
        body = self._post("/alds/Trade/TradeInfo", {"tid": str(tid)})
        data = body.get("Data")
        if not isinstance(data, dict):
            raise AgisoError("Agiso TradeInfo returned empty data", status_code=502, transient=True)
        return data

    def trades_buyer_pay(self, modify_time_start: str, page_no: int = 1, page_size: int = 20) -> list[dict[str, Any]]:
        body = self._post(
            "/alds/Trade/TradesBuyerPay",
            {
                "modifyTimeStart": modify_time_start,
                "pageNo": page_no,
                "pageSize": min(max(1, page_size), 100),
            },
        )
        data = body.get("Data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("List", "Trades", "Items"):
                nested = data.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
            if "Tid" in data:
                return [data]
        return []

    def create_order_result(self, tid: str, content: list[dict[str, Any]], alds_type: int = 1) -> dict[str, Any]:
        return self._post(
            "/alds/Trade/CreateOrderResult",
            {
                "tid": str(tid),
                "aldsType": alds_type,
                "content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
            },
        )

    def logistics_dummy_send(self, tids: list[str]) -> dict[str, Any]:
        return self._post("/alds/Trade/LogisticsDummySend", {"tids": ",".join(str(t) for t in tids)})

    def send_tb_app_card(self, tid: str) -> dict[str, Any]:
        return self._post("/alds/Trade/SendTbAppCard", {"tid": str(tid)})

    def send_ww_msg(self, tid: str, msg: str) -> dict[str, Any]:
        return self._post("/alds/WwMsg/Send", {"tid": str(tid), "msg": msg[:5000]})

    def item_skus_get(self, num_iids: list[str], fields: str = DEFAULT_SKUS_FIELDS) -> list[dict[str, Any]]:
        body = self._post(
            "/alds/Item/SkusGet",
            {
                "numIids": ",".join(str(item).strip() for item in num_iids[:40] if str(item).strip()),
                "fields": fields,
            },
        )
        data = body.get("Data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def card_pwd_category_get_list(self, page_no: int = 1, page_size: int = 100) -> dict[str, Any]:
        body = self._post(
            "/alds/CardPwdCategory/GetList",
            {"pageNo": page_no, "pageSize": min(max(1, page_size), 100)},
        )
        data = body.get("Data")
        return data if isinstance(data, dict) else {"List": [], "HasNext": False}

    def card_pwd_category_get_storage(self, id_nos: list[int | str]) -> list[dict[str, Any]]:
        body = self._post(
            "/alds/CardPwdCategory/GetStorage",
            {"idNos": ",".join(str(item) for item in id_nos)},
        )
        data = body.get("Data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def card_pwd_add(
        self,
        cpc_id: int | str,
        lines: list[str],
        *,
        is_use_first: bool = False,
        allow_replace: bool = False,
        summary: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cpcId": str(cpc_id),
            "data": "\n".join(lines),
            "isUseFirst": 1 if is_use_first else 0,
            "allowReplace": 1 if allow_replace else 0,
        }
        if summary:
            params["summary"] = summary
        body = self._post("/alds/CardPwd/Add", params)
        return body.get("Data") if isinstance(body.get("Data"), dict) else body


class DryRunAgisoClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.trades: dict[str, dict[str, Any]] = {}
        self.pending_trades: list[dict[str, Any]] = []
        self.remote_skus: list[dict[str, Any]] = []
        self.categories: list[dict[str, Any]] = []
        self.storage: dict[int, int] = {}
        self.card_pwd_add_response: Optional[dict[str, Any]] = None
        self.fail_methods: dict[str, Exception] = {}

    @property
    def is_ready(self) -> bool:
        return True

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append({"method": method, **kwargs})
        exc = self.fail_methods.get(method)
        if exc is not None:
            raise exc

    def trade_info(self, tid: str) -> dict[str, Any]:
        self._record("trade_info", tid=str(tid))
        trade = self.trades.get(str(tid))
        if not trade:
            raise AgisoError(f"Agiso error 9: 交易不存在 ({tid})", error_code=9, status_code=200)
        return trade

    def trades_buyer_pay(self, modify_time_start: str, page_no: int = 1, page_size: int = 20) -> list[dict[str, Any]]:
        self._record("trades_buyer_pay", modify_time_start=modify_time_start, page_no=page_no, page_size=page_size)
        start = (page_no - 1) * page_size
        return self.pending_trades[start : start + page_size]

    def create_order_result(self, tid: str, content: list[dict[str, Any]], alds_type: int = 1) -> dict[str, Any]:
        self._record("create_order_result", tid=str(tid), content=content, alds_type=alds_type)
        return {"IsSuccess": True, "Data": "", "Error_Code": 0, "Error_Msg": ""}

    def logistics_dummy_send(self, tids: list[str]) -> dict[str, Any]:
        self._record("logistics_dummy_send", tids=[str(t) for t in tids])
        return {"IsSuccess": True, "Data": {}, "Error_Code": 0, "Error_Msg": ""}

    def send_tb_app_card(self, tid: str) -> dict[str, Any]:
        self._record("send_tb_app_card", tid=str(tid))
        return {"IsSuccess": True, "Data": "发卡接口调用成功", "Error_Code": 0, "Error_Msg": ""}

    def send_ww_msg(self, tid: str, msg: str) -> dict[str, Any]:
        self._record("send_ww_msg", tid=str(tid), msg=msg)
        return {"IsSuccess": True, "Data": None, "Error_Code": 0, "Error_Msg": ""}

    def item_skus_get(self, num_iids: list[str], fields: str = DEFAULT_SKUS_FIELDS) -> list[dict[str, Any]]:
        self._record("item_skus_get", num_iids=list(num_iids), fields=fields)
        wanted = {str(item) for item in num_iids}
        if not wanted:
            return list(self.remote_skus)
        return [sku for sku in self.remote_skus if str(sku.get("NumIid", "")) in wanted]

    def card_pwd_category_get_list(self, page_no: int = 1, page_size: int = 100) -> dict[str, Any]:
        self._record("card_pwd_category_get_list", page_no=page_no, page_size=page_size)
        return {"List": list(self.categories), "HasNext": False}

    def card_pwd_category_get_storage(self, id_nos: list[int | str]) -> list[dict[str, Any]]:
        self._record("card_pwd_category_get_storage", id_nos=[str(i) for i in id_nos])
        rows: list[dict[str, Any]] = []
        for item in id_nos:
            if str(item).isdigit():
                cpc_id = int(item)
                rows.append({"IdNo": cpc_id, "Qty": int(self.storage.get(cpc_id, 0))})
            else:
                rows.append({"IdNo": item, "Qty": 0})
        return rows

    def card_pwd_add(
        self,
        cpc_id: int | str,
        lines: list[str],
        *,
        is_use_first: bool = False,
        allow_replace: bool = False,
        summary: str = "",
    ) -> dict[str, Any]:
        self._record(
            "card_pwd_add",
            cpc_id=str(cpc_id),
            lines=list(lines),
            is_use_first=is_use_first,
            allow_replace=allow_replace,
            summary=summary,
        )
        if self.card_pwd_add_response is not None:
            response = dict(self.card_pwd_add_response)
        else:
            response = {
                "AddSuccessCount": len(lines),
                "AddFailCount": 0,
                "ReplaceUnSoldSuccessCount": 0,
                "ReplaceSoldSuccessCount": 0,
                "ReplaceFailCount": 0,
            }
        if str(cpc_id).isdigit():
            key = int(cpc_id)
            added = int(response.get("AddSuccessCount") or 0)
            if added > 0:
                self.storage[key] = int(self.storage.get(key, 0)) + added
        return response
