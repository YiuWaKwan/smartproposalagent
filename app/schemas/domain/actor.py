"""Trusted actor context supplied by the future authentication integration."""

from uuid import UUID

from pydantic import Field

from app.schemas.domain.common import DomainModel


class ActorContext(DomainModel):
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    brand_id: str = Field(min_length=1)
    conversation_id: UUID
