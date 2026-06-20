"""Graph glue: IO is delegated to services/repositories, decisions to pure rules."""

from functools import wraps

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from app.graph.interrupts import ResumePayload
from app.routing import Route, decide
from app.rules.base import AuditContext, Finding
from app.rules.documents import check_documents
from app.rules.fields import low_confidence, missing_fields
from app.rules.registry import run_group
from app.rules.three_way_match import variance_entry
from app.schemas import Invoice, VarianceEntry
from app.services.review import apply_review, make_request, validate_resume


def audited(repo, name, node):
    @wraps(node)
    def execute(state):
        if name == "register_run":
            repo.register_run(state["run_id"], state["invoice_pdf_path"])
        repo.log(state["run_id"], name, "enter", {})
        try:
            result = node(state)
        except GraphInterrupt:
            repo.log(state["run_id"], name, "exit", {"paused": True})
            raise
        except Exception as exc:
            repo.log(state["run_id"], name, "exit", {"error": type(exc).__name__})
            raise
        repo.log(state["run_id"], name, "exit", {})
        return result

    return execute


class AuditNodes:
    def __init__(self, repo, extraction, explanation, policy):
        self.repo = repo
        self.extraction = extraction
        self.explanation = explanation
        self.policy = policy

    def context(self, state):
        return AuditContext.model_validate(state["context"])

    def register_run(self, state):
        self.repo.register_run(state["run_id"], state["invoice_pdf_path"])
        return {"status": "PROCESSING"}

    def extract_invoice(self, state):
        text, invoice, scores = self.extraction.extract(state["invoice_pdf_path"])
        return {"raw_text": text, "invoice": invoice.model_dump(mode="json"), "confidences": scores}

    def load_context(self, state):
        ctx = self.repo.load_context(
            Invoice.model_validate(state["invoice"]),
            state["run_id"],
            self.policy,
            state["confidences"],
        )
        return {"context": ctx.model_dump(mode="json")}

    def confidence_gate(self, state):
        ctx = self.context(state)
        findings = [] if missing_fields(ctx) else low_confidence(ctx)
        return {
            "findings": [{"__reset__": True}, *[f.model_dump(mode="json") for f in findings]],
            "next_step": "field_verify" if findings else "dispatch_rules",
            "route": "FIELD_VERIFY" if findings else state.get("route"),
        }

    def check(self, group):
        def execute(state):
            ctx = self.context(state)
            if group == "check_duplicate" and not missing_fields(ctx):
                flags = self.repo.reserve(ctx.invoice, state["run_id"])
                ctx = ctx.model_copy(update=flags)
            findings = run_group(group, ctx)
            if group == "check_duplicate" and ctx.gr_reserved_by_other and not missing_fields(ctx):
                findings += [f for f in check_documents(ctx) if f.code == "GR_ALREADY_INVOICED"]
            return {"findings": [f.model_dump(mode="json") for f in findings]}

        return execute

    def aggregate_findings(self, state):
        unique = {}
        for value in state.get("findings", []):
            finding = Finding.model_validate(value)
            unique[(finding.rule_id, finding.code, finding.field)] = finding
        findings = sorted(
            unique.values(),
            key=lambda f: (f.severity != "blocking", f.rule_id, f.code, f.field or ""),
        )
        self.repo.save_findings(state["run_id"], state.get("revision", 0), findings)
        ctx = self.context(state)
        variance = None if missing_fields(ctx) else variance_entry(ctx)
        if variance:
            self.repo.log(
                state["run_id"],
                "aggregate_findings",
                "rounding_variance",
                variance.model_dump(mode="json"),
            )
        return {
            "findings": [{"__reset__": True}, *[f.model_dump(mode="json") for f in findings]],
            "variance": variance.model_dump(mode="json") if variance else None,
        }

    def explain_findings(self, state):
        if not state.get("findings"):
            return {"explanation": None}
        text, error = self.explanation.for_review(
            Invoice.model_validate(state["invoice"]),
            [Finding.model_validate(f) for f in state["findings"]],
            state["context"],
        )
        if error:
            self.repo.log(
                state["run_id"],
                "explain_findings",
                "explanation_unavailable",
                {"error": error, "source": "rule_messages"},
            )
        return {"explanation": text}

    def route(self, state):
        route = decide(
            [Finding.model_validate(f) for f in state["findings"]],
            Invoice.model_validate(state["invoice"]).total_incl_tax,
            self.policy,
        )
        return {
            "route": route.value,
            "next_step": "finalize"
            if route == Route.AUTO_APPROVE
            else {
                Route.AP_REVIEW: "ap_review",
                Route.FIELD_VERIFY: "field_verify",
                Route.LEVEL1_APPROVAL: "level1_approval",
            }[route],
        }

    def review(self, kind):
        def execute(state):
            request = make_request(kind, state)
            payload = ResumePayload.model_validate(interrupt(request.model_dump(mode="json")))
            validate_resume(request, payload, state)
            result = apply_review(kind, state, payload, self.policy)
            self.repo.save_decision(state["run_id"], result["decisions"][0])
            return result

        return execute

    def finalize(self, state):
        status = "APPROVED" if state["status"] == "PROCESSING" else state["status"]
        invoice = Invoice.model_validate(state["invoice"])
        ctx = self.context(state)
        self.repo.finalize(
            state["run_id"],
            invoice,
            status,
            VarianceEntry.model_validate(state["variance"]) if state.get("variance") else None,
            ctx.vendor.vendor_id if ctx.vendor else None,
        )
        return {"status": status}
