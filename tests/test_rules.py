from datetime import date
from decimal import Decimal as D

import pytest

from app.config import Policy
from app.rules.arithmetic import check_arithmetic
from app.rules.base import AuditContext
from app.rules.documents import check_documents
from app.rules.duplicate import check_duplicate
from app.rules.fields import KEY_HEADERS, KEY_LINE_FIELDS, low_confidence, missing_fields
from app.rules.parties import check_buyer, check_vendor
from app.rules.registry import evaluate, run_group
from app.rules.three_way_match import (
    check_lines,
    check_price_and_tax,
    check_quantity,
    check_total_variance,
    expected_total,
    variance_entry,
)
from app.schemas import GoodsReceipt, GRLine, Invoice, InvoiceLine, POLine, PurchaseOrder, Vendor


@pytest.fixture
def ctx():
    policy = Policy()
    invoice = Invoice(
        invoice_number="INV-001",
        invoice_date=date(2026, 6, 17),
        seller_name="示例供应商",
        seller_tax_id="91310000MA1FL0001X",
        buyer_name=policy.company.buyer_name,
        buyer_tax_id=policy.company.buyer_tax_id,
        po_number="PO-001",
        gr_number="GR-001",
        total_excl_tax=D("200.00"),
        total_tax=D("26.00"),
        total_incl_tax=D("226.00"),
        lines=[
            InvoiceLine(
                line_no=1,
                sku="SKU-1",
                description="办公用品",
                quantity=D("2"),
                unit_price_excl_tax=D("100.0000"),
                amount_excl_tax=D("200.00"),
                tax_rate=D("0.13"),
                tax_amount=D("26.00"),
            )
        ],
    )
    confidences = {key: 1.0 for key in KEY_HEADERS}
    confidences.update({f"lines.0.{key}": 1.0 for key in KEY_LINE_FIELDS})
    return AuditContext(
        invoice=invoice,
        confidences=confidences,
        policy=policy,
        company=policy.company,
        run_id="run-current",
        vendor=Vendor(
            vendor_id="vendor-1", vendor_name=invoice.seller_name, tax_id=invoice.seller_tax_id
        ),
        po=PurchaseOrder(
            po_number=invoice.po_number,
            vendor_id="vendor-1",
            created_at=date(2026, 6, 1),
            lines=[
                POLine(
                    po_line_no=1,
                    sku="SKU-1",
                    description="办公用品",
                    quantity=D("2"),
                    unit_price_excl_tax=D("100.0000"),
                    tax_rate=D("0.13"),
                )
            ],
        ),
        gr=GoodsReceipt(
            gr_number=invoice.gr_number,
            po_number=invoice.po_number,
            received_date=date(2026, 6, 16),
            lines=[GRLine(po_line_no=1, quantity_received=D("2"))],
        ),
    )


def codes(findings):
    return {finding.code for finding in findings}


def test_all_rules_pass(ctx):
    assert evaluate(ctx) == []


@pytest.mark.parametrize("field", KEY_HEADERS)
def test_r01_missing_headers(ctx, field):
    setattr(ctx.invoice, field, None)
    assert codes(missing_fields(ctx)) == {"MISSING_FIELD"}


@pytest.mark.parametrize("value,failed", [(None, True), ("", True), ("  ", True), ("0", False)])
def test_r01_blank_boundary(ctx, value, failed):
    ctx.invoice.invoice_number = value
    assert bool(missing_fields(ctx)) is failed


def test_r01_zero_amount_is_present(ctx):
    ctx.invoice.total_tax = D(0)
    assert missing_fields(ctx) == []


def test_r01_requires_a_line_and_complete_numeric_fields(ctx):
    ctx.invoice.lines[0].quantity = None
    assert missing_fields(ctx)[0].field == "lines.0.quantity"
    ctx.invoice.lines = []
    assert missing_fields(ctx)[0].field == "lines"


@pytest.mark.parametrize(
    "confidence,failed", [(1, False), (0.85, False), (0.8499, True), (0, True)]
)
def test_r02_threshold(ctx, confidence, failed):
    ctx.confidences["invoice_number"] = confidence
    assert bool(low_confidence(ctx)) is failed


def test_r02_missing_confidence_requires_review(ctx):
    del ctx.confidences["lines.0.sku"]
    (finding,) = low_confidence(ctx)
    assert finding.field == "lines.0.sku"
    assert finding.severity == "review"


@pytest.mark.parametrize(
    "difference,failed",
    [
        ("0.00", False),
        ("0.01", False),
        ("-0.01", False),
        ("0.0101", True),
        ("-0.02", True),
    ],
)
def test_r03_line_amount_tolerance(ctx, difference, failed):
    ctx.invoice.lines[0].amount_excl_tax += D(difference)
    ctx.invoice.total_excl_tax += D(difference)
    ctx.invoice.total_incl_tax += D(difference)
    assert bool(check_arithmetic(ctx)) is failed


@pytest.mark.parametrize("field", ["total_excl_tax", "total_tax", "total_incl_tax"])
def test_r03_total_inconsistency(ctx, field):
    setattr(ctx.invoice, field, getattr(ctx.invoice, field) + D("0.02"))
    assert codes(check_arithmetic(ctx)) == {"ARITHMETIC_ERROR"}


def test_r03_uses_precise_unit_price_before_comparison(ctx):
    ctx.invoice.lines[0].unit_price_excl_tax = D("100.0001")
    assert check_arithmetic(ctx) == []
    ctx.invoice.lines[0].quantity = D("200")
    assert check_arithmetic(ctx)


@pytest.mark.parametrize("duplicate", [False, True])
def test_r04_duplicate_snapshot(ctx, duplicate):
    ctx.duplicate_invoice = duplicate
    assert bool(check_duplicate(ctx)) is duplicate


def test_r04_own_reservation_snapshot_passes(ctx):
    ctx.duplicate_invoice = False
    assert check_duplicate(ctx) == []


@pytest.mark.parametrize(
    "field,value,failed",
    [
        ("buyer_name", "示例科技有限公司", False),
        ("buyer_name", "　示例科技有限公司　", False),
        ("buyer_name", "另一家公司", True),
        ("buyer_tax_id", "９１３１００００ＭＡ１ＦＬ００００Ｘ", True),
        ("buyer_tax_id", "91310000MA1FL0001X", True),
        ("buyer_name", None, True),
    ],
)
def test_r05_identity_and_normalization(ctx, field, value, failed):
    setattr(ctx.invoice, field, value)
    assert bool(check_buyer(ctx)) is failed


def test_r05_normalizes_fullwidth_name(ctx):
    ctx.company.buyer_name = "示例科技(上海)有限公司"
    ctx.invoice.buyer_name = "　示例科技（上海）有限公司　"
    assert check_buyer(ctx) == []


@pytest.mark.parametrize(
    "status,expected", [("active", set()), ("blocked", {"VENDOR_NOT_APPROVED"})]
)
def test_r06_vendor_status(ctx, status, expected):
    ctx.vendor.status = status
    assert codes(check_vendor(ctx)) == expected


def test_r06_unknown_vendor(ctx):
    ctx.vendor = None
    assert codes(check_vendor(ctx)) == {"VENDOR_NOT_APPROVED"}


def test_r06_vendor_must_match_po(ctx):
    ctx.po.vendor_id = "other-vendor"
    assert codes(check_vendor(ctx)) == {"VENDOR_PO_MISMATCH"}


def test_r06_tax_id_must_match_loaded_vendor(ctx):
    ctx.vendor.tax_id = "other-tax-id"
    assert codes(check_vendor(ctx)) == {"VENDOR_NOT_APPROVED"}


@pytest.mark.parametrize("field,expected", [("po", "PO_NOT_FOUND"), ("gr", "GR_NOT_FOUND")])
def test_r07_missing_document(ctx, field, expected):
    setattr(ctx, field, None)
    assert codes(check_documents(ctx)) == {expected}


def test_r07_gr_must_belong_to_referenced_po(ctx):
    ctx.gr.po_number = "PO-OTHER"
    assert codes(check_documents(ctx)) == {"GR_NOT_FOUND"}


@pytest.mark.parametrize("owner,failed", [(None, False), ("run-current", False), ("other", True)])
def test_r07_receipt_ownership_boundary(ctx, owner, failed):
    ctx.gr.invoiced_by_run_id = owner
    assert bool(check_documents(ctx)) is failed


def test_r07_active_receipt_reservation(ctx):
    ctx.gr_reserved_by_other = True
    assert codes(check_documents(ctx)) == {"GR_ALREADY_INVOICED"}


@pytest.mark.parametrize("sku,failed", [("SKU-1", False), ("SKU-OTHER", True), ("", True)])
def test_r08_sku_matching(ctx, sku, failed):
    ctx.invoice.lines[0].sku = sku
    assert bool(check_lines(ctx)) is failed


def test_r08_duplicate_sku_cannot_reuse_one_receipt(ctx):
    ctx.invoice.lines.append(ctx.invoice.lines[0].model_copy(update={"line_no": 2}))
    assert codes(check_lines(ctx)) == {"LINE_NOT_ON_PO"}


def test_r08_description_is_not_a_matching_key(ctx):
    ctx.invoice.lines[0].description = "完全不同的展示名称"
    assert check_lines(ctx) == []


@pytest.mark.parametrize(
    "difference,failed", [("0", False), ("1", True), ("-1", True), (".0001", True)]
)
def test_r09_exact_quantity(ctx, difference, failed):
    ctx.invoice.lines[0].quantity += D(difference)
    assert bool(check_quantity(ctx)) is failed


def test_r09_missing_gr_line(ctx):
    ctx.gr.lines = []
    assert codes(check_quantity(ctx)) == {"QTY_MISMATCH"}


def test_r09_omitted_received_line(ctx):
    ctx.po.lines.append(ctx.po.lines[0].model_copy(update={"po_line_no": 2, "sku": "SKU-2"}))
    ctx.gr.lines.append(GRLine(po_line_no=2, quantity_received=D(1)))
    assert codes(check_quantity(ctx)) == {"QTY_MISMATCH"}


@pytest.mark.parametrize(
    "field,difference,expected",
    [
        ("unit_price_excl_tax", "0", set()),
        ("unit_price_excl_tax", ".0001", {"PRICE_MISMATCH"}),
        ("unit_price_excl_tax", "-.0001", {"PRICE_MISMATCH"}),
        ("tax_rate", "0", set()),
        ("tax_rate", ".0001", {"TAX_RATE_MISMATCH"}),
        ("tax_rate", "-.0001", {"TAX_RATE_MISMATCH"}),
    ],
)
def test_r10_precise_price_and_tax_rate(ctx, field, difference, expected):
    line = ctx.invoice.lines[0]
    setattr(line, field, getattr(line, field) + D(difference))
    assert codes(check_price_and_tax(ctx)) == expected


@pytest.mark.parametrize(
    "difference,failed,variance",
    [
        ("0.00", False, None),
        ("1.00", False, "1.00"),
        ("-1.00", False, "-1.00"),
        ("1.01", True, None),
        ("-1.01", True, None),
        (".01", False, ".01"),
    ],
)
def test_r11_variance_boundary(ctx, difference, failed, variance):
    ctx.invoice.total_excl_tax += D(difference)
    assert bool(check_total_variance(ctx)) is failed
    entry = variance_entry(ctx)
    if variance is None:
        assert entry is None
    else:
        assert entry.amount == D(variance)
        assert entry.account == "ROUNDING_VARIANCE"
        assert entry.invoice_number == ctx.invoice.invoice_number


def test_r11_rounds_each_line_half_up_before_summing(ctx):
    ctx.po.lines[0].unit_price_excl_tax = D(".0025")
    ctx.po.lines.append(ctx.po.lines[0].model_copy(update={"po_line_no": 2, "sku": "SKU-2"}))
    ctx.gr.lines.append(GRLine(po_line_no=2, quantity_received=D(2)))
    assert expected_total(ctx) == D(".02")


@pytest.mark.parametrize("field", ["po", "gr"])
def test_r11_unavailable_context_is_reported_by_r07(ctx, field):
    setattr(ctx, field, None)
    assert check_total_variance(ctx) == []
    assert variance_entry(ctx) is None


def test_rules_collect_independent_failures(ctx):
    ctx.invoice.buyer_tax_id = "wrong"
    ctx.invoice.lines[0].quantity += 1
    ctx.invoice.lines[0].unit_price_excl_tax += D(".0001")
    assert {"BUYER_MISMATCH", "QTY_MISMATCH", "PRICE_MISMATCH", "ARITHMETIC_ERROR"} <= codes(
        evaluate(ctx)
    )


def test_r01_failure_skips_r03_through_r11_in_all_groups(ctx):
    ctx.invoice.invoice_number = None
    ctx.duplicate_invoice = True
    ctx.po = None
    findings = evaluate(ctx)
    assert {finding.rule_id for finding in findings} == {"R01"}
    for name in ("check_duplicate", "check_parties_docs", "check_three_way_match"):
        assert run_group(name, ctx) == []


def test_every_rule_handles_incomplete_inputs(ctx):
    ctx.invoice = Invoice()
    ctx.po = ctx.gr = ctx.vendor = None
    for rule in (
        missing_fields,
        low_confidence,
        check_arithmetic,
        check_duplicate,
        check_buyer,
        check_vendor,
        check_documents,
        check_lines,
        check_quantity,
        check_price_and_tax,
        check_total_variance,
    ):
        assert isinstance(rule(ctx), list)
