"""Checkpoint state and deterministic reducers for parallel rule results."""

import operator
from typing import Annotated, Any, TypedDict


def add_findings(left: list[dict], right: list[dict] | dict) -> list[dict]:
    result = list(left)
    for item in right if isinstance(right, list) else [right]:
        if item.get("__reset__"):
            result = []
        else:
            result.append(item)
    return result


class AuditState(TypedDict, total=False):
    run_id: str
    invoice_pdf_path: str
    info_round: int
    raw_text: str | None
    invoice: dict | None
    confidences: dict[str, float]
    context: dict | None
    findings: Annotated[list[dict], add_findings]
    variance: dict | None
    explanation: str | None
    route: str | None
    decisions: Annotated[list[dict], operator.add]
    status: str
    next_step: str
    revision: int
    error: dict[str, Any] | None
