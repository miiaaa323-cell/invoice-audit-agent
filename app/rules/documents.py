"""R07 purchase and receipt references and receipt availability."""

from app.rules.base import AuditContext, Finding


def check_documents(ctx: AuditContext) -> list[Finding]:
    findings = []
    if ctx.po is None:
        findings.append(
            Finding(
                rule_id="R07",
                code="PO_NOT_FOUND",
                field="po_number",
                expected="ERP 中存在的采购单",
                actual=ctx.invoice.po_number,
                message="未找到发票引用的采购单。",
            )
        )
    if ctx.gr is None or ctx.gr.po_number != ctx.invoice.po_number:
        findings.append(
            Finding(
                rule_id="R07",
                code="GR_NOT_FOUND",
                field="gr_number",
                expected=f"属于采购单 {ctx.invoice.po_number} 的收货单",
                actual=ctx.invoice.gr_number,
                message="未找到属于该采购单的收货单。",
            )
        )
    if ctx.gr_reserved_by_other or (
        ctx.gr is not None
        and ctx.gr.invoiced_by_run_id is not None
        and ctx.gr.invoiced_by_run_id != ctx.run_id
    ):
        findings.append(
            Finding(
                rule_id="R07",
                code="GR_ALREADY_INVOICED",
                field="gr_number",
                expected="未被其他发票使用的收货单",
                actual=ctx.invoice.gr_number,
                message="收货单已入账或被其他发票审核占用。",
            )
        )
    return findings
