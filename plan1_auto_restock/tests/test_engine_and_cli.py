from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from plan1_auto_restock.alerts import AlertDispatcher, EmailAlertSettings
from plan1_auto_restock.cli import main as cli_main
from plan1_auto_restock.client_16688 import Platform16688Error
from plan1_auto_restock.client_canboso import CanbosoNetworkUncertainError
from plan1_auto_restock.config import (
    AppConfig,
    CanbosoConfig,
    Platform16688Config,
    RuntimeConfig,
    SafetyConfig,
    SupplierConfig,
)
from plan1_auto_restock.engine import RestockEngine
from plan1_auto_restock.exporter import EXPORT_COLUMNS, export_multistore_csv
from plan1_auto_restock.models import (
    CanbosoAccountItem,
    CanbosoBalance,
    CanbosoProduct,
    CanbosoPurchaseResult,
    Platform16688Goods,
    Platform16688StockUploadResult,
    PricingPolicy,
    SkuMapping,
)
from plan1_auto_restock.storage import DualDatabaseStore


@dataclass
class Fake16688Client:
    goods_map: dict[str, Platform16688Goods] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    complaints: list[dict[str, Any]] = field(default_factory=list)
    fail_next_upload: bool = False
    uploaded_calls: list[dict[str, Any]] = field(default_factory=list)
    status_changes: list[tuple[str, int]] = field(default_factory=list)
    price_updates: list[dict[str, Any]] = field(default_factory=list)

    def get_balance(self) -> dict[str, Decimal]:
        return {"platform_money": Decimal("0"), "deposit_money": Decimal("0"), "operate_money": Decimal("0")}

    def list_categories(self) -> list[dict[str, Any]]:
        return [{"category_no": "C365194", "name": "Google家庭组"}]

    def list_goods(self, **_: Any) -> list[Platform16688Goods]:
        return list(self.goods_map.values())

    def get_goods_detail(self, goods_no: str) -> Platform16688Goods:
        return self.goods_map[goods_no]

    def generate_cover(self, text: str) -> str:
        return f"https://cdn.example.com/{text}.png"

    def upsert_goods(self, **kwargs: Any) -> dict[str, Any]:
        self.price_updates.append(kwargs)
        return {"goods_no": kwargs.get("goods_no") or "G_NEW", "status": 1, "audit_status": 2}

    def change_goods_status(self, goods_no: str, status: int) -> dict[str, Any]:
        self.status_changes.append((goods_no, status))
        return {"goods_no": goods_no, "status": status, "audit_status": 2}

    def add_stock_secrets(
        self,
        *,
        goods_no: str,
        content: str,
        split_type: int = 1,
        remove_repeat: bool = True,
    ) -> Platform16688StockUploadResult:
        if self.fail_next_upload:
            self.fail_next_upload = False
            raise Platform16688Error("simulated 16688 upload failure")
        lines = [part for part in (content.split("\n") if split_type == 1 else content.split("\n\n")) if part.strip()]
        self.uploaded_calls.append({"goods_no": goods_no, "content": content, "count": len(lines)})
        current = self.goods_map[goods_no]
        new_avail = current.available_quantity + len(lines)
        self.goods_map[goods_no] = Platform16688Goods(
            goods_no=current.goods_no,
            source=current.source,
            name=current.name,
            image=current.image,
            description=current.description,
            delivery_method=current.delivery_method,
            available_quantity=new_avail,
            limit_quantity=current.limit_quantity,
            unit_price=current.unit_price,
            sales_count=current.sales_count,
        )
        return Platform16688StockUploadResult(
            goods_no=goods_no,
            import_count=len(lines),
            stock_dup_count=0,
            available_quantity=new_avail,
        )

    def list_orders(self, **_: Any) -> dict[str, Any]:
        return {"total": len(self.orders), "list": list(self.orders)}

    def list_complaints(self, **_: Any) -> dict[str, Any]:
        return {"total": len(self.complaints), "list": list(self.complaints)}


@dataclass
class FakeCanbosoClient:
    balances: dict[str, CanbosoBalance] = field(default_factory=dict)
    products: dict[tuple[str, str], CanbosoProduct] = field(default_factory=dict)
    supplier_usd_rates: dict[str, Decimal] = field(default_factory=lambda: {"primary": Decimal("26000")})
    last_rate_limits: dict[str, dict[str, str]] = field(default_factory=dict)
    purchase_calls: list[dict[str, Any]] = field(default_factory=list)
    fail_first_purchase_uncertain: bool = False
    _by_idempotency: dict[str, CanbosoPurchaseResult] = field(default_factory=dict)
    _order_seq: int = 0

    def get_balance(self, supplier: str) -> CanbosoBalance:
        return self.balances[supplier]

    def list_products(self, supplier: str) -> list[CanbosoProduct]:
        return [prod for (sup, _), prod in self.products.items() if sup == supplier]

    def get_product(self, supplier: str, product_id: str) -> CanbosoProduct | None:
        return self.products.get((supplier, product_id))

    def purchase_account(
        self,
        *,
        supplier: str,
        product_id: str,
        quantity: int,
        idempotency_key: str,
    ) -> CanbosoPurchaseResult:
        self.purchase_calls.append(
            {
                "supplier": supplier,
                "product_id": product_id,
                "quantity": quantity,
                "idempotency_key": idempotency_key,
            }
        )
        if self.fail_first_purchase_uncertain:
            self.fail_first_purchase_uncertain = False
            raise CanbosoNetworkUncertainError("simulated timeout after charge", supplier=supplier)

        if idempotency_key in self._by_idempotency:
            return self._by_idempotency[idempotency_key]

        self._order_seq += 1
        prod = self.products[(supplier, product_id)]
        accounts = tuple(
            CanbosoAccountItem(
                user=f"secret_user_{self._order_seq}_{idx}@example.com",
                password=f"SuperSecretPass_{self._order_seq}_{idx}!",
                verify_email=f"recovery_{self._order_seq}_{idx}@example.com",
            )
            for idx in range(quantity)
        )
        total = prod.price_amount * Decimal(quantity)
        bal = self.balances[supplier]
        new_bal = bal.balance_usd - total
        self.balances[supplier] = CanbosoBalance(
            supplier=supplier,
            wallet_currency=bal.wallet_currency,
            balance=new_bal,
            balance_usd=new_bal,
            balance_vnd=new_bal * bal.usd_rate,
            usd_rate=bal.usd_rate,
            balance_text=f"${new_bal:.2f}",
            updated_at=bal.updated_at,
        )
        result = CanbosoPurchaseResult(
            supplier=supplier,
            idempotency_key=idempotency_key,
            order_code=f"CB-TEST-{self._order_seq:04d}",
            status="completed",
            product_id=product_id,
            product_name=prod.name,
            product_type="account",
            quantity=quantity,
            bonus_quantity=0,
            final_quantity=quantity,
            amount=total,
            currency=prod.price_currency,
            balance_after=new_bal,
            accounts=accounts,
        )
        self._by_idempotency[idempotency_key] = result
        return result


def _make_config(
    tmp_path: Path,
    skus: tuple[SkuMapping, ...],
    *,
    dry_run: bool = True,
    daily_budget_usd: Decimal = Decimal("20.00"),
    min_wallet_balance_usd: Decimal = Decimal("2.00"),
    cooldown: int = 180,
) -> AppConfig:
    return AppConfig(
        base_dir=tmp_path,
        runtime=RuntimeConfig(
            dry_run=dry_run,
            poll_interval_seconds=60,
            ledger_db_path=tmp_path / "ledger.db",
            secrets_vault_db_path=tmp_path / "secrets_vault.db",
        ),
        safety=SafetyConfig(
            daily_budget_usd=daily_budget_usd,
            max_qty_per_purchase=2,
            min_wallet_balance_usd=min_wallet_balance_usd,
            restock_cooldown_seconds=cooldown,
            max_idempotent_replay_attempts=3,
            max_upstream_price_spike_ratio=Decimal("0.35"),
        ),
        platform_16688=Platform16688Config(),
        canboso=CanbosoConfig(
            usd_cny_rate=Decimal("7.30"),
            suppliers={"primary": SupplierConfig(name="primary", api_key_env="K1", label="主号")},
        ),
        skus=skus,
    )


def _default_fakes(
    *,
    stock_16688: int = 0,
    upstream_price: Decimal = Decimal("1.50"),
    upstream_avail: int | None = 10,
    product_type: str = "account",
    wallet_usd: Decimal = Decimal("17.30"),
) -> tuple[Fake16688Client, FakeCanbosoClient]:
    fake16 = Fake16688Client(
        goods_map={
            "G10001": Platform16688Goods(
                goods_no="G10001",
                source="local",
                name="Gemini成品号",
                image="https://img.example.com/1.png",
                description="desc",
                delivery_method=1,
                available_quantity=stock_16688,
                limit_quantity=1,
                unit_price=Decimal("0"),
                sales_count=0,
            )
        }
    )
    fakecb = FakeCanbosoClient(
        balances={
            "primary": CanbosoBalance(
                supplier="primary",
                wallet_currency="USD",
                balance=wallet_usd,
                balance_usd=wallet_usd,
                balance_vnd=wallet_usd * Decimal("26000"),
                usd_rate=Decimal("26000"),
                balance_text=f"${wallet_usd:.2f}",
                updated_at="2026-11-23T00:00:00Z",
            )
        },
        products={
            ("primary", "prod_acc_1"): CanbosoProduct(
                supplier="primary",
                product_id="prod_acc_1",
                name="Gemini Account 6M",
                product_type=product_type,
                price_amount=upstream_price,
                price_currency="USD",
                price_text=f"${upstream_price:.2f}",
                available=upstream_avail,
                sold=5,
            )
        },
    )
    return fake16, fakecb


def _default_sku(**overrides: Any) -> SkuMapping:
    defaults: dict[str, Any] = {
        "sku_id": "gemini-6m",
        "enabled": True,
        "goods_no_16688": "G10001",
        "canboso_supplier": "primary",
        "canboso_product_id": "prod_acc_1",
        "min_stock": 1,
        "target_stock": 2,
        "max_qty_per_run": 1,
        "pricing": PricingPolicy(baseline_cost_usd=Decimal("1.50"), auto_sync_price=True),
    }
    defaults.update(overrides)
    return SkuMapping(**defaults)


def test_dry_run_makes_zero_mutations(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, (_default_sku(),), dry_run=True)
    fake16, fakecb = _default_fakes(stock_16688=0)
    with DualDatabaseStore(cfg.runtime.ledger_db_path, cfg.runtime.secrets_vault_db_path) as store:
        alerts = AlertDispatcher(store=store)
        engine = RestockEngine(config=cfg, store=store, client_16688=fake16, client_canboso=fakecb, alerts=alerts)
        actions = engine.run_cycle()
        assert any(a.action == "DRY_RUN_RESTOCK" for a in actions)
        assert any(a.action == "DRY_RUN_SYNC_PRICE" for a in actions)
        assert len(fakecb.purchase_calls) == 0
        assert len(fake16.uploaded_calls) == 0
        assert len(fake16.price_updates) == 0
        assert store.summary()["intents_by_status"] == {}


def test_live_restock_dual_db_isolation_and_csv_export(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, (_default_sku(),), dry_run=False)
    fake16, fakecb = _default_fakes(stock_16688=0)
    fake16.orders = [
        {
            "trade_no": "T2611230001",
            "goods_no": "G10001",
            "goods_name": "Gemini成品号",
            "quantity": 1,
            "total_amount": 15.9,
            "status": 4,
            "create_time": 1795380000,
            "cards": [{"code": "LEAK_TEST_CARD_999"}],
        }
    ]
    fake16.complaints = [
        {"complaint_no": "SC001", "trade_no": "T2611230001", "status": 0, "reason": "登录失败"}
    ]

    now = [1795380100.0]
    with DualDatabaseStore(cfg.runtime.ledger_db_path, cfg.runtime.secrets_vault_db_path) as store:
        alerts = AlertDispatcher(store=store)
        engine = RestockEngine(
            config=cfg,
            store=store,
            client_16688=fake16,
            client_canboso=fakecb,
            alerts=alerts,
            clock=lambda: now[0],
        )
        actions = engine.run_cycle()
        assert any(a.action == "RESTOCK_UPLOADED" for a in actions)
        assert any(a.action == "PRICE_SYNCED" for a in actions)
        assert any(a.action == "ALERTED_16688_COMPLAINTS" for a in actions)
        assert len(fakecb.purchase_calls) == 1
        assert fake16.goods_map["G10001"].available_quantity == 1

        # Second cycle: complaint already alerted for status=0 -> must NOT re-alert; stock=1 >= min_stock=1 -> skip
        actions2 = engine.run_cycle()
        assert not any(a.action == "ALERTED_16688_COMPLAINTS" for a in actions2)
        assert any(a.action == "SKIP_SUFFICIENT_STOCK" for a in actions2)

        # Export CSV compatible with multistore-ai-ops (zero secrets)
        csv_path = tmp_path / "export.csv"
        exported = export_multistore_csv(store=store, suppliers=cfg.canboso.suppliers, output_path=csv_path)
        assert exported == 1

    # Verify ZERO secrets leaked into ledger.db or CSV, while secrets_vault.db has the credential
    ledger_bytes = (tmp_path / "ledger.db").read_bytes()
    assert b"SuperSecretPass_" not in ledger_bytes
    assert b"secret_user_" not in ledger_bytes
    assert b"LEAK_TEST_CARD_999" not in ledger_bytes

    csv_text = (tmp_path / "export.csv").read_text(encoding="utf-8-sig")
    assert "SuperSecretPass_" not in csv_text
    assert "secret_user_" not in csv_text
    rows = list(csv.DictReader(csv_text.splitlines()))
    assert list(rows[0].keys()) == EXPORT_COLUMNS
    assert rows[0]["采购单号"] == "CB-TEST-0001"
    assert rows[0]["实付金额"] == "10.95"  # 1.50 * 7.30

    vault_conn = sqlite3.connect(tmp_path / "secrets_vault.db")
    vault_rows = vault_conn.execute("SELECT user_field, upload_status FROM vault_items").fetchall()
    vault_conn.close()
    assert len(vault_rows) == 1
    assert vault_rows[0] == ("secret_user_1_0@example.com", "UPLOADED")


def test_upload_failure_recovers_from_vault_without_rebuying(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, (_default_sku(),), dry_run=False)
    fake16, fakecb = _default_fakes(stock_16688=0)
    fake16.fail_next_upload = True

    with DualDatabaseStore(cfg.runtime.ledger_db_path, cfg.runtime.secrets_vault_db_path) as store:
        alerts = AlertDispatcher(store=store)
        engine = RestockEngine(config=cfg, store=store, client_16688=fake16, client_canboso=fakecb, alerts=alerts)

        actions1 = engine.run_cycle()
        assert any(a.action == "RESTOCK_UPLOAD_FAILED" for a in actions1)
        assert len(fakecb.purchase_calls) == 1
        assert fake16.goods_map["G10001"].available_quantity == 0

        # Next cycle must upload from vault and NOT purchase again on Canboso
        actions2 = engine.run_cycle()
        assert any(a.action == "UPLOAD_RECOVERED" for a in actions2)
        assert len(fakecb.purchase_calls) == 1
        assert fake16.goods_map["G10001"].available_quantity == 1
        assert store.summary()["intents_by_status"] == {"UPLOADED": 1}


def test_network_timeout_replays_exact_idempotency_key(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, (_default_sku(),), dry_run=False)
    fake16, fakecb = _default_fakes(stock_16688=0)
    fakecb.fail_first_purchase_uncertain = True

    with DualDatabaseStore(cfg.runtime.ledger_db_path, cfg.runtime.secrets_vault_db_path) as store:
        alerts = AlertDispatcher(store=store)
        engine = RestockEngine(config=cfg, store=store, client_16688=fake16, client_canboso=fakecb, alerts=alerts)

        actions1 = engine.run_cycle()
        assert any(a.action == "PURCHASE_UNCERTAIN_QUEUED_REPLAY" for a in actions1)
        assert len(fakecb.purchase_calls) == 1
        first_key = fakecb.purchase_calls[0]["idempotency_key"]

        # Second cycle replays exact same key, succeeds, uploads to 16688, and does not create a second purchase
        actions2 = engine.run_cycle()
        assert any(a.action == "UPLOAD_RECOVERED" for a in actions2)
        assert len(fakecb.purchase_calls) == 2
        assert fakecb.purchase_calls[1]["idempotency_key"] == first_key
        assert fake16.goods_map["G10001"].available_quantity == 1


def test_guardrails_slot_rejection_price_spike_oos_and_budget(tmp_path: Path) -> None:
    # 1. Reject productType=slot
    cfg_slot = _make_config(tmp_path / "s1", (_default_sku(),), dry_run=False)
    fake16, fakecb = _default_fakes(stock_16688=0, product_type="slot")
    with DualDatabaseStore(cfg_slot.runtime.ledger_db_path, cfg_slot.runtime.secrets_vault_db_path) as store:
        engine = RestockEngine(
            config=cfg_slot, store=store, client_16688=fake16, client_canboso=fakecb, alerts=AlertDispatcher(store=store)
        )
        actions = engine.run_cycle()
        assert any(a.action == "SKIP_INCOMPATIBLE_TYPE" for a in actions)
        assert len(fakecb.purchase_calls) == 0

    # 2. Price spike > 35% trips circuit breaker and off-shelves 16688
    cfg_spike = _make_config(tmp_path / "s2", (_default_sku(),), dry_run=False)
    fake16, fakecb = _default_fakes(stock_16688=0, upstream_price=Decimal("2.50"))  # +66.7% over baseline 1.50
    with DualDatabaseStore(cfg_spike.runtime.ledger_db_path, cfg_spike.runtime.secrets_vault_db_path) as store:
        engine = RestockEngine(
            config=cfg_spike, store=store, client_16688=fake16, client_canboso=fakecb, alerts=AlertDispatcher(store=store)
        )
        actions = engine.run_cycle()
        assert any(a.action == "PRICE_SPIKE_CIRCUIT_BROKEN" for a in actions)
        assert ("G10001", 0) in fake16.status_changes
        assert len(fakecb.purchase_calls) == 0

    # 3. Upstream OOS + 16688 stock 0 -> auto off-shelf; later restock + auto_on_shelf -> re-list
    sku_oos = _default_sku(auto_on_shelf_when_restocked=True)
    cfg_oos = _make_config(tmp_path / "s3", (sku_oos,), dry_run=False)
    fake16, fakecb = _default_fakes(stock_16688=0, upstream_avail=0)
    with DualDatabaseStore(cfg_oos.runtime.ledger_db_path, cfg_oos.runtime.secrets_vault_db_path) as store:
        engine = RestockEngine(
            config=cfg_oos, store=store, client_16688=fake16, client_canboso=fakecb, alerts=AlertDispatcher(store=store)
        )
        actions_oos = engine.run_cycle()
        assert any(a.action == "AUTO_OFF_SHELF" for a in actions_oos)
        assert fake16.status_changes == [("G10001", 0)]

        # Upstream recovers
        fakecb.products[("primary", "prod_acc_1")] = CanbosoProduct(
            supplier="primary",
            product_id="prod_acc_1",
            name="Gemini Account 6M",
            product_type="account",
            price_amount=Decimal("1.50"),
            price_currency="USD",
            price_text="$1.50",
            available=5,
            sold=5,
        )
        actions_back = engine.run_cycle()
        assert any(a.action == "RESTOCK_UPLOADED" for a in actions_back)
        assert any(a.action == "AUTO_ON_SHELF" for a in actions_back)
        assert fake16.status_changes == [("G10001", 0), ("G10001", 1)]

    # 4. Daily budget cap blocks purchase
    cfg_budget = _make_config(tmp_path / "s4", (_default_sku(),), dry_run=False, daily_budget_usd=Decimal("1.00"))
    fake16, fakecb = _default_fakes(stock_16688=0, upstream_price=Decimal("1.50"))
    with DualDatabaseStore(cfg_budget.runtime.ledger_db_path, cfg_budget.runtime.secrets_vault_db_path) as store:
        engine = RestockEngine(
            config=cfg_budget, store=store, client_16688=fake16, client_canboso=fakecb, alerts=AlertDispatcher(store=store)
        )
        actions = engine.run_cycle()
        assert any(a.action == "SKIP_DAILY_BUDGET" for a in actions)
        assert len(fakecb.purchase_calls) == 0


def test_cli_blocks_live_without_explicit_spend_confirmation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  dry_run: true
  ledger_db_path: "ledger.db"
  secrets_vault_db_path: "vault.db"
canboso:
  suppliers:
    primary:
      api_key_env: "TEST_CB_KEY"
      label: "Test"
skus: []
""",
        encoding="utf-8",
    )
    rc = cli_main(["-c", str(config_file), "sync-once", "--live"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--i-confirm-real-spend" in err

    rc_status = cli_main(["-c", str(config_file), "status"])
    assert rc_status == 0


def test_upsert_16688_order_maps_live_total_amount_and_success_time(tmp_path: Path) -> None:
    with DualDatabaseStore(tmp_path / "ledger.db", tmp_path / "vault.db") as store:
        store.upsert_16688_order(
            {
                "trade_no": "FF260101000001",
                "goods_no": "G10000001",
                "goods_name": "测试成品号秒发",
                "quantity": 1,
                "total_amount": 0.02,
                "goods_total_amount": 0.02,
                "status": 4,
                "create_time": 1795382046,
                "success_time": 1795382288,
                "deliver_time": 1795382288,
                "contact_email": "buyer@example.com",
                "cards": [{"code": "MUST_NOT_LEAK_CARD"}],
            },
            status_label="已发货",
        )
        row = store._ledger.execute(
            "SELECT trade_no, amount_cny, created_at_ts, paid_at_ts, meta_json FROM orders_16688 WHERE trade_no=?",
            ("FF260101000001",),
        ).fetchone()
        assert row is not None
        assert row["amount_cny"] == "0.02"
        assert row["created_at_ts"] == 1795382046
        assert row["paid_at_ts"] == 1795382288
        assert "MUST_NOT_LEAK_CARD" not in row["meta_json"]
        assert "buyer@example.com" in row["meta_json"]


def test_email_alert_level_filter_throttle_and_sanitization(tmp_path: Path) -> None:
    sent_messages: list[Any] = []
    now_box = [1_700_000_000]

    def fake_smtp(msg: Any, _settings: EmailAlertSettings) -> None:
        sent_messages.append(msg)

    email_cfg = EmailAlertSettings(
        enabled=True,
        smtp_host="smtp.qq.com",
        smtp_port=465,
        use_ssl=True,
        username="ops-alert@example.com",
        password="dummy-auth-code",
        to_addrs=("ops-alert@example.com",),
        min_level="WARNING",
        cooldown_seconds=1800,
        subject_prefix="[16688补库告警]",
    )

    with DualDatabaseStore(tmp_path / "ledger.db", tmp_path / "vault.db") as store:
        dispatcher = AlertDispatcher(
            store=store,
            email_settings=email_cfg,
            smtp_sender=fake_smtp,
            clock=lambda: now_box[0],
        )
        try:
            dispatcher.emit(level="INFO", event_type="PRICE_SYNCED", sku_id="sku1", message="normal info")
            assert len(sent_messages) == 0

            dispatcher.emit(
                level="WARNING",
                event_type="LOW_WALLET_BALANCE",
                sku_id="sku1",
                message="余额不足",
                detail={"balance_usd": "0.50", "cards": [{"code": "SECRET_CARD_123"}]},
            )
            assert len(sent_messages) == 1
            body = sent_messages[0].get_content()
            assert "SECRET_CARD_123" not in body
            assert "0.50" in body

            # Same event+sku within cooldown should be throttled
            now_box[0] += 600
            dispatcher.emit(level="WARNING", event_type="LOW_WALLET_BALANCE", sku_id="sku1", message="余额不足")
            assert len(sent_messages) == 1

            # Distinct complaint numbers should never be throttled by each other
            dispatcher.emit(
                level="WARNING",
                event_type="COMPLAINT_16688",
                message="投诉1",
                detail={"complaint_no": "C001", "status": 0},
            )
            dispatcher.emit(
                level="WARNING",
                event_type="COMPLAINT_16688",
                message="投诉2",
                detail={"complaint_no": "C002", "status": 0},
            )
            assert len(sent_messages) == 3

            # After cooldown expires, same event sends again
            now_box[0] += 1800
            dispatcher.emit(level="WARNING", event_type="LOW_WALLET_BALANCE", sku_id="sku1", message="余额不足")
            assert len(sent_messages) == 4
        finally:
            dispatcher.close()


def test_email_alert_smtp_failure_degrades_without_throttling(tmp_path: Path) -> None:
    attempts = 0

    def flaky_smtp(_msg: Any, _settings: EmailAlertSettings) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary smtp network error")

    email_cfg = EmailAlertSettings(
        enabled=True,
        username="ops-alert@example.com",
        password="dummy-auth-code",
        to_addrs=("ops-alert@example.com",),
        cooldown_seconds=1800,
    )
    with DualDatabaseStore(tmp_path / "ledger.db", tmp_path / "vault.db") as store:
        dispatcher = AlertDispatcher(
            store=store,
            email_settings=email_cfg,
            smtp_sender=flaky_smtp,
            clock=lambda: 1_700_000_100,
        )
        try:
            # First attempt fails gracefully and does not lock cooldown
            dispatcher.emit(level="ERROR", event_type="CANBOSO_AUTH_ERROR", sku_id="sku1", message="401")
            # Second attempt immediately retries and succeeds
            dispatcher.emit(level="ERROR", event_type="CANBOSO_AUTH_ERROR", sku_id="sku1", message="401")
            # Third attempt is throttled because second succeeded
            dispatcher.emit(level="ERROR", event_type="CANBOSO_AUTH_ERROR", sku_id="sku1", message="401")
            assert attempts == 2
        finally:
            dispatcher.close()


def test_cli_test_alert_sends_with_loaded_email_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[Any] = []
    monkeypatch.setattr(
        AlertDispatcher,
        "_send_via_smtp",
        staticmethod(lambda msg, settings: captured.append((msg, settings))),
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALERT_EMAIL_USER=sender@qq.com\nALERT_EMAIL_PASSWORD=authcode16charsx\nALERT_EMAIL_TO=recv@qq.com\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  dry_run: true
  ledger_db_path: "ledger.db"
  secrets_vault_db_path: "vault.db"
  alert_email:
    enabled: true
    smtp_host: "smtp.qq.com"
    smtp_port: 465
    use_ssl: true
canboso:
  suppliers:
    primary:
      api_key_env: "TEST_CB_KEY"
      label: "Test"
skus: []
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("ALERT_EMAIL_USER", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_PASSWORD", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)

    rc = cli_main(["-c", str(config_file), "--env-file", str(env_file), "test-alert", "--message", "测试正文"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"email": "sent"' in out
    assert len(captured) == 1
    sent_msg, sent_settings = captured[0]
    assert sent_settings.username == "sender@qq.com"
    assert sent_settings.to_addrs == ("recv@qq.com",)
    assert "测试正文" in sent_msg.get_content()


def test_email_alert_skips_excluded_event_types_like_complaint_16688(tmp_path: Path) -> None:
    sent_messages: list[Any] = []

    email_cfg = EmailAlertSettings(
        enabled=True,
        username="ops-alert@example.com",
        password="dummy-auth-code",
        to_addrs=("ops-alert@example.com",),
        min_level="WARNING",
        exclude_event_types=("COMPLAINT_16688",),
    )
    with DualDatabaseStore(tmp_path / "ledger.db", tmp_path / "vault.db") as store:
        dispatcher = AlertDispatcher(
            store=store,
            email_settings=email_cfg,
            smtp_sender=lambda msg, _settings: sent_messages.append(msg),
        )
        try:
            dispatcher.emit(
                level="CRITICAL",
                event_type="COMPLAINT_16688",
                message="16688 买家投诉",
                detail={"complaint_no": "C999", "status": 1},
            )
            assert len(sent_messages) == 0
            row = store._ledger.execute(
                "SELECT event_type, level FROM events WHERE event_type='COMPLAINT_16688'"
            ).fetchone()
            assert row is not None
            assert row["level"] == "CRITICAL"

            dispatcher.emit(level="WARNING", event_type="LOW_WALLET_BALANCE", sku_id="sku1", message="余额低")
            assert len(sent_messages) == 1
        finally:
            dispatcher.close()
