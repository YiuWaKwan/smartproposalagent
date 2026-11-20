"""Deterministic recommendation confirmation, polling and projection services."""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.schemas.business_api import AdapterError, AdapterErrorKind, AdapterResponse, OperationReceipt, OperationStatus
from app.schemas.domain import BusinessOperationStatus, BusinessOperationType, PendingBusinessOperation, WorkflowDraft
from app.schemas.recommendation import (
    CandidatePreview,
    CandidateProjection,
    JavaCandidateDetail,
    JavaRecommendationResults,
    JavaTask,
    JavaTaskStatus,
    RecommendationExecutionPayload,
    RecommendationOperationConfirmation,
    RecommendationPollEvent,
    RecommendationPreview,
    RecommendationReadRequest,
    RecommendationRun,
    RecommendationRunStatus,
    RecommendationRecoveryAction,
    RecommendationRecoveryDecision,
    RecommendationSnapshotReference,
    RecommendationStartAccepted,
    RecommendationStartRequest,
    RecommendationStartRequested,
)
from app.schemas.domain import AgentWorkflowStage, NextAction
from app.services.adapters.smartproposal import canonical_json, canonical_payload_hash


def _actor_values(actor_scope):
    return actor_scope.tenant_id, actor_scope.user_id, actor_scope.brand_id


def _task_read_request(run: RecommendationRun) -> RecommendationReadRequest:
    return RecommendationReadRequest(
        actor_tenant_id=run.actor_tenant_id,
        actor_user_id=run.actor_user_id,
        actor_brand_id=run.actor_brand_id,
        campaign_id=run.campaign_id,
        business_task_id=run.business_task_id,
        downstream_task_id=run.downstream_task_id,
        operation_id=run.operation_id,
        request_id=run.request_id,
    )


def _trusted_task_response(result, run: RecommendationRun):
    if not isinstance(result, AdapterResponse) or not isinstance(result.value, JavaTask):
        raise ValueError("Java Task read did not return a typed Task")
    if result.operation_id != run.operation_id or result.request_id != run.request_id:
        raise ValueError("Java Task read response does not match the current operation")
    task = result.value
    if task.campaign_id != run.campaign_id or task.business_task_id != run.business_task_id:
        raise ValueError("Java Task response does not belong to the current Campaign/Task")
    return task


def prepare_recommendation_preview(
    draft: WorkflowDraft,
    *,
    actor_scope,
    trusted_brand,
    task: JavaTask,
) -> tuple[WorkflowDraft, RecommendationPreview]:
    if draft.business_task_id is None or draft.business_campaign_id is None:
        raise ValueError("a Java-created Campaign and Task are required before recommend")
    if task.business_task_id != draft.business_task_id or task.campaign_id != draft.business_campaign_id:
        raise ValueError("recommend Task does not match the current Agent workflow")
    if task.status.lower() != JavaTaskStatus.PENDING.value:
        raise ValueError("recommend can only be previewed for a pending Java Task")
    if trusted_brand is None or trusted_brand.brand_id != actor_scope.brand_id:
        raise ValueError("trusted current brand context is required")
    if draft.stage != AgentWorkflowStage.TASK_CREATED:
        raise ValueError("recommend can only start after Task create is complete")
    if draft.pending_business_operation is not None:
        raise ValueError("another business operation must be resolved before recommend")

    execution = RecommendationExecutionPayload(
        campaign_id=task.campaign_id,
        business_task_id=task.business_task_id,
        task_status=JavaTaskStatus.PENDING,
        downstream_task_id=task.downstream_task_id,
        task_name=draft.recommendation_task.name,
        platform=task.platform,
        kol_count=draft.recommendation_task.kol_count,
        budget=draft.recommendation_task.budget,
        tier_distribution=draft.recommendation_task.tier_distribution,
        talent_type=draft.recommendation_task.talent_type,
    )
    canonical = canonical_json(execution)
    digest = canonical_payload_hash(execution)
    current_revision = draft.form_revision
    operation = PendingBusinessOperation(
        operation_id=uuid4(),
        request_id=uuid4(),
        operation_type=BusinessOperationType.START_RECOMMENDATION,
        agent_task_id=draft.task_id,
        actor_tenant_id=actor_scope.tenant_id,
        actor_user_id=actor_scope.user_id,
        actor_brand_id=actor_scope.brand_id,
        campaign_id=task.campaign_id,
        business_task_id=task.business_task_id,
        form_revision=current_revision,
        payload_hash=digest,
        confirmation_id=uuid4(),
        canonical_payload=canonical,
    )
    run = RecommendationRun(
        run_id=uuid4(),
        campaign_id=task.campaign_id,
        business_task_id=task.business_task_id,
        agent_task_id=draft.task_id,
        operation_id=operation.operation_id,
        request_id=operation.request_id,
        idempotency_key=operation.operation_id,
        form_revision=current_revision,
        payload_hash=digest,
        canonical_payload=canonical,
        actor_tenant_id=actor_scope.tenant_id,
        actor_user_id=actor_scope.user_id,
        actor_brand_id=actor_scope.brand_id,
        business_task_status=JavaTaskStatus.PENDING,
        confirmation_id=operation.confirmation_id,
        status=RecommendationRunStatus.AWAITING_CONFIRMATION,
    )
    updated = draft.model_copy(deep=True)
    updated.pending_business_operation = operation
    updated.recommendation_run = run
    updated.stage = AgentWorkflowStage.AWAITING_RECOMMENDATION_CONFIRMATION
    updated.next_action = NextAction.WAIT_FOR_RECOMMENDATION_CONFIRMATION
    from app.schemas.domain import PendingUI
    updated.pending_ui = PendingUI(
        type="recommendation_confirmation",
        form_revision=current_revision,
    )
    preview = RecommendationPreview(
        run_id=run.run_id,
        task_id=draft.task_id,
        campaign_id=task.campaign_id,
        business_task_id=task.business_task_id,
        business_task_status=JavaTaskStatus.PENDING,
        operation_id=operation.operation_id,
        request_id=operation.request_id,
        idempotency_key=operation.operation_id,
        confirmation_id=operation.confirmation_id,
        form_revision=current_revision,
        payload_hash=digest,
        downstream_task_id=task.downstream_task_id,
        task_name=draft.recommendation_task.name,
        platform=task.platform,
        kol_count=draft.recommendation_task.kol_count,
        budget=draft.recommendation_task.budget,
        tier_distribution=draft.recommendation_task.tier_distribution,
        talent_type=draft.recommendation_task.talent_type,
    )
    return updated, preview


def build_recommendation_start_request(run: RecommendationRun) -> RecommendationStartRequest:
    if run.status != RecommendationRunStatus.CONFIRMED or run.start_attempted:
        raise ValueError("recommendation run is not ready for a start request")
    encoded = canonical_json(RecommendationExecutionPayload.model_validate_json(run.canonical_payload))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if digest != run.payload_hash:
        raise ValueError("recommendation canonical payload hash changed")
    return RecommendationStartRequest(
        actor_tenant_id=run.actor_tenant_id,
        actor_user_id=run.actor_user_id,
        actor_brand_id=run.actor_brand_id,
        campaign_id=run.campaign_id,
        business_task_id=run.business_task_id,
        agent_task_id=run.agent_task_id,
        operation_id=run.operation_id,
        request_id=run.request_id,
        idempotency_key=run.idempotency_key,
        confirmation_id=run.confirmation_id,
        form_revision=run.form_revision,
        payload_hash=run.payload_hash,
        canonical_payload=run.canonical_payload,
    )


def confirm_recommendation(draft: WorkflowDraft, event: RecommendationOperationConfirmation, *, actor_scope):
    run = draft.recommendation_run
    operation = draft.pending_business_operation
    if run is None or operation is None:
        raise ValueError("no recommendation start preview is pending")
    if operation.operation_type != BusinessOperationType.START_RECOMMENDATION:
        raise ValueError("pending operation is not recommendation start")
    if operation.status != BusinessOperationStatus.AWAITING_CONFIRMATION or run.status != RecommendationRunStatus.AWAITING_CONFIRMATION:
        raise ValueError("recommendation operation is not awaiting confirmation")
    expected = (
        event.task_id == draft.task_id == run.agent_task_id == operation.agent_task_id
        and event.campaign_id == run.campaign_id == operation.campaign_id == draft.business_campaign_id
        and event.business_task_id == run.business_task_id == operation.business_task_id == draft.business_task_id
        and event.operation_id == run.operation_id == operation.operation_id
        and event.request_id == run.request_id == operation.request_id
        and event.confirmation_id == run.confirmation_id == operation.confirmation_id
        and event.form_revision == run.form_revision == operation.form_revision == draft.form_revision
        and event.payload_hash == run.payload_hash == operation.payload_hash
        and actor_scope.tenant_id == run.actor_tenant_id
        and actor_scope.user_id == run.actor_user_id
        and actor_scope.brand_id == run.actor_brand_id
    )
    if not expected:
        raise ValueError("recommend confirmation does not match the current actor/task/preview")
    updated = draft.model_copy(deep=True)
    updated.pending_business_operation.confirmed = True
    updated.pending_business_operation.status = BusinessOperationStatus.CONFIRMED
    updated.recommendation_run.status = RecommendationRunStatus.CONFIRMED
    updated.next_action = NextAction.RESERVE_BUSINESS_OPERATION
    updated.pending_ui = None
    return updated


def build_recommendation_request(draft: WorkflowDraft, *, actor_scope) -> RecommendationStartRequest:
    run = draft.recommendation_run
    operation = draft.pending_business_operation
    if run is None or operation is None or not operation.confirmed:
        raise ValueError("recommendation start lacks an independent explicit user confirmation")
    if operation.operation_type != BusinessOperationType.START_RECOMMENDATION:
        raise ValueError("Task create confirmation cannot authorize recommendation start")
    if operation.status != BusinessOperationStatus.CONFIRMED or run.status != RecommendationRunStatus.CONFIRMED or run.start_attempted:
        raise ValueError("recommendation operation is not in a confirmed ready state")
    if not (
        actor_scope.tenant_id == run.actor_tenant_id
        and actor_scope.user_id == run.actor_user_id
        and actor_scope.brand_id == run.actor_brand_id
    ):
        raise ValueError("recommend actor does not match the actor who reviewed the preview")
    if draft.form_revision != run.form_revision:
        raise ValueError("recommendation preview is stale")
    actual_hash = hashlib.sha256(run.canonical_payload.encode("utf-8")).hexdigest()
    if actual_hash != run.payload_hash or operation.payload_hash != run.payload_hash:
        raise ValueError("recommendation preview payload hash is invalid")
    return RecommendationStartRequest(
        actor_tenant_id=run.actor_tenant_id,
        actor_user_id=run.actor_user_id,
        actor_brand_id=run.actor_brand_id,
        campaign_id=run.campaign_id,
        business_task_id=run.business_task_id,
        agent_task_id=run.agent_task_id,
        operation_id=run.operation_id,
        request_id=run.request_id,
        idempotency_key=run.idempotency_key,
        confirmation_id=run.confirmation_id,
        form_revision=run.form_revision,
        payload_hash=run.payload_hash,
        canonical_payload=run.canonical_payload,
    )


def classify_java_status(raw_status: str) -> JavaTaskStatus | None:
    try:
        return JavaTaskStatus(raw_status.lower())
    except (ValueError, AttributeError):
        return None


def decide_recommendation_recovery(run: RecommendationRun) -> RecommendationRecoveryDecision:
    """Choose a local recovery read/write boundary without restarting an unknown write."""

    if run.status == RecommendationRunStatus.AWAITING_CONFIRMATION:
        return RecommendationRecoveryDecision(
            action=RecommendationRecoveryAction.WAIT_FOR_CONFIRMATION,
            reason="recommendation still needs an independent user confirmation",
        )
    if run.status == RecommendationRunStatus.CONFIRMED and not run.start_attempted:
        return RecommendationRecoveryDecision(
            action=RecommendationRecoveryAction.START_ONCE,
            reason="recommendation confirmation exists and no start attempt was recorded",
            may_start_write=True,
        )
    if run.status == RecommendationRunStatus.CONFIRMED and run.start_attempted:
        return RecommendationRecoveryDecision(
            action=RecommendationRecoveryAction.MANUAL_RECONCILIATION,
            reason="a start attempt was recorded but no correlated response is available",
        )
    if run.status in {
        RecommendationRunStatus.ACCEPTED,
        RecommendationRunStatus.PENDING,
        RecommendationRunStatus.IN_PROGRESS,
    }:
        return RecommendationRecoveryDecision(
            action=RecommendationRecoveryAction.QUERY_TASK,
            reason="the Java start was accepted or the task is still running",
        )
    if run.status == RecommendationRunStatus.RECOMMEND_COMPLETED:
        return RecommendationRecoveryDecision(
            action=RecommendationRecoveryAction.READ_RESULTS,
            reason="Java reports recommendation completion; read results before projecting candidates",
        )
    if run.status in {
        RecommendationRunStatus.UNKNOWN,
        RecommendationRunStatus.RECONCILIATION_REQUIRED,
    }:
        return RecommendationRecoveryDecision(
            action=RecommendationRecoveryAction.MANUAL_RECONCILIATION,
            reason="write or read outcome is uncertain; query and reconcile without starting again",
        )
    return RecommendationRecoveryDecision(
        action=RecommendationRecoveryAction.STOP,
        reason="recommendation run is terminal and cannot be resumed automatically",
    )


def apply_start_result(run: RecommendationRun, result: AdapterResponse | OperationReceipt) -> RecommendationRun:
    updated = run.model_copy(deep=True)
    updated.start_attempted = True
    if isinstance(result, OperationReceipt):
        if result.status == OperationStatus.UNKNOWN:
            updated.status = RecommendationRunStatus.UNKNOWN
        else:
            updated.status = RecommendationRunStatus.FAILED
        updated.reconciliation_required = result.status == OperationStatus.UNKNOWN or bool(
            result.error and result.error.reconciliation_required
        )
        updated.write_outcome_unknown = result.status == OperationStatus.UNKNOWN
        updated.last_error_kind = result.error.kind.value if result.error else "unknown"
        return updated
    if result.operation_id != run.operation_id or result.request_id != run.request_id or result.payload_hash != run.payload_hash:
        updated.status = RecommendationRunStatus.UNKNOWN
        updated.reconciliation_required = True
        updated.write_outcome_unknown = True
        updated.last_error_kind = "response_operation_mismatch"
        return updated
    if not isinstance(result.value, RecommendationStartAccepted) or not result.value.accepted:
        updated.status = RecommendationRunStatus.UNKNOWN
        updated.reconciliation_required = True
        updated.write_outcome_unknown = True
        updated.last_error_kind = "invalid_acceptance_response"
        return updated
    updated.status = RecommendationRunStatus.ACCEPTED
    updated.write_outcome_unknown = False
    return updated


def apply_task_poll(run: RecommendationRun, task: JavaTask, *, poll_sequence: int) -> RecommendationRun:
    if task.business_task_id != run.business_task_id or task.campaign_id != run.campaign_id:
        raise ValueError("polled Java Task does not belong to recommendation run")
    if poll_sequence <= run.poll_sequence or run.result_ref is not None:
        return run.model_copy(deep=True)
    updated = run.model_copy(deep=True)
    if updated.write_outcome_unknown:
        updated.poll_sequence = poll_sequence
        updated.poll_attempts += 1
        updated.raw_java_status = task.status
        updated.progress = task.progress
        updated.current_stage = task.current_stage
        if task.downstream_task_id:
            updated.downstream_task_id = task.downstream_task_id
        updated.status = RecommendationRunStatus.RECONCILIATION_REQUIRED
        updated.reconciliation_required = True
        updated.last_error_kind = "write_outcome_still_unknown"
        return updated
    mapped = classify_java_status(task.status)
    updated.poll_sequence = poll_sequence
    updated.poll_attempts += 1
    updated.consecutive_read_failures = 0
    updated.raw_java_status = task.status
    updated.progress = task.progress
    updated.current_stage = task.current_stage
    if task.downstream_task_id:
        if updated.downstream_task_id and task.downstream_task_id != updated.downstream_task_id:
            updated.status = RecommendationRunStatus.UNKNOWN
            updated.reconciliation_required = True
            updated.last_error_kind = "downstream_task_id_changed"
            return updated
        updated.downstream_task_id = task.downstream_task_id
    if mapped is None:
        updated.status = RecommendationRunStatus.UNKNOWN
        updated.reconciliation_required = True
        updated.last_error_kind = "unknown_java_task_status"
    elif mapped == JavaTaskStatus.PENDING:
        updated.business_task_status = mapped
        updated.status = RecommendationRunStatus.PENDING
        updated.write_outcome_unknown = False
    elif mapped == JavaTaskStatus.IN_PROGRESS:
        updated.business_task_status = mapped
        updated.status = RecommendationRunStatus.IN_PROGRESS
        updated.write_outcome_unknown = False
    elif mapped == JavaTaskStatus.RECOMMEND_COMPLETED:
        updated.business_task_status = mapped
        updated.status = RecommendationRunStatus.RECOMMEND_COMPLETED
        updated.progress = 100
        updated.write_outcome_unknown = False
    elif mapped == JavaTaskStatus.FAILED:
        updated.business_task_status = mapped
        updated.status = RecommendationRunStatus.FAILED
        updated.write_outcome_unknown = False
    else:
        updated.status = RecommendationRunStatus.UNKNOWN
        updated.reconciliation_required = True
        updated.last_error_kind = "task_ended_without_recommendation_context"
    return updated


def mark_read_error(run: RecommendationRun, error: AdapterError, *, source: str = "task") -> RecommendationRun:
    updated = run.model_copy(deep=True)
    updated.last_error_kind = error.kind.value
    if error.retryable:
        # bounded consecutive local policy: four read failures, after which stop
        # autonomous polling and keep the run available for manual reconciliation.
        if source == "results":
            updated.consecutive_result_read_failures += 1
            failures = updated.consecutive_result_read_failures
        else:
            updated.consecutive_read_failures += 1
            failures = updated.consecutive_read_failures
        if failures >= 4:
            updated.status = RecommendationRunStatus.RECONCILIATION_REQUIRED
            updated.reconciliation_required = True
        return updated
    updated.status = RecommendationRunStatus.UNKNOWN
    updated.reconciliation_required = True
    return updated


def project_candidate(candidate) -> CandidatePreview:
    detail: JavaCandidateDetail | None = candidate.kol_detail
    reason = detail.recommendation_reason if detail else None
    excerpt = reason[:240] if reason else None
    return CandidatePreview(
        kol_backend_id=candidate.kol_backend_id,
        source=candidate.source,
        kol_name=candidate.kol_name or (detail.name if detail else None),
        tier=detail.tier if detail else None,
        avatar_url=_safe_https_url(detail.avatar_url) if detail else None,
        homepage_url=_safe_https_url(detail.homepage_url) if detail else None,
        shortlist=detail.shortlist if detail else None,
        backup=detail.backup if detail else None,
        recommendation_reason_excerpt=excerpt,
        selected=candidate.selected,
        rejected=candidate.rejected,
        reject_reason=candidate.reject_reason,
    )


def _safe_https_url(value: str | None) -> str | None:
    if not value or not value.startswith("https://") or len(value) > 2_048:
        return None
    return value


def recommendation_content_hash(results: JavaRecommendationResults) -> str:
    return hashlib.sha256(results.model_dump_json(by_alias=True, exclude_none=True).encode()).hexdigest()


def validate_results_and_make_snapshot(run: RecommendationRun, results: JavaRecommendationResults):
    if run.status != RecommendationRunStatus.RECOMMEND_COMPLETED:
        raise ValueError("results can only be snapshotted after Java recommend_completed")
    if not run.downstream_task_id:
        raise ValueError("completed recommendation lacks downstreamTaskId")
    ids = [candidate.kol_backend_id for candidate in results.kols]
    if len(ids) != len(set(ids)):
        raise ValueError("recommendation results contain duplicate KOL IDs")
    digest = recommendation_content_hash(results)
    snapshot_ref = uuid4()
    preview = [project_candidate(candidate) for candidate in results.kols[:20]]
    snapshot = RecommendationSnapshotReference(
        snapshot_ref=snapshot_ref,
        campaign_id=run.campaign_id,
        business_task_id=run.business_task_id,
        downstream_task_id=run.downstream_task_id,
        operation_id=run.operation_id,
        request_id=run.request_id,
        fetched_at=datetime.now(timezone.utc),
        candidate_count=len(results.kols),
        content_hash=digest,
        response_complete=True,
        candidate_universe_complete=False,
        snapshot_uri=f"java://smartproposal/campaigns/{run.campaign_id}/tasks/{run.business_task_id}/results",
        java_persistence_verified=False,
        preview=preview,
    )
    projection = CandidateProjection(
        campaign_id=run.campaign_id,
        business_task_id=run.business_task_id,
        downstream_task_id=run.downstream_task_id,
        run_id=run.run_id,
        snapshot_ref=snapshot_ref,
        snapshot_hash=digest,
        fetched_count=len(results.kols),
        preview_count=len(preview),
        omitted_count=len(results.kols) - len(preview),
        total_count=None,
        truncated=len(results.kols) > len(preview),
        response_complete=True,
        candidate_universe_complete=False,
        durable=False,
        items=preview,
    )
    updated = run.model_copy(deep=True)
    updated.result_ref = snapshot_ref
    updated.result_hash = digest
    updated.snapshot = snapshot
    return updated, snapshot, projection
