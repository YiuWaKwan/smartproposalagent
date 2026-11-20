"""Typed, disabled Java selection write boundary and bounded local review page."""

import hashlib
import json
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, model_validator

from app.schemas.business_api import ActorScope
from app.schemas.domain.common import DomainModel
from app.schemas.domain.selection import KolID, SelectionPreview
from app.schemas.recommendation import CandidatePreview


class JavaSelectionKol(DomainModel):
    kol_backend_id: KolID = Field(alias="kolBackendId")
    selected: StrictBool
    rejected: StrictBool
    reject_reason: str | None = Field(default=None, alias="rejectReason", max_length=2_000)

    @model_validator(mode="after")
    def validate_action(self):
        if self.selected and self.rejected:
            raise ValueError("selection and rejection are mutually exclusive")
        if self.reject_reason is not None and not self.rejected:
            raise ValueError("reject reason requires rejection")
        return self


class JavaSelectionTask(DomainModel):
    task_id: StrictInt = Field(alias="taskId", gt=0)
    kols: list[JavaSelectionKol] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.kol_backend_id for item in self.kols]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate KOL IDs")
        return self


class JavaSelectionPayload(DomainModel):
    # One workflow/run targets exactly one Java Task, despite Java's batch shape.
    items: list[JavaSelectionTask] = Field(min_length=1, max_length=1)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json", by_alias=True, exclude_none=True),
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class SelectionWriteRequest(DomainModel):
    kind: str
    java_operation: ClassVar[str]
    actor_scope: ActorScope
    preview: SelectionPreview
    payload: JavaSelectionPayload
    # Local reference above is never represented as a Java snapshot/version.
    java_snapshot_version: None = None

    @model_validator(mode="after")
    def matches_preview(self):
        preview = self.preview
        canonical = self.payload.canonical_json()
        if (
            preview.kind != self.kind
            or preview.business_task_id != self.payload.items[0].task_id
            or (preview.actor_tenant_id, preview.actor_user_id, preview.actor_brand_id)
            != (self.actor_scope.tenant_id, self.actor_scope.user_id, self.actor_scope.brand_id)
            or preview.canonical_payload != canonical
            or preview.payload_hash != hashlib.sha256(canonical.encode()).hexdigest()
        ):
            raise ValueError("selection request does not match the current operation preview")
        if self.kind == "confirm_selection" and (
            not preview.selected_kol_ids
            or preview.selected_kol_ids != [item.kol_backend_id for item in self.payload.items[0].kols if item.selected]
        ):
            raise ValueError("final selection payload does not match the non-empty confirmed list")
        return self


class SelectionSaveRequest(SelectionWriteRequest):
    kind: Literal["save_selection"] = "save_selection"
    java_operation = "batch-select"


class SelectionConfirmRequest(SelectionWriteRequest):
    kind: Literal["confirm_selection"] = "confirm_selection"
    java_operation = "batch-confirm-selection"


class CandidateReviewPage(DomainModel):
    snapshot_ref: UUID
    snapshot_hash: str
    offset: int = Field(ge=0)
    next_offset: int | None
    fetched_count: int
    universe_total: int | None = None
    candidate_universe_complete: bool = False
    scope: Literal["local_fetched_response"] = "local_fetched_response"
    items: list[CandidatePreview] = Field(max_length=20)
