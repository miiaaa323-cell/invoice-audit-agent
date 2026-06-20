"""Application boundary for checkpoint execution and concurrent human actions."""

import json
from uuid import uuid4

from langgraph.types import Command

from app.config import load_policy
from app.graph.interrupts import ResumeBody, ResumePayload, ReviewRequest
from app.repo.repositories import ReservationConflict
from app.schemas import Invoice
from app.services.review import ReviewError, validate_resume

TERMINAL = {"APPROVED", "REJECTED", "CANCELLED"}


class AuditService:
    def __init__(self, graph, repo, settings, policy=None):
        self.graph = graph
        self.repo = repo
        self.settings = settings
        self.policy = policy or load_policy(settings.policy_path)

    @staticmethod
    def config(run_id):
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 120}

    def get(self, run_id):
        record = self.repo.get_run(run_id)
        if record is None:
            raise ReviewError("审核记录不存在", 404)
        snapshot = self.graph.get_state(self.config(run_id))
        state = dict(snapshot.values or {})
        pending = None
        if record["status"] not in TERMINAL:
            for task in snapshot.tasks:
                if task.interrupts:
                    pending = ReviewRequest.model_validate(task.interrupts[0].value).model_dump(
                        mode="json"
                    )
                    break
        stored = json.loads(record.get("snapshot_json") or "{}")
        return {
            "run_id": run_id,
            "status": record["status"],
            "version": record["version"],
            "route": state.get("route") or record["route"],
            "invoice": state.get("invoice"),
            "confidences": state.get("confidences", {}),
            "findings": state.get("findings", []),
            "explanation": state.get("explanation"),
            "variance": state.get("variance"),
            "decisions": state.get("decisions", []),
            "info_round": state.get("info_round", 0),
            "pending": pending,
            "error": stored.get("error"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def list(self, status=None):
        return [self.get(row["run_id"]) for row in self.repo.list_runs(status)]

    def _execute(self, run_id, value):
        try:
            self.graph.invoke(value, self.config(run_id))
        except Exception as exc:
            state = self.graph.get_state(self.config(run_id)).values or {}
            self.repo.finalize(
                run_id, Invoice.model_validate(state.get("invoice") or {}), "REJECTED"
            )
            self.repo.save_run(
                run_id, snapshot_json={"error": {"code": type(exc).__name__, "message": str(exc)}}
            )
            raise
        state = self.graph.get_state(self.config(run_id)).values
        self.repo.save_run(
            run_id,
            status=state["status"],
            route=state.get("route"),
            pdf_path=state["invoice_pdf_path"],
            snapshot_json=state,
        )

    def submit(self, pdf_path):
        run_id = str(uuid4())
        self.repo.register_run(run_id, str(pdf_path))
        self.repo.acquire_run(run_id, 0)
        success = False
        try:
            self._execute(
                run_id,
                {
                    "run_id": run_id,
                    "invoice_pdf_path": str(pdf_path),
                    "info_round": 0,
                    "revision": 0,
                    "findings": [],
                    "decisions": [],
                    "status": "PROCESSING",
                    "variance": None,
                    "explanation": None,
                    "confidences": {},
                },
            )
            success = True
        finally:
            self.repo.release_run(run_id, success=success)
        return self.get(run_id)

    def _resume(self, run_id, body, role, document_path=None):
        body = body if isinstance(body, ResumeBody) else ResumeBody.model_validate(body)
        current = self.get(run_id)
        if current["status"] in TERMINAL or not current["pending"]:
            raise ReviewError("此审核当前没有可处理的待办", 409)
        if role not in {"ap_clerk", "finance_manager"}:
            raise ReviewError("无效的审核角色")
        request = ReviewRequest.model_validate(current["pending"])
        payload = ResumePayload(**body.model_dump(), role=role, document_path=document_path)
        state = self.graph.get_state(self.config(run_id)).values
        validate_resume(request, payload, state)
        if payload.action in {"approve", "approve_with_note"}:
            context = self.repo.load_context(
                Invoice.model_validate(state["invoice"]),
                run_id,
                self.policy,
                state.get("confidences", {}),
            )
            if context.duplicate_invoice:
                raise ReservationConflict("DUPLICATE_INVOICE")
            if context.gr_reserved_by_other:
                raise ReservationConflict("GR_ALREADY_INVOICED")
        version = body.version if body.version is not None else current["version"]
        self.repo.acquire_run(run_id, version)
        success = False
        try:
            self._execute(run_id, Command(resume=payload.model_dump(mode="json")))
            success = True
        finally:
            self.repo.release_run(run_id, success=success)
        return self.get(run_id)

    def resume(self, run_id, body, role):
        return self._resume(run_id, body, role)

    def document(self, run_id, pdf_path, role="ap_clerk"):
        if self.get(run_id)["status"] != "WAITING_INFO":
            raise ReviewError("仅等待补材料的审核允许补交发票", 409)
        return self._resume(run_id, {"action": "submit_document"}, role, str(pdf_path))
