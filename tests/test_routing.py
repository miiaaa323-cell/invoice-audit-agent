from decimal import Decimal

import pytest

from app.config import Policy
from app.routing import Route, decide, requires_level2
from app.rules.base import Finding, Severity


def finding(severity):
    return Finding(rule_id="R01", code="EXAMPLE", severity=severity, message="待核对")


@pytest.mark.parametrize(
    "findings,total,expected",
    [
        ([finding(Severity.BLOCKING)], "100", Route.AP_REVIEW),
        ([finding(Severity.REVIEW), finding(Severity.BLOCKING)], "60000", Route.AP_REVIEW),
        ([finding(Severity.REVIEW)], "100", Route.FIELD_VERIFY),
        ([finding(Severity.REVIEW)], "60000", Route.FIELD_VERIFY),
        ([], "100", Route.AUTO_APPROVE),
        ([], "50000.00", Route.AUTO_APPROVE),
        ([], "50000.01", Route.LEVEL1_APPROVAL),
    ],
)
def test_all_routing_branches(findings, total, expected):
    assert decide(findings, Decimal(total)) == expected


@pytest.mark.parametrize(
    "amount,expected", [("49999.99", False), ("50000.00", False), ("50000.01", True)]
)
def test_r12_large_amount_boundary(amount, expected):
    assert requires_level2(Decimal(amount)) is expected


def test_routing_uses_decimal_policy_threshold():
    policy = Policy(level2_threshold="0.10")
    assert decide([], Decimal("0.10"), policy) == Route.AUTO_APPROVE
    assert decide([], Decimal("0.1001"), policy) == Route.LEVEL1_APPROVAL


def test_missing_total_is_routed_by_blocking_findings():
    assert decide([finding(Severity.BLOCKING)], None) == Route.AP_REVIEW
