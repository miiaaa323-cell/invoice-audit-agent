"""Human decision policy and field corrections, independent of LangGraph."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from app.config import Policy
from app.graph.interrupts import ResumePayload, ReviewRequest
from app.routing import requires_level2
from app.schemas import Invoice

ACTIONS = {
    "field_verify": ["confirm_fields"],
    "ap_review": ["approve_with_note", "reject", "request_info"],
    "level1_approval": ["approve", "reject"],
    "level2_approval": ["approve", "reject"],
    "wait_for_info": ["submit_document"],
}


class ReviewError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def correction_result(invoice: dict, corrections: dict, confidences: dict) -> tuple[dict, dict]:
    values = deepcopy(invoice)
    scores = dict(confidences)
    for path, value in corrections.items():
        parts = path.split(".")
        if len(parts) == 1 and path in Invoice.model_fields and path != "lines":
            values[path] = value
        elif len(parts) == 3 and parts[0] == "lines" and parts[1].isdigit():
            index = int(parts[1])
            lines = values.get("lines", [])
            if index >= len(lines) or parts[2] not in lines[index]:
                raise ReviewError(f"无效的字段路径：{path}")
            lines[index][parts[2]] = value
        else:
            raise ReviewError(f"无效的字段路径：{path}")
        scores[path] = 1.0
    try:
        result = Invoice.model_validate(values)
    except ValidationError as exc:
        raise ReviewError("修正字段的格式不正确") from exc
    return result.model_dump(mode="json"), scores


def make_request(kind: str, state: dict, policy: Policy | None = None) -> ReviewRequest:
    invoice = state.get("invoice") or {}
    rounds_left = (policy or Policy()).max_info_rounds - state.get("info_round", 0)
    fields = [
        f["field"]
        for f in state.get("findings", [])
        if f["code"] == "LOW_CONFIDENCE" and f.get("field")
    ]
    return ReviewRequest(
        type=kind,
        invoice_summary={
            key: invoice.get(key)
            for key in (
                "invoice_number",
                "seller_name",
                "buyer_name",
                "po_number",
                "gr_number",
                "total_incl_tax",
            )
        },
        findings=state.get("findings", []),
        explanation=state.get("explanation"),
        allowed_actions=ACTIONS[kind],
        required_role="finance_manager" if kind == "level2_approval" else "ap_clerk",
        fields=fields,
        info_rounds_left=max(rounds_left, 0) if kind == "ap_review" else None,
    )


def info_rounds_exhausted(state: dict, policy: Policy) -> dict | None:
    """Another request beyond the limit cancels at once instead of asking for a PDF."""
    if state.get("info_round", 0) < policy.max_info_rounds:
        return None
    return {"status": "CANCELLED", "next_step": "finalize"}


def validate_resume(request: ReviewRequest, payload: ResumePayload, state: dict) -> None:
    if payload.role != request.required_role:
        raise ReviewError(f"此操作需要 {request.required_role} 角色")
    if payload.action not in request.allowed_actions:
        raise ReviewError("当前待办不允许此操作")
    if payload.action in {"approve_with_note", "reject", "request_info"}:
        if not payload.note or not payload.note.strip():
            raise ReviewError("请填写处理理由或需要补充的材料")
    if request.type == "field_verify":
        corrections = payload.corrected_fields or {}
        if not corrections or any(path not in corrections for path in request.fields):
            raise ReviewError("请确认或修正全部待核对字段")
        correction_result(state["invoice"], corrections, state.get("confidences", {}))
    elif payload.corrected_fields:
        raise ReviewError("请在字段核对待办中修正字段")
    if request.type == "wait_for_info" and not payload.document_path:
        raise ReviewError("请通过补交接口上传新的发票 PDF")
    if payload.action == "approve_with_note":
        invoice = Invoice.model_validate(state["invoice"])
        required = (
            invoice.invoice_number,
            invoice.seller_tax_id,
            invoice.gr_number,
            invoice.total_excl_tax,
            invoice.total_tax,
            invoice.total_incl_tax,
        )
        if any(value is None or value == "" for value in required):
            raise ReviewError("入账所需字段不完整，请要求补材料")


def apply_review(kind: str, state: dict, payload: ResumePayload, policy: Policy) -> dict:
    decision = {
        "decision_id": str(uuid4()),
        "node": kind,
        "role": payload.role,
        "action": payload.action,
        "note": (payload.note or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    update = {"decisions": [decision], "next_step": "finalize"}
    if kind == "field_verify":
        invoice, scores = correction_result(
            state["invoice"], payload.corrected_fields or {}, state.get("confidences", {})
        )
        update.update(
            invoice=invoice,
            confidences=scores,
            findings=[{"__reset__": True}],
            variance=None,
            explanation=None,
            context=None,
            next_step="load_context",
            revision=state.get("revision", 0) + 1,
        )
    elif kind == "wait_for_info":
        update.update(
            invoice_pdf_path=payload.document_path,
            info_round=state.get("info_round", 0) + 1,
            invoice=None,
            context=None,
            raw_text=None,
            confidences={},
            findings=[{"__reset__": True}],
            variance=None,
            explanation=None,
            route=None,
            status="PROCESSING",
            next_step="extract_invoice",
            revision=state.get("revision", 0) + 1,
        )
    elif payload.action == "reject":
        update["status"] = "REJECTED"
    elif payload.action == "request_info":
        update.update(status="WAITING_INFO", next_step="wait_for_info")
    elif kind in {"ap_review", "level1_approval"} and requires_level2(
        Invoice.model_validate(state["invoice"]).total_incl_tax, policy
    ):
        update.update(next_step="level2_approval", status="PROCESSING")
    else:
        update["status"] = "APPROVED"
    return update
