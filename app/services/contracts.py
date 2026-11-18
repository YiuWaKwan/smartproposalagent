"""Typed boundaries for capabilities that will be implemented in this phase."""

from typing import Protocol
from uuid import UUID

from app.schemas.domain import ActorContext, CampaignDraft, RecommendationTaskDraft


class BusinessServices(Protocol):
    # TODO(adapter, design §2.5): replace these provisional signatures after the
    # official identity, idempotency, request and response contracts are agreed.
    async def create_campaign(self, actor: ActorContext, draft: CampaignDraft) -> int | str: ...
    async def create_task(self, actor: ActorContext, campaign_id: int | str, draft: RecommendationTaskDraft) -> int | str: ...


class ConversationRepository(Protocol):
    # TODO(repository, design §§4.3, 5, 7): transactionally persist events,
    # projections and outbox; implement authorization, deduplication and leases.
    async def exists_for_actor(self, actor: ActorContext, conversation_id: UUID) -> bool: ...


class MemoryService(Protocol):
    # TODO(memory, design §8): add typed list/set/revoke contracts once approved
    # keys, scopes, evidence and retention rules are defined.
    async def validate_scope(self, actor: ActorContext) -> None: ...
