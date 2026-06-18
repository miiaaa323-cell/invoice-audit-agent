"""R04 consumes a reservation snapshot; this module performs no database writes."""

from app.rules.base import AuditContext, Finding


def check_duplicate(ctx: AuditContext) -> list[Finding]:
    if not ctx.duplicate_invoice:
        return []
    return [
        Finding(
            rule_id="R04",
            code="DUPLICATE_INVOICE",
            field="invoice_number",
            expected="未入账且未被其他审核占用的发票",
            actual={
                "seller_tax_id": ctx.invoice.seller_tax_id,
                "invoice_number": ctx.invoice.invoice_number,
            },
            message="同一销方税号与发票号码已入账或正在处理中。",
        )
    ]
