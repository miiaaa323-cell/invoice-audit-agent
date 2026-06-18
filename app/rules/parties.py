"""R05 buyer identity and R06 approved vendor identity."""

from app.rules.base import AuditContext, Finding
from app.services.money import normalize_text


def check_buyer(ctx: AuditContext) -> list[Finding]:
    company = ctx.company or ctx.policy.company
    findings = []
    for field in ("buyer_name", "buyer_tax_id"):
        actual = getattr(ctx.invoice, field)
        expected = getattr(company, field)
        value = normalize_text(actual) if field == "buyer_name" and actual is not None else actual
        target = normalize_text(expected) if field == "buyer_name" else expected
        if value != target:
            findings.append(
                Finding(
                    rule_id="R05",
                    code="BUYER_MISMATCH",
                    field=field,
                    expected=expected,
                    actual=actual,
                    message=f"购方字段 {field} 与公司信息不一致。",
                )
            )
    return findings


def check_vendor(ctx: AuditContext) -> list[Finding]:
    vendor = ctx.vendor
    findings = []
    if vendor is None or vendor.status != "active" or vendor.tax_id != ctx.invoice.seller_tax_id:
        findings.append(
            Finding(
                rule_id="R06",
                code="VENDOR_NOT_APPROVED",
                field="seller_tax_id",
                expected="主数据中的 active 供应商",
                actual=ctx.invoice.seller_tax_id,
                message="销方未列入合格供应商主数据，或供应商已停用。",
            )
        )
    if vendor is not None and ctx.po is not None and vendor.vendor_id != ctx.po.vendor_id:
        findings.append(
            Finding(
                rule_id="R06",
                code="VENDOR_PO_MISMATCH",
                field="seller_tax_id",
                expected=ctx.po.vendor_id,
                actual=vendor.vendor_id,
                message="销方供应商与采购单的供应商不一致。",
            )
        )
    return findings
