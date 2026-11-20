"""Checkpointable graph state; dependencies and credentials stay outside it."""

from typing import TypedDict

from app.schemas.domain import WorkflowDraft
from app.schemas.business_api import (
    ActorScope,
    OperationReceipt,
    TrustedBrand,
)
from app.schemas.events import AgentEvent
from app.schemas.domain.selection import SelectionReceipt
from app.schemas.selection import CandidateReviewPage
from app.schemas.recommendation import (
    CandidateProjection,
    RecommendationPreview,
    RecommendationOperationResult,
    RecommendationRun,
)


class AgentState(TypedDict, total=False):
    schema_version: int
    graph_version: int
    draft: WorkflowDraft
    incoming_event: AgentEvent
    actor_scope: ActorScope
    trusted_brand: TrustedBrand | None
    operation_receipt: OperationReceipt | None
    recommendation_run: RecommendationRun | None
    candidate_projection: CandidateProjection | None
    candidate_review_page: CandidateReviewPage | None
    selection_receipt: SelectionReceipt | None
    recommendation_preview: RecommendationPreview | None
    recommendation_result: RecommendationOperationResult | None
    applied_event_id: str | None
    pending_ui_ref: str | None
    request_id: str | None
    candidate_snapshot_ref: str | None
    result_ref: str | None
    resume_event_id: str | None


# TODO(state, design §§4, 6): add messages, task summary, memory references,
# operation/confirmation records and persisted event metadata.
# Confirm the selected serializer and schema migration with the Postgres saver.
