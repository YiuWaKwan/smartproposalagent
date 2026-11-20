"""Small shared domain types with no workflow or transport dependencies."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldProvenance(DomainModel):
    """Evidence for the value currently used by a form field."""

    source: Literal["user_input", "form_edit", "user_memory", "brand_default", "system_default"]
    source_id: str | None = None
    source_version: int | None = Field(default=None, ge=0)
    updated_at: datetime | None = None


class ValidationIssue(DomainModel):
    code: str
    message: str
    field: str | None = None
    blocking: bool = True
