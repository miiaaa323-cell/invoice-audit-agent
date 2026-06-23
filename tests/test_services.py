import copy
import json
from types import SimpleNamespace

import pytest

from app.config import ROOT
from app.schemas import ExtractionResult, Invoice
from app.services.explanation import Explanation, ExplanationService
from app.services.extraction import (
    ExtractionService,
    InvoiceExtractionError,
    PDFExtractionError,
    cache_extraction,
    confidence_map,
    field_paths,
    read_pdf_text,
)
from app.services.llm_client import CacheMissError, LLMClient, LLMResponseError
from data.generate import write_invoice_pdf


@pytest.fixture
def invoice():
    labels = json.loads((ROOT / "data/labels.json").read_text(encoding="utf-8"))
    return Invoice.model_validate(labels[0]["truth"])


@pytest.fixture
def pdf(tmp_path, invoice):
    return write_invoice_pdf(tmp_path / "invoice.pdf", invoice)


def test_replay_extracts_exact_cached_pdf_without_api_key(tmp_path, invoice, pdf, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = LLMClient("replay", tmp_path / "cache")
    cache_extraction(client, pdf, invoice)
    text, actual, confidences = ExtractionService(client).extract(pdf)
    assert actual == invoice
    assert invoice.buyer_name in text
    assert all(confidences[field] == 0.99 for field in field_paths(invoice))


def test_cache_miss_is_read_only_and_never_falls_back(tmp_path):
    client = LLMClient("replay", tmp_path / "missing")
    with pytest.raises(CacheMissError):
        client.complete("extract_invoice", {"pdf_text": "unknown"}, ExtractionResult)
    assert not client.cache_dir.exists()


def test_cache_key_covers_task_model_payload_schema_and_prompt(tmp_path, monkeypatch):
    client = LLMClient("replay", tmp_path)
    request = client.request("extract_invoice", {"pdf_text": "a"}, ExtractionResult)
    original = client.request_hash(request)
    for field, value in (
        ("model", "other"),
        ("prompt", "changed"),
        ("payload", {"pdf_text": "b"}),
        ("schema", {"type": "object"}),
    ):
        modified = copy.deepcopy(request)
        modified[field] = value
        assert client.request_hash(modified) != original


def test_record_calls_sdk_tool_and_replay_uses_recorded_response(tmp_path, monkeypatch):
    response = {"invoice": {}, "confidences": {}}
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name="submit_result", input=response)]
        )

    client = LLMClient("record", tmp_path, api_key="unused")
    client._sdk = SimpleNamespace(messages=SimpleNamespace(create=create))
    payload = {"pdf_text": "example"}
    assert client.complete("extract_invoice", payload, ExtractionResult) == response
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "submit_result"}
    cache_file = next(tmp_path.glob("*.json"))
    assert json.loads(cache_file.read_text())["source"] == "recorded"
    # Regenerating synthetic fixtures must never replace genuine API recordings.
    client.cache_response("extract_invoice", payload, ExtractionResult, {"wrong": True})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    replay = LLMClient("replay", tmp_path)
    assert replay.complete("extract_invoice", payload, ExtractionResult) == response


def test_live_calls_sdk_without_writing_cache(tmp_path):
    client = LLMClient("live", tmp_path / "cache")
    response = {"text": "金额有差异"}
    client._sdk = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name="submit_result", input=response)]
            )
        )
    )
    assert client.complete("explain_findings", {}, Explanation) == response
    assert not client.cache_dir.exists()


def test_invalid_tool_response_is_explicit(tmp_path):
    client = LLMClient("live", tmp_path)
    client._sdk = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(content=[]))
    )
    with pytest.raises(LLMResponseError):
        client.complete("extract_invoice", {}, ExtractionResult)


def test_tampered_cache_is_rejected(tmp_path):
    client = LLMClient("replay", tmp_path)
    path = client.cache_response("extract_invoice", {}, ExtractionResult, {})
    entry = json.loads(path.read_text())
    entry["request"]["model"] = "tampered"
    path.write_text(json.dumps(entry))
    with pytest.raises(LLMResponseError):
        client.complete("extract_invoice", {}, ExtractionResult)


class Responses:
    def __init__(self, responses):
        self.responses, self.payloads = iter(responses), []

    def complete(self, task, payload, schema):
        self.payloads.append(payload)
        return next(self.responses)


def test_schema_validation_retries_once_with_error(pdf, invoice):
    good = {
        "invoice": invoice.model_dump(mode="json"),
        "confidences": {p: 0.9 for p in field_paths(invoice)},
    }
    client = Responses([{"invoice": {"invoice_date": "not a date"}}, good])
    assert ExtractionService(client).extract(pdf)[1] == invoice
    assert len(client.payloads) == 2
    assert "validation_error" in client.payloads[1]
    assert client.payloads[0]["pdf_text"] == client.payloads[1]["pdf_text"]


def test_two_invalid_extractions_fail_without_guessing(pdf):
    client = Responses([{"invoice": {"total_tax": "n/a"}}] * 2)
    with pytest.raises(InvoiceExtractionError):
        ExtractionService(client).extract(pdf)
    assert len(client.payloads) == 2


@pytest.mark.parametrize("score", [-0.01, 1.01, float("nan"), float("inf"), True, "0.9"])
def test_invalid_confidence_is_rejected(invoice, score):
    with pytest.raises(ValueError):
        confidence_map(invoice, {"invoice_number": score})


def test_missing_confidence_is_conservative(invoice):
    assert set(confidence_map(invoice, {}).values()) == {0.0}


@pytest.mark.parametrize("score", [True, "0.99"])
def test_extraction_rejects_coercible_confidence_values(pdf, invoice, score):
    response = {
        "invoice": invoice.model_dump(mode="json"),
        "confidences": {path: 0.99 for path in field_paths(invoice)},
    }
    response["confidences"]["invoice_number"] = score
    client = Responses([response, response])
    with pytest.raises(InvoiceExtractionError):
        ExtractionService(client).extract(pdf)
    assert len(client.payloads) == 2


def test_empty_or_corrupt_pdf_is_explicit(tmp_path):
    from reportlab.pdfgen import canvas

    empty = tmp_path / "empty.pdf"
    document = canvas.Canvas(str(empty))
    document.showPage()
    document.save()
    with pytest.raises(PDFExtractionError, match="扫描件"):
        read_pdf_text(empty)
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf")
    with pytest.raises(PDFExtractionError, match="无法读取"):
        read_pdf_text(corrupt)


def test_explanation_is_bounded_and_never_called_for_clean_invoice(invoice):
    client = Responses([{"text": "第一行数量为101，收货数量为100，请核对。"}])
    service = ExplanationService(client)
    assert service.explain(invoice, []) == ""
    assert client.payloads == []
    findings = [{"rule_id": "R09", "code": "QTY_MISMATCH", "message": "数量不一致"}]
    assert "数量" in service.explain(invoice, findings)
    assert len(client.payloads) == 1


def test_explanation_rejects_approval_conclusion(invoice):
    client = Responses([{"text": "建议批准付款。"}])
    with pytest.raises(ValueError, match="审批结论"):
        ExplanationService(client).explain(invoice, [{"rule_id": "R09", "code": "QTY_MISMATCH"}])
