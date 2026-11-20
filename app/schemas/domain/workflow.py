"""Agent orchestration state, separate from Java Task status."""

from enum import StrEnum
from uuid import UUID

from pydantic import Field

from app.schemas.domain.campaign import CampaignBinding, CampaignDraft
from app.schemas.domain.common import DomainModel, FieldProvenance, ValidationIssue
from app.schemas.domain.selection import SelectionDraft
from app.schemas.domain.task import RecommendationTaskDraft
from app.schemas.recommendation import RecommendationRun


class AgentWorkflowStage(StrEnum):
    """Where this Agent workflow is; it is not a Campaign lifecycle status."""

    CAMPAIGN_DRAFT = "campaign_draft"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    CAMPAIGN_SELECTION = "campaign_selection"
    CAMPAIGN_CREATING = "campaign_creating"
    CAMPAIGN_CREATED = "campaign_created"
    TASK_CREATING = "task_creating"
    TASK_CREATED = "task_created"
    AWAITING_RECOMMENDATION_CONFIRMATION = "awaiting_recommendation_confirmation"
    RECOMMENDATION_STARTING = "recommendation_starting"
    RECOMMENDATION_RUNNING = "recommendation_running"
    CANDIDATE_REVIEW = "candidate_review"
    AWAITING_SELECTION_CONFIRMATION = "awaiting_selection_confirmation"
    SELECTION_SUBMITTING = "selection_submitting"
    COMPLETED = "completed"
    PARTIAL_FAILURE = "partial_failure"
    CANCELLED = "cancelled"


class BusinessTaskStatus(StrEnum):
    """Status returned by the existing Java selection-task service."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RECOMMEND_COMPLETED = "recommend_completed"
    ENDED = "ended"
    FAILED = "failed"


class NextAction(StrEnum):
    WAIT_FOR_USER = "wait_for_user"
    WAIT_FOR_OPERATION_CONFIRMATION = "wait_for_operation_confirmation"
    WAIT_FOR_RECOMMENDATION_CONFIRMATION = "wait_for_recommendation_confirmation"
    RESERVE_BUSINESS_OPERATION = "reserve_business_operation"
    OPERATION_BLOCKED = "operation_blocked"
    NONE = "none"


class BusinessOperationType(StrEnum):
    CREATE_CAMPAIGN = "create_campaign"
    CREATE_TASK = "create_task"
    START_RECOMMENDATION = "start_recommendation"


class BusinessOperationStatus(StrEnum):
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    ACCEPTED = "accepted"


class PendingBusinessOperation(DomainModel):
    operation_id: UUID
    request_id: UUID
    operation_type: BusinessOperationType
    agent_task_id: UUID
    actor_tenant_id: str
    actor_user_id: str
    actor_brand_id: str
    campaign_id: int | None = None
    business_task_id: int | None = None
    form_revision: int = Field(ge=0)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_id: UUID
    canonical_payload: str
    confirmed: bool = False
    status: BusinessOperationStatus = BusinessOperationStatus.AWAITING_CONFIRMATION


class PendingUI(DomainModel):
    type: str = "clarification"
    missing_fields: list[str] = Field(default_factory=list)
    ambiguities: list[ValidationIssue] = Field(default_factory=list)
    form_revision: int = Field(ge=0)


class WorkflowDraft(DomainModel):
    """One Agent task context; a Campaign can have many such task contexts."""

    task_id: UUID
    stage: AgentWorkflowStage = AgentWorkflowStage.CAMPAIGN_DRAFT
    campaign_binding: CampaignBinding = Field(default_factory=CampaignBinding)
    campaign: CampaignDraft = Field(default_factory=CampaignDraft)
    recommendation_task: RecommendationTaskDraft = Field(default_factory=RecommendationTaskDraft)
    form_revision: int = Field(default=0, ge=0)
    business_task_id: int | None = None
    business_campaign_id: int | None = None
    business_task_status: BusinessTaskStatus | None = None
    downstream_task_id: str | None = None
    field_sources: dict[str, FieldProvenance] = Field(default_factory=dict)
    suppressed_defaults: set[str] = Field(default_factory=set)
    validation_errors: list[ValidationIssue] = Field(default_factory=list)
    ambiguities: list[ValidationIssue] = Field(default_factory=list)
    pending_ui: PendingUI | None = None
    next_action: NextAction = NextAction.NONE
    selection: SelectionDraft = Field(default_factory=SelectionDraft)
    pending_business_operation: PendingBusinessOperation | None = None
    recommendation_run: RecommendationRun | None = None
    candidate_snapshot_ref: str | None = None
    result_ref: str | None = None
