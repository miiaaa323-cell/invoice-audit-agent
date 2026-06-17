"""Decimal arithmetic and display normalization shared by the domain."""

import unicodedata
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def q2(value: Decimal | str | int) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def line_amount(unit_price: Decimal, quantity: Decimal) -> Decimal:
    return q2(unit_price * quantity)


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()
