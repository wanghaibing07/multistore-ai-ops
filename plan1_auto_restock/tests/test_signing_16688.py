from __future__ import annotations

from decimal import Decimal

import pytest

from plan1_auto_restock.signing_16688 import (
    build_sign_plain,
    build_signed_payload,
    canonical_value,
    compute_signature,
    generate_nonce,
    split_controller_action,
    validate_nonce,
)


def test_split_controller_action_preserves_route_case_and_lowercases_sign() -> None:
    route, controller_lower, action_lower = split_controller_action("/openApi/goodsCategory/list")
    assert route == "/goodsCategory/list"
    assert controller_lower == "goodscategory"
    assert action_lower == "list"

    route2, c2, a2 = split_controller_action("/openApi/stockSecretBatch/add")
    assert route2 == "/stockSecretBatch/add"
    assert c2 == "stocksecretbatch"
    assert a2 == "add"


def test_nonce_validation_rejects_uuid_hyphens_and_bounds() -> None:
    generated = generate_nonce(32)
    assert len(generated) == 32
    assert validate_nonce(generated) == generated

    with pytest.raises(ValueError):
        validate_nonce("12345678-1234-1234-1234-1234567890ab")
    with pytest.raises(ValueError):
        validate_nonce("short12345")
    with pytest.raises(ValueError):
        validate_nonce("a" * 65)


def test_php_canonical_value_rules() -> None:
    assert canonical_value(Decimal("10.00")) == "10"
    assert canonical_value(Decimal("10.50")) == "10.5"
    assert canonical_value(10.0) == "10"
    assert canonical_value(True) == "true"
    assert canonical_value(False) == "false"
    assert canonical_value(None) == ""
    assert canonical_value({"b": 1.0, "a": {}}) == '{"a":[],"b":1}'
    assert canonical_value("中文测试") == "中文测试"


def test_build_signed_payload_matches_spec_and_skips_none() -> None:
    payload, signature, plain = build_signed_payload(
        app_id="2088000000000001",
        secret="0123456789abcdef" * 4,
        path="/openApi/goods/create",
        params={
            "price": Decimal("12.00"),
            "cost_price": Decimal("8.50"),
            "name": "测试成品号",
            "ignored": None,
        },
        timestamp="1710000000",
        nonce="a1b2c3d4e5f67890",
    )
    assert "ignored" not in payload
    assert payload["price"] == 12
    assert payload["cost_price"] == 8.5
    expected_plain = (
        "POST\ngoods\ncreate\n"
        "app_id=2088000000000001&cost_price=8.5&name=测试成品号&nonce=a1b2c3d4e5f67890&price=12&timestamp=1710000000"
    )
    assert plain == expected_plain
    assert signature == compute_signature("0123456789abcdef" * 4, expected_plain)
    assert payload["sign"] == signature
    assert build_sign_plain("POST", "/openApi/goods/create", payload) == expected_plain
