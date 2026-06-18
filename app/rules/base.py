"""Pure rule inputs and explainable findings."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import Policy
from app.schemas import CompanyProfile, GoodsReceipt, Invoice, PurchaseOrder, Vendor


class Severity(str, Enum):
    BLOCKING = "blocking"
    REVIEW = "review"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    code: str
    severity: Severity = Severity.BLOCKING
    expected: Any = None
    actual: Any = None
    message: str
    field: str | None = None


class AuditContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice: Invoice
    confidences: dict[str, float] = Field(default_factory=dict)
    policy: Policy = Field(default_factory=Policy)
    company: CompanyProfile | None = None
    po: PurchaseOrder | None = None
    gr: GoodsReceipt | None = None
    vendor: Vendor | None = None
    run_id: str = ""
    duplicate_invoice: bool = False
    gr_reserved_by_other: bool = False
