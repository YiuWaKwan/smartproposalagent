"""Deterministic selection review and previews. This slice never dispatches writes."""

from hashlib import sha256
from uuid import uuid4

from app.schemas.business_api import ActorScope
from app.schemas.domain import AgentWorkflowStage, BusinessTaskStatus, SelectionDraft, WorkflowDraft
from app.schemas.domain.selection import (
    SelectionAction, SelectionOperation, SelectionOperationKind, SelectionOperationStatus,
    SelectionPreview, SelectionReceipt,
)
from app.schemas.events import (
    ConfirmSelection, SelectionContextEvent, SelectionPatchEvent, SelectionReviewRequested,
    SelectionSaveRequested,
)
from app.schemas.recommendation import JavaRecommendationResults, JavaTaskStatus, RecommendationRunStatus
from app.schemas.selection import (
    CandidateReviewPage, JavaSelectionKol, JavaSelectionPayload, JavaSelectionTask,
    SelectionConfirmRequest, SelectionSaveRequest,
)
from app.services.contracts import RecommendationSnapshotStore
from app.services.recommendations import project_candidate, recommendation_content_hash

SAVE = SelectionOperationKind.SAVE
FINAL = SelectionOperationKind.FINAL
Status = SelectionOperationStatus
UNSETTLED = {Status.IN_FLIGHT, Status.ACCEPTED, Status.UNKNOWN}


def _operation(selection: SelectionDraft, kind: SelectionOperationKind) -> SelectionOperation | None:
    return selection.save_operation if kind == SAVE else selection.final_operation


def selection_recovery_action(selection: SelectionDraft) -> str:
    """A saved preview/confirmation is never sufficient to resume a write."""
    operations = (selection.save_operation, selection.final_operation)
    if any(op and (op.status in UNSETTLED or (op.attempted and op.status == Status.PREVIEW)) for op in operations):
        return "reconcile_only"
    if selection.final_operation and selection.final_operation.status == Status.CONFIRMED:
        return "stop"
    return "review_only"


def selection_results(
    draft: WorkflowDraft, event: SelectionContextEvent, actor: ActorScope | None,
    store: RecommendationSnapshotStore,
) -> JavaRecommendationResults:
    run = draft.recommendation_run
    if not run or not run.snapshot or actor is None:
        raise ValueError("selection requires a trusted actor and current recommendation snapshot")
    snapshot = run.snapshot
    if (actor.tenant_id, actor.user_id, actor.brand_id) != (
        run.actor_tenant_id, run.actor_user_id, run.actor_brand_id,
    ):
        raise ValueError("selection actor does not match the current recommendation")
    if (
        event.task_id != draft.task_id or event.task_id != run.agent_task_id
        or event.campaign_id != draft.business_campaign_id or event.campaign_id != run.campaign_id
        or (draft.campaign_binding.campaign_id is not None
            and event.campaign_id != draft.campaign_binding.campaign_id)
        or event.business_task_id != draft.business_task_id or event.business_task_id != run.business_task_id
        or run.form_revision != draft.form_revision
    ):
        raise ValueError("selection workflow/Campaign/Task or form revision mismatch")
    if (
        run.status != RecommendationRunStatus.RECOMMEND_COMPLETED
        or run.business_task_status != JavaTaskStatus.RECOMMEND_COMPLETED
        or draft.business_task_status != BusinessTaskStatus.RECOMMEND_COMPLETED
        or run.reconciliation_required or run.write_outcome_unknown
        or not run.downstream_task_id or run.downstream_task_id != draft.downstream_task_id
    ):
        raise ValueError("selection requires a current completed recommendation")
    if (
        event.recommendation_run_id != run.run_id or event.snapshot_ref != run.result_ref
        or event.snapshot_ref != snapshot.snapshot_ref or str(event.snapshot_ref) != draft.candidate_snapshot_ref
        or event.candidate_snapshot_hash != run.result_hash or event.candidate_snapshot_hash != snapshot.content_hash
        or (snapshot.campaign_id, snapshot.business_task_id, snapshot.downstream_task_id,
            snapshot.operation_id, snapshot.request_id)
        != (run.campaign_id, run.business_task_id, run.downstream_task_id, run.operation_id, run.request_id)
    ):
        raise ValueError("stale or mismatched selection snapshot")
    reference = store.get_reference(event.snapshot_ref)
    results = store.get_results(event.snapshot_ref)
    if reference != snapshot or results is None or recommendation_content_hash(results) != snapshot.content_hash:
        raise ValueError("local selection snapshot is unavailable or changed; review again")
    return results


def _matches_snapshot(selection: SelectionDraft, event: SelectionContextEvent) -> bool:
    return (selection.recommendation_run_id, selection.snapshot_ref, selection.candidate_snapshot_hash) == (
        event.recommendation_run_id, event.snapshot_ref, event.candidate_snapshot_hash,
    )


def _payload(selection: SelectionDraft, task_id: int, kind: SelectionOperationKind) -> JavaSelectionPayload | None:
    # Preserve the requested order of selected IDs. Non-selected changes are explicit.
    kols = [JavaSelectionKol(kolBackendId=kol, selected=True, rejected=False) for kol in selection.selected_kol_ids]
    kols += [JavaSelectionKol(kolBackendId=kol, selected=False, rejected=True, rejectReason=reason)
             for kol, reason in selection.rejected_kols.items()]
    kols += [JavaSelectionKol(kolBackendId=kol, selected=False, rejected=False)
             for kol in selection.cancelled_kol_ids]
    if kind == SAVE:
        kols = [item for item in kols if item.kol_backend_id in selection.edited_kol_ids]
    if not kols or (kind == FINAL and not selection.selected_kol_ids):
        return None
    return JavaSelectionPayload(items=[JavaSelectionTask(taskId=task_id, kols=kols)])


def _new_preview(draft: WorkflowDraft, kind: SelectionOperationKind) -> SelectionOperation:
    run = draft.recommendation_run
    selection = draft.selection
    payload = _payload(selection, run.business_task_id, kind)
    canonical = payload.canonical_json() if payload else ""
    reasons = ["selection_write_contract_unverified"]
    if kind == SAVE:
        reasons.append("save_authorization_unverified")
        if payload is None:
            reasons.append("no_selection_operations")
    else:
        if not run.snapshot.candidate_universe_complete:
            reasons.append("candidate_universe_incomplete")
        if not selection.selected_kol_ids:
            reasons.append("empty_selection")
        reasons.append("java_snapshot_and_revision_unverified")
    return SelectionOperation(preview=SelectionPreview(
        preview_id=uuid4(), operation_id=uuid4(), request_id=uuid4(), confirmation_id=uuid4(), kind=kind,
        agent_task_id=draft.task_id, campaign_id=run.campaign_id, business_task_id=run.business_task_id,
        downstream_task_id=run.downstream_task_id, recommendation_run_id=run.run_id,
        snapshot_ref=run.snapshot.snapshot_ref, candidate_snapshot_hash=run.snapshot.content_hash,
        selection_revision=selection.revision, form_revision=draft.form_revision,
        actor_tenant_id=run.actor_tenant_id, actor_user_id=run.actor_user_id, actor_brand_id=run.actor_brand_id,
        selected_kol_ids=list(selection.selected_kol_ids), canonical_payload=canonical,
        payload_hash=sha256(canonical.encode()).hexdigest(), blocked_reasons=reasons,
    ))


def bind_selection_snapshot(draft: WorkflowDraft, results: JavaRecommendationResults) -> None:
    """Initialize a server-owned draft from fetched Java selection facts, never AI flags."""
    run = draft.recommendation_run
    previous = draft.selection
    if (previous.recommendation_run_id, previous.snapshot_ref, previous.candidate_snapshot_hash) == (
        run.run_id, run.snapshot.snapshot_ref, run.snapshot.content_hash,
    ):
        return
    if selection_recovery_action(previous) != "review_only":
        raise ValueError("existing selection requires reconciliation or is already finalized")
    draft.selection = SelectionDraft(
        revision=previous.revision + int(previous.snapshot_ref is not None),
        recommendation_run_id=run.run_id, snapshot_ref=run.snapshot.snapshot_ref,
        candidate_snapshot_hash=run.snapshot.content_hash,
        selected_kol_ids=[item.kol_backend_id for item in results.kols if item.selected and not item.rejected],
        rejected_kols={item.kol_backend_id: item.reject_reason for item in results.kols if item.rejected},
        event_hashes=previous.event_hashes.copy(),
    )
    _refresh_previews(draft)


def _refresh_previews(draft: WorkflowDraft) -> None:
    draft.selection.save_operation = _new_preview(draft, SAVE)
    draft.selection.final_operation = _new_preview(draft, FINAL)


def review_candidates(results: JavaRecommendationResults, event: SelectionReviewRequested) -> CandidateReviewPage:
    end = event.offset + event.limit
    return CandidateReviewPage(
        snapshot_ref=event.snapshot_ref, snapshot_hash=event.candidate_snapshot_hash,
        offset=event.offset, next_offset=end if end < len(results.kols) else None,
        fetched_count=len(results.kols), items=[project_candidate(kol) for kol in results.kols[event.offset:end]],
    )


def _validate_ids(ids: list[str], results: JavaRecommendationResults) -> None:
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate selection KOL IDs")
    if not set(ids).issubset({item.kol_backend_id for item in results.kols}):
        raise ValueError("selection contains an unknown KOL ID or an ID outside the current snapshot")


def _event_is_duplicate(selection: SelectionDraft, event: SelectionContextEvent) -> bool:
    prior = selection.event_hashes.get(str(event.client_event_id))
    if prior is None:
        return False
    if prior != _event_hash(event):
        raise ValueError("selection event ID was reused with different content")
    return True


def _event_hash(event: SelectionContextEvent) -> str:
    return sha256(event.model_dump_json().encode()).hexdigest()


def _record_event(selection: SelectionDraft, event: SelectionContextEvent) -> None:
    # Bounded local replay guard. Older edits remain subject to snapshot/revision checks.
    selection.event_hashes[str(event.client_event_id)] = _event_hash(event)
    if len(selection.event_hashes) > 256:
        del selection.event_hashes[next(iter(selection.event_hashes))]


def apply_selection_patch(draft: WorkflowDraft, event: SelectionPatchEvent, results: JavaRecommendationResults) -> None:
    selection = draft.selection
    if not _matches_snapshot(selection, event):
        raise ValueError("selection draft is not bound to the current snapshot")
    if _event_is_duplicate(selection, event):
        return
    if selection.revision != event.expected_selection_revision:
        raise ValueError("stale selection revision")
    if selection_recovery_action(selection) != "review_only":
        raise ValueError("selection is not editable until reconciled or is already finalized")
    _validate_ids([change.kol_backend_id for change in event.changes], results)
    for change in event.changes:
        kol = change.kol_backend_id
        selection.edited_kol_ids.add(kol)
        if kol in selection.selected_kol_ids and change.action != SelectionAction.SELECT:
            selection.selected_kol_ids.remove(kol)
        selection.rejected_kols.pop(kol, None)
        if kol in selection.cancelled_kol_ids:
            selection.cancelled_kol_ids.remove(kol)
        if change.action == SelectionAction.SELECT and kol not in selection.selected_kol_ids:
            selection.selected_kol_ids.append(kol)
        elif change.action == SelectionAction.REJECT:
            selection.rejected_kols[kol] = change.reject_reason
        elif change.action == SelectionAction.CANCEL:
            selection.cancelled_kol_ids.append(kol)
    selection.revision += 1
    _refresh_previews(draft)
    _record_event(selection, event)


def check_selection_intent(
    draft: WorkflowDraft, event: SelectionSaveRequested | ConfirmSelection, actor: ActorScope,
    results: JavaRecommendationResults,
) -> SelectionReceipt:
    kind = FINAL if isinstance(event, ConfirmSelection) else SAVE
    selection = draft.selection
    operation = _operation(selection, kind)
    if not _matches_snapshot(selection, event) or operation is None:
        raise ValueError("no current selection preview")
    preview = operation.preview
    if (
        (event.selection_preview_id, event.confirmation_id, event.selection_revision)
        != (preview.preview_id, preview.confirmation_id, selection.revision)
        or (preview.agent_task_id, preview.campaign_id, preview.business_task_id, preview.downstream_task_id,
            preview.recommendation_run_id, preview.snapshot_ref, preview.candidate_snapshot_hash,
            preview.selection_revision, preview.form_revision, preview.kind)
        != (event.task_id, event.campaign_id, event.business_task_id, draft.downstream_task_id, event.recommendation_run_id,
            event.snapshot_ref, event.candidate_snapshot_hash, selection.revision, draft.form_revision, kind)
        or (preview.actor_tenant_id, preview.actor_user_id, preview.actor_brand_id)
        != (actor.tenant_id, actor.user_id, actor.brand_id)
    ):
        raise ValueError("stale or mismatched selection preview/confirmation")
    _validate_ids(selection.selected_kol_ids + list(selection.rejected_kols) + selection.cancelled_kol_ids, results)
    if isinstance(event, ConfirmSelection):
        _validate_ids(event.selected_kol_ids, results)
        if not event.selected_kol_ids or event.selected_kol_ids != selection.selected_kol_ids:
            raise ValueError("final selection requires the exact non-empty current list")
    payload = _payload(selection, event.business_task_id, kind)
    canonical = payload.canonical_json() if payload else ""
    if (
        preview.canonical_payload != canonical or preview.payload_hash != sha256(canonical.encode()).hexdigest()
        or preview.selected_kol_ids != selection.selected_kol_ids
    ):
        raise ValueError("selection payload changed since preview")
    duplicate = _event_is_duplicate(selection, event)
    if operation.receipt is not None:
        if not duplicate:
            _record_event(selection, event)
        return operation.receipt
    if selection_recovery_action(selection) == "reconcile_only":
        return SelectionReceipt.from_preview(preview, Status.UNKNOWN, "selection_reconciliation_required")
    if selection_recovery_action(selection) == "stop":
        return SelectionReceipt.from_preview(preview, Status.REJECTED, "selection_already_finalized")
    # Construct only the local typed contract. Even a complete fixture is never sent.
    if payload is not None:
        request_type = SelectionConfirmRequest if kind == FINAL else SelectionSaveRequest
        request_type(actor_scope=actor, preview=preview, payload=payload)
    reason = (
        "candidate_universe_incomplete" if kind == FINAL and not draft.recommendation_run.snapshot.candidate_universe_complete
        else "java_snapshot_and_revision_unverified" if kind == FINAL
        else "save_authorization_unverified" if payload is not None
        else "no_selection_operations"
    )
    receipt = SelectionReceipt.from_preview(preview, Status.BLOCKED, reason)
    operation.status = Status.BLOCKED
    operation.receipt = receipt
    _record_event(selection, event)
    return receipt


def apply_selection_receipt(draft: WorkflowDraft, receipt: SelectionReceipt, actor: ActorScope) -> WorkflowDraft:
    """Reduce a correlated fixture/result only for an attempted operation; never retry."""
    selection = draft.selection
    run = draft.recommendation_run
    operation = _operation(selection, receipt.kind)
    if not run or not run.snapshot or not operation or not operation.attempted or not operation.explicitly_confirmed:
        return draft
    preview = operation.preview
    if receipt.status == Status.CONFIRMED and (
        not run.snapshot.candidate_universe_complete or not selection.selected_kol_ids
    ):
        return draft
    payload = _payload(selection, preview.business_task_id, preview.kind)
    canonical = payload.canonical_json() if payload else ""
    if (
        operation.status not in {Status.IN_FLIGHT, Status.ACCEPTED}
        or preview.agent_task_id != draft.task_id or preview.form_revision != draft.form_revision
        or run.form_revision != draft.form_revision
        or run.status != RecommendationRunStatus.RECOMMEND_COMPLETED
        or draft.business_task_status != BusinessTaskStatus.RECOMMEND_COMPLETED
        or (preview.actor_tenant_id, preview.actor_user_id, preview.actor_brand_id)
        != (actor.tenant_id, actor.user_id, actor.brand_id)
        or (run.actor_tenant_id, run.actor_user_id, run.actor_brand_id)
        != (actor.tenant_id, actor.user_id, actor.brand_id)
        or (run.agent_task_id, run.campaign_id, run.business_task_id, run.downstream_task_id)
        != (draft.task_id, draft.business_campaign_id, draft.business_task_id, draft.downstream_task_id)
        or preview.kind != receipt.kind
        or preview.selected_kol_ids != selection.selected_kol_ids
        or preview.canonical_payload != canonical or preview.payload_hash != sha256(canonical.encode()).hexdigest()
        or (preview.campaign_id, preview.business_task_id, preview.downstream_task_id,
            preview.recommendation_run_id, preview.snapshot_ref,
            preview.candidate_snapshot_hash, preview.selection_revision)
        != (draft.business_campaign_id, draft.business_task_id, draft.downstream_task_id, run.run_id, run.snapshot.snapshot_ref,
            run.snapshot.content_hash, selection.revision)
        or receipt != SelectionReceipt.from_preview(preview, receipt.status, receipt.error_code)
        or receipt.status not in {Status.ACCEPTED, Status.REJECTED, Status.UNKNOWN,
                                  Status.CONFIRMED if receipt.kind == FINAL else Status.SAVED}
    ):
        return draft
    updated = draft.model_copy(deep=True)
    operation = _operation(updated.selection, receipt.kind)
    operation.status = receipt.status
    operation.receipt = receipt
    if receipt.status == Status.CONFIRMED:
        # Only a matching terminal final receipt can project a submitted list.
        updated.selection.submitted_kol_ids = preview.selected_kol_ids.copy()
        updated.stage = AgentWorkflowStage.COMPLETED
        updated.business_task_status = BusinessTaskStatus.ENDED
    return updated
