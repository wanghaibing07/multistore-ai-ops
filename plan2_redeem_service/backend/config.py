from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean setting value: {value!r}")


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

    @classmethod
    def from_env(cls) -> "Settings":
        values: dict[str, Any] = cls().__dict__.copy()
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
                    values[key] = loaded[key]
        converters = {
            "smtp_port": int,
            "poll_interval_seconds": int,
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
        }
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

