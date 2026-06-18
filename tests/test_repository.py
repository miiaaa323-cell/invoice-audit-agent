"""Database guarantees that remain true even when graph nodes are retried."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest

from app.config import Policy
from app.repo.repositories import Repository, ReservationConflict, RunConflict
from app.schemas import Invoice, VarianceEntry


@pytest.fixture
def seeded_repo(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    documents = {
        "company.json": {"buyer_name": "示例科技有限公司", "buyer_tax_id": "91310000MA1FL0000X"},
        "vendors.json": [
            {"vendor_id": "V1", "vendor_name": "示例供应商", "tax_id": "TAX1", "status": "active"}
        ],
        "purchase_orders.json": [
            {
                "po_number": "PO1",
                "vendor_id": "V1",
                "currency": "CNY",
                "created_at": "2026-06-17",
                "lines": [
                    {
                        "po_line_no": 1,
                        "sku": "SKU1",
                        "description": "材料",
                        "quantity": "2",
                        "unit_price_excl_tax": "10.0001",
                        "tax_rate": "0.13",
                    }
                ],
            }
        ],
        "goods_receipts.json": [
            {
                "gr_number": number,
                "po_number": "PO1",
                "received_date": "2026-06-18",
                "lines": [{"po_line_no": 1, "quantity_received": "2"}],
            }
            for number in ("GR1", "GR2", "GR3")
        ],
    }
    for filename, document in documents.items():
        (seed / filename).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    repo = Repository(tmp_path / "audit.sqlite")
    repo.seed(seed)
    yield repo, seed
    repo.close()


@pytest.fixture
def invoice():
    return Invoice(
        invoice_number="INV1",
        seller_tax_id="TAX1",
        gr_number="GR1",
        po_number="PO1",
        total_excl_tax="20.00",
        total_tax="2.60",
        total_incl_tax="22.60",
    )


def count(repo, table):
    with repo.db.connection() as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_context_preserves_four_decimal_unit_price(seeded_repo, invoice):
    repo, _ = seeded_repo
    context = repo.load_context(invoice, "run1", Policy(), {"invoice_number": 0.9})
    assert context.po.lines[0].unit_price_excl_tax == Decimal("10.0001")
    assert context.gr.lines[0].quantity_received == Decimal("2")
    assert context.vendor.vendor_id == "V1"
    assert context.company == Policy().company
    assert context.confidences == {"invoice_number": 0.9}


def test_missing_context_leaves_entities_none(seeded_repo):
    repo, _ = seeded_repo
    context = repo.load_context(Invoice(), "run1", Policy())
    assert context.po is context.gr is context.vendor is None


def test_repeat_reservation_same_run_is_valid(seeded_repo, invoice):
    repo, _ = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    assert not any(repo.reserve(invoice, "run1").values())
    assert not any(repo.reserve(invoice, "run1").values())
    assert count(repo, "invoice_reservations") == 1


def test_concurrent_duplicate_only_one_wins(seeded_repo, invoice):
    repo, _ = seeded_repo
    barrier = Barrier(2)
    for run_id in ("run1", "run2"):
        repo.register_run(run_id, "invoice.pdf")

    def claim(run_id):
        barrier.wait()
        return repo.reserve(invoice, run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("run1", "run2")))
    assert sorted(result["duplicate_invoice"] for result in claims) == [False, True]
    assert sorted(result["gr_reserved_by_other"] for result in claims) == [False, True]
    assert count(repo, "invoice_reservations") == 1


def test_changed_document_releases_old_number_and_receipt(seeded_repo, invoice):
    repo, _ = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    repo.register_run("run2", "invoice.pdf")
    repo.reserve(invoice, "run1")
    replacement = invoice.model_copy(update={"invoice_number": "INV2", "gr_number": "GR2"})
    assert not any(repo.reserve(replacement, "run1").values())
    assert not any(repo.reserve(invoice, "run2").values())
    with repo.db.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM invoice_reservations WHERE state = 'active'"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("status", ["REJECTED", "CANCELLED"])
def test_reject_and_cancel_release_identifiers(seeded_repo, invoice, status):
    repo, _ = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    repo.register_run("run2", "invoice.pdf")
    repo.reserve(invoice, "run1")
    repo.finalize("run1", invoice, status)
    assert not any(repo.reserve(invoice, "run2").values())
    assert count(repo, "ap_ledger") == count(repo, "payment_queue") == 0


def test_waiting_info_keeps_identifiers(seeded_repo, invoice):
    repo, _ = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    repo.register_run("run2", "invoice.pdf")
    repo.reserve(invoice, "run1")
    repo.save_run("run1", status="WAITING_INFO")
    assert repo.reserve(invoice, "run2")["duplicate_invoice"]


def test_finalize_approved_is_atomic_idempotent_and_stores_text(seeded_repo, invoice):
    repo, seed = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    repo.reserve(invoice, "run1")
    variance = VarianceEntry(invoice_number="INV1", amount="-0.01", account="ROUNDING_VARIANCE")
    for _ in range(2):
        repo.finalize("run1", invoice, "APPROVED", variance, "V1")
    for table in ("ap_ledger", "payment_queue", "variance_entries"):
        assert count(repo, table) == 1
    with repo.db.connection() as connection:
        assert tuple(
            connection.execute(
                "SELECT total_excl_tax, typeof(total_excl_tax) FROM ap_ledger"
            ).fetchone()
        ) == ("20.00", "text")
        assert (
            connection.execute(
                "SELECT invoiced_by_run_id FROM goods_receipts WHERE gr_number = 'GR1'"
            ).fetchone()[0]
            == "run1"
        )
        assert connection.execute("SELECT amount FROM variance_entries").fetchone()[0] == "-0.01"
    repo.seed(seed)
    assert repo.load_context(invoice, "run1", Policy()).gr.invoiced_by_run_id == "run1"
    assert [event["event"] for event in repo.audit("run1")] == ["rounding_variance"]


def test_finalize_conflict_cannot_override_duplicate(seeded_repo, invoice):
    repo, _ = seeded_repo
    for run_id in ("run1", "run2"):
        repo.register_run(run_id, "invoice.pdf")
    repo.reserve(invoice, "run1")
    repo.finalize("run1", invoice, "APPROVED", vendor_id="V1")
    with pytest.raises(ReservationConflict, match="DUPLICATE_INVOICE"):
        repo.finalize("run2", invoice, "APPROVED", vendor_id="V1")
    assert repo.get_run("run2")["status"] == "PROCESSING"
    assert count(repo, "ap_ledger") == count(repo, "payment_queue") == 1


def test_finalize_conflict_cannot_override_reserved_gr(seeded_repo, invoice):
    repo, _ = seeded_repo
    for run_id in ("run1", "run2"):
        repo.register_run(run_id, "invoice.pdf")
    repo.reserve(invoice, "run1")
    other_number = invoice.model_copy(update={"invoice_number": "INV2"})
    with pytest.raises(ReservationConflict, match="GR_ALREADY_INVOICED"):
        repo.finalize("run2", other_number, "APPROVED", vendor_id="V1")
    assert count(repo, "ap_ledger") == count(repo, "payment_queue") == 0


def test_payment_write_failure_rolls_back_whole_finalization(seeded_repo, invoice):
    repo, _ = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    repo.reserve(invoice, "run1")
    with repo.db.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_payment BEFORE INSERT ON payment_queue "
            "BEGIN SELECT RAISE(ABORT, 'payment failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="payment failure"):
        repo.finalize("run1", invoice, "APPROVED", vendor_id="V1")
    assert count(repo, "ap_ledger") == count(repo, "payment_queue") == 0
    assert repo.get_run("run1")["status"] == "PROCESSING"
    assert repo.load_context(invoice, "run1", Policy()).gr.invoiced_by_run_id is None
    with repo.db.connection() as connection:
        assert (
            connection.execute("SELECT state FROM invoice_reservations").fetchone()[0] == "active"
        )


def test_finalized_status_cannot_change(seeded_repo, invoice):
    repo, _ = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    repo.finalize("run1", invoice, "REJECTED")
    with pytest.raises(RunConflict):
        repo.finalize("run1", invoice, "APPROVED")
    with pytest.raises(RunConflict):
        repo.save_run("run1", status="PROCESSING")


def test_concurrent_resume_has_one_lock_winner(seeded_repo):
    repo, _ = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    barrier = Barrier(2)

    def claim(_):
        barrier.wait()
        try:
            repo.acquire_run("run1", 0)
        except RunConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, (1, 2))) == [False, True]
    repo.release_run("run1")
    assert repo.get_run("run1")["version"] == 1
    with pytest.raises(RunConflict):
        repo.acquire_run("run1", 0)
    repo.acquire_run("run1", 1)
    repo.release_run("run1", success=False)
    assert repo.get_run("run1")["version"] == 1


def test_snapshots_audit_and_idempotent_decisions(seeded_repo):
    repo, _ = seeded_repo
    repo.register_run("run1", "invoice.pdf")
    repo.save_run("run1", route="AP_REVIEW", snapshot_json={"total": "20.00"})
    decision = {
        "decision_id": "decision1",
        "node": "ap_review",
        "role": "ap_clerk",
        "action": "reject",
        "note": "抬头错误",
        "created_at": "2026-06-17T10:00:00+00:00",
    }
    repo.save_decision("run1", decision)
    repo.save_decision("run1", decision)
    repo.log("run1", "ap_review", "enter", {"amount": Decimal("20.00")})
    assert count(repo, "decisions") == 1
    assert json.loads(repo.get_run("run1")["snapshot_json"]) == {"total": "20.00"}
    assert repo.audit("run1")[0]["payload"] == {"amount": "20.00"}
    assert len(repo.list_runs("PROCESSING")) == 1
    with pytest.raises(ValueError):
        repo.save_run("run1", locked=1)


def test_sqlite_partial_indexes_reject_direct_duplicate_claim(seeded_repo):
    repo, _ = seeded_repo
    for run_id in ("run1", "run2"):
        repo.register_run(run_id, "invoice.pdf")
    with repo.db.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO invoice_reservations (seller_tax_id, invoice_number, gr_number, run_id, state) VALUES ('TAX1', 'INV1', 'GR1', 'run1', 'active')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with repo.db.connection(write=True) as connection:
            connection.execute(
                "INSERT INTO invoice_reservations (seller_tax_id, invoice_number, gr_number, run_id, state) VALUES ('TAX1', 'INV1', 'GR2', 'run2', 'active')"
            )
