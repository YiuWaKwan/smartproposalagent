"""Deterministic draft patching and validation for the first vertical slice."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from app.schemas.domain import (
    AgentWorkflowStage,
    BusinessOperationType,
    BusinessOperationStatus,
    CampaignDraft,
    CampaignBindingMode,
    FieldProvenance,
    NextAction,
    PendingUI,
    PendingBusinessOperation,
    RecommendationTaskDraft,
    ValidationIssue,
    WorkflowDraft,
)
from app.schemas.events import CampaignSelectionEvent, FormPatchEvent
from app.schemas.business_api import (
    ActorScope,
    BusinessOperationConfirmation,
    CampaignCreateRequest,
    TaskCreateRequest,
    TrustedBrand,
)
from app.schemas.recommendation import RecommendationRunStatus
from app.schemas.patch import FieldPatch, FormPatch
from app.services.adapters.smartproposal import (
    campaign_payload_from_draft,
    canonical_json,
    canonical_payload_hash,
    task_payload_from_draft,
)


_CAMPAIGN_FIELDS = frozenset(CampaignDraft.model_fields)
_TASK_FIELDS = frozenset(RecommendationTaskDraft.model_fields)
_REQUIRED_CAMPAIGN_FIELDS = ("platform", "persona", "product", "selling_point")
_REQUIRED_TASK_FIELDS = ("kol_count", "budget", "tier_distribution", "talent_type")


def apply_form_patch(draft: WorkflowDraft, event: FormPatchEvent) -> WorkflowDraft:
    """Apply a validated structured patch and increment revision once."""

    _validate_task_ownership(draft, event.task_id)
    if event.expected_form_revision != draft.form_revision:
        raise ValueError(
            f"stale form revision: expected {event.expected_form_revision}, current {draft.form_revision}"
        )

    updated = draft.model_copy(deep=True)
    run = updated.recommendation_run
    if run and run.status in {
        RecommendationRunStatus.ACCEPTED,
        RecommendationRunStatus.PENDING,
        RecommendationRunStatus.IN_PROGRESS,
        RecommendationRunStatus.UNKNOWN,
        RecommendationRunStatus.RECONCILIATION_REQUIRED,
        RecommendationRunStatus.RECOMMEND_COMPLETED,
    }:
        raise ValueError("Task fields cannot change while a recommendation run needs polling or reconciliation")
    existing_operation = updated.pending_business_operation
    if existing_operation and existing_operation.status in {
        BusinessOperationStatus.UNKNOWN,
        BusinessOperationStatus.IN_FLIGHT,
    }:
        raise ValueError("pending business operation requires reconciliation before further edits")
    changed = False
    for operation in event.patch.operations:
        changed = _apply_operation(updated, operation) or changed
    if event.patch.ambiguities:
        updated.ambiguities = list(event.patch.ambiguities)
        changed = True
    if changed:
        updated.form_revision += 1
        updated.field_sources.update(
            {
                f"{operation.target}.{operation.field}": FieldProvenance(
                    source="form_edit",
                    source_id=str(event.client_event_id),
                    updated_at=datetime.now(timezone.utc),
                )
                for operation in event.patch.operations
                if operation.op != "use_default"
            }
        )
        if not existing_operation or existing_operation.status != BusinessOperationStatus.UNKNOWN:
            updated.pending_business_operation = None
        if updated.recommendation_run and not updated.recommendation_run.start_attempted:
            updated.recommendation_run = None
    return updated


def apply_campaign_selection(draft: WorkflowDraft, event: CampaignSelectionEvent) -> WorkflowDraft:
    _validate_task_ownership(draft, event.task_id)
    if draft.pending_business_operation and draft.pending_business_operation.status in {
        BusinessOperationStatus.UNKNOWN,
        BusinessOperationStatus.IN_FLIGHT,
    }:
        raise ValueError("pending business operation requires reconciliation before Campaign rebinding")
    if event.expected_form_revision != draft.form_revision:
        raise ValueError(
            f"stale form revision: expected {event.expected_form_revision}, current {draft.form_revision}"
        )
    updated = draft.model_copy(deep=True)
    run = updated.recommendation_run
    if run and run.status not in {RecommendationRunStatus.AWAITING_CONFIRMATION, RecommendationRunStatus.CONFIRMED}:
        raise ValueError("Campaign cannot be rebound while recommendation requires reconciliation")
    updated.campaign_binding.mode = CampaignBindingMode.EXISTING
    updated.campaign_binding.campaign_id = event.campaign_id
    updated.form_revision += 1
    updated.pending_business_operation = None
    updated.recommendation_run = None
    updated.stage = AgentWorkflowStage.CAMPAIGN_SELECTION
    updated.field_sources["campaign_binding.campaign_id"] = FieldProvenance(
        source="form_edit",
        source_id=str(event.client_event_id),
        updated_at=datetime.now(timezone.utc),
    )
    return updated


def validate_and_prepare(draft: WorkflowDraft) -> WorkflowDraft:
    """Validate local business completeness without invoking an external service."""

    updated = draft.model_copy(deep=True)
    errors: list[ValidationIssue] = []
    if updated.campaign_binding.mode == CampaignBindingMode.EXISTING:
        if not updated.campaign_binding.campaign_id:
            errors.append(ValidationIssue(code="campaign_missing", field="campaign_id", message="请选择已有 Campaign"))
        elif updated.business_campaign_id not in {None, updated.campaign_binding.campaign_id}:
            errors.append(ValidationIssue(code="campaign_binding_conflict", field="campaign_id", message="Campaign 绑定与已创建 Campaign 不一致"))
    else:
        errors.extend(_missing_required(updated.campaign, _REQUIRED_CAMPAIGN_FIELDS, "campaign"))
    if updated.campaign_binding.mode == CampaignBindingMode.EXISTING:
        errors.extend(_missing_required(updated.recommendation_task, _REQUIRED_TASK_FIELDS, "recommendation_task"))
    if (
        updated.campaign_binding.mode == CampaignBindingMode.NEW
        and updated.campaign.start_date
        and updated.campaign.end_date
        and updated.campaign.start_date > updated.campaign.end_date
    ):
        errors.append(ValidationIssue(code="date_order_invalid", field="campaign.end_date", message="结束日期不能早于开始日期"))
    if updated.campaign_binding.mode == CampaignBindingMode.NEW and updated.campaign.budget is not None and updated.campaign.budget < 0:
        errors.append(ValidationIssue(code="campaign_budget_invalid", field="campaign.budget", message="Campaign 总预算不能为负数"))
    if updated.recommendation_task.budget is not None and updated.recommendation_task.budget <= 0:
        errors.append(ValidationIssue(code="task_budget_invalid", field="recommendation_task.budget", message="任务预算必须大于 0"))

    updated.validation_errors = errors
    updated.next_action = NextAction.NONE
    if updated.ambiguities or errors:
        updated.stage = AgentWorkflowStage.AWAITING_CLARIFICATION
        updated.next_action = NextAction.WAIT_FOR_USER
        updated.pending_ui = PendingUI(
            missing_fields=[issue.field for issue in errors if issue.code == "required_missing" and issue.field],
            ambiguities=list(updated.ambiguities),
            form_revision=updated.form_revision,
        )
        return updated

    if updated.campaign_binding.mode == CampaignBindingMode.EXISTING:
        updated.stage = AgentWorkflowStage.TASK_CREATING
    else:
        updated.stage = AgentWorkflowStage.CAMPAIGN_CREATING
    pending = updated.pending_business_operation
    if (
        pending is None
        or pending.agent_task_id != updated.task_id
        or pending.form_revision != updated.form_revision
    ):
        updated.pending_business_operation = None
        updated.pending_ui = None
        updated.next_action = NextAction.WAIT_FOR_OPERATION_CONFIRMATION
    elif pending.confirmed:
        updated.pending_ui = None
        if pending.status == BusinessOperationStatus.CONFIRMED:
            updated.next_action = NextAction.RESERVE_BUSINESS_OPERATION
        else:
            updated.next_action = NextAction.OPERATION_BLOCKED
    else:
        updated.pending_ui = PendingUI(
            type="business_operation_confirmation",
            form_revision=updated.form_revision,
        )
        updated.next_action = NextAction.WAIT_FOR_OPERATION_CONFIRMATION
    return updated


def prepare_business_operation(
    draft: WorkflowDraft,
    *,
    actor_scope: ActorScope,
    trusted_brand: TrustedBrand | None = None,
) -> WorkflowDraft:
    """Create a stable preview operation; no API call is made here."""

    updated = draft.model_copy(deep=True)
    if updated.campaign_binding.mode == CampaignBindingMode.NEW:
        if trusted_brand is None or trusted_brand.brand_id != actor_scope.brand_id:
            raise ValueError("trusted brand must match the authenticated actor brand")
        payload = campaign_payload_from_draft(updated.campaign, trusted_brand)
        operation_type = BusinessOperationType.CREATE_CAMPAIGN
        campaign_id = None
        canonical_payload = canonical_json(payload)
    else:
        campaign_id = updated.campaign_binding.campaign_id
        if campaign_id is None or campaign_id <= 0:
            raise ValueError("existing Campaign selection is required before creating a Task")
        payload = task_payload_from_draft(updated.recommendation_task)
        operation_type = BusinessOperationType.CREATE_TASK
        canonical_payload = canonical_json(payload)

    payload_hash = canonical_payload_hash(payload)
    current = updated.pending_business_operation
    if (
        current
        and current.operation_type == operation_type
        and current.actor_tenant_id == actor_scope.tenant_id
        and current.actor_user_id == actor_scope.user_id
        and current.actor_brand_id == actor_scope.brand_id
        and current.form_revision == updated.form_revision
        and current.payload_hash == payload_hash
    ):
        return updated

    operation = PendingBusinessOperation(
        operation_id=uuid4(),
        request_id=uuid4(),
        operation_type=operation_type,
        agent_task_id=updated.task_id,
        actor_tenant_id=actor_scope.tenant_id,
        actor_user_id=actor_scope.user_id,
        actor_brand_id=actor_scope.brand_id,
        campaign_id=campaign_id,
        form_revision=updated.form_revision,
        payload_hash=payload_hash,
        confirmation_id=uuid4(),
        canonical_payload=canonical_payload,
    )
    updated.pending_business_operation = operation
    updated.pending_ui = PendingUI(
        type="business_operation_confirmation",
        form_revision=updated.form_revision,
    )
    updated.next_action = NextAction.WAIT_FOR_OPERATION_CONFIRMATION
    return updated


def confirm_business_operation(
    draft: WorkflowDraft,
    event: BusinessOperationConfirmation,
    *,
    actor_scope: ActorScope,
) -> WorkflowDraft:
    _validate_task_ownership(draft, event.task_id)
    pending = draft.pending_business_operation
    if pending is None:
        raise ValueError("there is no pending business operation to confirm")
    if pending.status != BusinessOperationStatus.AWAITING_CONFIRMATION:
        raise ValueError("business operation is not awaiting confirmation; reconcile its current status")
    if (
        event.operation_id != pending.operation_id
        or event.operation_type != pending.operation_type
        or event.form_revision != draft.form_revision
        or event.payload_hash != pending.payload_hash
        or event.confirmation_id != pending.confirmation_id
        or pending.agent_task_id != draft.task_id
        or actor_scope.tenant_id != pending.actor_tenant_id
        or actor_scope.user_id != pending.actor_user_id
        or actor_scope.brand_id != pending.actor_brand_id
    ):
        raise ValueError("business operation confirmation does not match the current preview")
    updated = draft.model_copy(deep=True)
    updated.pending_business_operation.confirmed = True
    updated.pending_business_operation.status = BusinessOperationStatus.CONFIRMED
    updated.pending_ui = None
    updated.next_action = NextAction.RESERVE_BUSINESS_OPERATION
    return updated


def build_campaign_create_request(
    draft: WorkflowDraft,
    *,
    actor_scope: ActorScope,
    trusted_brand: TrustedBrand,
) -> CampaignCreateRequest:
    pending = _confirmed_operation(draft, BusinessOperationType.CREATE_CAMPAIGN)
    _assert_operation_actor(pending, actor_scope)
    if trusted_brand.brand_id != actor_scope.brand_id:
        raise ValueError("trusted brand must match the authenticated actor brand")
    payload = campaign_payload_from_draft(draft.campaign, trusted_brand)
    _assert_payload_matches(pending.canonical_payload, payload, pending.payload_hash)
    return CampaignCreateRequest(
        actor_scope=actor_scope,
        trusted_brand=trusted_brand,
        operation_id=pending.operation_id,
        request_id=pending.request_id,
        idempotency_key=pending.operation_id,
        agent_task_id=draft.task_id,
        form_revision=draft.form_revision,
        confirmation_id=pending.confirmation_id,
        payload_hash=pending.payload_hash,
        payload=payload,
    )


def build_task_create_request(
    draft: WorkflowDraft,
    *,
    actor_scope: ActorScope,
) -> TaskCreateRequest:
    pending = _confirmed_operation(draft, BusinessOperationType.CREATE_TASK)
    _assert_operation_actor(pending, actor_scope)
    if pending.campaign_id != draft.campaign_binding.campaign_id:
        raise ValueError("pending Task operation is not bound to current Campaign")
    payload = task_payload_from_draft(draft.recommendation_task)
    _assert_payload_matches(pending.canonical_payload, payload, pending.payload_hash)
    return TaskCreateRequest(
        actor_scope=actor_scope,
        campaign_id=pending.campaign_id,
        operation_id=pending.operation_id,
        request_id=pending.request_id,
        idempotency_key=pending.operation_id,
        agent_task_id=draft.task_id,
        form_revision=draft.form_revision,
        confirmation_id=pending.confirmation_id,
        payload_hash=pending.payload_hash,
        payload=payload,
    )


def _confirmed_operation(draft: WorkflowDraft, expected_type: BusinessOperationType):
    pending = draft.pending_business_operation
    if (
        pending is None
        or not pending.confirmed
        or pending.operation_type != expected_type
        or pending.agent_task_id != draft.task_id
        or pending.form_revision != draft.form_revision
    ):
        raise ValueError("current business operation lacks a valid explicit user confirmation")
    return pending


def _assert_payload_matches(canonical_payload: str, payload: Any, expected_hash: str) -> None:
    if canonical_json(payload) != canonical_payload or canonical_payload_hash(payload) != expected_hash:
        raise ValueError("operation payload changed after user confirmation")


def _assert_operation_actor(pending: PendingBusinessOperation, actor_scope: ActorScope) -> None:
    if (
        pending.actor_tenant_id != actor_scope.tenant_id
        or pending.actor_user_id != actor_scope.user_id
        or pending.actor_brand_id != actor_scope.brand_id
    ):
        raise ValueError("business operation actor scope changed after preview")


def _validate_task_ownership(draft: WorkflowDraft, event_task_id) -> None:
    if event_task_id != draft.task_id:
        raise ValueError("event task_id does not match workflow task_id")


def _apply_operation(draft: WorkflowDraft, operation: FieldPatch) -> bool:
    model = draft.campaign if operation.target == "campaign" else draft.recommendation_task
    allowed = _CAMPAIGN_FIELDS if operation.target == "campaign" else _TASK_FIELDS
    if operation.field not in allowed:
        raise ValueError(f"unsupported {operation.target} field: {operation.field}")
    if operation.op in {"add", "remove"}:
        raise ValueError(f"list operation is unsupported for scalar field: {operation.target}.{operation.field}")
    if operation.op == "use_default":
        # Remove suppression only. No default provider exists in this phase;
        # an unset field remains unset until a trusted default contract exists.
        key = f"{operation.target}.{operation.field}"
        was_suppressed = key in draft.suppressed_defaults
        old = getattr(model, operation.field)
        setattr(model, operation.field, None)
        draft.suppressed_defaults.discard(key)
        draft.field_sources.pop(key, None)
        return was_suppressed or old is not None
    if operation.op == "set" and operation.value is None:
        raise ValueError("set null is ambiguous; use clear to suppress defaults or use_default to allow them")
    value: Any = None if operation.op == "clear" else operation.value
    field_info = type(model).model_fields[operation.field]
    try:
        if value is not None:
            value = TypeAdapter(field_info.annotation).validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"invalid value for {operation.target}.{operation.field}: {exc}") from exc
    old = getattr(model, operation.field)
    setattr(model, operation.field, value)
    if operation.op == "clear":
        draft.suppressed_defaults.add(f"{operation.target}.{operation.field}")
    else:
        draft.suppressed_defaults.discard(f"{operation.target}.{operation.field}")
    return old != value or operation.op == "clear"


def _missing_required(model: Any, fields: tuple[str, ...], prefix: str) -> list[ValidationIssue]:
    return [
        ValidationIssue(code="required_missing", field=f"{prefix}.{field}", message=f"缺少必填字段：{field}")
        for field in fields
        if getattr(model, field) is None or (isinstance(getattr(model, field), str) and not getattr(model, field).strip())
    ]
