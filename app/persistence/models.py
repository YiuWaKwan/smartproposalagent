"""Typed persistence envelopes; database rows stay behind repositories."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from app.schemas.business_api import ActorScope
from app.schemas.domain.common import DomainModel


class ConversationRef(DomainModel):
    conversation_id: UUID
    workflow_id: UUID
    thread_id: str
    actor_scope: ActorScope
    brand_id: str


class EventReceipt(DomainModel):
    event_id: UUID
    conversation_id: UUID
    workflow_id: UUID
    client_event_id: UUID
    seq: int = Field(ge=1)
    status: Literal["accepted", "applied", "conflict", "rejected", "unknown"]
    body_hash: str
    event_type: str
    error_code: str | None = None


class MessageRecord(DomainModel):
    message_id: UUID
    conversation_id: UUID
    workflow_id: UUID | None
    seq: int
    role: Literal["user", "assistant", "system", "tool"]
    content: str | None
    content_json: dict[str, Any] | None
    source_event_id: UUID | None
    created_at: datetime


class OutboxLease(DomainModel):
    outbox_id: UUID
    workflow_id: UUID
    source_event_id: UUID | None
    topic: str
    aggregate_id: str
    payload: dict[str, Any]
    lease_owner: str
    fencing_token: int
    attempts: int


class UIEvent(DomainModel):
    event_id: UUID
    conversation_id: UUID
    workflow_id: UUID
    seq: int
    source_event_id: UUID | None
    event_type: Literal["form_snapshot", "selection_snapshot", "event_rejected"]
    schema_version: int
    payload: dict[str, Any]
    created_at: datetime


class ContextSummary(DomainModel):
    summary_id: UUID
    conversation_id: UUID
    workflow_id: UUID
    covered_until_message_id: UUID
    source_message_ids: list[UUID]
    summary_json: dict[str, Any]
    created_at: datetime
