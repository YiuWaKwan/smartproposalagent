"""Typed form patch events used before natural-language extraction is added."""

from typing import Any, Literal

from pydantic import Field

from app.schemas.domain.common import DomainModel, ValidationIssue


PatchTarget = Literal["campaign", "recommendation_task"]
PatchOperation = Literal["set", "clear", "use_default", "add", "remove"]


class FieldPatch(DomainModel):
    target: PatchTarget
    field: str = Field(min_length=1)
    op: PatchOperation
    value: Any = None


class FormPatch(DomainModel):
    operations: list[FieldPatch] = Field(min_length=1)
    ambiguities: list[ValidationIssue] = Field(default_factory=list)
