"""Shared domain values; money stays Decimal until JSON serialization."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CompanyProfile(DomainModel):
    buyer_name: str
    buyer_tax_id: str = Field(min_length=18, max_length=18)


class Vendor(DomainModel):
    vendor_id: str
    vendor_name: str
    tax_id: str
    status: Literal["active", "blocked"] = "active"


class POLine(DomainModel):
    po_line_no: int
    sku: str
    description: str
    quantity: Decimal
    unit_price_excl_tax: Decimal
    tax_rate: Decimal


class PurchaseOrder(DomainModel):
    po_number: str
    vendor_id: str
    currency: Literal["CNY"] = "CNY"
    created_at: date
    lines: list[POLine]


class GRLine(DomainModel):
    po_line_no: int
    quantity_received: Decimal


class GoodsReceipt(DomainModel):
    gr_number: str
    po_number: str
    received_date: date
    lines: list[GRLine]
    invoiced_by_run_id: str | None = None


class InvoiceLine(DomainModel):
    line_no: int
    sku: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    unit_price_excl_tax: Decimal | None = None
    amount_excl_tax: Decimal | None = None
    tax_rate: Decimal | None = None
    tax_amount: Decimal | None = None


class Invoice(DomainModel):
    invoice_number: str | None = None
    invoice_date: date | None = None
    seller_name: str | None = None
    seller_tax_id: str | None = None
    buyer_name: str | None = None
    buyer_tax_id: str | None = None
    po_number: str | None = None
    gr_number: str | None = None
    total_excl_tax: Decimal | None = None
    total_tax: Decimal | None = None
    total_incl_tax: Decimal | None = None
    lines: list[InvoiceLine] = Field(default_factory=list)


class ExtractionResult(DomainModel):
    invoice: Invoice
    confidences: dict[str, float] = Field(default_factory=dict)


class VarianceEntry(DomainModel):
    invoice_number: str
    amount: Decimal
    account: str
