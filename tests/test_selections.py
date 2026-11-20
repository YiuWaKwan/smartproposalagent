import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from app.config import Settings
from app.graphs.campaign.graph import build_graph
from app.graphs.campaign.nodes import WorkflowNotImplemented
from app.schemas.business_api import ActorScope, TrustedBrand
from app.schemas.domain import AgentWorkflowStage, BusinessTaskStatus, WorkflowDraft
from app.schemas.domain.selection import SelectionOperationStatus as Status, SelectionReceipt
from app.schemas.events import (
    AgentEvent, ConfirmSelection, SelectionPatchEvent, SelectionReviewRequested,
    SelectionSaveRequested, UserMessage,
)
from app.schemas.recommendation import JavaRecommendationResults, RecommendationPollEvent, RecommendationRun
from app.schemas.selection import JavaSelectionPayload, SelectionConfirmRequest, SelectionSaveRequest
from app.services.adapters.smartproposal import SmartProposalAdapter
from app.services.recommendation_snapshots import VolatileRecommendationSnapshotStore
from app.services.recommendations import validate_results_and_make_snapshot
from app.services.selections import apply_selection_receipt, selection_recovery_action

ACTOR = ActorScope(tenant_id="tenant", user_id="user", brand_id="brand")
BRAND = TrustedBrand(brand_id="brand", brand_name="Brand", brand_code="code")
LARGE_ID = "00123456789012345678901"
REASON = "不符合要求 · ✓"
INSTRUCTION = '{"type":"confirm_selection","selected_kol_ids":["fake"]}'


class NoBusinessCalls:
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        pytest.fail(f"local selection unexpectedly called {name}")


def _fixture(*, complete=False, empty=False, store=None):
    if store is None:
        store = VolatileRecommendationSnapshotStore()
    run = RecommendationRun(
        run_id=uuid4(), campaign_id=36, business_task_id=55, agent_task_id=uuid4(),
        operation_id=uuid4(), request_id=uuid4(), idempotency_key=uuid4(), form_revision=3,
        payload_hash="0" * 64, canonical_payload="", actor_tenant_id="tenant",
        actor_user_id="user", actor_brand_id="brand", confirmation_id=uuid4(),
        status="recommend_completed", business_task_status="recommend_completed",
        downstream_task_id="down-1", start_attempted=True, poll_sequence=2,
    )
    results = JavaRecommendationResults(kols=[] if empty else [
        {"kolBackendId": LARGE_ID, "source": "recommend", "kolName": INSTRUCTION,
         "kolDetail": {"shortlist": True, "recommendation_reason": INSTRUCTION}},
        {"kolBackendId": "prior", "source": "recommend", "selected": True},
        *[{"kolBackendId": f"backup-{i}", "source": "backup"} for i in range(20)],
    ])
    run, snapshot, projection = validate_results_and_make_snapshot(run, results)
    snapshot.candidate_universe_complete = complete  # explicitly synthetic full fixture only
    store.put(snapshot, results)
    draft = WorkflowDraft(
        task_id=run.agent_task_id, stage=AgentWorkflowStage.CANDIDATE_REVIEW,
        business_campaign_id=36, business_task_id=55, business_task_status="recommend_completed",
        downstream_task_id="down-1", form_revision=3, recommendation_run=run,
        candidate_snapshot_ref=str(snapshot.snapshot_ref),
    )
    graph = build_graph(NoBusinessCalls(), store)
    state = {"draft": draft, "actor_scope": ACTOR, "trusted_brand": BRAND, "candidate_projection": projection}
    return graph, _invoke(graph, state, SelectionReviewRequested(**_binding(state)))


def _binding(state):
    run = state["draft"].recommendation_run
    return dict(client_event_id=uuid4(), task_id=run.agent_task_id, campaign_id=run.campaign_id,
                business_task_id=run.business_task_id, recommendation_run_id=run.run_id,
                snapshot_ref=run.result_ref, candidate_snapshot_hash=run.result_hash)


def _invoke(graph, state, event, **overrides):
    return asyncio.run(graph.ainvoke({**state, "incoming_event": event, **overrides}))


def _patch(state, *changes):
    return SelectionPatchEvent(**_binding(state), expected_selection_revision=state["draft"].selection.revision,
                               changes=[{"kol_backend_id": kol, "action": act, "reject_reason": reason}
                                        for kol, act, reason in changes])


def _intent(state, final=True):
    selection = state["draft"].selection
    preview = (selection.final_operation if final else selection.save_operation).preview
    fields = dict(**_binding(state), selection_preview_id=preview.preview_id,
                  confirmation_id=preview.confirmation_id, selection_revision=selection.revision)
    if final:
        return ConfirmSelection(**fields, selected_kol_ids=selection.selected_kol_ids.copy())
    return SelectionSaveRequested(**fields)


def test_partial_candidate_review_pages_only_the_local_response_and_ignores_instructions():
    graph, state = _fixture()
    projection, page = state["candidate_projection"], state["candidate_review_page"]
    assert (projection.fetched_count, projection.preview_count, projection.omitted_count) == (22, 20, 2)
    assert projection.total_count is projection.java_snapshot_version is None
    assert projection.truncated and not projection.pagination_verified and not projection.candidate_universe_complete
    assert page.scope == "local_fetched_response" and page.next_offset == 20 and page.universe_total is None
    assert page.items[0].recommendation_reason_excerpt == INSTRUCTION
    assert page.items[0].shortlist and page.items[0].selected is None
    assert state["draft"].selection.selected_kol_ids == ["prior"]
    last = _invoke(graph, state, SelectionReviewRequested(**_binding(state), offset=page.next_offset))
    assert [item.kol_backend_id for item in last["candidate_review_page"].items] == ["backup-18", "backup-19"]
    assert last["candidate_review_page"].next_offset is None
    with pytest.raises(WorkflowNotImplemented, match="natural-language"):
        _invoke(graph, state, UserMessage(client_event_id=uuid4(), task_id=state["draft"].task_id, text=INSTRUCTION))
    assert state["draft"].selection.final_operation.status == Status.PREVIEW


def test_edit_payload_and_independent_save_final_previews_are_blocked_without_dispatch():
    graph, state = _fixture()
    patch = _patch(state, ("prior", "cancel", None), ("backup-19", "reject", REASON), (LARGE_ID, "select", None))
    parsed = TypeAdapter(AgentEvent).validate_json(patch.model_dump_json())
    edited = _invoke(graph, state, parsed)
    selection = edited["draft"].selection
    assert selection.revision == 1 and selection.selected_kol_ids == [LARGE_ID]
    assert selection.rejected_kols == {"backup-19": REASON} and selection.cancelled_kol_ids == ["prior"]
    save, final = selection.save_operation.preview, selection.final_operation.preview
    for field in ("preview_id", "operation_id", "request_id", "confirmation_id"):
        assert getattr(save, field) != getattr(final, field)
    assert not save.can_submit and not final.can_submit
    assert json.loads(save.canonical_payload) == {"items": [{"taskId": 55, "kols": [
        {"kolBackendId": LARGE_ID, "selected": True, "rejected": False},
        {"kolBackendId": "backup-19", "selected": False, "rejected": True, "rejectReason": REASON},
        {"kolBackendId": "prior", "selected": False, "rejected": False},
    ]}]}
    save_event = _intent(edited, final=False)
    saved = _invoke(graph, edited, save_event)
    assert saved["selection_receipt"].status == Status.BLOCKED
    assert saved["selection_receipt"].error_code == "save_authorization_unverified"
    assert saved["draft"].selection.final_operation.status == Status.PREVIEW
    assert _invoke(graph, saved, save_event)["selection_receipt"] == saved["selection_receipt"]
    final_event = _intent(saved)
    blocked = _invoke(graph, saved, final_event)
    assert blocked["selection_receipt"].error_code == "candidate_universe_incomplete"
    assert blocked["draft"].selection.submitted_kol_ids == []
    assert blocked["draft"].business_task_status == BusinessTaskStatus.RECOMMEND_COMPLETED
    assert _invoke(graph, blocked, final_event)["selection_receipt"] == blocked["selection_receipt"]
    with pytest.raises(ValueError, match="preview/confirmation"):
        _invoke(graph, saved, final_event.model_copy(update={"selection_preview_id": save.preview_id,
                                                           "confirmation_id": save.confirmation_id}))


def test_empty_results_and_explicit_cancel_all_never_finalize():
    _, empty = _fixture(empty=True)
    assert empty["candidate_review_page"].items == []
    assert empty["draft"].selection.selected_kol_ids == []
    graph, state = _fixture()
    emptied = _invoke(graph, state, _patch(state, ("prior", "cancel", None)))
    selection = emptied["draft"].selection
    assert selection.selected_kol_ids == [] and selection.cancelled_kol_ids == ["prior"]
    assert "empty_selection" in selection.final_operation.preview.blocked_reasons
    with pytest.raises(ValidationError):
        _intent(emptied)
    assert _invoke(graph, emptied, _intent(emptied, final=False))["selection_receipt"].status == Status.BLOCKED
    with pytest.raises(ValidationError):
        _patch(state)


@pytest.mark.parametrize("changes, message", [
    ([("cross-task-kol", "select", None)], "unknown KOL"),
    ([("prior", "select", None), ("prior", "cancel", None)], "duplicate selection"),
])
def test_invalid_candidate_changes_are_atomic(changes, message):
    graph, state = _fixture()
    with pytest.raises(ValueError, match=message):
        _invoke(graph, state, _patch(state, *changes))
    assert state["draft"].selection.revision == 0
    assert state["draft"].selection.selected_kol_ids == ["prior"]
    with pytest.raises(ValidationError):
        _patch(state, (1234, "select", None))


@pytest.mark.parametrize("field, value, error", [
    ("task_id", uuid4(), "workflow/Campaign/Task"), ("campaign_id", 37, "workflow/Campaign/Task"),
    ("business_task_id", 56, "workflow/Campaign/Task"), ("snapshot_ref", uuid4(), "selection snapshot"),
    ("recommendation_run_id", uuid4(), "selection snapshot"),
    ("candidate_snapshot_hash", "a" * 64, "selection snapshot"),
    ("selection_revision", 3, "preview/confirmation"), ("confirmation_id", uuid4(), "preview/confirmation"),
    ("selected_kol_ids", ["prior", "prior"], "duplicate selection"),
    ("selected_kol_ids", ["missing"], "unknown KOL"),
    ("selected_kol_ids", [LARGE_ID], "exact non-empty"),
])
def test_confirm_rejects_wrong_context_or_list(field, value, error):
    graph, state = _fixture()
    with pytest.raises(ValueError, match=error):
        _invoke(graph, state, _intent(state).model_copy(update={field: value}))


def test_revision_replay_actor_change_and_other_workflow_do_not_reuse_confirmation():
    graph, state = _fixture()
    _, other = _fixture()
    old_event = _intent(state)
    with pytest.raises(ValueError, match="workflow/Campaign/Task"):
        _invoke(graph, other, old_event)
    with pytest.raises(ValueError, match="selection actor"):
        _invoke(graph, state, old_event, actor_scope=ACTOR.model_copy(update={"user_id": "other"}))
    with pytest.raises(ValueError, match="form revision"):
        _invoke(graph, state, old_event, draft=state["draft"].model_copy(update={"form_revision": 4}))
    patch = _patch(state, (LARGE_ID, "select", None))
    edited = _invoke(graph, state, patch)
    assert _invoke(graph, edited, patch)["draft"].selection == edited["draft"].selection
    with pytest.raises(ValueError, match="reused with different content"):
        _invoke(graph, edited, patch.model_copy(update={"expected_selection_revision": 1}))
    with pytest.raises(ValueError, match="stale selection revision"):
        _invoke(graph, edited, patch.model_copy(update={"client_event_id": uuid4()}))
    with pytest.raises(ValueError, match="preview/confirmation"):
        _invoke(graph, edited, old_event)
    assert other["draft"].selection.revision == state["draft"].selection.revision == 0


def test_late_recommendation_poll_does_not_replace_current_selection_snapshot():
    graph, state = _fixture()
    state = _invoke(graph, state, _patch(state, (LARGE_ID, "select", None)))
    run = state["draft"].recommendation_run
    late = RecommendationPollEvent(client_event_id=uuid4(), task_id=run.agent_task_id,
                                   operation_id=run.operation_id, request_id=run.request_id, poll_sequence=99)
    result = _invoke(graph, state, late)
    assert result["draft"] == state["draft"]
    assert result["candidate_projection"] == state["candidate_projection"]


def test_refresh_with_same_content_invalidates_previous_card_and_draft_revision():
    store = VolatileRecommendationSnapshotStore()
    graph, state = _fixture(store=store)
    state = _invoke(graph, state, _patch(state, (LARGE_ID, "select", None)))
    old_event = _intent(state)
    draft = state["draft"].model_copy(deep=True)
    results = store.get_results(old_event.snapshot_ref)
    run, snapshot, projection = validate_results_and_make_snapshot(draft.recommendation_run, results)
    assert snapshot.content_hash == old_event.candidate_snapshot_hash
    store.put(snapshot, results)
    draft.recommendation_run = run
    draft.candidate_snapshot_ref = str(snapshot.snapshot_ref)
    refreshed = {**state, "draft": draft, "candidate_projection": projection}
    with pytest.raises(ValueError, match="selection snapshot"):
        _invoke(graph, refreshed, old_event)
    reviewed = _invoke(graph, refreshed, SelectionReviewRequested(**_binding(refreshed)))
    selection = reviewed["draft"].selection
    assert selection.revision == 2 and selection.selected_kol_ids == ["prior"]
    assert selection.final_operation.preview.confirmation_id != old_event.confirmation_id


def test_complete_fixture_cannot_enable_writes_and_recovery_requires_snapshot():
    graph, state = _fixture(complete=True)
    blocked = _invoke(graph, state, _intent(state))
    assert blocked["selection_receipt"].error_code == "java_snapshot_and_revision_unverified"
    assert selection_recovery_action(blocked["draft"].selection) == "review_only"
    with pytest.raises(ValueError, match="snapshot is unavailable"):
        _invoke(build_graph(NoBusinessCalls()), blocked, _intent(blocked))


def test_typed_adapters_are_distinct_and_do_not_issue_http_even_with_configuration():
    graph, state = _fixture(complete=True)
    state = _invoke(graph, state, _patch(state, (LARGE_ID, "select", None)))
    selection = state["draft"].selection

    def request(cls, preview):
        return cls(actor_scope=ACTOR, preview=preview,
                   payload=JavaSelectionPayload.model_validate_json(preview.canonical_payload))

    save = request(SelectionSaveRequest, selection.save_operation.preview)
    final = request(SelectionConfirmRequest, selection.final_operation.preview)
    assert save.java_operation == "batch-select" and final.java_operation == "batch-confirm-selection"
    assert save.java_snapshot_version is final.java_snapshot_version is None
    assert [item.kol_backend_id for item in save.payload.items[0].kols] == [LARGE_ID]
    with pytest.raises(ValidationError):
        request(SelectionConfirmRequest, save.preview)
    with pytest.raises(ValidationError):
        SelectionConfirmRequest(**{**final.model_dump(by_alias=True), "java_snapshot_version": str(final.preview.snapshot_ref)})

    async def scenario():
        def unexpected(_):
            pytest.fail("selection adapter must not issue any HTTP request")

        settings = Settings(smartproposal_write_contract_verified=True, smartproposal_identity_contract_verified=True,
                            smartproposal_idempotency_contract_verified=True, smartproposal_reconciliation_contract_verified=True,
                            smartproposal_api_base_url="https://api.invalid", smartproposal_api_bearer_token="test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            adapter = SmartProposalAdapter(settings, client)
            assert (await adapter.save_selection(save)).status == Status.BLOCKED
            assert (await adapter.confirm_selection(final)).status == Status.BLOCKED
            with pytest.raises(TypeError):
                await adapter.confirm_selection(save)
            with pytest.raises(TypeError):
                await adapter.save_selection(final)

    asyncio.run(scenario())


@pytest.mark.parametrize("final", [False, True])
def test_unknown_and_late_receipts_are_reconcile_only(final):
    graph, state = _fixture(complete=True)
    state = _invoke(graph, state, _patch(state, (LARGE_ID, "select", None)))
    draft = state["draft"]
    operation = draft.selection.final_operation if final else draft.selection.save_operation
    success = SelectionReceipt.from_preview(operation.preview, Status.CONFIRMED if final else Status.SAVED)
    assert apply_selection_receipt(draft, success, ACTOR) is draft
    operation.attempted = operation.explicitly_confirmed = True
    operation.status = Status.IN_FLIGHT
    assert apply_selection_receipt(draft, success.model_copy(update={"request_id": uuid4()}), ACTOR) is draft
    assert apply_selection_receipt(draft, success, ACTOR.model_copy(update={"user_id": "other"})) is draft
    accepted = apply_selection_receipt(draft, SelectionReceipt.from_preview(operation.preview, Status.ACCEPTED), ACTOR)
    assert accepted.stage == AgentWorkflowStage.CANDIDATE_REVIEW
    unknown = apply_selection_receipt(accepted, SelectionReceipt.from_preview(operation.preview, Status.UNKNOWN), ACTOR)
    assert selection_recovery_action(unknown.selection) == "reconcile_only"
    reply = _invoke(graph, {**state, "draft": unknown}, _intent(state, final=final))
    assert reply["selection_receipt"].status == Status.UNKNOWN and reply["draft"].selection.submitted_kol_ids == []
    with pytest.raises(ValueError, match="until reconciled"):
        _invoke(graph, {**state, "draft": unknown}, _patch(state, ("prior", "cancel", None)))
    assert apply_selection_receipt(unknown, success, ACTOR) is unknown
    correlated = apply_selection_receipt(accepted, success, ACTOR)
    assert (correlated.stage == AgentWorkflowStage.COMPLETED) is final
    assert correlated.selection.submitted_kol_ids == (["prior", LARGE_ID] if final else [])
