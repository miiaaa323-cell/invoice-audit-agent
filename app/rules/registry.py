"""Rule groups mirror graph fan-out while preserving the R01 prerequisite."""

from collections.abc import Callable

from app.rules.arithmetic import check_arithmetic
from app.rules.base import AuditContext, Finding
from app.rules.documents import check_documents
from app.rules.duplicate import check_duplicate
from app.rules.fields import low_confidence, missing_fields
from app.rules.parties import check_buyer, check_vendor
from app.rules.three_way_match import (
    check_lines,
    check_price_and_tax,
    check_quantity,
    check_total_variance,
)

Rule = Callable[[AuditContext], list[Finding]]
RULE_GROUPS: dict[str, tuple[Rule, ...]] = {
    "check_fields_arith": (missing_fields, low_confidence, check_arithmetic),
    "check_duplicate": (check_duplicate,),
    "check_parties_docs": (check_buyer, check_vendor, check_documents),
    "check_three_way_match": (
        check_lines,
        check_quantity,
        check_price_and_tax,
        check_total_variance,
    ),
}


def run_group(name: str, ctx: AuditContext) -> list[Finding]:
    missing = missing_fields(ctx)
    if missing:
        return missing + low_confidence(ctx) if name == "check_fields_arith" else []
    return [finding for rule in RULE_GROUPS[name] for finding in rule(ctx)]


def evaluate(ctx: AuditContext) -> list[Finding]:
    return [finding for name in RULE_GROUPS for finding in run_group(name, ctx)]
