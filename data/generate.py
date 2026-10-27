"""Reproducible synthetic ERP records, text invoices and explicitly synthetic replay data."""

import argparse
import json
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from app.config import ROOT, load_policy
from app.schemas import (
    GoodsReceipt,
    GRLine,
    Invoice,
    InvoiceLine,
    POLine,
    PurchaseOrder,
    Vendor,
)
from app.services.explanation import Explanation, explanation_payload
from app.services.extraction import cache_extraction
from app.services.llm_client import LLMClient
from app.services.money import q2

SEED = 20260617
FONT_NAME = "STSong-Light"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_invoice_pdf(path: Path | str, invoice: Invoice) -> Path:
    """Render a single-page Chinese invoice; all values remain selectable text."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
    page = canvas.Canvas(str(path), pagesize=A4, invariant=1, pageCompression=1)
    page.setTitle(f"增值税专用发票 {invoice.invoice_number or ''}")
    page.setAuthor("示例科技有限公司")
    width, height = A4
    blue = colors.HexColor("#204f68")
    gray = colors.HexColor("#63727a")
    page.setStrokeColor(blue)
    page.setFillColor(blue)
    page.setFont(FONT_NAME, 22)
    page.drawCentredString(width / 2, height - 58, "增值税专用发票")
    page.setFont(FONT_NAME, 9)
    page.drawCentredString(width / 2, height - 79, "采购业务示例票据")
    page.line(35, height - 96, width - 35, height - 96)
    page.setFillColor(colors.black)
    page.setFont(FONT_NAME, 10)
    page.drawString(36, height - 118, f"发票号码：{invoice.invoice_number or ''}")
    page.drawRightString(width - 36, height - 118, f"开票日期：{invoice.invoice_date or ''}")
    party_y = height - 148
    for name, value, tax in (
        ("购买方", invoice.buyer_name, invoice.buyer_tax_id),
        ("销售方", invoice.seller_name, invoice.seller_tax_id),
    ):
        page.setStrokeColor(colors.HexColor("#c7d6de"))
        page.rect(35, party_y - 51, width - 70, 53)
        page.setFillColor(blue)
        page.drawString(46, party_y - 15, name)
        page.setFillColor(colors.black)
        page.drawString(104, party_y - 15, f"名称：{value or ''}")
        page.drawString(104, party_y - 36, f"纳税人识别号：{tax or ''}")
        party_y -= 66
    top = party_y - 16
    columns = [35, 63, 126, 232, 278, 354, 430, 476, width - 35]
    headers = [
        "序号",
        "商品编码",
        "货物或服务名称",
        "数量",
        "不含税单价",
        "不含税金额",
        "税率",
        "税额",
    ]
    row_height = 34
    bottom = top - row_height * (max(6, len(invoice.lines)) + 1)
    page.setFillColor(colors.HexColor("#eaf1f5"))
    page.rect(35, top - row_height, width - 70, row_height, stroke=0, fill=1)
    page.setStrokeColor(colors.HexColor("#b1c4cf"))
    for x in columns:
        page.line(x, top, x, bottom)
    for row in range(max(6, len(invoice.lines)) + 2):
        y = top - row * row_height
        page.line(35, y, width - 35, y)
    page.setFillColor(blue)
    page.setFont(FONT_NAME, 8)
    for index, header in enumerate(headers):
        page.drawCentredString((columns[index] + columns[index + 1]) / 2, top - 21, header)
    page.setFillColor(colors.black)
    for row_index, line in enumerate(invoice.lines):
        y = top - row_height * (row_index + 1) - 21
        values = [
            line.line_no,
            line.sku or "",
            line.description or "",
            line.quantity,
            line.unit_price_excl_tax,
            line.amount_excl_tax,
            f"{line.tax_rate * 100:g}%" if line.tax_rate is not None else "",
            line.tax_amount,
        ]
        for index, value in enumerate(values):
            display = "" if value is None else str(value)
            if index in {3, 4, 5, 7}:
                page.drawRightString(columns[index + 1] - 5, y, display)
            else:
                page.drawString(columns[index] + 4, y, display)
    total_y = bottom - 26
    page.setFont(FONT_NAME, 10)
    page.drawString(43, total_y, f"不含税合计：¥ {invoice.total_excl_tax or '0.00'}")
    page.drawRightString(width - 43, total_y, f"税额合计：¥ {invoice.total_tax or '0.00'}")
    page.setFillColor(blue)
    page.setFont(FONT_NAME, 13)
    page.drawString(43, total_y - 34, f"价税合计：¥ {invoice.total_incl_tax or '0.00'}")
    page.setFillColor(colors.black)
    page.setFont(FONT_NAME, 10)
    page.drawString(43, total_y - 71, f"备注：采购单 PO：{invoice.po_number or ''}")
    page.drawString(43, total_y - 93, f"收货单 GR：{invoice.gr_number or ''}")
    page.setFillColor(gray)
    page.setFont(FONT_NAME, 8)
    page.drawString(36, 40, "示例票据，仅用于采购发票审核演示")
    page.showPage()
    page.save()
    return path


def recalculate(invoice: Invoice) -> None:
    for line in invoice.lines:
        line.tax_amount = q2(line.amount_excl_tax * line.tax_rate)
    invoice.total_excl_tax = q2(sum(line.amount_excl_tax for line in invoice.lines))
    invoice.total_tax = q2(sum(line.tax_amount for line in invoice.lines))
    invoice.total_incl_tax = q2(invoice.total_excl_tax + invoice.total_tax)


def category(index: int) -> str:
    for last, kind in (
        (20, "rounding"),
        (45, "large"),
        (160, "clean"),
        (167, "quantity"),
        (173, "price"),
        (178, "amount"),
        (183, "duplicate"),
        (188, "buyer"),
        (192, "vendor"),
        (196, "receipt"),
        (200, "tax_rate"),
    ):
        if index <= last:
            return kind
    raise ValueError("Invoice index out of range")


def inclusive_rounding_example(
    price: Decimal, rate: Decimal, quantity: Decimal, sign: Decimal
) -> tuple[Decimal, Decimal]:
    """Find a four-decimal price with a one-cent gross-to-net rounding difference."""
    for step in range(1000):
        candidate = (price + Decimal(".0001") * step).quantize(Decimal(".0001"))
        gross_unit_price = q2(candidate * (1 + rate))
        invoice_amount = q2(gross_unit_price * quantity / (1 + rate))
        if invoice_amount - q2(candidate * quantity) == sign * Decimal(".01"):
            return candidate, invoice_amount
    raise ValueError("Could not construct a bounded inclusive-price rounding example")


def generate(
    output_dir: Path | str = ROOT / "data", cache_dir: Path | str = ROOT / "fixtures/llm_cache"
):
    output_dir, cache_dir = Path(output_dir), Path(cache_dir)
    rng = random.Random(SEED)
    policy = load_policy()
    vendors = [
        Vendor(
            vendor_id=f"V{i:03}",
            vendor_name=f"华新采购供应商{i:02}有限公司",
            tax_id=f"91310000VD{i:08}",
            status="blocked" if i == 12 else "active",
        )
        for i in range(1, 13)
    ]
    purchase_orders, receipts, labels, invoices = [], [], [], []
    for index in range(1, 201):
        kind = category(index)
        vendor_index = (index - 1) % 11
        if kind == "duplicate":
            vendor_index = (index - 179) % 11
        if kind == "vendor" and index <= 190:
            vendor_index = 11
        vendor = vendors[vendor_index]
        po_lines = []
        for line_index in range(1, rng.randint(2, 6) + 1):
            places = 2 + (index + line_index) % 3
            whole = rng.randint(300, 450) if kind == "large" else rng.randint(10, 40)
            price = Decimal(f"{whole}.{rng.randrange(10**places):0{places}d}")
            po_lines.append(
                POLine(
                    po_line_no=line_index,
                    sku=f"SKU-{line_index:03}",
                    description=f"采购物料{line_index:02}",
                    quantity=Decimal("100"),
                    unit_price_excl_tax=price,
                    tax_rate=rng.choice([Decimal("0.13"), Decimal("0.09"), Decimal("0.06")]),
                )
            )
        po = PurchaseOrder(
            po_number=f"PO-2026-{index:04}",
            vendor_id=vendor.vendor_id,
            created_at=date(2026, 6, 16),
            lines=po_lines,
        )
        gr = GoodsReceipt(
            gr_number=f"GR-2026-{index:04}",
            po_number=po.po_number,
            received_date=date(2026, 6, 17),
            lines=[
                GRLine(po_line_no=line.po_line_no, quantity_received=line.quantity)
                for line in po.lines
            ],
        )
        invoice = Invoice(
            invoice_number=f"INV-2026-{index:04}",
            invoice_date=date(2026, 6, 17),
            seller_name=vendor.vendor_name,
            seller_tax_id=vendor.tax_id,
            buyer_name=policy.company.buyer_name,
            buyer_tax_id=policy.company.buyer_tax_id,
            po_number=po.po_number,
            gr_number=gr.gr_number,
            lines=[
                InvoiceLine(
                    line_no=line.po_line_no,
                    sku=line.sku,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price_excl_tax=line.unit_price_excl_tax,
                    amount_excl_tax=q2(line.quantity * line.unit_price_excl_tax),
                    tax_rate=line.tax_rate,
                    tax_amount=Decimal("0"),
                )
                for line in po.lines
            ],
        )
        codes, variance = [], None
        if kind == "rounding":
            sign = Decimal("1") if index % 2 else Decimal("-1")
            adjusted = 1 + (index % len(invoice.lines))
            for line in invoice.lines[:adjusted]:
                price, amount = inclusive_rounding_example(
                    line.unit_price_excl_tax, line.tax_rate, line.quantity, sign
                )
                po.lines[line.line_no - 1].unit_price_excl_tax = price
                line.unit_price_excl_tax, line.amount_excl_tax = price, amount
            variance = str(q2(sign * adjusted * Decimal("0.01")))
        elif kind == "quantity":
            line = invoice.lines[0]
            line.quantity += Decimal("1") if index % 2 else Decimal("-1")
            line.amount_excl_tax = q2(line.quantity * line.unit_price_excl_tax)
            codes = ["QTY_MISMATCH", "AMOUNT_OVER_TOLERANCE"]
        elif kind == "price":
            line = invoice.lines[0]
            line.unit_price_excl_tax += Decimal("0.5000")
            line.amount_excl_tax = q2(line.quantity * line.unit_price_excl_tax)
            codes = ["PRICE_MISMATCH", "AMOUNT_OVER_TOLERANCE"]
        elif kind == "amount":
            invoice.lines[0].amount_excl_tax += Decimal("2.00")
            codes = ["ARITHMETIC_ERROR", "AMOUNT_OVER_TOLERANCE"]
        elif kind == "duplicate":
            invoice.invoice_number = f"INV-2026-{index - 178:04}"
            codes = ["DUPLICATE_INVOICE"]
        elif kind == "buyer":
            if index % 2:
                invoice.buyer_name = "其他采购有限公司"
            else:
                invoice.buyer_tax_id = "91310000MA1FL0000Y"
            codes = ["BUYER_MISMATCH"]
        elif kind == "vendor":
            if index > 190:
                invoice.seller_name = "未登记供应商有限公司"
                invoice.seller_tax_id = f"91310000UN{index:08}"
            codes = ["VENDOR_NOT_APPROVED"]
        elif kind == "receipt":
            if index <= 194:
                invoice.gr_number = f"GR-MISSING-{index:04}"
                codes = ["GR_NOT_FOUND"]
            else:
                gr.invoiced_by_run_id = f"historical-{index}"
                codes = ["GR_ALREADY_INVOICED"]
        elif kind == "tax_rate":
            invoice.lines[0].tax_rate = (
                Decimal("0.09") if invoice.lines[0].tax_rate == Decimal("0.13") else Decimal("0.13")
            )
            codes = ["TAX_RATE_MISMATCH"]
        recalculate(invoice)
        route = "AP_REVIEW" if codes else ("LEVEL1_APPROVAL" if kind == "large" else "AUTO_APPROVE")
        filename = f"invoices/invoice_{index:04}.pdf"
        labels.append(
            {
                "invoice_file": filename,
                "expected_codes": codes,
                "expected_route": route,
                "expected_variance": variance,
                "category": kind,
                "truth": invoice.model_dump(mode="json"),
            }
        )
        purchase_orders.append(po.model_dump(mode="json"))
        receipts.append(gr.model_dump(mode="json"))
        invoices.append(invoice)
    write_json(output_dir / "seed/company.json", policy.company.model_dump(mode="json"))
    write_json(output_dir / "seed/vendors.json", [v.model_dump(mode="json") for v in vendors])
    write_json(output_dir / "seed/purchase_orders.json", purchase_orders)
    write_json(output_dir / "seed/goods_receipts.json", receipts)
    write_json(output_dir / "labels.json", labels)
    client = LLMClient("replay", cache_dir)
    for label, invoice in zip(labels, invoices, strict=True):
        path = write_invoice_pdf(output_dir / label["invoice_file"], invoice)
        cache_extraction(client, path, invoice)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "README.md").write_text(
        "# Replay fixtures\n\n"
        "Bundled responses have `source: synthetic`: they were generated from known invoice inputs, "
        "not recorded from Claude. They test deterministic auditing and workflow behavior, not live "
        "LLM extraction quality. Cache keys cover the exact PDF text, model, versioned prompt and schema.\n\n"
        "`LLM_MODE=record` stores actual Claude responses with `source: recorded`. Re-running the "
        "generator preserves recorded entries. Missing replay entries raise CacheMissError; there "
        "is no model, parser or truth-label fallback at runtime.\n",
        encoding="utf-8",
    )
    return labels


def cache_finding_explanations(output_dir: Path | str, cache_dir: Path | str) -> int:
    """Author synthetic explanations using actual pure-rule findings and seeded contexts."""
    from tempfile import TemporaryDirectory

    from app.repo.repositories import Repository
    from app.rules.registry import RULE_GROUPS, run_group

    output_dir = Path(output_dir)
    labels = json.loads((output_dir / "labels.json").read_text(encoding="utf-8"))
    client, count = LLMClient("replay", cache_dir), 0
    policy = load_policy()
    with TemporaryDirectory(prefix="invoice-cache-") as tmp:
        repo = Repository(Path(tmp) / "audit.sqlite")
        repo.seed(output_dir / "seed")
        for index, label in enumerate(labels, 1):
            invoice = Invoice.model_validate(label["truth"])
            run_id = f"fixture-{index}"
            repo.register_run(run_id, str(output_dir / label["invoice_file"]))
            flags = repo.reserve(invoice, run_id)
            from app.services.extraction import field_paths

            ctx = repo.load_context(
                invoice, run_id, policy, {p: 0.99 for p in field_paths(invoice)}
            )
            ctx.duplicate_invoice = flags["duplicate_invoice"]
            ctx.gr_reserved_by_other = flags["gr_reserved_by_other"]
            findings = [finding for group in RULE_GROUPS for finding in run_group(group, ctx)]
            if findings:
                messages = list(dict.fromkeys(f.message for f in findings))
                text = "".join(messages)[:120]
                client.cache_response(
                    "explain_findings",
                    explanation_payload(invoice, findings),
                    Explanation,
                    {"text": text},
                    source="synthetic",
                )
                count += 1
            else:
                repo.finalize(run_id, invoice, "APPROVED", vendor_id=ctx.vendor.vendor_id)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "fixtures/llm_cache")
    parser.add_argument("--with-explanations", action="store_true")
    args = parser.parse_args()
    labels = generate(args.output_dir, args.cache_dir)
    if args.with_explanations:
        cache_finding_explanations(args.output_dir, args.cache_dir)
    print(
        f"Generated {len(labels)} synthetic invoices; replay fixtures are synthetic, not Claude recordings."
    )


if __name__ == "__main__":
    main()
