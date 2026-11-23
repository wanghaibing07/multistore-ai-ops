from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import string
import time
from decimal import Decimal
from typing import Any

NONCE_ALPHABET = string.ascii_lowercase + string.digits
NONCE_PATTERN = re.compile(r"^[A-Za-z0-9]{16,64}$")


def generate_nonce(length: int = 32) -> str:
    if length < 16 or length > 64:
        raise ValueError("16688 nonce 长度必须在 16 到 64 位之间")
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))


def validate_nonce(nonce: str) -> str:
    if not isinstance(nonce, str) or not NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("16688 nonce 必须是 16~64 位纯字母或数字，不能包含连字符")
    return nonce


def split_controller_action(path: str) -> tuple[str, str, str]:
    cleaned = path.strip()
    if cleaned.startswith("/openApi/"):
        suffix = cleaned[len("/openApi/") :]
    elif cleaned.startswith("openApi/"):
        suffix = cleaned[len("openApi/") :]
    else:
        suffix = cleaned.lstrip("/")

    segments = [segment for segment in suffix.split("/") if segment]
    if len(segments) != 2:
        raise ValueError(f"非法的 16688 Open API 路径，应为 /openApi/<controller>/<action>: {path!r}")

    controller_raw, action_raw = segments
    route_path = f"/{controller_raw}/{action_raw}"
    return route_path, controller_raw.lower(), action_raw.lower()


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


def normalize_for_php(value: Any, *, is_nested: bool = False) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return _decimal_to_php_number(value)
    if isinstance(value, float):
        return _decimal_to_php_number(Decimal(str(value)))
    if isinstance(value, (list, tuple)):
        return [normalize_for_php(item, is_nested=True) for item in value]
    if isinstance(value, dict):
        if is_nested and not value:
            return []
        return {
            str(dict_key): normalize_for_php(dict_value, is_nested=True)
            for dict_key, dict_value in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    return str(value)


def canonical_value(value: Any) -> str:
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
        normalized = normalize_for_php(value, is_nested=True)
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def build_sign_plain(method: str, path: str, params: dict[str, Any]) -> str:
    _, controller_lower, action_lower = split_controller_action(path)
    filtered_items = [
        (str(param_key), param_value)
        for param_key, param_value in params.items()
        if str(param_key) != "sign"
    ]
    filtered_items.sort(key=lambda entry: entry[0])
    query_pairs = [f"{param_key}={canonical_value(param_value)}" for param_key, param_value in filtered_items]
    return f"{method.upper()}\n{controller_lower}\n{action_lower}\n{'&'.join(query_pairs)}"


def compute_signature(secret: str, plain: str) -> str:
    if not secret:
        raise ValueError("16688 API secret 不能为空")
    return hmac.new(secret.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256).hexdigest()


def build_signed_payload(
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
    final_nonce = validate_nonce(nonce if nonce is not None else generate_nonce(32))
    final_timestamp = str(int(time.time())) if timestamp is None else str(timestamp)

    payload: dict[str, Any] = {}
    if params:
        for param_key, param_value in params.items():
            if param_key == "sign" or param_value is None:
                continue
            payload[str(param_key)] = normalize_for_php(param_value, is_nested=isinstance(param_value, (dict, list)))

    payload["app_id"] = str(app_id)
    payload["timestamp"] = final_timestamp
    payload["nonce"] = final_nonce

    plain = build_sign_plain(method, path, payload)
    signature = compute_signature(secret, plain)
    signed_payload = dict(payload)
    signed_payload["sign"] = signature
    return signed_payload, signature, plain
