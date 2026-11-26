from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app import create_app
from backend.config import Settings
from backend.gateways import DryRunCanboso, UpstreamTimeout
from backend.service import Mailer


class Fake16688:
    def __init__(self):
        self.allowed = True
        self.imported = []

    def verify_order(self, code, goods_no):
        if not self.allowed:
            return {"ok": False, "error": "ORDER_NOT_ELIGIBLE", "trade_no": "TB-001", "final_rejection": True}
        return {"ok": True, "trade_no": "TB-001"}

    def stock_secret_add(self, goods_no, content):
        self.imported.append((goods_no, content))
        return {"goods_no": goods_no, "import_count": len(content.split("\n\n")), "stock_dup_count": 0}


class TimeoutAfterCharge(DryRunCanboso):
    def __init__(self):
        super().__init__()
        self.fail_once = True

    def purchase(self, key, payload):
        result = super().purchase(key, payload)
        if self.fail_once:
            self.fail_once = False
            raise UpstreamTimeout("timeout after charge")
        return result


@pytest.fixture()
def components(tmp_path: Path):
    settings = Settings(db_path=str(tmp_path / "redeem.sqlite3"), storage_dir=str(tmp_path / "private"), admin_token="test-admin", public_base_url="http://testserver", execution_mode="dry_run", merchant_email="merchant@example.com")
    canboso = DryRunCanboso(initial_balance=1000)
    channel = Fake16688()
    mailer = Mailer(settings)
    app = create_app(settings, canboso=canboso, channel=channel, mailer=mailer)
    return app, settings, canboso, channel, mailer

