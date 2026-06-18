"""R03 checks the invoice's own arithmetic, independently of ERP prices."""

from decimal import Decimal

from app.rules.base import AuditContext, Finding


def check_arithmetic(ctx: AuditContext) -> list[Finding]:
    invoice = ctx.invoice
    findings = []
    tolerance = ctx.policy.line_amount_tolerance

    def compare(field, expected, actual, allowed=tolerance):
        if expected is not None and actual is not None and abs(expected - actual) > allowed:
            findings.append(
                Finding(
                    rule_id="R03",
                    code="ARITHMETIC_ERROR",
                    field=field,
                    expected=expected,
                    actual=actual,
                    message=f"字段 {field} 与发票内部计算不一致。",
                )
            )

    for index, line in enumerate(invoice.lines):
        if line.quantity is not None and line.unit_price_excl_tax is not None:
            compare(
                f"lines.{index}.amount_excl_tax",
                line.quantity * line.unit_price_excl_tax,
                line.amount_excl_tax,
            )
    if invoice.lines and all(line.amount_excl_tax is not None for line in invoice.lines):
        compare(
            "total_excl_tax",
            sum((line.amount_excl_tax for line in invoice.lines), Decimal(0)),
            invoice.total_excl_tax,
        )
    if invoice.lines and all(line.tax_amount is not None for line in invoice.lines):
        compare(
            "total_tax",
            sum((line.tax_amount for line in invoice.lines), Decimal(0)),
            invoice.total_tax,
        )
    if invoice.total_excl_tax is not None and invoice.total_tax is not None:
        compare(
            "total_incl_tax",
            invoice.total_excl_tax + invoice.total_tax,
            invoice.total_incl_tax,
            Decimal(0),
        )
    return findings
