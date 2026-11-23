from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .models import PricingPolicy, SkuMapping


class ConfigError(ValueError):
    """Raised when configuration or required secrets are invalid."""


VALID_ALERT_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class EmailAlertConfig:
    enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    use_ssl: bool = True
    username_env: str = "ALERT_EMAIL_USER"
    password_env: str = "ALERT_EMAIL_PASSWORD"
    to_addr_env: str = "ALERT_EMAIL_TO"
    min_level: str = "WARNING"
    cooldown_seconds: int = 1800
    subject_prefix: str = "[16688补库告警]"
    timeout_seconds: float = 15.0
    exclude_event_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeConfig:
    dry_run: bool = True
    poll_interval_seconds: int = 60
    ledger_db_path: Path = Path(".local_data/ledger.db")
    secrets_vault_db_path: Path = Path(".local_data/secrets_vault.db")
    alert_webhook_env: str = "ALERT_WEBHOOK_URL"
    alert_email: EmailAlertConfig = field(default_factory=EmailAlertConfig)


@dataclass(frozen=True)
class SafetyConfig:
    daily_budget_usd: Decimal = Decimal("20.00")
    max_qty_per_purchase: int = 2
    min_wallet_balance_usd: Decimal = Decimal("2.00")
    restock_cooldown_seconds: int = 180
    max_idempotent_replay_attempts: int = 3
    max_upstream_price_spike_ratio: Decimal = Decimal("0.35")


@dataclass(frozen=True)
class Platform16688Config:
    gateway: str = "https://www.16688.com.cn/openApi"
    app_id_env: str = "PLATFORM_16688_APP_ID"
    secret_env: str = "PLATFORM_16688_SECRET"
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class SupplierConfig:
    name: str
    api_key_env: str
    label: str


@dataclass(frozen=True)
class CanbosoConfig:
    base_url: str = "https://canboso.com"
    timeout_seconds: float = 20.0
    catalog_cache_ttl_seconds: int = 180
    usd_cny_rate: Decimal = Decimal("7.30")
    suppliers: dict[str, SupplierConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    runtime: RuntimeConfig
    safety: SafetyConfig
    platform_16688: Platform16688Config
    canboso: CanbosoConfig
    skus: tuple[SkuMapping, ...]

    def resolve_env(self, env_name: str, *, required: bool = False) -> str:
        value = os.environ.get(env_name, "").strip()
        if required and not value:
            raise ConfigError(f"缺少环境变量：{env_name}")
        return value


def load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        env_key = key.strip()
        env_value = raw_value.strip().strip('"').strip("'")
        if env_key and env_key not in os.environ:
            os.environ[env_key] = env_value


def _to_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ConfigError(f"配置项 {field_name} 不是合法数字: {value!r}") from exc


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    return candidate if candidate.is_absolute() else (base_dir / candidate).resolve()


def load_config(config_path: str | Path, *, env_file: str | Path | None = None) -> AppConfig:
    path = Path(config_path).resolve()
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    base_dir = path.parent

    if env_file is not None:
        load_dotenv_file(Path(env_file).resolve())
    else:
        load_dotenv_file(base_dir / ".env")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件根节点必须是 YAML 映射对象")

    rt_raw = raw.get("runtime") or {}
    poll_interval = int(rt_raw.get("poll_interval_seconds", 60))
    if poll_interval < 15:
        raise ConfigError("runtime.poll_interval_seconds 不能小于 15 秒（防止触发上游限流）")

    email_raw = rt_raw.get("alert_email") or {}
    if not isinstance(email_raw, dict):
        raise ConfigError("runtime.alert_email 必须是对象")
    email_min_level = str(email_raw.get("min_level", "WARNING")).strip().upper()
    if email_min_level not in VALID_ALERT_LEVELS:
        raise ConfigError(f"runtime.alert_email.min_level 非法：{email_min_level!r}，可选 {VALID_ALERT_LEVELS}")
    email_port = int(email_raw.get("smtp_port", 465))
    if email_port < 1 or email_port > 65535:
        raise ConfigError(f"runtime.alert_email.smtp_port 非法：{email_port}")
    email_cooldown = max(0, int(email_raw.get("cooldown_seconds", 1800)))
    email_timeout = float(email_raw.get("timeout_seconds", 15.0))
    if email_timeout <= 0:
        raise ConfigError("runtime.alert_email.timeout_seconds 必须大于 0")
    raw_excludes = email_raw.get("exclude_event_types") or []
    if isinstance(raw_excludes, str) or not isinstance(raw_excludes, list):
        raise ConfigError("runtime.alert_email.exclude_event_types 必须是字符串列表")
    exclude_event_types = tuple(
        dict.fromkeys(str(item).strip().upper() for item in raw_excludes if str(item).strip())
    )
    alert_email = EmailAlertConfig(
        enabled=bool(email_raw.get("enabled", False)),
        smtp_host=str(email_raw.get("smtp_host", "smtp.qq.com")).strip(),
        smtp_port=email_port,
        use_ssl=bool(email_raw.get("use_ssl", True)),
        username_env=str(email_raw.get("username_env", "ALERT_EMAIL_USER")).strip(),
        password_env=str(email_raw.get("password_env", "ALERT_EMAIL_PASSWORD")).strip(),
        to_addr_env=str(email_raw.get("to_addr_env", "ALERT_EMAIL_TO")).strip(),
        min_level=email_min_level,
        cooldown_seconds=email_cooldown,
        subject_prefix=str(email_raw.get("subject_prefix", "[16688补库告警]")).strip() or "[16688补库告警]",
        timeout_seconds=email_timeout,
        exclude_event_types=exclude_event_types,
    )

    runtime = RuntimeConfig(
        dry_run=bool(rt_raw.get("dry_run", True)),
        poll_interval_seconds=poll_interval,
        ledger_db_path=_resolve_path(base_dir, str(rt_raw.get("ledger_db_path", ".local_data/ledger.db"))),
        secrets_vault_db_path=_resolve_path(
            base_dir, str(rt_raw.get("secrets_vault_db_path", ".local_data/secrets_vault.db"))
        ),
        alert_webhook_env=str(rt_raw.get("alert_webhook_env", "ALERT_WEBHOOK_URL")),
        alert_email=alert_email,
    )
    if runtime.ledger_db_path == runtime.secrets_vault_db_path:
        raise ConfigError("ledger_db_path 与 secrets_vault_db_path 必须是不同文件，禁止把卡密混入主账本")

    safety_raw = raw.get("safety") or {}
    max_qty = int(safety_raw.get("max_qty_per_purchase", 2))
    if max_qty < 1:
        raise ConfigError("safety.max_qty_per_purchase 必须 >= 1")
    safety = SafetyConfig(
        daily_budget_usd=_to_decimal(safety_raw.get("daily_budget_usd", "20.0"), "safety.daily_budget_usd"),
        max_qty_per_purchase=max_qty,
        min_wallet_balance_usd=_to_decimal(
            safety_raw.get("min_wallet_balance_usd", "2.0"), "safety.min_wallet_balance_usd"
        ),
        restock_cooldown_seconds=max(0, int(safety_raw.get("restock_cooldown_seconds", 180))),
        max_idempotent_replay_attempts=max(1, int(safety_raw.get("max_idempotent_replay_attempts", 3))),
        max_upstream_price_spike_ratio=_to_decimal(
            safety_raw.get("max_upstream_price_spike_ratio", "0.35"), "safety.max_upstream_price_spike_ratio"
        ),
    )

    p16_raw = raw.get("platform_16688") or {}
    platform_16688 = Platform16688Config(
        gateway=str(p16_raw.get("gateway", "https://www.16688.com.cn/openApi")).rstrip("/"),
        app_id_env=str(p16_raw.get("app_id_env", "PLATFORM_16688_APP_ID")),
        secret_env=str(p16_raw.get("secret_env", "PLATFORM_16688_SECRET")),
        timeout_seconds=float(p16_raw.get("timeout_seconds", 15.0)),
    )

    cb_raw = raw.get("canboso") or {}
    suppliers_raw = cb_raw.get("suppliers") or {}
    suppliers: dict[str, SupplierConfig] = {}
    for sup_name, sup_info in suppliers_raw.items():
        if not isinstance(sup_info, dict):
            raise ConfigError(f"canboso.suppliers.{sup_name} 必须是对象")
        suppliers[str(sup_name)] = SupplierConfig(
            name=str(sup_name),
            api_key_env=str(sup_info.get("api_key_env", "")).strip(),
            label=str(sup_info.get("label", sup_name)).strip(),
        )
    usd_cny_rate = _to_decimal(cb_raw.get("usd_cny_rate", "7.30"), "canboso.usd_cny_rate")
    if usd_cny_rate <= 0:
        raise ConfigError("canboso.usd_cny_rate 必须大于 0")
    canboso = CanbosoConfig(
        base_url=str(cb_raw.get("base_url", "https://canboso.com")).rstrip("/"),
        timeout_seconds=float(cb_raw.get("timeout_seconds", 20.0)),
        catalog_cache_ttl_seconds=max(30, int(cb_raw.get("catalog_cache_ttl_seconds", 180))),
        usd_cny_rate=usd_cny_rate,
        suppliers=suppliers,
    )

    seen_sku_ids: set[str] = set()
    skus: list[SkuMapping] = []
    for idx, sku_raw in enumerate(raw.get("skus") or []):
        if not isinstance(sku_raw, dict):
            raise ConfigError(f"skus[{idx}] 必须是对象")
        sku_id = str(sku_raw.get("sku_id", "")).strip()
        if not sku_id:
            raise ConfigError(f"skus[{idx}].sku_id 不能为空")
        if sku_id in seen_sku_ids:
            raise ConfigError(f"重复的 sku_id: {sku_id}")
        seen_sku_ids.add(sku_id)

        supplier_name = str(sku_raw.get("canboso_supplier", "")).strip()
        if supplier_name not in suppliers:
            raise ConfigError(f"SKU {sku_id} 引用了未定义的 canboso_supplier: {supplier_name!r}")

        min_stock = int(sku_raw.get("min_stock", 1))
        target_stock = int(sku_raw.get("target_stock", 2))
        max_qty_per_run = int(sku_raw.get("max_qty_per_run", 1))
        split_type = int(sku_raw.get("split_type", 1))
        if min_stock < 1 or target_stock < min_stock:
            raise ConfigError(f"SKU {sku_id} 水位配置错误：需要 1 <= min_stock <= target_stock")
        if max_qty_per_run < 1:
            raise ConfigError(f"SKU {sku_id} max_qty_per_run 必须 >= 1")
        if split_type not in (1, 2):
            raise ConfigError(f"SKU {sku_id} split_type 仅支持 1（单行一张）或 2（空行分块）")

        pr_raw = sku_raw.get("pricing") or {}
        baseline_raw = pr_raw.get("baseline_cost_usd")
        pricing = PricingPolicy(
            markup_ratio=_to_decimal(pr_raw.get("markup_ratio", "0.30"), f"{sku_id}.pricing.markup_ratio"),
            fixed_markup_cny=_to_decimal(pr_raw.get("fixed_markup_cny", "0.00"), f"{sku_id}.pricing.fixed_markup_cny"),
            min_margin_cny=_to_decimal(pr_raw.get("min_margin_cny", "1.00"), f"{sku_id}.pricing.min_margin_cny"),
            min_sell_price_cny=max(
                Decimal("0.02"),
                _to_decimal(pr_raw.get("min_sell_price_cny", "0.02"), f"{sku_id}.pricing.min_sell_price_cny"),
            ),
            round_to=_to_decimal(pr_raw.get("round_to", "0.10"), f"{sku_id}.pricing.round_to"),
            auto_sync_price=bool(pr_raw.get("auto_sync_price", False)),
            price_sync_threshold_cny=_to_decimal(
                pr_raw.get("price_sync_threshold_cny", "1.00"), f"{sku_id}.pricing.price_sync_threshold_cny"
            ),
            baseline_cost_usd=_to_decimal(baseline_raw, f"{sku_id}.pricing.baseline_cost_usd")
            if baseline_raw is not None
            else None,
        )
        skus.append(
            SkuMapping(
                sku_id=sku_id,
                enabled=bool(sku_raw.get("enabled", False)),
                goods_no_16688=str(sku_raw.get("goods_no_16688", "")).strip(),
                canboso_supplier=supplier_name,
                canboso_product_id=str(sku_raw.get("canboso_product_id", "")).strip(),
                min_stock=min_stock,
                target_stock=target_stock,
                max_qty_per_run=min(max_qty_per_run, safety.max_qty_per_purchase),
                split_type=split_type,
                card_delimiter=str(sku_raw.get("card_delimiter", "----")),
                remove_repeat=bool(sku_raw.get("remove_repeat", True)),
                goods_category_no=str(sku_raw.get("goods_category_no", "")).strip(),
                pricing=pricing,
                auto_off_shelf_when_oos=bool(sku_raw.get("auto_off_shelf_when_oos", True)),
                auto_on_shelf_when_restocked=bool(sku_raw.get("auto_on_shelf_when_restocked", False)),
            )
        )

    return AppConfig(
        base_dir=base_dir,
        runtime=runtime,
        safety=safety,
        platform_16688=platform_16688,
        canboso=canboso,
        skus=tuple(skus),
    )
