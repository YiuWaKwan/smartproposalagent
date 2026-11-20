"""Typed Java recommendation API and locally recoverable recommendation state."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import hashlib
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from app.schemas.domain.common import DomainModel


class JavaTaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RECOMMEND_COMPLETED = "recommend_completed"
    FAILED = "failed"
    ENDED = "ended"


class RecommendationRunStatus(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    STARTING = "starting"
    ACCEPTED = "accepted"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RECOMMEND_COMPLETED = "recommend_completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELLED = "cancelled"


class RecommendationRecoveryAction(StrEnum):
    WAIT_FOR_CONFIRMATION = "wait_for_confirmation"
    START_ONCE = "start_once"
    QUERY_TASK = "query_task"
    READ_RESULTS = "read_results"
    MANUAL_RECONCILIATION = "manual_reconciliation"
    STOP = "stop"


class PollDisposition(StrEnum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RecommendationError(DomainModel):
    kind: str
    message: str


class RecommendationRecoveryDecision(DomainModel):
    action: RecommendationRecoveryAction
    reason: str
    may_start_write: bool = False


class RecommendationStartRequest(DomainModel):
    actor_tenant_id: StrictStr
    actor_user_id: StrictStr
    actor_brand_id: StrictStr
    campaign_id: Annotated[StrictInt, Field(gt=0)]
    business_task_id: Annotated[StrictInt, Field(gt=0)]
    agent_task_id: UUID
    operation_id: UUID
    request_id: UUID
    idempotency_key: UUID
    confirmation_id: UUID
    form_revision: int = Field(ge=0)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_payload: str

    @model_validator(mode="after")
    def verify_canonical_payload_hash(self):
        if hashlib.sha256(self.canonical_payload.encode("utf-8")).hexdigest() != self.payload_hash:
            raise ValueError("recommendation request payload hash does not match canonical payload")
        if self.idempotency_key != self.operation_id:
            raise ValueError("recommendation idempotency key must equal operation ID")
        return self


class RecommendationStartRequested(DomainModel):
    """User requests a preview; this event alone does not authorize starting."""

    type: Literal["recommendation.start_requested"] = "recommendation.start_requested"
    client_event_id: UUID
    task_id: UUID
    expected_form_revision: int = Field(ge=0)


class RecommendationPollEvent(DomainModel):
    """Internal poll tick; it carries no write authorization."""

    type: Literal["recommendation.poll"] = "recommendation.poll"
    client_event_id: UUID
    task_id: UUID
    operation_id: UUID
    request_id: UUID
    poll_sequence: int = Field(ge=1)


class RecommendationReadRequest(DomainModel):
    actor_tenant_id: StrictStr
    actor_user_id: StrictStr
    actor_brand_id: StrictStr
    campaign_id: Annotated[StrictInt, Field(gt=0)]
    business_task_id: Annotated[StrictInt, Field(gt=0)]
    downstream_task_id: str | None = None
    operation_id: UUID
    request_id: UUID


class RecommendationReadResult(DomainModel):
    campaign_id: int
    business_task_id: int
    downstream_task_id: str
    operation_id: UUID
    request_id: UUID
    results: JavaRecommendationResults


class JavaTask(DomainModel):
    model_config = {"extra": "ignore", "populate_by_name": True}

    business_task_id: Annotated[StrictInt, Field(gt=0)] = Field(alias="id")
    campaign_id: Annotated[StrictInt, Field(gt=0)] = Field(alias="campaignId")
    status: StrictStr
    progress: int | None = None
    current_stage: str | None = Field(default=None, alias="currentStage")
    downstream_task_id: str | None = Field(default=None, alias="downstreamTaskId")
    recommend_count: int | None = Field(default=None, alias="recommendCount")
    platform: str | None = None

class RecommendationStartAccepted(DomainModel):
    campaign_id: Annotated[StrictInt, Field(gt=0)]
    business_task_id: Annotated[StrictInt, Field(gt=0)]
    operation_id: UUID
    request_id: UUID
    accepted: StrictBool = True


class RecommendationPreview(DomainModel):
    run_id: UUID
    task_id: UUID
    campaign_id: int
    business_task_id: int
    business_task_status: JavaTaskStatus
    operation_id: UUID
    request_id: UUID
    idempotency_key: UUID
    confirmation_id: UUID
    form_revision: int = Field(ge=0)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    downstream_task_id: str | None = None
    task_name: str | None = None
    platform: str | None = None
    kol_count: int | None = None
    budget: Decimal | None = None
    tier_distribution: str | None = None
    talent_type: str | None = None


class RecommendationExecutionPayload(DomainModel):
    campaign_id: int = Field(gt=0)
    business_task_id: int = Field(gt=0)
    task_status: JavaTaskStatus
    downstream_task_id: str | None = None
    task_name: str | None = None
    platform: str | None = None
    kol_count: int | None = None
    budget: Decimal | None = None
    tier_distribution: str | None = None
    talent_type: str | None = None


class RecommendationOperationConfirmation(DomainModel):
    type: Literal["recommendation.confirm"] = "recommendation.confirm"
    client_event_id: UUID
    task_id: UUID
    operation_id: UUID
    request_id: UUID
    confirmation_id: UUID
    campaign_id: int = Field(gt=0)
    business_task_id: int = Field(gt=0)
    form_revision: int = Field(ge=0)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecommendationOperationResult(DomainModel):
    disposition: PollDisposition
    run: RecommendationRun
    snapshot_ref: RecommendationSnapshotReference | None = None
    projection: CandidateProjection | None = None
    error: RecommendationError | None = None


class RecommendationTaskRead(DomainModel):
    campaign_id: Annotated[StrictInt, Field(gt=0)]
    business_task_id: Annotated[StrictInt, Field(gt=0)]
    status: JavaTaskStatus | None = None
    raw_status: str
    progress: int | None = Field(default=None, ge=0, le=100)
    current_stage: str | None = None
    downstream_task_id: str | None = None
    operation_id: UUID
    request_id: UUID

    @model_validator(mode="after")
    def validate_mapped_status(self):
        if self.status is None:
            raise ValueError("unknown Java Task status cannot be mapped to a successful read")
        return self


class JavaRecommendationMetric(DomainModel):
    """Limited display-safe metric value; extra Java fields are ignored."""

    model_config = {"extra": "ignore"}

    name: str | None = None
    value: str | int | float | Decimal | None = None


class JavaRecommendationPricing(DomainModel):
    model_config = {"extra": "ignore"}

    name: str | None = None
    twenty_to_sixty: JavaRecommendationMetric | None = Field(default=None, alias="twenty_to_sixty")
    sixty_plus: JavaRecommendationMetric | None = Field(default=None, alias="sixty_plus")


class JavaRecommendationMetrics(DomainModel):
    model_config = {"extra": "ignore"}

    fan_count: JavaRecommendationMetric | None = Field(default=None, alias="fan_count")
    cpm: JavaRecommendationMetric | None = None
    cpe: JavaRecommendationMetric | None = None
    cps: JavaRecommendationMetric | None = None
    pricing: JavaRecommendationPricing | None = None
    estimated_impression: JavaRecommendationMetric | None = Field(default=None, alias="estimated_impression")
    estimated_interaction: JavaRecommendationMetric | None = Field(default=None, alias="estimated_interaction")


class JavaCandidateDetail(DomainModel):
    model_config = {"extra": "ignore"}

    backend_id: str | None = Field(default=None, alias="backend_id")
    name: str | None = None
    description: str | None = None
    tier: str | None = None
    homepage_url: str | None = Field(default=None, alias="homepage_url")
    avatar_url: str | None = Field(default=None, alias="avatar_url")
    shortlist: bool | None = None
    backup: bool | None = None
    metrics: JavaRecommendationMetrics | None = None
    recommendation_reason: str | None = Field(default=None, alias="recommendation_reason", max_length=20_000)


class JavaRecommendationCandidate(DomainModel):
    model_config = {"extra": "ignore"}

    kol_backend_id: StrictStr = Field(alias="kolBackendId", min_length=1)
    source: StrictStr = Field(min_length=1)
    kol_name: str | None = Field(default=None, alias="kolName")
    kol_detail: JavaCandidateDetail | None = Field(default=None, alias="kolDetail")
    selected: bool | None = None
    rejected: bool | None = None
    reject_reason: str | None = Field(default=None, alias="rejectReason", max_length=2_000)


class JavaRecommendationSummary(DomainModel):
    model_config = {"extra": "ignore"}

    shortlist_count: int | None = Field(default=None, alias="shortlistCount")
    task_kol_count: int | None = Field(default=None, alias="taskKolCount")
    budget: Decimal | None = None
    selected_budget: Decimal | None = Field(default=None, alias="selectedBudget")
    estimated_impression: Decimal | None = Field(default=None, alias="estimatedImpression")
    estimated_interaction: Decimal | None = Field(default=None, alias="estimatedInteraction")


class JavaRecommendationResults(DomainModel):
    model_config = {"extra": "ignore"}

    summary: JavaRecommendationSummary | None = None
    kols: list[JavaRecommendationCandidate]


class JavaTaskList(DomainModel):
    items: list[JavaTask]


class CandidatePreview(DomainModel):
    kol_backend_id: str
    source: str
    kol_name: str | None = None
    tier: str | None = None
    avatar_url: str | None = None
    homepage_url: str | None = None
    shortlist: bool | None = None
    backup: bool | None = None
    recommendation_reason_excerpt: str | None = None
    selected: bool | None = None
    rejected: bool | None = None
    reject_reason: str | None = None


class RecommendationSnapshotReference(DomainModel):
    """Volatile pointer to Java results; this is not durable snapshot storage."""

    snapshot_ref: UUID
    campaign_id: int
    business_task_id: int
    downstream_task_id: str
    operation_id: UUID
    request_id: UUID
    fetched_at: datetime
    candidate_count: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_complete: bool
    candidate_universe_complete: bool = False
    snapshot_uri: str
    storage_owner: Literal["java_smartproposal"] = "java_smartproposal"
    java_persistence_verified: bool = False
    preview: list[CandidatePreview] = Field(max_length=20)


class RecommendationRun(DomainModel):
    run_id: UUID
    campaign_id: int
    business_task_id: int
    agent_task_id: UUID
    operation_id: UUID
    request_id: UUID
    idempotency_key: UUID
    form_revision: int = Field(ge=0)
    payload_hash: str
    canonical_payload: str
    actor_tenant_id: str
    actor_user_id: str
    actor_brand_id: str
    business_task_status: JavaTaskStatus = JavaTaskStatus.PENDING
    confirmation_id: UUID
    status: RecommendationRunStatus
    start_attempted: bool = False
    write_outcome_unknown: bool = False
    downstream_task_id: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    current_stage: str | None = None
    raw_java_status: str | None = None
    poll_sequence: int = Field(default=0, ge=0)
    poll_attempts: int = Field(default=0, ge=0)
    consecutive_read_failures: int = Field(default=0, ge=0)
    consecutive_result_read_failures: int = Field(default=0, ge=0)
    result_ref: UUID | None = None
    result_hash: str | None = None
    last_error_kind: str | None = None
    reconciliation_required: bool = False
    user_stopped_waiting: bool = False
    snapshot: RecommendationSnapshotReference | None = None


class PollResponseEnvelope(DomainModel):
    campaign_id: int
    business_task_id: int
    downstream_task_id: str | None = None
    operation_id: UUID
    request_id: UUID
    poll_sequence: int
    task: RecommendationTaskRead | None = None


class RecommendationRecoveryInput(DomainModel):
    """Serializable recovery input; it does not imply durable checkpointing."""

    agent_task_id: UUID
    campaign_id: int
    business_task_id: int
    actor_tenant_id: str
    actor_user_id: str
    actor_brand_id: str
    run: RecommendationRun


class CandidateProjection(DomainModel):
    campaign_id: int
    business_task_id: int
    downstream_task_id: str
    run_id: UUID
    snapshot_ref: UUID
    snapshot_hash: str
    # Count in this fetched response is distinct from an authoritative universe total.
    fetched_count: int = Field(ge=0)
    preview_count: int = Field(ge=0, le=20)
    omitted_count: int = Field(ge=0)
    total_count: int | None = Field(default=None, ge=0)
    truncated: bool
    pagination_verified: bool = False
    java_snapshot_version: str | None = None
    response_complete: bool
    candidate_universe_complete: bool = False
    durable: bool = False
    items: list[CandidatePreview] = Field(max_length=20)
