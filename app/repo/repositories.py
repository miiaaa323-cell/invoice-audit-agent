"""Persistence for ERP data, reservations, review runs and approved payables."""

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import Policy
from app.repo.db import Database
from app.schemas import CompanyProfile, GoodsReceipt, Invoice, PurchaseOrder, VarianceEntry, Vendor
from app.services.money import q2


class RunConflict(RuntimeError):
    """A run changed or is already being advanced by another request."""


class ReservationConflict(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (Decimal, date, datetime, Path)):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


class Repository:
    def __init__(self, db_path: Path | str):
        self.db = Database(db_path)
        self.db_path = db_path

    def seed(self, seed_dir: Path | str):
        seed_dir = Path(seed_dir)
        company = CompanyProfile.model_validate_json((seed_dir / "company.json").read_text())
        vendors = [
            Vendor.model_validate(v)
            for v in json.loads((seed_dir / "vendors.json").read_text(encoding="utf-8"))
        ]
        orders = [
            PurchaseOrder.model_validate(v)
            for v in json.loads((seed_dir / "purchase_orders.json").read_text(encoding="utf-8"))
        ]
        receipts = [
            GoodsReceipt.model_validate(v)
            for v in json.loads((seed_dir / "goods_receipts.json").read_text(encoding="utf-8"))
        ]
        with self.db.connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO company_profile VALUES (1, ?, ?)",
                (company.buyer_name, company.buyer_tax_id),
            )
            for vendor in vendors:
                connection.execute(
                    "INSERT OR IGNORE INTO vendors VALUES (?, ?, ?, ?)",
                    (
                        vendor.vendor_id,
                        vendor.vendor_name,
                        vendor.tax_id,
                        vendor.status,
                    ),
                )
            for po in orders:
                connection.execute(
                    "INSERT OR IGNORE INTO purchase_orders VALUES (?, ?, ?, ?)",
                    (
                        po.po_number,
                        po.vendor_id,
                        po.currency,
                        po.created_at.isoformat(),
                    ),
                )
                for line in po.lines:
                    connection.execute(
                        "INSERT OR IGNORE INTO po_lines VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            po.po_number,
                            line.po_line_no,
                            line.sku,
                            line.description,
                            str(line.quantity),
                            str(line.unit_price_excl_tax),
                            str(line.tax_rate),
                        ),
                    )
            for gr in receipts:
                connection.execute(
                    "INSERT OR IGNORE INTO goods_receipts VALUES (?, ?, ?, ?)",
                    (
                        gr.gr_number,
                        gr.po_number,
                        gr.received_date.isoformat(),
                        gr.invoiced_by_run_id,
                    ),
                )
                for line in gr.lines:
                    connection.execute(
                        "INSERT OR IGNORE INTO gr_lines VALUES (?, ?, ?)",
                        (
                            gr.gr_number,
                            line.po_line_no,
                            str(line.quantity_received),
                        ),
                    )

    def register_run(self, run_id: str, pdf_path: Path | str):
        timestamp = now()
        with self.db.connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO runs "
                "(run_id, pdf_path, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, str(pdf_path), "PROCESSING", timestamp, timestamp),
            )

    def save_run(self, run_id: str, **fields):
        allowed = {"pdf_path", "status", "route", "snapshot_json"}
        invalid = fields.keys() - allowed
        if invalid:
            raise ValueError(f"Unsupported run fields: {sorted(invalid)}")
        if not fields:
            return
        if "snapshot_json" in fields and not isinstance(fields["snapshot_json"], (str, type(None))):
            fields["snapshot_json"] = _json(fields["snapshot_json"])
        fields["updated_at"] = now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.db.connection(write=True) as connection:
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] in {"APPROVED", "REJECTED", "CANCELLED"}:
                if "status" in fields and fields["status"] != row["status"]:
                    raise RunConflict("A finalized run cannot change status")
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?", (*fields.values(), run_id)
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return dict(row) if row is not None else None

    def list_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            query = "SELECT * FROM runs"
            values = ()
            if status:
                query += " WHERE status = ?"
                values = (status,)
            rows = connection.execute(
                query + " ORDER BY created_at DESC, run_id", values
            ).fetchall()
            return [dict(row) for row in rows]

    def acquire_run(self, run_id: str, version: int):
        with self.db.connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE runs SET locked = 1 WHERE run_id = ? AND version = ? AND locked = 0",
                (run_id, version),
            )
            if cursor.rowcount != 1:
                raise RunConflict("The run is busy or its version has changed")

    def release_run(self, run_id: str, success: bool = True):
        with self.db.connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE runs SET locked = 0, version = version + ? WHERE run_id = ? AND locked = 1",
                (int(success), run_id),
            )
            if cursor.rowcount != 1:
                raise RunConflict("The run does not hold an execution lock")

    @staticmethod
    def _conflicts(connection, invoice: Invoice, run_id: str) -> dict[str, bool]:
        invoice_args = (invoice.seller_tax_id, invoice.invoice_number, run_id)
        duplicate = (
            connection.execute(
                "SELECT 1 FROM ap_ledger WHERE seller_tax_id = ? AND invoice_number = ? AND run_id != ? "
                "UNION ALL SELECT 1 FROM invoice_reservations "
                "WHERE seller_tax_id = ? AND invoice_number = ? AND run_id != ? AND state = 'active' "
                "LIMIT 1",
                (*invoice_args, *invoice_args),
            ).fetchone()
            is not None
        )
        gr_args = (invoice.gr_number, run_id)
        gr_conflict = (
            connection.execute(
                "SELECT 1 FROM goods_receipts WHERE gr_number = ? AND invoiced_by_run_id IS NOT NULL "
                "AND invoiced_by_run_id != ? UNION ALL SELECT 1 FROM invoice_reservations "
                "WHERE gr_number = ? AND run_id != ? AND state = 'active' LIMIT 1",
                (*gr_args, *gr_args),
            ).fetchone()
            is not None
        )
        return {"duplicate_invoice": duplicate, "gr_reserved_by_other": gr_conflict}

    def reserve(self, invoice: Invoice | dict, run_id: str) -> dict[str, bool]:
        invoice = Invoice.model_validate(invoice)
        with self.db.connection(write=True) as connection:
            run = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] in {"APPROVED", "REJECTED", "CANCELLED"}:
                raise RunConflict("A finalized run cannot reserve an invoice")
            existing = connection.execute(
                "SELECT * FROM invoice_reservations WHERE run_id = ? AND state = 'active'",
                (run_id,),
            ).fetchone()
            identifiers = (invoice.seller_tax_id, invoice.invoice_number, invoice.gr_number)
            if existing is not None and identifiers == (
                existing["seller_tax_id"],
                existing["invoice_number"],
                existing["gr_number"],
            ):
                return self._conflicts(connection, invoice, run_id)
            connection.execute(
                "UPDATE invoice_reservations SET state = 'released' WHERE run_id = ? AND state = 'active'",
                (run_id,),
            )
            conflicts = self._conflicts(connection, invoice, run_id)
            if all(identifiers) and not any(conflicts.values()):
                connection.execute(
                    "INSERT INTO invoice_reservations "
                    "(seller_tax_id, invoice_number, gr_number, run_id, state) VALUES (?, ?, ?, ?, 'active')",
                    (*identifiers, run_id),
                )
            return conflicts

    def load_context(
        self,
        invoice: Invoice | dict,
        run_id: str,
        policy: Policy,
        confidences: dict[str, float] | None = None,
    ):
        from app.rules.base import AuditContext

        invoice = Invoice.model_validate(invoice)
        with self.db.connection() as connection:
            po_row = connection.execute(
                "SELECT * FROM purchase_orders WHERE po_number = ?", (invoice.po_number,)
            ).fetchone()
            po = None
            if po_row is not None:
                po_data = dict(po_row)
                po_data["lines"] = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT po_line_no, sku, description, quantity, unit_price_excl_tax, tax_rate "
                        "FROM po_lines WHERE po_number = ? ORDER BY po_line_no",
                        (invoice.po_number,),
                    )
                ]
                po = PurchaseOrder.model_validate(po_data)
            gr_row = connection.execute(
                "SELECT * FROM goods_receipts WHERE gr_number = ?", (invoice.gr_number,)
            ).fetchone()
            gr = None
            if gr_row is not None:
                gr_data = dict(gr_row)
                gr_data["lines"] = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT po_line_no, quantity_received FROM gr_lines "
                        "WHERE gr_number = ? ORDER BY po_line_no",
                        (invoice.gr_number,),
                    )
                ]
                gr = GoodsReceipt.model_validate(gr_data)
            vendor_row = connection.execute(
                "SELECT * FROM vendors WHERE tax_id = ?", (invoice.seller_tax_id,)
            ).fetchone()
            vendor = Vendor.model_validate(dict(vendor_row)) if vendor_row else None
            conflicts = self._conflicts(connection, invoice, run_id)
        return AuditContext(
            invoice=invoice,
            confidences=confidences or {},
            policy=policy,
            company=policy.company,
            po=po,
            gr=gr,
            vendor=vendor,
            run_id=run_id,
            **conflicts,
        )

    def save_findings(self, run_id: str, round: int, findings: list):
        with self.db.connection(write=True) as connection:
            connection.execute(
                "DELETE FROM findings WHERE run_id = ? AND round = ?", (run_id, round)
            )
            for finding in findings:
                data = (
                    finding.model_dump(mode="json") if isinstance(finding, BaseModel) else finding
                )
                connection.execute(
                    "INSERT INTO findings "
                    "(run_id, round, rule_id, code, severity, expected, actual, message, field) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        round,
                        data["rule_id"],
                        data["code"],
                        data["severity"],
                        _json(data.get("expected")),
                        _json(data.get("actual")),
                        data["message"],
                        data.get("field"),
                    ),
                )

    def save_decision(self, run_id: str, decision: dict | BaseModel):
        data = (
            decision.model_dump(mode="json") if isinstance(decision, BaseModel) else dict(decision)
        )
        data.setdefault("created_at", now())
        fingerprint = hashlib.sha256(_json(data).encode()).hexdigest()
        with self.db.connection(write=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO decisions "
                "(run_id, decision_id, node, role, action, note, created_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    data.get("decision_id", fingerprint),
                    data["node"],
                    data["role"],
                    data["action"],
                    data.get("note"),
                    data["created_at"],
                    _json(data),
                ),
            )

    def log(self, run_id: str, node: str, event: str, payload=None):
        with self.db.connection(write=True) as connection:
            connection.execute(
                "INSERT INTO audit_log (run_id, node, event, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, node, event, _json(payload or {}), now()),
            )

    def audit(self, run_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def finalize(
        self,
        run_id: str,
        invoice: Invoice | dict,
        status: str,
        variance: VarianceEntry | dict | None = None,
        vendor_id: str | None = None,
    ):
        if status not in {"APPROVED", "REJECTED", "CANCELLED"}:
            raise ValueError("Final status must be APPROVED, REJECTED or CANCELLED")
        invoice = Invoice.model_validate(invoice)
        if variance is not None:
            variance = VarianceEntry.model_validate(variance)
        timestamp = now()
        with self.db.connection(write=True) as connection:
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] in {"APPROVED", "REJECTED", "CANCELLED"}:
                if row["status"] != status:
                    raise RunConflict("A finalized run cannot change status")
                return
            if status == "APPROVED":
                conflicts = self._conflicts(connection, invoice, run_id)
                if conflicts["duplicate_invoice"]:
                    raise ReservationConflict("DUPLICATE_INVOICE")
                if conflicts["gr_reserved_by_other"]:
                    raise ReservationConflict("GR_ALREADY_INVOICED")
                amounts = (invoice.total_excl_tax, invoice.total_tax, invoice.total_incl_tax)
                if (
                    not invoice.invoice_number
                    or not invoice.seller_tax_id
                    or any(v is None for v in amounts)
                ):
                    raise ValueError("An approved payable needs an invoice identity and totals")
                connection.execute(
                    "INSERT INTO ap_ledger VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        invoice.seller_tax_id,
                        invoice.invoice_number,
                        *(str(q2(value)) for value in amounts),
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO payment_queue VALUES (?, ?, ?, 'ready')",
                    (
                        run_id,
                        vendor_id,
                        str(q2(invoice.total_incl_tax)),
                    ),
                )
                if variance is not None and variance.amount:
                    connection.execute(
                        "INSERT INTO variance_entries VALUES (?, ?, ?, ?)",
                        (
                            run_id,
                            variance.invoice_number,
                            str(q2(variance.amount)),
                            variance.account,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO audit_log (run_id, node, event, payload_json, created_at) "
                        "VALUES (?, 'finalize', 'rounding_variance', ?, ?)",
                        (run_id, _json(variance), timestamp),
                    )
                connection.execute(
                    "UPDATE goods_receipts SET invoiced_by_run_id = ? WHERE gr_number = ?",
                    (run_id, invoice.gr_number),
                )
            connection.execute(
                "UPDATE invoice_reservations SET state = 'released' WHERE run_id = ? AND state = 'active'",
                (run_id,),
            )
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, timestamp, run_id),
            )

    def close(self):
        self.db.close()
