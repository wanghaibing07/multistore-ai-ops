import json
from contextlib import closing

import pytest

from backend.app import create_app
from backend.config import Settings
from backend.gateways import DryRun16688, DryRunCanboso, UpstreamError


GOODS = "G10000001"
SKU = "ultra_shared_slot"


def make_service(tmp_path, rules=()):
    settings = Settings(
        db_path=str(tmp_path / "redeem.sqlite3"),
        storage_dir=str(tmp_path / "private"),
        admin_token="test-admin",
        execution_mode="dry_run",
        channel16688_restock_enabled=True,
        channel16688_restock_rules_json=json.dumps(rules),
    )
    return create_app(settings, canboso=DryRunCanboso(), channel=DryRun16688()).state.service


def test_16688_live_batch_exists_in_ledger_before_inventory_upload_and_is_not_exported(tmp_path):
    svc = make_service(tmp_path)
    svc.settings.execution_mode = "live"

    class Reading16688(DryRun16688):
        def stock_secret_add(self, goods_no, content):
            codes = [line.split("：", 1)[1] for line in content.splitlines() if line.startswith("兑换码：")]
            assert len(codes) == 2
            assert all(svc.ledger.get_code(code)["status"] == "unused" for code in codes)
            return super().stock_secret_add(goods_no, content)

    svc.channel = Reading16688()
    result = svc.batch(SKU, "16688", 2, GOODS, "")
    assert result["count"] == 2
    assert result["codes"] == []
    assert not result["export_lines_code_only"]
    assert not result["export_lines_with_url"]
    with closing(svc.ledger.connect()) as conn:
        assert [r[0] for r in conn.execute("SELECT reason FROM audit_events")] == ["issued", "issued"]


def test_16688_partial_upload_is_uncertain_and_leaves_original_batch_for_reconciliation(tmp_path):
    svc = make_service(tmp_path)

    class Partial16688(DryRun16688):
        def stock_secret_add(self, goods_no, content):
            return super().stock_secret_add(goods_no, content.split("\n\n")[0])

    svc.channel = Partial16688()
    with pytest.raises(UpstreamError) as exc:
        svc.batch(SKU, "16688", 2, GOODS, "")
    assert not exc.value.rejected
    assert len(svc.channel.stock[GOODS]) == 1
    with closing(svc.ledger.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM codes WHERE status='unused'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_16688_restock_reports_consumed_and_duplicate_cards_without_changing_platform_stock(tmp_path):
    rules = [{"sku_key": SKU, "goods_no": GOODS, "min_stock": 1, "target_stock": 2, "max_push": 10, "enabled": True}]
    svc = make_service(tmp_path, rules)
    svc.channel = channel = DryRun16688()
    good_codes = svc.batch(SKU, "16688", 50, GOODS, "")["codes"]
    used, sold = svc.batch(SKU, "16688", 2, GOODS, "")["codes"]
    used_item, sold_item = channel.stock[GOODS][-2:]
    assert used in used_item["secret_code"]
    with svc.ledger.transaction(immediate=True) as conn:
        conn.execute("UPDATE codes SET status='completed' WHERE code IN (?,?)", (used, sold))
    sold_item.update(status=2, trade_no="SOLD-1")
    channel.stock_secret_add(GOODS, f"PREFIX-{used}")
    partial_match = channel.stock[GOODS][-1]
    channel.stock_secret_add(GOODS, f"兑换码：{good_codes[0]}")
    duplicate = channel.stock[GOODS][-1]
    before = svc.check_and_restock_channel16688_cards(dry_check=True, sync_sold=False)
    assert before["stock_conflicts"] == 0 and used_item["status"] == 0

    result = svc.check_and_restock_channel16688_cards(sync_sold=False)
    assert result["stock_conflicts"] == 2
    assert result["items"][0]["action"] == "stock_conflict"
    assert result["total_pushed"] == 0
    assert used_item["status"] == 0 and sold_item["status"] == 2 and partial_match["status"] == 0
    assert channel.stock[GOODS][0]["status"] == 0 and duplicate["status"] == 0
    assert all(svc.ledger.get_code(code)["status"] == "unused" for code in good_codes)


def test_legacy_resold_16688_card_hides_original_exclusive_credentials_after_late_sale_import(tmp_path):
    svc = make_service(tmp_path)
    row = svc.issue("ultra_exclusive_admin", "16688", goods_no=GOODS)
    code = row["code"]
    svc.ledger.transition(code, "purchasing", "admin_manual_canboso_purchase_16688_api_paused", allowed=("unused",))
    svc.ledger.transition(
        code, "completed", "exclusive_delivered", allowed=("purchasing",),
        updates={"delivery_json": json.dumps({"account_text": "prior@example.net", "password": "mock-password"})},
    )
    svc.ledger.transition(
        code, "completed", "channel16688_sold_sync", allowed=("completed",),
        updates={"external_order_id": "COURSE-ORDER-1"},
    )

    public = svc.public_row(svc.ledger.get_code(code))
    assert public["status"] == "needs_manual_review"
    assert public["buyer_email_masked"] is None
    assert public["delivery"] is None and public["warranty"] is None
    assert svc.warranty(code, "buyer@example.net", None, "")[1] == "WARRANTY_NOT_ALLOWED"
    assert svc.ledger.get_code(code)["status"] == "completed"
