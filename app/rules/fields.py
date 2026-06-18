"""R01 completeness and R02 confidence checks."""

from app.rules.base import AuditContext, Finding, Severity

KEY_HEADERS = (
    "invoice_number",
    "invoice_date",
    "seller_tax_id",
    "buyer_tax_id",
    "po_number",
    "gr_number",
    "total_excl_tax",
    "total_tax",
    "total_incl_tax",
)
KEY_LINE_FIELDS = (
    "line_no",
    "sku",
    "quantity",
    "unit_price_excl_tax",
    "amount_excl_tax",
    "tax_rate",
    "tax_amount",
)


def key_field_paths(ctx: AuditContext) -> list[str]:
    return [
        *KEY_HEADERS,
        *(
            f"lines.{index}.{name}"
            for index in range(len(ctx.invoice.lines))
            for name in KEY_LINE_FIELDS
        ),
    ]


def missing_fields(ctx: AuditContext) -> list[Finding]:
    invoice = ctx.invoice
    missing = [name for name in KEY_HEADERS if _missing(getattr(invoice, name))]
    if not invoice.lines:
        missing.append("lines")
    for index, line in enumerate(invoice.lines):
        missing.extend(
            f"lines.{index}.{name}" for name in KEY_LINE_FIELDS if _missing(getattr(line, name))
        )
    return [
        Finding(
            rule_id="R01",
            code="MISSING_FIELD",
            field=name,
            expected="非空字段",
            actual=None,
            message=f"关键字段 {name} 缺失。",
        )
        for name in missing
    ]


def _missing(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def low_confidence(ctx: AuditContext) -> list[Finding]:
    return [
        Finding(
            rule_id="R02",
            code="LOW_CONFIDENCE",
            severity=Severity.REVIEW,
            field=path,
            expected=ctx.policy.confidence_threshold,
            actual=ctx.confidences.get(path, 0),
            message=f"字段 {path} 需要人工核对。",
        )
        for path in key_field_paths(ctx)
        if ctx.confidences.get(path, 0) < ctx.policy.confidence_threshold
    ]
