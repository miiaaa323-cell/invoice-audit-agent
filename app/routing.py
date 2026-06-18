"""Pure approval routing: only validated policy and rule findings decide."""

from collections.abc import Iterable
from decimal import Decimal
from enum import Enum

from app.config import Policy
from app.rules.base import Finding, Severity


class Route(str, Enum):
    AP_REVIEW = "AP_REVIEW"
    FIELD_VERIFY = "FIELD_VERIFY"
    AUTO_APPROVE = "AUTO_APPROVE"
    LEVEL1_APPROVAL = "LEVEL1_APPROVAL"


def requires_level2(total_incl_tax: Decimal | None, policy: Policy | None = None) -> bool:
    return total_incl_tax is not None and total_incl_tax > (policy or Policy()).level2_threshold


def decide(
    findings: Iterable[Finding],
    total_incl_tax: Decimal | None,
    policy: Policy | None = None,
) -> Route:
    findings = list(findings)
    if any(finding.severity == Severity.BLOCKING for finding in findings):
        return Route.AP_REVIEW
    if any(finding.severity == Severity.REVIEW for finding in findings):
        return Route.FIELD_VERIFY
    if requires_level2(total_incl_tax, policy):
        return Route.LEVEL1_APPROVAL
    return Route.AUTO_APPROVE
