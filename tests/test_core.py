from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Policy, load_policy
from app.schemas import InvoiceLine
from app.services.money import line_amount, normalize_text, q2


def test_money_rounds_half_up_and_preserves_price_precision():
    assert q2("1.005") == Decimal("1.01")
    assert q2("-1.005") == Decimal("-1.01")
    assert line_amount(Decimal("1.0001"), Decimal("1000")) == Decimal("1000.10")
    assert InvoiceLine(line_no=1, unit_price_excl_tax="1.0001").unit_price_excl_tax == Decimal("1.0001")


def test_policy_loads_decimal_thresholds():
    assert load_policy().level2_threshold == Decimal("50000.00")
    with pytest.raises(ValidationError):
        Policy(confidence_threshold="1.1")


def test_decimal_nonfinite_values_are_rejected():
    with pytest.raises(ValidationError):
        InvoiceLine(line_no=1, unit_price_excl_tax="NaN")


def test_company_name_normalization():
    assert normalize_text("  ＡＢＣ科技有限公司 ") == "ABC科技有限公司"
