"""SQLite schema. Decimal values are stored as TEXT without float coercion."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1), buyer_name TEXT NOT NULL, buyer_tax_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id TEXT PRIMARY KEY, vendor_name TEXT NOT NULL, tax_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('active', 'blocked'))
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
    currency TEXT NOT NULL CHECK (currency = 'CNY'), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS po_lines (
    po_number TEXT NOT NULL REFERENCES purchase_orders(po_number), po_line_no INTEGER NOT NULL,
    sku TEXT NOT NULL, description TEXT NOT NULL, quantity TEXT NOT NULL,
    unit_price_excl_tax TEXT NOT NULL, tax_rate TEXT NOT NULL,
    PRIMARY KEY (po_number, po_line_no), UNIQUE (po_number, sku)
);
CREATE TABLE IF NOT EXISTS goods_receipts (
    gr_number TEXT PRIMARY KEY, po_number TEXT NOT NULL REFERENCES purchase_orders(po_number),
    received_date TEXT NOT NULL, invoiced_by_run_id TEXT
);
CREATE TABLE IF NOT EXISTS gr_lines (
    gr_number TEXT NOT NULL REFERENCES goods_receipts(gr_number), po_line_no INTEGER NOT NULL,
    quantity_received TEXT NOT NULL, PRIMARY KEY (gr_number, po_line_no)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, pdf_path TEXT NOT NULL, status TEXT NOT NULL, route TEXT,
    snapshot_json TEXT, version INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_tax_id TEXT NOT NULL, invoice_number TEXT NOT NULL, gr_number TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    state TEXT NOT NULL CHECK (state IN ('active', 'released'))
);
CREATE UNIQUE INDEX IF NOT EXISTS active_invoice_reservation
    ON invoice_reservations(seller_tax_id, invoice_number) WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS active_gr_reservation
    ON invoice_reservations(gr_number) WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS active_run_reservation
    ON invoice_reservations(run_id) WHERE state = 'active';
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
    round INTEGER NOT NULL, rule_id TEXT NOT NULL, code TEXT NOT NULL, severity TEXT NOT NULL,
    expected TEXT, actual TEXT, message TEXT NOT NULL, field TEXT
);
CREATE INDEX IF NOT EXISTS findings_run_round ON findings(run_id, round);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
    decision_id TEXT NOT NULL, node TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
    note TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
    UNIQUE (run_id, decision_id)
);
CREATE TABLE IF NOT EXISTS ap_ledger (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), seller_tax_id TEXT NOT NULL,
    invoice_number TEXT NOT NULL, total_excl_tax TEXT NOT NULL, total_tax TEXT NOT NULL,
    total_incl_tax TEXT NOT NULL, approved_at TEXT NOT NULL,
    UNIQUE (seller_tax_id, invoice_number)
);
CREATE TABLE IF NOT EXISTS variance_entries (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), invoice_number TEXT NOT NULL,
    amount TEXT NOT NULL, account TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_queue (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), vendor_id TEXT,
    amount TEXT NOT NULL, status TEXT NOT NULL CHECK (status = 'ready')
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
    node TEXT NOT NULL, event TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_run ON audit_log(run_id, id);
"""
