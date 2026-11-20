"""Typed boundaries for capabilities that will be implemented in this phase."""

from typing import Protocol
from uuid import UUID

from app.schemas.business_api import (
    AdapterError,
    AdapterResponse,
    CampaignDetails,
    CampaignCreated,
    CampaignCreateRequest,
    OperationReceipt,
    TaskCreated,
    TaskCreateRequest,
)
from app.schemas.business_api import ActorScope
from app.schemas.domain import ActorContext
from app.schemas.domain.selection import SelectionReceipt
from app.schemas.recommendation import (
    JavaRecommendationResults,
    JavaTask,
    JavaTaskList,
    RecommendationStartAccepted,
    RecommendationReadRequest,
    RecommendationStartRequest,
    RecommendationSnapshotReference,
)
from app.schemas.selection import SelectionConfirmRequest, SelectionSaveRequest


class BusinessServices(Protocol):
    """Typed adapter contract. It never decides whether an operation is authorized."""

    async def create_campaign(
        self, request: CampaignCreateRequest
    ) -> AdapterResponse[CampaignCreated] | OperationReceipt: ...

    async def create_task(
        self, request: TaskCreateRequest
    ) -> AdapterResponse[TaskCreated] | OperationReceipt: ...

    async def read_campaign(
        self, campaign_id: int, actor: ActorScope
    ) -> AdapterResponse[CampaignDetails] | AdapterError: ...

    async def start_recommendation(
        self, request: RecommendationStartRequest
    ) -> AdapterResponse[RecommendationStartAccepted] | OperationReceipt: ...

    async def read_task(self, request: RecommendationReadRequest) -> AdapterResponse[JavaTask] | AdapterError: ...

    async def list_tasks(self, request: RecommendationReadRequest) -> AdapterResponse[JavaTaskList] | AdapterError: ...

    async def read_recommendation_results(
        self, request: RecommendationReadRequest
    ) -> AdapterResponse[JavaRecommendationResults] | AdapterError: ...

    async def save_selection(self, request: SelectionSaveRequest) -> SelectionReceipt: ...

    async def confirm_selection(self, request: SelectionConfirmRequest) -> SelectionReceipt: ...


class RecommendationSnapshotStore(Protocol):
    """Storage port; a volatile implementation is only a local placeholder."""

    def put(self, reference: RecommendationSnapshotReference, results: JavaRecommendationResults) -> None: ...

    def get_reference(self, snapshot_ref: UUID) -> RecommendationSnapshotReference | None: ...

    def get_results(self, snapshot_ref: UUID) -> JavaRecommendationResults | None: ...


class RecommendationSnapshotRepository(RecommendationSnapshotStore, Protocol):
    """Durable snapshot store port for a future Java-confirmed blob/database adapter."""


class ConversationRepository(Protocol):
    # TODO(repository, design §§4.3, 5, 7): transactionally persist events,
    # projections and outbox; implement authorization, deduplication and leases.
    async def exists_for_actor(self, actor: ActorContext, conversation_id: UUID) -> bool: ...


class MemoryService(Protocol):
    # TODO(memory, design §8): add typed list/set/revoke contracts once approved
    # keys, scopes, evidence and retention rules are defined.
    async def validate_scope(self, actor: ActorContext) -> None: ...
