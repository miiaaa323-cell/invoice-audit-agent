import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import ROOT, Settings, load_policy
from app.graph.builder import build_graph
from app.graph.state import add_findings
from app.repo.repositories import Repository, ReservationConflict, RunConflict
from app.rules.registry import evaluate
from app.schemas import Invoice, VarianceEntry
from app.services.audit import AuditService
from app.services.explanation import Explanation, ExplanationService, explanation_payload
from app.services.extraction import ExtractionService, cache_extraction, field_paths
from app.services.llm_client import CacheMissError, LLMClient
from app.services.review import ReviewError
from data.generate import write_invoice_pdf


class Runtime:
    def __init__(self, path):
        self.settings = Settings(data_dir=path, cache_dir=path / "cache")
        shutil.copytree(ROOT / "fixtures/llm_cache", self.settings.cache_dir)
        self.repo = Repository(self.settings.db_path)
        self.repo.seed(self.settings.seed_dir)
        self.policy = load_policy()
        self.client = LLMClient("replay", self.settings.cache_dir)
        self.labels = json.loads((ROOT / "data/labels.json").read_text())
        self.open_graph()

    def open_graph(self):
        self.connection = sqlite3.connect(
            str(self.settings.checkpoint_path), check_same_thread=False
        )
        saver = SqliteSaver(self.connection)
        self.graph = build_graph(
            saver,
            self.repo,
            ExtractionService(self.client),
            ExplanationService(self.client),
            self.policy,
        )
        self.service = AuditService(self.graph, self.repo, self.settings, self.policy)

    def sample(self, index):
        return ROOT / "data" / self.labels[index]["invoice_file"]

    def invoice(self, index):
        return Invoice.model_validate(self.labels[index]["truth"])

    def prepare(self, invoice, confidences=None, flags=()):
        path = (
            self.settings.data_dir
            / f"custom-{len(list(self.settings.data_dir.glob('custom-*')))}.pdf"
        )
        write_invoice_pdf(path, invoice)
        scores = confidences or {p: 0.99 for p in field_paths(invoice)}
        cache_extraction(self.client, path, invoice, scores)
        ctx = self.repo.load_context(invoice, "fixture", self.policy, scores)
        for update in ({}, *flags):
            findings = evaluate(ctx.model_copy(update=update))
            if findings:
                self.client.cache_response(
                    "explain_findings",
                    explanation_payload(invoice, findings),
                    Explanation,
                    {"text": "票面信息与采购收货记录存在差异，请核对列出的字段及原始凭证。"},
                )
        return path

    def count(self, table, run_id):
        assert table in {
            "ap_ledger",
            "payment_queue",
            "variance_entries",
            "invoice_reservations",
            "decisions",
        }
        with self.repo.db.connection() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()[0]

    def close(self):
        self.connection.close()
        self.repo.close()


@pytest.fixture
def runtime(tmp_path):
    value = Runtime(tmp_path)
    yield value
    value.close()


def test_auto_approval_and_finalization_are_idempotent(runtime):
    result = runtime.service.submit(runtime.sample(0))
    assert result["status"] == "APPROVED"
    assert result["route"] == "AUTO_APPROVE"
    assert result["pending"] is None
    run_id = result["run_id"]
    for _ in range(2):
        runtime.repo.finalize(
            run_id, result["invoice"], "APPROVED", VarianceEntry.model_validate(result["variance"])
        )
    for table in ("ap_ledger", "payment_queue", "variance_entries"):
        assert runtime.count(table, run_id) == 1
    ctx = runtime.repo.load_context(result["invoice"], run_id, runtime.policy)
    assert ctx.gr.invoiced_by_run_id == run_id
    logs = runtime.repo.audit(run_id)
    entered = {row["node"] for row in logs if row["event"] == "enter"}
    exited = {row["node"] for row in logs if row["event"] == "exit"}
    assert entered == exited
    assert {
        "check_duplicate",
        "check_fields_arith",
        "check_parties_docs",
        "check_three_way_match",
    } <= entered


def test_field_verification_reloads_context_and_clears_old_findings(runtime):
    invoice = runtime.invoice(45)
    correct_po = invoice.po_number
    invoice = invoice.model_copy(update={"po_number": "PO-NEEDS-CORRECTION"})
    scores = {p: 0.99 for p in field_paths(invoice)}
    scores["po_number"] = 0.4
    result = runtime.service.submit(runtime.prepare(invoice, scores))
    assert result["pending"]["type"] == "field_verify"
    assert result["pending"]["fields"] == ["po_number"]
    result = runtime.service.resume(
        result["run_id"],
        {
            "action": "confirm_fields",
            "corrected_fields": {"po_number": correct_po},
        },
        "ap_clerk",
    )
    assert result["status"] == "APPROVED"
    assert result["confidences"]["po_number"] == 1
    assert result["invoice"]["po_number"] == correct_po
    assert result["findings"] == []
    assert len(result["decisions"]) == 1


def test_missing_fields_skip_all_rules_except_r01_r02(runtime):
    invoice = runtime.invoice(45).model_copy(update={"invoice_number": None})
    result = runtime.service.submit(runtime.prepare(invoice))
    assert result["pending"]["type"] == "ap_review"
    assert {f["rule_id"] for f in result["findings"]} == {"R01"}
    with pytest.raises(ReviewError, match="不完整"):
        runtime.service.resume(
            result["run_id"], {"action": "approve_with_note", "note": "接受"}, "ap_clerk"
        )
    assert runtime.service.get(result["run_id"])["decisions"] == []


def test_clerk_reject_releases_reservation(runtime):
    result = runtime.service.submit(runtime.sample(160))
    assert "QTY_MISMATCH" in {f["code"] for f in result["findings"]}
    result = runtime.service.resume(
        result["run_id"], {"action": "reject", "note": "收货数量不符"}, "ap_clerk"
    )
    assert result["status"] == "REJECTED"
    assert runtime.count("ap_ledger", result["run_id"]) == 0
    next_run = runtime.service.submit(runtime.sample(160))
    assert "DUPLICATE_INVOICE" not in {f["code"] for f in next_run["findings"]}


def test_clerk_can_accept_difference_with_note(runtime):
    result = runtime.service.submit(runtime.sample(160))
    result = runtime.service.resume(
        result["run_id"],
        {
            "action": "approve_with_note",
            "note": "收货凭证已补充，认可此次差异",
        },
        "ap_clerk",
    )
    assert result["status"] == "APPROVED"
    assert runtime.count("payment_queue", result["run_id"]) == 1


def test_large_invoice_two_level_approval(runtime):
    result = runtime.service.submit(runtime.sample(20))
    assert result["pending"]["type"] == "level1_approval"
    assert runtime.count("ap_ledger", result["run_id"]) == 0
    result = runtime.service.resume(result["run_id"], {"action": "approve"}, "ap_clerk")
    assert result["pending"]["type"] == "level2_approval"
    assert runtime.count("payment_queue", result["run_id"]) == 0
    result = runtime.service.resume(result["run_id"], {"action": "approve"}, "finance_manager")
    assert result["status"] == "APPROVED"
    assert [d["role"] for d in result["decisions"]] == ["ap_clerk", "finance_manager"]


def test_large_clerk_override_still_needs_manager(runtime):
    invoice = runtime.invoice(20).model_copy(update={"buyer_name": "错误抬头"})
    result = runtime.service.submit(runtime.prepare(invoice))
    assert result["pending"]["type"] == "ap_review"
    result = runtime.service.resume(
        result["run_id"],
        {
            "action": "approve_with_note",
            "note": "已核对相关合同，认可差异",
        },
        "ap_clerk",
    )
    assert result["pending"]["type"] == "level2_approval"
    result = runtime.service.resume(result["run_id"], {"action": "approve"}, "finance_manager")
    assert result["status"] == "APPROVED"


@pytest.mark.parametrize("level", [1, 2])
def test_reject_at_each_approval_level(runtime, level):
    result = runtime.service.submit(runtime.sample(20))
    if level == 2:
        result = runtime.service.resume(result["run_id"], {"action": "approve"}, "ap_clerk")
    result = runtime.service.resume(
        result["run_id"],
        {"action": "reject", "note": "采购凭证待核实"},
        "ap_clerk" if level == 1 else "finance_manager",
    )
    assert result["status"] == "REJECTED"
    assert runtime.count("ap_ledger", result["run_id"]) == 0


def test_replacement_pdf_reextracts_and_releases_old_identity(runtime):
    result = runtime.service.submit(runtime.sample(160))
    run_id = result["run_id"]
    original = result["invoice"]
    result = runtime.service.resume(
        run_id, {"action": "request_info", "note": "请提供正确数量的发票"}, "ap_clerk"
    )
    assert result["status"] == "WAITING_INFO"
    assert result["pending"]["type"] == "wait_for_info"
    assert runtime.count("invoice_reservations", run_id) == 1
    result = runtime.service.document(run_id, runtime.sample(45))
    assert result["status"] == "APPROVED"
    assert result["info_round"] == 1
    assert result["findings"] == []
    assert result["invoice"]["invoice_number"] != original["invoice_number"]
    ctx = runtime.repo.load_context(original, "another-run", runtime.policy)
    assert not ctx.duplicate_invoice and not ctx.gr_reserved_by_other


def test_more_than_two_supplements_cancel_and_release(runtime):
    result = runtime.service.submit(runtime.sample(160))
    run_id = result["run_id"]
    for round_no in range(1, 3):
        assert result["pending"]["info_rounds_left"] == 3 - round_no
        result = runtime.service.resume(
            run_id, {"action": "request_info", "note": "请重新核对数量"}, "ap_clerk"
        )
        result = runtime.service.document(run_id, runtime.sample(160))
        assert result["info_round"] == round_no
    assert result["pending"]["info_rounds_left"] == 0
    result = runtime.service.resume(
        run_id, {"action": "request_info", "note": "请重新核对数量"}, "ap_clerk"
    )
    assert result["status"] == "CANCELLED"
    assert result["info_round"] == 2
    assert result["pending"] is None
    assert any(row["event"] == "info_rounds_exhausted" for row in runtime.repo.audit(run_id))
    assert runtime.count("ap_ledger", run_id) == 0
    ctx = runtime.repo.load_context(result["invoice"], "other-run", runtime.policy)
    assert not ctx.duplicate_invoice and not ctx.gr_reserved_by_other


def test_failed_replacement_pdf_keeps_waiting_and_can_be_retried(runtime):
    result = runtime.service.submit(runtime.sample(160))
    run_id = result["run_id"]
    original = result["invoice"]
    waiting = runtime.service.resume(
        run_id, {"action": "request_info", "note": "请提供正确数量的发票"}, "ap_clerk"
    )
    invoice = runtime.invoice(45).model_copy(update={"invoice_number": "UNREADABLE-COPY"})
    unknown = write_invoice_pdf(runtime.settings.data_dir / "unknown-copy.pdf", invoice)
    with pytest.raises(CacheMissError):
        runtime.service.document(run_id, unknown)
    result = runtime.service.get(run_id)
    assert result["status"] == "WAITING_INFO"
    assert result["version"] == waiting["version"]
    assert result["info_round"] == 0
    assert result["pending"]["type"] == "wait_for_info"
    assert result["invoice"] == original
    assert result["error"]["code"] == "CacheMissError"
    assert runtime.count("invoice_reservations", run_id) == 1
    ctx = runtime.repo.load_context(original, "another-run", runtime.policy)
    assert ctx.duplicate_invoice and ctx.gr_reserved_by_other
    assert any(row["event"] == "rolled_back" for row in runtime.repo.audit(run_id))
    result = runtime.service.document(run_id, runtime.sample(45))
    assert result["status"] == "APPROVED"
    assert result["info_round"] == 1
    assert result["error"] is None


def test_failed_approval_returns_to_the_same_task(runtime, monkeypatch):
    result = runtime.service.submit(runtime.sample(20))
    run_id = result["run_id"]

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime.repo, "finalize", unavailable)
    result = runtime.service.resume(run_id, {"action": "approve"}, "ap_clerk")
    with pytest.raises(sqlite3.OperationalError):
        runtime.service.resume(run_id, {"action": "approve"}, "finance_manager")
    monkeypatch.undo()
    result = runtime.service.get(run_id)
    assert result["status"] == "PROCESSING"
    assert result["pending"]["type"] == "level2_approval"
    assert runtime.count("ap_ledger", run_id) == 0
    result = runtime.service.resume(run_id, {"action": "approve"}, "finance_manager")
    assert result["status"] == "APPROVED"
    assert runtime.count("ap_ledger", run_id) == 1


def test_resume_after_recreating_graph_and_sqlite_connection(runtime):
    result = runtime.service.submit(runtime.sample(20))
    run_id = result["run_id"]
    runtime.connection.close()
    runtime.open_graph()
    assert runtime.service.get(run_id)["pending"]["type"] == "level1_approval"
    result = runtime.service.resume(run_id, {"action": "approve"}, "ap_clerk")
    runtime.connection.close()
    runtime.open_graph()
    result = runtime.service.resume(run_id, {"action": "approve"}, "finance_manager")
    assert result["status"] == "APPROVED"


@pytest.mark.parametrize(
    "body,role",
    [
        ({"action": "approve"}, "finance_manager"),
        ({"action": "request_info", "note": "补料"}, "ap_clerk"),
        ({"action": "reject", "note": " "}, "ap_clerk"),
        ({"action": "approve", "corrected_fields": {"invoice_number": "changed"}}, "ap_clerk"),
    ],
)
def test_invalid_resume_does_not_mutate_state(runtime, body, role):
    result = runtime.service.submit(runtime.sample(20))
    before_record = runtime.repo.get_run(result["run_id"])
    before_log = runtime.repo.audit(result["run_id"])
    with pytest.raises(ReviewError):
        runtime.service.resume(result["run_id"], body, role)
    assert runtime.repo.get_run(result["run_id"]) == before_record
    assert runtime.repo.audit(result["run_id"]) == before_log
    assert runtime.service.get(result["run_id"]) == result


def test_concurrent_resume_only_one_request_advances(runtime):
    result = runtime.service.submit(runtime.sample(20))
    barrier = Barrier(2)

    def approve():
        barrier.wait()
        try:
            return runtime.service.resume(
                result["run_id"], {"action": "approve", "version": result["version"]}, "ap_clerk"
            )["pending"]["type"]
        except (RunConflict, ReviewError):
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: approve(), range(2)))
    assert sorted(responses) == ["conflict", "level2_approval"]
    assert runtime.count("decisions", result["run_id"]) == 1
    assert runtime.count("ap_ledger", result["run_id"]) == 0


def test_concurrent_duplicate_submission_and_override_guard(runtime):
    path = runtime.prepare(
        runtime.invoice(160),
        flags=[
            {
                "duplicate_invoice": True,
                "gr_reserved_by_other": True,
            }
        ],
    )
    barrier = Barrier(2)

    def submit():
        barrier.wait()
        return runtime.service.submit(path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    duplicates = [r for r in results if "DUPLICATE_INVOICE" in {f["code"] for f in r["findings"]}]
    assert len(duplicates) == 1
    loser = duplicates[0]
    before = runtime.repo.get_run(loser["run_id"])
    with pytest.raises(ReservationConflict):
        runtime.service.resume(
            loser["run_id"], {"action": "approve_with_note", "note": "接受差异"}, "ap_clerk"
        )
    assert runtime.repo.get_run(loser["run_id"]) == before
    assert runtime.count("ap_ledger", loser["run_id"]) == 0


def test_cache_failure_releases_occupancy_and_leaves_auditable_failure(runtime):
    invoice = runtime.invoice(45).model_copy(update={"invoice_number": "UNKNOWN-INVOICE"})
    path = write_invoice_pdf(runtime.settings.data_dir / "unknown.pdf", invoice)
    with pytest.raises(CacheMissError):
        runtime.service.submit(path)
    record = runtime.repo.list_runs()[0]
    assert record["status"] == "REJECTED"
    assert record["locked"] == 0
    assert runtime.service.get(record["run_id"])["error"]["code"] == "CacheMissError"


def test_explanation_outage_preserves_rule_decision_and_review(runtime):
    invoice = runtime.invoice(160).model_copy(update={"invoice_number": "NEW-EXPLANATION"})
    path = write_invoice_pdf(runtime.settings.data_dir / "no-explanation.pdf", invoice)
    cache_extraction(runtime.client, path, invoice)
    result = runtime.service.submit(path)
    assert result["status"] == "PROCESSING"
    assert result["pending"]["type"] == "ap_review"
    assert "QTY_MISMATCH" in {f["code"] for f in result["findings"]}
    assert result["explanation"] and len(result["explanation"]) <= 120
    assert any(
        row["event"] == "explanation_unavailable" for row in runtime.repo.audit(result["run_id"])
    )
    assert runtime.count("ap_ledger", result["run_id"]) == 0


def test_findings_reducer_reset_does_not_retain_previous_round():
    assert add_findings([{"code": "old"}], [{"__reset__": True}, {"code": "new"}]) == [
        {"code": "new"}
    ]
    assert add_findings([{"code": "old"}], {"__reset__": True}) == []
