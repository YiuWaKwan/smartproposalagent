"""External event shapes; identity must never be accepted in the event payload."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt

from app.schemas.domain import DomainModel
from app.schemas.domain.selection import KolID, SelectionChange
from app.schemas.patch import FormPatch
from app.schemas.business_api import BusinessOperationConfirmation
from app.schemas.recommendation import (
    RecommendationOperationConfirmation,
    RecommendationPollEvent,
    RecommendationStartRequested,
)


class UserMessage(DomainModel):
    type: Literal["user_message"] = "user_message"
    client_event_id: UUID
    task_id: UUID
    text: str = Field(min_length=1)


class SelectionContextEvent(DomainModel):
    client_event_id: UUID
    task_id: UUID
    campaign_id: StrictInt = Field(gt=0)
    business_task_id: StrictInt = Field(gt=0)
    recommendation_run_id: UUID
    snapshot_ref: UUID
    candidate_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelectionReviewRequested(SelectionContextEvent):
    type: Literal["selection_review"] = "selection_review"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=20)


class SelectionPatchEvent(SelectionContextEvent):
    type: Literal["selection_patch"] = "selection_patch"
    expected_selection_revision: StrictInt = Field(ge=0)
    changes: list[SelectionChange] = Field(min_length=1, max_length=1000)


class SelectionSaveRequested(SelectionContextEvent):
    type: Literal["selection_save"] = "selection_save"
    selection_preview_id: UUID
    selection_revision: StrictInt = Field(ge=0)
    confirmation_id: UUID


class ConfirmSelection(SelectionContextEvent):
    type: Literal["confirm_selection"] = "confirm_selection"
    selection_preview_id: UUID
    selection_revision: StrictInt = Field(ge=0)
    selected_kol_ids: list[KolID] = Field(min_length=1)
    confirmation_id: UUID


class FormPatchEvent(DomainModel):
    type: Literal["form_patch"] = "form_patch"
    client_event_id: UUID
    task_id: UUID
    expected_form_revision: int = Field(ge=0)
    patch: FormPatch


class CampaignSelectionEvent(DomainModel):
    type: Literal["campaign_selection"] = "campaign_selection"
    client_event_id: UUID
    task_id: UUID
    expected_form_revision: int = Field(ge=0)
    campaign_id: int = Field(gt=0)


# TODO(events, design §5): add cancellation, memory operations, event envelopes,
# and durable idempotency.
AgentEvent = Annotated[
    UserMessage | FormPatchEvent | CampaignSelectionEvent | ConfirmSelection
    | BusinessOperationConfirmation | RecommendationStartRequested | RecommendationOperationConfirmation
    | RecommendationPollEvent | SelectionReviewRequested | SelectionPatchEvent | SelectionSaveRequested,
    Field(discriminator="type"),
]
