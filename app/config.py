"""Validated policy and process settings, with explicit paths for isolated runs."""

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import CompanyProfile

ROOT = Path(__file__).resolve().parents[1]


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    currency: Literal["CNY"] = "CNY"
    rounding_tolerance: Decimal = Field(default=Decimal("1.00"), ge=0)
    line_amount_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)
    level2_threshold: Decimal = Field(default=Decimal("50000.00"), gt=0)
    confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    max_info_rounds: int = Field(default=2, ge=0)
    variance_account: str = "ROUNDING_VARIANCE"
    company: CompanyProfile = Field(default_factory=lambda: CompanyProfile(
        buyer_name="示例科技有限公司", buyer_tax_id="91310000MA1FL0000X"
    ))


def load_policy(path: Path | str = ROOT / "config/policy.yaml") -> Policy:
    return Policy.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


class Settings(BaseModel):
    data_dir: Path = ROOT / "data"
    db_path: Path | None = None
    checkpoint_path: Path | None = None
    upload_dir: Path | None = None
    cache_dir: Path = ROOT / "fixtures/llm_cache"
    seed_dir: Path = ROOT / "data/seed"
    policy_path: Path = ROOT / "config/policy.yaml"
    llm_mode: Literal["live", "record", "replay"] = "replay"
    llm_model: str = "claude-sonnet-4-6"
    anthropic_api_key: str | None = Field(default=None, repr=False)
    seed_on_start: bool = True
    max_upload_bytes: int = 10 * 1024 * 1024

    @model_validator(mode="after")
    def set_paths(self):
        self.db_path = self.db_path or self.data_dir / "app.sqlite"
        self.checkpoint_path = self.checkpoint_path or self.data_dir / "checkpoints.sqlite"
        self.upload_dir = self.upload_dir or self.data_dir / "uploads"
        return self

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        values = {}
        for name in cls.model_fields:
            key = name.upper()
            if key in os.environ:
                values[name] = os.environ[key]
        return cls(**values)
