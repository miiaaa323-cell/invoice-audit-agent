import json
from collections import Counter
from decimal import Decimal

import pytest

from app.config import ROOT
from app.schemas import Invoice, PurchaseOrder, Vendor
from app.services.extraction import ExtractionService
from app.services.llm_client import LLMClient
from app.services.money import q2
from data.generate import generate, write_invoice_pdf


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    output = tmp_path_factory.mktemp("synthetic-data")
    cache = output / "cache"
    labels = generate(output, cache)
    return output, cache, labels


def test_dataset_distribution_matches_specification(dataset):
    output, cache, labels = dataset
    assert len(labels) == len(list((output / "invoices").glob("*.pdf"))) == 200
    assert Counter(row["category"] for row in labels) == {
        "rounding": 20,
        "large": 25,
        "clean": 115,
        "quantity": 7,
        "price": 6,
        "amount": 5,
        "duplicate": 5,
        "buyer": 5,
        "vendor": 4,
        "receipt": 4,
        "tax_rate": 4,
    }
    assert sum(bool(label["expected_codes"]) for label in labels) == 40
    vendors = [
        Vendor.model_validate(v) for v in json.loads((output / "seed/vendors.json").read_text())
    ]
    assert len(vendors) == 12
    assert sum(v.status == "blocked" for v in vendors) == 1
    orders = [
        PurchaseOrder.model_validate(p)
        for p in json.loads((output / "seed/purchase_orders.json").read_text())
    ]
    assert len(orders) == 200
    assert all(2 <= len(po.lines) <= 6 for po in orders)
    assert {
        abs(line.unit_price_excl_tax.as_tuple().exponent) for po in orders for line in po.lines
    } == {2, 3, 4}


def test_generated_fixture_replays_pdf_text_not_label_lookup(dataset):
    output, cache, labels = dataset
    extraction = ExtractionService(LLMClient("replay", cache))
    for position in (0, 19, 20, 159, 160, 173, 178, 199):
        label = labels[position]
        text, invoice, confidence = extraction.extract(output / label["invoice_file"])
        assert invoice == Invoice.model_validate(label["truth"])
        assert invoice.buyer_name in text
        assert invoice.po_number in text
        assert all(score == 0.99 for score in confidence.values())


def test_rounding_cases_keep_internal_arithmetic_within_tolerance(dataset):
    output, cache, labels = dataset
    for row in labels[:20]:
        invoice = Invoice.model_validate(row["truth"])
        assert Decimal(".01") <= abs(Decimal(row["expected_variance"])) <= Decimal(".99")
        assert sum(line.amount_excl_tax for line in invoice.lines) == invoice.total_excl_tax
        assert sum(line.tax_amount for line in invoice.lines) == invoice.total_tax
        for line in invoice.lines:
            assert abs(line.quantity * line.unit_price_excl_tax - line.amount_excl_tax) <= Decimal(
                ".01"
            )
            if line.amount_excl_tax != q2(line.quantity * line.unit_price_excl_tax):
                gross_price = q2(line.unit_price_excl_tax * (1 + line.tax_rate))
                assert line.amount_excl_tax == q2(gross_price * line.quantity / (1 + line.tax_rate))


def test_fixed_seed_and_pdf_bytes_are_reproducible(dataset, tmp_path):
    output, cache, labels = dataset
    bundled = json.loads((ROOT / "data/labels.json").read_text(encoding="utf-8"))
    assert labels == bundled
    invoice = Invoice.model_validate(labels[0]["truth"])
    first = write_invoice_pdf(tmp_path / "first.pdf", invoice)
    second = write_invoice_pdf(tmp_path / "second.pdf", invoice)
    assert first.read_bytes() == second.read_bytes()


def test_bundled_cache_provenance_is_explicit(dataset):
    output, cache, labels = dataset
    entries = [json.loads(path.read_text(encoding="utf-8")) for path in cache.glob("*.json")]
    assert len(entries) == 200
    assert {entry["source"] for entry in entries} == {"synthetic"}
    assert "not recorded from Claude" in (cache / "README.md").read_text()
