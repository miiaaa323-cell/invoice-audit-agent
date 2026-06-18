"""R08–R11 compare invoice values with structured PO and GR records."""

from collections import Counter
from decimal import Decimal

from app.rules.base import AuditContext, Finding
from app.schemas import VarianceEntry
from app.services.money import line_amount, q2


def check_lines(ctx: AuditContext) -> list[Finding]:
    if ctx.po is None:
        return []
    po_counts = Counter(line.sku for line in ctx.po.lines)
    invoice_counts = Counter(line.sku for line in ctx.invoice.lines)
    return [
        Finding(
            rule_id="R08",
            code="LINE_NOT_ON_PO",
            field=f"lines.{index}.sku",
            expected="采购单中唯一对应的 SKU，且发票中只出现一次",
            actual=line.sku,
            message=f"发票第 {line.line_no} 行 SKU 不在采购单中，或存在重复匹配。",
        )
        for index, line in enumerate(ctx.invoice.lines)
        if po_counts[line.sku] != 1 or invoice_counts[line.sku] != 1
    ]


def _matched_lines(ctx: AuditContext):
    if ctx.po is None:
        return
    counts = Counter(line.sku for line in ctx.po.lines)
    by_sku = {line.sku: line for line in ctx.po.lines}
    for index, line in enumerate(ctx.invoice.lines):
        if counts[line.sku] == 1:
            yield index, line, by_sku[line.sku]


def check_quantity(ctx: AuditContext) -> list[Finding]:
    if ctx.po is None or ctx.gr is None or ctx.gr.po_number != ctx.po.po_number:
        return []
    receipts = {line.po_line_no: line.quantity_received for line in ctx.gr.lines}
    findings = []
    invoiced_po_lines = set()
    for index, line, po_line in _matched_lines(ctx):
        invoiced_po_lines.add(po_line.po_line_no)
        expected = receipts.get(po_line.po_line_no)
        if expected is None or line.quantity != expected:
            findings.append(
                Finding(
                    rule_id="R09",
                    code="QTY_MISMATCH",
                    field=f"lines.{index}.quantity",
                    expected=expected,
                    actual=line.quantity,
                    message=f"发票第 {line.line_no} 行数量与收货数量不一致。",
                )
            )
    for po_line_no, received in receipts.items():
        if received != 0 and po_line_no not in invoiced_po_lines:
            findings.append(
                Finding(
                    rule_id="R09",
                    code="QTY_MISMATCH",
                    field="lines",
                    expected={"po_line_no": po_line_no, "quantity_received": received},
                    actual=None,
                    message=f"收货单第 {po_line_no} 行未在发票中列出。",
                )
            )
    return findings


def check_price_and_tax(ctx: AuditContext) -> list[Finding]:
    findings = []
    for index, line, po_line in _matched_lines(ctx):
        for field, code, label in (
            ("unit_price_excl_tax", "PRICE_MISMATCH", "不含税单价"),
            ("tax_rate", "TAX_RATE_MISMATCH", "税率"),
        ):
            actual = getattr(line, field)
            expected = getattr(po_line, field)
            if actual != expected:
                findings.append(
                    Finding(
                        rule_id="R10",
                        code=code,
                        field=f"lines.{index}.{field}",
                        expected=expected,
                        actual=actual,
                        message=f"发票第 {line.line_no} 行{label}与采购单不一致。",
                    )
                )
    return findings


def expected_total(ctx: AuditContext) -> Decimal | None:
    if ctx.po is None or ctx.gr is None or ctx.gr.po_number != ctx.po.po_number:
        return None
    po_lines = {line.po_line_no: line for line in ctx.po.lines}
    if any(line.po_line_no not in po_lines for line in ctx.gr.lines):
        return None
    return sum(
        (
            line_amount(po_lines[line.po_line_no].unit_price_excl_tax, line.quantity_received)
            for line in ctx.gr.lines
        ),
        Decimal("0.00"),
    )


def check_total_variance(ctx: AuditContext) -> list[Finding]:
    expected = expected_total(ctx)
    actual = ctx.invoice.total_excl_tax
    if (
        expected is None
        or actual is None
        or abs(actual - expected) <= ctx.policy.rounding_tolerance
    ):
        return []
    return [
        Finding(
            rule_id="R11",
            code="AMOUNT_OVER_TOLERANCE",
            field="total_excl_tax",
            expected=expected,
            actual=actual,
            message="发票不含税合计与采购收货期望金额的差额超过尾差限额。",
        )
    ]


def variance_entry(ctx: AuditContext) -> VarianceEntry | None:
    expected = expected_total(ctx)
    actual = ctx.invoice.total_excl_tax
    if expected is None or actual is None or ctx.invoice.invoice_number is None:
        return None
    difference = q2(actual - expected)
    if difference == 0 or abs(actual - expected) > ctx.policy.rounding_tolerance:
        return None
    return VarianceEntry(
        invoice_number=ctx.invoice.invoice_number,
        amount=difference,
        account=ctx.policy.variance_account,
    )
