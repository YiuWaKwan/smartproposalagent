"""Drafts and identifiers: external API contracts remain subject to confirmation."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActorContext(DomainModel):
    """Constructed only by the future trusted authentication integration."""

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    brand_id: str = Field(min_length=1)
    conversation_id: UUID


class CampaignDraft(DomainModel):
    name: str | None = None
    campaign_type: str | None = None
    platform: str | None = None
    budget: Decimal | None = None
    start_date: date | None = None
    end_date: date | None = None
    persona: str | None = None
    emotion: str | None = None
    product: str | None = None
    selling_point: str | None = None
    core_selling_point: str | None = None
    competitors: str | None = None


class RecommendationTaskDraft(DomainModel):
    name: str | None = None
    kol_count: int | None = Field(default=None, ge=1)
    budget: Decimal | None = None
    tier_distribution: str | None = None
    talking_angle: str | None = None
    talent_type: str | None = None
    scene: str | None = None
    content_preference: str | None = None
    talent_ability: str | None = None
    remark: str | None = None


class WorkflowStage(StrEnum):
    CAMPAIGN_DRAFT = "campaign_draft"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    CAMPAIGN_SELECTION = "campaign_selection"
    CAMPAIGN_CREATING = "campaign_creating"
    CAMPAIGN_CREATED = "campaign_created"
    TASK_CREATING = "task_creating"
    RECOMMENDATION_RUNNING = "recommendation_running"
    CANDIDATE_REVIEW = "candidate_review"
    AWAITING_SELECTION_CONFIRMATION = "awaiting_selection_confirmation"
    SELECTION_SUBMITTING = "selection_submitting"
    COMPLETED = "completed"
    PARTIAL_FAILURE = "partial_failure"
    CANCELLED = "cancelled"


class SelectionDraft(DomainModel):
    revision: int = Field(default=0, ge=0)
    selected_kol_ids: list[str] = Field(default_factory=list)
    recommendation_run_id: UUID | None = None
    candidate_snapshot_hash: str | None = None


class WorkflowDraft(DomainModel):
    task_id: UUID
    stage: WorkflowStage = WorkflowStage.CAMPAIGN_DRAFT
    campaign: CampaignDraft = Field(default_factory=CampaignDraft)
    recommendation_task: RecommendationTaskDraft = Field(default_factory=RecommendationTaskDraft)
    form_revision: int = Field(default=0, ge=0)
    campaign_id: int | str | None = None
    business_task_id: int | str | None = None
    downstream_task_id: str | None = None
    selection: SelectionDraft = Field(default_factory=SelectionDraft)
