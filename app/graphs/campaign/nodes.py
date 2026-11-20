"""Workflow nodes for the local draft and validation slice."""

from uuid import uuid4

from langgraph.types import interrupt
from pydantic import TypeAdapter, ValidationError

from app.graphs.campaign.state import AgentState
from app.schemas.business_api import (
    AdapterError,
    AdapterResponse,
    BusinessOperationConfirmation,
    CampaignCreated,
    CampaignDetails,
    OperationReceipt,
    OperationStatus,
    TaskCreated,
)
from app.schemas.recommendation import (
    CandidateProjection,
    JavaRecommendationResults,
    JavaTask,
    JavaTaskStatus,
    PollDisposition,
    RecommendationOperationConfirmation,
    RecommendationPollEvent,
    RecommendationOperationResult,
    RecommendationError,
    RecommendationExecutionPayload,
    RecommendationReadRequest,
    RecommendationRunStatus,
    RecommendationStartRequested,
)
from app.schemas.domain import (
    AgentWorkflowStage,
    BusinessOperationStatus,
    BusinessOperationType,
    BusinessTaskStatus,
    CampaignBindingMode,
    NextAction,
)
from app.schemas.events import (
    AgentEvent,
    CampaignSelectionEvent, ConfirmSelection, FormPatchEvent, SelectionPatchEvent,
    SelectionReviewRequested, SelectionSaveRequested, UserMessage,
)
from app.services.contracts import BusinessServices
from app.services.contracts import RecommendationSnapshotStore
from app.services.draft_service import (
    apply_campaign_selection,
    apply_form_patch,
    build_campaign_create_request,
    build_task_create_request,
    confirm_business_operation,
    prepare_business_operation,
    validate_and_prepare,
)
from app.services.recommendations import (
    _task_read_request,
    _trusted_task_response,
    apply_start_result,
    apply_task_poll,
    build_recommendation_start_request as build_start_request,
    confirm_recommendation,
    mark_read_error,
    prepare_recommendation_preview as create_recommendation_preview,
    validate_results_and_make_snapshot,
)
from app.services.selections import (
    apply_selection_patch, bind_selection_snapshot, check_selection_intent,
    review_candidates, selection_results,
)


class WorkflowNotImplemented(NotImplementedError):
    """Raised when a later external or durable capability is reached."""


def prepare_context(state: AgentState) -> AgentState:
    # External authorization, brand rules and memory are deliberately deferred;
    # this node only establishes a stable local state for the first slice.
    if "draft" not in state:
        raise ValueError("workflow state is missing draft")
    return {"schema_version": state.get("schema_version", 1), "graph_version": state.get("graph_version", 1)}


def interpret_event(state: AgentState) -> AgentState:
    event = state.get("incoming_event")
    if event is None:
        raise ValueError("workflow state is missing incoming_event")
    if isinstance(event, (
        FormPatchEvent,
        CampaignSelectionEvent,
        BusinessOperationConfirmation,
        RecommendationStartRequested,
        RecommendationOperationConfirmation,
        RecommendationPollEvent,
        SelectionReviewRequested,
        SelectionPatchEvent,
        SelectionSaveRequested,
        ConfirmSelection,
    )):
        return {}
    if isinstance(event, UserMessage):
        # Natural-language extraction is a later substage. Do not pretend text
        # has been converted into a patch.
        raise WorkflowNotImplemented("interpret_event: natural-language extraction is not implemented")
    raise ValueError("interpret_event: unsupported structured event")


def apply_event(state: AgentState) -> AgentState:
    draft = state["draft"]
    event = state["incoming_event"]
    if isinstance(event, BusinessOperationConfirmation):
        if "actor_scope" not in state:
            raise ValueError("trusted actor scope is required to confirm a business operation")
        return {
            "draft": confirm_business_operation(draft, event, actor_scope=state["actor_scope"]),
            "applied_event_id": str(event.client_event_id),
        }
    if isinstance(event, FormPatchEvent):
        return {"draft": apply_form_patch(draft, event), "applied_event_id": str(event.client_event_id)}
    if isinstance(event, CampaignSelectionEvent):
        return {"draft": apply_campaign_selection(draft, event), "applied_event_id": str(event.client_event_id)}
    raise WorkflowNotImplemented("apply_event: this event is handled by a later substage")


def validate_and_route(state: AgentState) -> AgentState:
    draft = validate_and_prepare(state["draft"])
    return {"draft": draft}


async def prepare_recommendation_confirmation_preview(
    state: AgentState, *, business_services: BusinessServices | None
) -> AgentState:
    event = state.get("incoming_event")
    draft = state["draft"]
    if not isinstance(event, RecommendationStartRequested):
        raise ValueError("recommendation preview requires recommendation.start_requested")
    if event.task_id != draft.task_id:
        raise ValueError("recommendation start event task_id does not match workflow task_id")
    if event.expected_form_revision != draft.form_revision:
        raise ValueError("recommendation start request uses a stale form revision")
    if "actor_scope" not in state or state.get("trusted_brand") is None:
        raise ValueError("trusted actor and authorized brand are required before recommend")
    if business_services is None:
        raise WorkflowNotImplemented("Java Task detail service is not configured")
    if draft.business_campaign_id is None or draft.business_task_id is None:
        raise ValueError("recommend requires completed Campaign and Task create operations")

    request = RecommendationReadRequest(
        actor_tenant_id=state["actor_scope"].tenant_id,
        actor_user_id=state["actor_scope"].user_id,
        actor_brand_id=state["actor_scope"].brand_id,
        campaign_id=draft.business_campaign_id,
        business_task_id=draft.business_task_id,
        downstream_task_id=draft.downstream_task_id,
        operation_id=uuid4(),
        request_id=uuid4(),
    )
    task_result = await business_services.read_task(request)
    if isinstance(task_result, AdapterError):
        return {"operation_receipt": OperationReceipt(
            status=OperationStatus.UNKNOWN if task_result.retryable or task_result.reconciliation_required else OperationStatus.REJECTED,
            operation_id=request.operation_id,
            request_id=request.request_id,
            payload_hash="0" * 64,
            campaign_id=request.campaign_id,
            business_task_id=request.business_task_id,
            error=task_result,
        )}
    if not isinstance(task_result, AdapterResponse) or not isinstance(task_result.value, JavaTask):
        raise ValueError("Java Task detail did not return typed Task data")
    task = task_result.value
    if task.campaign_id != draft.business_campaign_id or task.business_task_id != draft.business_task_id:
        raise ValueError("Java Task detail does not match the current workflow")
    if task.status.lower() != JavaTaskStatus.PENDING.value:
        raise ValueError("Java Task is not pending; cannot create a recommendation start preview")
    if task.downstream_task_id is not None:
        raise ValueError("pending Java Task already has a downstream recommendation; reconcile before starting")
    if draft.recommendation_run is not None and draft.recommendation_run.status in {
        RecommendationRunStatus.ACCEPTED,
        RecommendationRunStatus.PENDING,
        RecommendationRunStatus.IN_PROGRESS,
        RecommendationRunStatus.UNKNOWN,
        RecommendationRunStatus.RECONCILIATION_REQUIRED,
    }:
        raise ValueError("an existing recommendation run must be reconciled; never start a duplicate")
    updated, preview = create_recommendation_preview(
        draft, actor_scope=state["actor_scope"], trusted_brand=state.get("trusted_brand"), task=task
    )
    return {"draft": updated, "recommendation_preview": preview}


async def prepare_recommendation_preview(state: AgentState, *, business_services=None) -> AgentState:
    return await prepare_recommendation_confirmation_preview(state, business_services=business_services)


def apply_recommendation_confirmation(state: AgentState) -> AgentState:
    event = state.get("incoming_event")
    if not isinstance(event, RecommendationOperationConfirmation):
        raise ValueError("recommendation confirmation event is required")
    if "actor_scope" not in state:
        raise ValueError("trusted actor scope is required to confirm recommendation start")
    updated = confirm_recommendation(state["draft"], event, actor_scope=state["actor_scope"])
    return {"draft": updated, "applied_event_id": str(event.client_event_id)}


async def execute_recommendation_operation(
    state: AgentState,
    *,
    business_services: BusinessServices | None,
    snapshot_store: RecommendationSnapshotStore | None,
) -> AgentState:
    draft = state["draft"]
    run = draft.recommendation_run
    event = state.get("incoming_event")
    if run is None:
        raise ValueError("recommendation run is missing")
    if "actor_scope" not in state:
        raise ValueError("trusted actor scope is required for recommendation operation")
    if (
        state["actor_scope"].tenant_id != run.actor_tenant_id
        or state["actor_scope"].user_id != run.actor_user_id
        or state["actor_scope"].brand_id != run.actor_brand_id
    ):
        raise ValueError("current actor scope does not match recommendation preview")
    trusted_brand = state.get("trusted_brand")
    if trusted_brand is None or trusted_brand.brand_id != run.actor_brand_id:
        raise ValueError("current authorized brand does not match recommendation preview")
    if business_services is None:
        raise WorkflowNotImplemented("Java recommendation adapter is not configured")

    if isinstance(event, RecommendationOperationConfirmation):
        draft = confirm_recommendation(draft, event, actor_scope=state["actor_scope"])
        run = draft.recommendation_run
        state = {**state, "draft": draft}
        # Re-read the task immediately before the write to check actor/task/brand-state guards.
        request = _task_read_request(run)
        task_result = await business_services.read_task(request)
        if isinstance(task_result, AdapterError):
            updated_run = mark_read_error(run, task_result)
            updated_run.status = RecommendationRunStatus.RECONCILIATION_REQUIRED
            updated_run.reconciliation_required = True
            return _state_with_run(draft, updated_run, PollDisposition.UNKNOWN, error=task_result.kind.value)
        task = _trusted_task_response(task_result, run)
        preview_payload = RecommendationExecutionPayload.model_validate_json(run.canonical_payload)
        if (
            task.status.lower() != JavaTaskStatus.PENDING.value
            or (
                preview_payload.platform is not None
                and task.platform != preview_payload.platform
            )
            or task.downstream_task_id != preview_payload.downstream_task_id
        ):
            updated = draft.model_copy(deep=True)
            updated.recommendation_run.status = RecommendationRunStatus.RECONCILIATION_REQUIRED
            updated.recommendation_run.reconciliation_required = True
            updated.recommendation_run.last_error_kind = "task_no_longer_pending"
            updated.next_action = NextAction.OPERATION_BLOCKED
            return {"draft": updated}
        start_request = build_start_request(run)
        # The adapter performs a single POST and never retries. A duplicate event cannot
        # repeat it because successful/unknown status is not STARTING/CONFIRMED anymore.
        result = await business_services.start_recommendation(start_request)
        updated_run = apply_start_result(run, result)
        updated = draft.model_copy(deep=True)
        updated.recommendation_run = updated_run
        if isinstance(result, OperationReceipt):
            updated.pending_business_operation.status = (
                BusinessOperationStatus.UNKNOWN
                if result.status == OperationStatus.UNKNOWN
                else BusinessOperationStatus.REJECTED
            )
            updated.next_action = NextAction.OPERATION_BLOCKED
            return {"draft": updated, "operation_receipt": result}
        if updated_run.status in {
            RecommendationRunStatus.UNKNOWN,
            RecommendationRunStatus.RECONCILIATION_REQUIRED,
        }:
            updated.pending_business_operation.status = BusinessOperationStatus.UNKNOWN
            updated.next_action = NextAction.OPERATION_BLOCKED
        else:
            updated.pending_business_operation.status = BusinessOperationStatus.ACCEPTED
            updated.stage = AgentWorkflowStage.RECOMMENDATION_RUNNING
        return {"draft": updated, "recommendation_result": RecommendationOperationResult(
            disposition=PollDisposition.CONTINUE, run=updated_run,
        )}

    if not isinstance(event, RecommendationPollEvent):
        raise ValueError("recommendation operation only accepts confirmation or internal poll events")
    if (
        event.task_id != run.agent_task_id
        or event.operation_id != run.operation_id
        or event.request_id != run.request_id
    ):
        raise ValueError("poll event does not match the current recommendation run")
    if run.result_ref is not None:
        # A committed local snapshot is immutable. A late poll cannot replace its
        # card or reopen a selection operation, even with a higher sequence.
        return {}
    if event.poll_sequence <= run.poll_sequence:
        return {"draft": draft, "recommendation_result": RecommendationOperationResult(
            disposition=PollDisposition.CONTINUE, run=run,
        )}
    recovery_unknown = run.status in {
        RecommendationRunStatus.UNKNOWN,
        RecommendationRunStatus.RECONCILIATION_REQUIRED,
    }
    if run.status not in {
        RecommendationRunStatus.ACCEPTED,
        RecommendationRunStatus.PENDING,
        RecommendationRunStatus.IN_PROGRESS,
        RecommendationRunStatus.RECOMMEND_COMPLETED,
        RecommendationRunStatus.UNKNOWN,
        RecommendationRunStatus.RECONCILIATION_REQUIRED,
    }:
        raise ValueError("run is not in a queryable status")
    read_request = _task_read_request(run)
    task_result = await business_services.read_task(read_request)
    if isinstance(task_result, AdapterError):
        updated_run = mark_read_error(run, task_result)
        return _state_with_run(draft, updated_run, PollDisposition.UNKNOWN, error=task_result.kind.value)
    task = _trusted_task_response(task_result, run)
    updated_run = apply_task_poll(run, task, poll_sequence=event.poll_sequence)
    if recovery_unknown and updated_run.status in {
        RecommendationRunStatus.UNKNOWN,
        RecommendationRunStatus.RECONCILIATION_REQUIRED,
    }:
        return _state_with_run(draft, updated_run, PollDisposition.UNKNOWN)
    if updated_run.status != RecommendationRunStatus.RECOMMEND_COMPLETED:
        disposition = PollDisposition.FAILED if updated_run.status == RecommendationRunStatus.FAILED else PollDisposition.UNKNOWN if updated_run.status in {RecommendationRunStatus.UNKNOWN, RecommendationRunStatus.RECONCILIATION_REQUIRED} else PollDisposition.CONTINUE
        return _state_with_run(draft, updated_run, disposition)

    result_request = _task_read_request(updated_run)
    result_result = await business_services.read_recommendation_results(result_request)
    if isinstance(result_result, AdapterError):
        updated_run = mark_read_error(updated_run, result_result, source="results")
        return _state_with_run(draft, updated_run, PollDisposition.UNKNOWN, error=result_result)
    if not isinstance(result_result, AdapterResponse) or not isinstance(result_result.value, JavaRecommendationResults):
        raise ValueError("Java recommendation results are not typed")
    if result_result.operation_id != run.operation_id or result_result.request_id != run.request_id:
        updated_run.status = RecommendationRunStatus.UNKNOWN
        updated_run.reconciliation_required = True
        updated_run.last_error_kind = "result_operation_mismatch"
        return _state_with_run(draft, updated_run, PollDisposition.UNKNOWN)
    updated_run, snapshot, projection = validate_results_and_make_snapshot(updated_run, result_result.value)
    updated_run.consecutive_result_read_failures = 0
    if snapshot_store is not None:
        snapshot_store.put(snapshot, result_result.value)
    updated = draft.model_copy(deep=True)
    updated.recommendation_run = updated_run
    updated.candidate_snapshot_ref = str(snapshot.snapshot_ref)
    updated.result_ref = str(snapshot.snapshot_ref)
    updated.stage = AgentWorkflowStage.CANDIDATE_REVIEW
    updated.business_task_status = BusinessTaskStatus.RECOMMEND_COMPLETED
    updated.downstream_task_id = updated_run.downstream_task_id
    updated.next_action = NextAction.NONE
    bind_selection_snapshot(updated, result_result.value)
    return {"draft": updated, "candidate_projection": projection,
            "candidate_review_page": None, "selection_receipt": None,
            "recommendation_result": RecommendationOperationResult(
                disposition=PollDisposition.COMPLETED, run=updated_run,
                snapshot_ref=snapshot, projection=projection,
            )}


def _state_with_run(draft, run, disposition, error=None):
    updated = draft.model_copy(deep=True)
    updated.recommendation_run = run
    updated.business_task_status = {
        JavaTaskStatus.PENDING: BusinessTaskStatus.PENDING,
        JavaTaskStatus.IN_PROGRESS: BusinessTaskStatus.IN_PROGRESS,
        JavaTaskStatus.RECOMMEND_COMPLETED: BusinessTaskStatus.RECOMMEND_COMPLETED,
        JavaTaskStatus.FAILED: BusinessTaskStatus.FAILED,
        JavaTaskStatus.ENDED: BusinessTaskStatus.ENDED,
    }.get(run.business_task_status)
    if run.downstream_task_id is not None:
        updated.downstream_task_id = run.downstream_task_id
    if run.status in {RecommendationRunStatus.UNKNOWN, RecommendationRunStatus.RECONCILIATION_REQUIRED}:
        updated.next_action = NextAction.OPERATION_BLOCKED
    error_model = RecommendationError(
        kind=error.kind.value if isinstance(error, AdapterError) else str(error),
        message="recommendation operation could not be completed",
    ) if error is not None else None
    return {"draft": updated, "recommendation_result": RecommendationOperationResult(
        disposition=disposition, run=run, error=error_model,
    )}


def prepare_operation_preview(state: AgentState) -> AgentState:
    if "actor_scope" not in state:
        raise ValueError("trusted actor scope is required to preview a business operation")
    draft = prepare_business_operation(
        state["draft"],
        actor_scope=state["actor_scope"],
        trusted_brand=state.get("trusted_brand"),
    )
    return {"draft": draft}


def wait_for_user(state: AgentState) -> AgentState:
    draft = state["draft"]
    pending = draft.pending_ui.model_dump(mode="json") if draft.pending_ui else {
        "type": "user_input",
        "form_revision": draft.form_revision,
    }
    resumed = interrupt({
        "type": "pending_ui",
        "workflow_id": str(draft.task_id),
        "pending_ui": pending,
        "form_revision": draft.form_revision,
        "selection_revision": draft.selection.revision,
    })
    try:
        event = TypeAdapter(AgentEvent).validate_python(resumed)
    except ValidationError as exc:
        raise ValueError("resume payload is not a supported structured event") from exc
    if event.task_id != draft.task_id:
        raise ValueError("resume event targets a different workflow")
    return {"incoming_event": event, "pending_ui_ref": None}


async def reserve_business_operation(
    state: AgentState, *, business_services: BusinessServices | None = None
) -> AgentState:
    # TODO(operations, design §4.3): atomically reserve campaign/task request
    # and outbox; reconcile unknown outcomes before any repeat submission.
    draft = state["draft"]
    if draft.next_action != NextAction.RESERVE_BUSINESS_OPERATION:
        raise ValueError("business operation is not ready")
    if not draft.pending_business_operation or not draft.pending_business_operation.confirmed:
        raise ValueError("business operation requires explicit current user confirmation")
    operation = draft.pending_business_operation
    if "actor_scope" not in state:
        raise ValueError("trusted actor scope is required for business operation")
    if business_services is None:
        raise WorkflowNotImplemented("business adapter is not configured")
    if operation.operation_type == BusinessOperationType.CREATE_CAMPAIGN:
        brand = state.get("trusted_brand")
        if brand is None:
            raise ValueError("authorized brand list entry is required for Campaign creation")
        request = build_campaign_create_request(draft, actor_scope=state["actor_scope"], trusted_brand=brand)
        result = await business_services.create_campaign(request)
        if isinstance(result, AdapterResponse) and isinstance(result.value, CampaignCreated):
            updated = draft.model_copy(deep=True)
            updated.business_campaign_id = result.value.campaign_id
            updated.stage = AgentWorkflowStage.CAMPAIGN_CREATED
            updated.campaign_binding.mode = CampaignBindingMode.EXISTING
            updated.campaign_binding.campaign_id = result.value.campaign_id
            updated.pending_business_operation = None
            ready = validate_and_prepare(updated)
            if ready.validation_errors or ready.ambiguities:
                ready.stage = AgentWorkflowStage.AWAITING_CLARIFICATION
                ready.next_action = NextAction.WAIT_FOR_USER
                return {"draft": ready}
            ready.stage = AgentWorkflowStage.TASK_CREATING
            ready = prepare_business_operation(
                ready,
                actor_scope=state["actor_scope"],
                trusted_brand=state.get("trusted_brand"),
            )
            return {"draft": ready}
    else:
        request = build_task_create_request(draft, actor_scope=state["actor_scope"])
        campaign_read = await business_services.read_campaign(request.campaign_id, state["actor_scope"])
        if not isinstance(campaign_read, AdapterResponse) or not isinstance(campaign_read.value, CampaignDetails):
            if isinstance(campaign_read, AdapterError):
                updated = draft.model_copy(deep=True)
                updated.pending_business_operation.status = (
                    BusinessOperationStatus.UNKNOWN
                    if campaign_read.reconciliation_required
                    else BusinessOperationStatus.REJECTED
                )
                updated.next_action = NextAction.OPERATION_BLOCKED
                return {
                    "draft": updated,
                    "operation_receipt": OperationReceipt(
                        status=OperationStatus.UNKNOWN if campaign_read.reconciliation_required else OperationStatus.REJECTED,
                        operation_id=operation.operation_id,
                        request_id=operation.request_id,
                        payload_hash=operation.payload_hash,
                        campaign_id=request.campaign_id,
                        error=campaign_read,
                    )
                }
            raise ValueError("existing Campaign authorization could not be verified")
        trusted_brand = state.get("trusted_brand")
        if (
            trusted_brand is None
            or campaign_read.value.brand_name != trusted_brand.brand_name
            or campaign_read.value.brand_code != trusted_brand.brand_code
        ):
            raise ValueError("existing Campaign does not match current authorized brand context")
        result = await business_services.create_task(request)
        if isinstance(result, AdapterResponse) and isinstance(result.value, TaskCreated):
            updated = draft.model_copy(deep=True)
            updated.business_task_id = result.value.business_task_id
            updated.stage = AgentWorkflowStage.TASK_CREATED
            updated.business_task_status = BusinessTaskStatus.PENDING
            updated.pending_business_operation = None
            return {"draft": updated}
    if isinstance(result, OperationReceipt):
        updated = draft.model_copy(deep=True)
        updated.pending_business_operation.status = (
            BusinessOperationStatus.UNKNOWN
            if result.status == OperationStatus.UNKNOWN
            else BusinessOperationStatus.REJECTED
        )
        updated.next_action = NextAction.OPERATION_BLOCKED
        return {"draft": updated, "operation_receipt": result}
    raise ValueError("business adapter returned an invalid typed response")


def handle_selection_event(state: AgentState, *, snapshot_store: RecommendationSnapshotStore) -> AgentState:
    event = state["incoming_event"]
    actor = state.get("actor_scope")
    if not isinstance(event, (SelectionReviewRequested, SelectionPatchEvent, SelectionSaveRequested, ConfirmSelection)):
        raise ValueError("selection requires a typed event")
    brand = state.get("trusted_brand")
    if brand is not None and (actor is None or brand.brand_id != actor.brand_id):
        raise ValueError("selection actor does not match the current brand context")
    draft = state["draft"].model_copy(deep=True)
    results = selection_results(draft, event, actor, snapshot_store)
    bind_selection_snapshot(draft, results)
    output: AgentState = {
        "draft": draft, "selection_receipt": None, "candidate_review_page": None,
        "applied_event_id": str(event.client_event_id),
    }
    if isinstance(event, SelectionReviewRequested):
        output["candidate_review_page"] = review_candidates(results, event)
    elif isinstance(event, SelectionPatchEvent):
        apply_selection_patch(draft, event, results)
    else:
        output["selection_receipt"] = check_selection_intent(draft, event, actor, results)
    return output
