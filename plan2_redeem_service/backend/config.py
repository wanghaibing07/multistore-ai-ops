from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


def _parse_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean setting value: {value!r}")


def _parse_int_or_default(default: int) -> Callable[[Any], int]:
    def _convert(value: Any) -> int:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        return int(text)

    return _convert


def _parse_float_or_default(default: float) -> Callable[[Any], float]:
    def _convert(value: Any) -> float:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        return float(text)

    return _convert


@dataclass
class Settings:
    execution_mode: str = "dry_run"
    db_path: str = "./data/redeem.sqlite3"
    storage_dir: str = "./data/private"
    public_base_url: str = "http://127.0.0.1:8000"
    admin_token: str = "change-me"
    canboso_base_url: str = "https://canboso.com"
    canboso_api_secret: str = ""
    canboso_timeout_seconds: float = 25.0
    canboso_products_cache_ttl_seconds: float = 60.0
    canboso_get_retries: int = 2
    canboso_low_balance_threshold_usd: float = 80.0
    channel16688_base_url: str = "https://www.16688.com.cn"
    channel16688_api_key: str = ""
    channel16688_api_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    merchant_email: str = ""
    poll_interval_seconds: int = 1200
    purchasing_stuck_timeout_seconds: int = 300
    shared_sla_eta_text: str = "一般 3-7 小时内发家庭组邀请，最晚 24 小时"
    support_contact_hint: str = "有问题请把订单号和兑换码发给客服。"
    agiso_app_id: str = ""
    agiso_app_secret: str = ""
    agiso_access_token: str = ""
    agiso_api_base_url: str = "https://gw-api.agiso.com"
    agiso_timeout_seconds: float = 15.0
    agiso_timestamp_tolerance_seconds: int = 600
    agiso_auto_create_result: bool = True
    agiso_auto_dummy_send: bool = True
    agiso_auto_send_tb_card: bool = True
    agiso_auto_send_ww_msg: bool = False
    agiso_auto_revoke_on_refund: bool = True
    agiso_auto_restore_on_refund_close: bool = True
    agiso_poll_enabled: bool = False
    agiso_poll_interval_seconds: int = 120
    agiso_sku_rules_json: str = ""
    agiso_restock_enabled: bool = False
    agiso_restock_interval_seconds: int = 900
    agiso_restock_rules_json: str = ""
    channel16688_restock_enabled: bool = False
    channel16688_restock_interval_seconds: int = 900
    channel16688_restock_rules_json: str = ""
    tg_warranty_enabled: bool = False
    tg_transport: str = "auto"
    tg_api_id: int = 0
    tg_api_hash: str = field(default="", repr=False)
    tg_session: str = field(default="", repr=False)
    tg_admin_target: str = ""
    tg_web_profile_dir: str = ""
    tg_web_url: str = "https://web.telegram.org/k/"
    tg_timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> "Settings":
        values: dict[str, Any] = cls().__dict__.copy()
        converters = {
            "smtp_port": int,
            "poll_interval_seconds": int,
            "purchasing_stuck_timeout_seconds": int,
            "canboso_timeout_seconds": float,
            "canboso_products_cache_ttl_seconds": float,
            "canboso_get_retries": int,
            "canboso_low_balance_threshold_usd": float,
            "agiso_timeout_seconds": float,
            "agiso_timestamp_tolerance_seconds": int,
            "agiso_auto_create_result": _parse_bool,
            "agiso_auto_dummy_send": _parse_bool,
            "agiso_auto_send_tb_card": _parse_bool,
            "agiso_auto_send_ww_msg": _parse_bool,
            "agiso_auto_revoke_on_refund": _parse_bool,
            "agiso_auto_restore_on_refund_close": _parse_bool,
            "agiso_poll_enabled": _parse_bool,
            "agiso_poll_interval_seconds": int,
            "agiso_restock_enabled": _parse_bool,
            "agiso_restock_interval_seconds": int,
            "channel16688_restock_enabled": _parse_bool,
            "channel16688_restock_interval_seconds": int,
            "tg_warranty_enabled": _parse_bool,
            "tg_transport": lambda value: (str(value).strip().lower() if value is not None else "") or "auto",
            "tg_api_id": _parse_int_or_default(0),
            "tg_timeout_seconds": _parse_float_or_default(20.0),
        }
        config_path = os.environ.get("PLAN2_CONFIG_FILE")
        if config_path:
            path = Path(config_path).expanduser()
            if not path.is_file():
                raise ValueError(f"PLAN2_CONFIG_FILE does not exist: {path}")
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("PLAN2_CONFIG_FILE must contain a YAML mapping")
            for key in values:
                if key in loaded:
                    raw_value = loaded[key]
                    if key in converters:
                        values[key] = converters[key](raw_value)
                    elif raw_value is None:
                        values[key] = ""
                    else:
                        values[key] = str(raw_value)
        for key in values:
            env_name = "PLAN2_" + key.upper()
            if env_name in os.environ:
                values[key] = converters.get(key, str)(os.environ[env_name])
        return cls(**values)

    def prepare(self) -> None:
        Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        Path(self.storage_dir).expanduser().mkdir(parents=True, exist_ok=True)
        if self.execution_mode not in {"dry_run", "live"}:
            raise ValueError("execution_mode must be dry_run or live")
        if (self.tg_transport or "auto").strip().lower() not in {"auto", "telethon", "web"}:
            raise ValueError("tg_transport must be auto, telethon, or web")

