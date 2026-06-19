"""Turn rule findings into short Chinese explanations, without delegating decisions."""

from typing import Any

from anthropic import APIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas import Invoice
from app.services.llm_client import CacheMissError, LLMClient, LLMResponseError


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=120)


def explanation_payload(invoice: Invoice, findings: list, context: Any = None) -> dict:
    records = [f.model_dump(mode="json") if hasattr(f, "model_dump") else f for f in findings]
    records = sorted(records, key=lambda f: (f["rule_id"], f["code"], str(f.get("field", ""))))
    return {"invoice_number": invoice.invoice_number, "findings": records}


class ExplanationService:
    def __init__(self, client: LLMClient):
        self.client = client

    def explain(self, invoice: Invoice, findings: list, context: Any = None) -> str:
        if not findings:
            return ""
        response = self.client.complete(
            "explain_findings", explanation_payload(invoice, findings, context), Explanation
        )
        explanation = Explanation.model_validate(response).text
        if any(
            term in explanation
            for term in (
                "审核通过",
                "批准付款",
                "应予批准",
                "应予驳回",
                "建议驳回",
                "建议批准",
                "拒绝付款",
            )
        ):
            raise ValueError("差异说明包含审批结论")
        return explanation

    def for_review(
        self, invoice: Invoice, findings: list, context: Any = None
    ) -> tuple[str, str | None]:
        """A wording failure must never decide whether a payable is accepted."""
        try:
            return self.explain(invoice, findings, context), None
        except (APIError, CacheMissError, LLMResponseError, ValidationError, ValueError) as exc:
            messages = dict.fromkeys(f.message for f in findings)
            return "".join(messages)[:120], type(exc).__name__
