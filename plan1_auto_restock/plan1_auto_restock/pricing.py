from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Sequence

from .models import CanbosoAccountItem, PricingDecision, PricingPolicy

MIN_16688_PRICE_CNY = Decimal("0.02")
DEFAULT_VND_PER_USD = Decimal("26000")


def convert_to_usd(amount: Decimal, currency: str, vnd_per_usd: Decimal = DEFAULT_VND_PER_USD) -> Decimal:
    code = currency.strip().upper()
    if code in ("", "USD", "USDT", "US$"):
        return amount.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if code == "VND":
        rate = vnd_per_usd if vnd_per_usd > 0 else DEFAULT_VND_PER_USD
        return (amount / rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    raise ValueError(f"不支持的上游币种: {currency!r}")


def convert_usd_to_cny(cost_usd: Decimal, usd_cny_rate: Decimal) -> Decimal:
    return (cost_usd * usd_cny_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return (units * step).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def evaluate_pricing(
    *,
    unit_cost_usd: Decimal,
    usd_cny_rate: Decimal,
    policy: PricingPolicy,
    baseline_cost_usd: Decimal | None,
    max_spike_ratio: Decimal,
) -> PricingDecision:
    unit_cost_cny = convert_usd_to_cny(unit_cost_usd, usd_cny_rate)
    price_by_ratio = unit_cost_cny * (Decimal("1") + policy.markup_ratio)
    price_by_fixed = unit_cost_cny + policy.fixed_markup_cny
    min_safe_price_cny = max(
        MIN_16688_PRICE_CNY,
        policy.min_sell_price_cny,
        unit_cost_cny + policy.min_margin_cny,
    ).quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    raw_suggested = max(price_by_ratio, price_by_fixed, min_safe_price_cny)
    suggested_price_cny = max(MIN_16688_PRICE_CNY, round_up_to_step(raw_suggested, policy.round_to))

    spike_ratio: Decimal | None = None
    price_spiked = False
    if baseline_cost_usd is not None and baseline_cost_usd > 0:
        spike_ratio = ((unit_cost_usd - baseline_cost_usd) / baseline_cost_usd).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        price_spiked = spike_ratio > max_spike_ratio

    return PricingDecision(
        unit_cost_usd=unit_cost_usd,
        unit_cost_cny=unit_cost_cny,
        suggested_price_cny=suggested_price_cny,
        min_safe_price_cny=min_safe_price_cny,
        price_spiked=price_spiked,
        spike_ratio=spike_ratio,
    )


def _clean_single_line(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def format_cards_for_16688(
    accounts: Sequence[CanbosoAccountItem],
    *,
    split_type: int = 1,
    delimiter: str = "----",
) -> tuple[str, list[str]]:
    if split_type not in (1, 2):
        raise ValueError(f"split_type 仅支持 1（单行一张）或 2（空行分块），当前: {split_type}")
    if not accounts:
        raise ValueError("Canboso 未返回任何可入库账号卡密")

    clean_delimiter = delimiter or "----"
    cards: list[str] = []
    for account in accounts:
        if split_type == 1:
            parts: list[str] = []
            if account.user:
                parts.append(_clean_single_line(account.user))
            if account.password:
                parts.append(_clean_single_line(account.password))
            if account.verify_email:
                parts.append(f"辅助邮箱:{_clean_single_line(account.verify_email)}")
            if account.expiry_text:
                parts.append(f"有效期:{_clean_single_line(account.expiry_text)}")
            if account.other_info:
                parts.append(f"备注:{_clean_single_line(account.other_info)}")
            card_text = clean_delimiter.join(parts).strip()
        else:
            lines: list[str] = []
            if account.user:
                lines.append(f"账号: {account.user.strip()}")
            if account.password:
                lines.append(f"密码: {account.password.strip()}")
            if account.verify_email:
                lines.append(f"辅助邮箱: {account.verify_email.strip()}")
            if account.expiry_text:
                lines.append(f"有效期: {account.expiry_text.strip()}")
            if account.other_info:
                lines.append(f"说明: {account.other_info.strip()}")
            card_text = "\n".join(line for line in lines if line.strip()).strip()

        if not card_text:
            raise ValueError("Canboso 返回的账号条目字段全为空，拒绝入库")
        cards.append(card_text)

    separator = "\n" if split_type == 1 else "\n\n"
    return separator.join(cards), cards
