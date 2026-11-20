"""Local selection facts and operation previews, separate from Java persistence."""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictStr, model_validator

from app.schemas.domain.common import DomainModel

KolID = Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]


class SelectionAction(StrEnum):
    SELECT = "select"
    CANCEL = "cancel"
    REJECT = "reject"


class SelectionChange(DomainModel):
    kol_backend_id: KolID
    action: SelectionAction
    reject_reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def reason_only_for_rejection(self):
        if self.reject_reason is not None and self.action != SelectionAction.REJECT:
            raise ValueError("reject reason requires an explicit reject action")
        return self


class SelectionOperationKind(StrEnum):
    SAVE = "save_selection"
    FINAL = "confirm_selection"


class SelectionOperationStatus(StrEnum):
    PREVIEW = "preview"
    BLOCKED = "blocked"
    IN_FLIGHT = "in_flight"
    ACCEPTED = "accepted"
    SAVED = "saved"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class SelectionPreview(DomainModel):
    preview_id: UUID
    operation_id: UUID
    request_id: UUID
    confirmation_id: UUID
    kind: SelectionOperationKind
    agent_task_id: UUID
    campaign_id: int
    business_task_id: int
    downstream_task_id: str
    recommendation_run_id: UUID
    snapshot_ref: UUID
    candidate_snapshot_hash: str
    selection_revision: int = Field(ge=0)
    form_revision: int = Field(ge=0)
    actor_tenant_id: str
    actor_user_id: str
    actor_brand_id: str
    selected_kol_ids: list[KolID]
    payload_hash: str
    canonical_payload: str
    can_submit: Literal[False] = False
    blocked_reasons: list[str]


class SelectionReceipt(DomainModel):
    kind: SelectionOperationKind
    status: SelectionOperationStatus
    operation_id: UUID
    request_id: UUID
    confirmation_id: UUID
    agent_task_id: UUID
    campaign_id: int
    business_task_id: int
    downstream_task_id: str
    recommendation_run_id: UUID
    snapshot_ref: UUID
    selection_revision: int
    candidate_snapshot_hash: str
    payload_hash: str
    error_code: str | None = None

    @classmethod
    def from_preview(cls, preview: SelectionPreview, status: SelectionOperationStatus, error_code: str | None = None):
        return cls(
            kind=preview.kind, status=status, operation_id=preview.operation_id, request_id=preview.request_id,
            confirmation_id=preview.confirmation_id, agent_task_id=preview.agent_task_id,
            campaign_id=preview.campaign_id, business_task_id=preview.business_task_id,
            downstream_task_id=preview.downstream_task_id, recommendation_run_id=preview.recommendation_run_id,
            snapshot_ref=preview.snapshot_ref,
            selection_revision=preview.selection_revision, candidate_snapshot_hash=preview.candidate_snapshot_hash,
            payload_hash=preview.payload_hash, error_code=error_code,
        )


class SelectionOperation(DomainModel):
    preview: SelectionPreview
    status: SelectionOperationStatus = SelectionOperationStatus.PREVIEW
    explicitly_confirmed: bool = False
    attempted: bool = False
    receipt: SelectionReceipt | None = None


class SelectionDraft(DomainModel):
    revision: int = Field(default=0, ge=0)
    selected_kol_ids: list[KolID] = Field(default_factory=list)
    rejected_kols: dict[str, str | None] = Field(default_factory=dict)
    cancelled_kol_ids: list[KolID] = Field(default_factory=list)
    edited_kol_ids: set[KolID] = Field(default_factory=set)
    recommendation_run_id: UUID | None = None
    snapshot_ref: UUID | None = None
    candidate_snapshot_hash: str | None = None
    # These are local previews/receipts. They are not a durable operation ledger.
    save_operation: SelectionOperation | None = None
    final_operation: SelectionOperation | None = None
    submitted_kol_ids: list[KolID] = Field(default_factory=list)
    event_hashes: dict[str, str] = Field(default_factory=dict)
