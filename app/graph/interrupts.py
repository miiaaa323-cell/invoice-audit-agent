"""Typed wire contracts shared by graph interrupts and HTTP validation."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["ap_clerk", "finance_manager"]
ReviewType = Literal[
    "field_verify", "ap_review", "level1_approval", "level2_approval", "wait_for_info"
]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ReviewType
    invoice_summary: dict[str, Any]
    findings: list[dict] = Field(default_factory=list)
    explanation: str | None = None
    allowed_actions: list[str]
    required_role: Role
    fields: list[str] = Field(default_factory=list)
    info_rounds_left: int | None = None


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    note: str | None = Field(default=None, max_length=2000)
    corrected_fields: dict[str, Any] | None = None
    version: int | None = Field(default=None, ge=0)


class ResumePayload(ResumeBody):
    role: Role
    document_path: str | None = None
