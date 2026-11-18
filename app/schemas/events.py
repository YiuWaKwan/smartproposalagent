"""External event shapes; identity must never be accepted in the event payload."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.schemas.domain import DomainModel


class UserMessage(DomainModel):
    type: Literal["user_message"] = "user_message"
    client_event_id: UUID
    task_id: UUID
    text: str = Field(min_length=1)


class ConfirmSelection(DomainModel):
    type: Literal["confirm_selection"] = "confirm_selection"
    client_event_id: UUID
    task_id: UUID
    selection_preview_id: UUID
    recommendation_run_id: UUID
    candidate_snapshot_hash: str = Field(min_length=1)
    selection_revision: int = Field(ge=0)
    selected_kol_ids: list[str] = Field(min_length=1)
    confirmation_id: UUID


# TODO(events, design §5): add typed form/selection patches, cancellation,
# memory operations, event envelopes, idempotency and expected revisions.
AgentEvent = Annotated[UserMessage | ConfirmSelection, Field(discriminator="type")]
