"""Text-PDF extraction with one schema-repair attempt."""

import math
from pathlib import Path

import pdfplumber
from pydantic import ValidationError

from app.schemas import ExtractionResult, Invoice
from app.services.llm_client import LLMClient


class PDFExtractionError(ValueError):
    pass


class InvoiceExtractionError(ValueError):
    pass


def read_pdf_text(pdf_path: Path | str) -> str:
    try:
        with pdfplumber.open(pdf_path) as document:
            if not document.pages:
                raise PDFExtractionError("PDF 没有页面")
            text = "\n\n".join(page.extract_text() or "" for page in document.pages).strip()
    except PDFExtractionError:
        raise
    except Exception as exc:
        raise PDFExtractionError("无法读取 PDF 文件") from exc
    if not text:
        raise PDFExtractionError("PDF 无可提取文本；请上传文字版发票，扫描件暂不支持")
    return text


def field_paths(invoice: Invoice) -> list[str]:
    paths = list(Invoice.model_fields)
    for index, line in enumerate(invoice.lines):
        paths.extend(f"lines.{index}.{field}" for field in type(line).model_fields)
    return paths


def confidence_map(invoice: Invoice, supplied: dict[str, float]) -> dict[str, float]:
    result = {}
    for path in field_paths(invoice):
        score = supplied.get(path, 0.0)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(f"Invalid confidence for {path}")
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(f"Confidence outside [0, 1] for {path}")
        result[path] = float(score)
    return result


def extraction_payload(raw_text: str) -> dict:
    return {"pdf_text": raw_text}


class ExtractionService:
    def __init__(self, client: LLMClient):
        self.client = client

    def extract(self, pdf_path: Path | str) -> tuple[str, Invoice, dict[str, float]]:
        text = read_pdf_text(pdf_path)
        payload = extraction_payload(text)
        for attempt in range(2):
            response = self.client.complete("extract_invoice", payload, ExtractionResult)
            try:
                extraction = ExtractionResult.model_validate(response)
                confidences = confidence_map(extraction.invoice, response.get("confidences", {}))
                return text, extraction.invoice, confidences
            except (ValidationError, ValueError) as exc:
                if attempt:
                    raise InvoiceExtractionError("发票抽取结构校验失败，请检查 PDF 字段") from exc
                payload = {"pdf_text": text, "validation_error": str(exc)}
        raise AssertionError("unreachable")


def cache_extraction(
    client: LLMClient,
    pdf_path: Path | str,
    invoice: Invoice,
    confidences: dict[str, float] | None = None,
) -> Path:
    """Build a clearly synthetic replay fixture from known generator inputs."""
    scores = confidences if confidences is not None else {p: 0.99 for p in field_paths(invoice)}
    response = ExtractionResult(invoice=invoice, confidences=scores).model_dump(mode="json")
    return client.cache_response(
        "extract_invoice",
        extraction_payload(read_pdf_text(pdf_path)),
        ExtractionResult,
        response,
        source="synthetic",
    )
