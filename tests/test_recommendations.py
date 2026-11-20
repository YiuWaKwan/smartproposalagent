from decimal import Decimal
from uuid import uuid4

import pytest

from app.schemas.business_api import (
    AdapterError,
    AdapterErrorKind,
    AdapterResponse,
    ActorScope,
    OperationReceipt,
    OperationStatus,
    TrustedBrand,
)
from app.schemas.domain import AgentWorkflowStage, WorkflowDraft
from app.schemas.recommendation import (
    CandidateProjection,
    JavaRecommendationCandidate,
    JavaRecommendationResults,
    JavaTask,
    PollDisposition,
    RecommendationOperationConfirmation,
    RecommendationRecoveryAction,
    RecommendationRunStatus,
    RecommendationStartRequested,
    RecommendationStartAccepted,
)
from app.graphs.campaign.graph import build_graph
from app.services.recommendations import (
    apply_start_result,
    apply_task_poll,
    build_recommendation_start_request,
    confirm_recommendation,
    decide_recommendation_recovery,
    mark_read_error,
    prepare_recommendation_preview,
    validate_results_and_make_snapshot,
)


SCOPE = ActorScope(tenant_id="tenant", user_id="user", brand_id="brand")
BRAND = TrustedBrand(brand_id="brand", brand_name="Brand", brand_code="brand-code")


def _draft_and_task():
    draft = WorkflowDraft(
        task_id=uuid4(),
        stage=AgentWorkflowStage.TASK_CREATED,
        business_campaign_id=36,
        business_task_id=55,
    )
    draft.recommendation_task.name = "春季投放"
    draft.recommendation_task.kol_count = 2
    draft.recommendation_task.budget = Decimal("20000")
    draft.recommendation_task.tier_distribution = "Tier3:2人"
    draft.recommendation_task.talent_type = "种草"
    task = JavaTask(id=55, campaignId=36, status="pending", platform="Douyin")
    return draft, task


def _previewed():
    draft, task = _draft_and_task()
    return prepare_recommendation_preview(
        draft, actor_scope=SCOPE, trusted_brand=BRAND, task=task
    )


def test_recommendation_confirmation_is_independent_and_request_is_bound():
    draft, preview = _previewed()
    operation = draft.pending_business_operation
    assert operation is not None
    assert draft.next_action.value == "wait_for_recommendation_confirmation"

    with pytest.raises(ValueError, match="not ready"):
        build_recommendation_start_request(draft.recommendation_run)

    confirmed = confirm_recommendation(
        draft,
        RecommendationOperationConfirmation(
            client_event_id=uuid4(),
            task_id=draft.task_id,
            operation_id=operation.operation_id,
            request_id=operation.request_id,
            confirmation_id=operation.confirmation_id,
            campaign_id=36,
            business_task_id=55,
            form_revision=draft.form_revision,
            payload_hash=operation.payload_hash,
        ),
        actor_scope=SCOPE,
    )
    request = build_recommendation_start_request(confirmed.recommendation_run)
    assert request.operation_id == preview.operation_id
    assert request.idempotency_key == request.operation_id
    assert request.canonical_payload


def test_acceptance_is_not_completion_and_duplicate_start_is_blocked():
    draft, _ = _previewed()
    operation = draft.pending_business_operation
    confirmed = confirm_recommendation(
        draft,
        RecommendationOperationConfirmation(
            client_event_id=uuid4(), task_id=draft.task_id,
            operation_id=operation.operation_id, request_id=operation.request_id,
            confirmation_id=operation.confirmation_id, campaign_id=36,
            business_task_id=55, form_revision=draft.form_revision,
            payload_hash=operation.payload_hash,
        ),
        actor_scope=SCOPE,
    )
    run = confirmed.recommendation_run
    accepted = AdapterResponse(
        value=RecommendationStartAccepted(
            campaign_id=36, business_task_id=55,
            operation_id=run.operation_id, request_id=run.request_id,
        ),
        http_status=200, business_code=200,
        operation_id=run.operation_id, request_id=run.request_id,
        payload_hash=run.payload_hash,
    )
    updated = apply_start_result(run, accepted)
    assert updated.status == RecommendationRunStatus.ACCEPTED
    assert updated.start_attempted is True
    with pytest.raises(ValueError, match="not ready"):
        build_recommendation_start_request(updated)


def test_unknown_write_never_becomes_a_second_start_and_pending_read_stays_uncertain():
    draft, _ = _previewed()
    run = draft.recommendation_run
    unknown = apply_start_result(
        run,
        OperationReceipt(
            status=OperationStatus.UNKNOWN,
            operation_id=run.operation_id,
            request_id=run.request_id,
            payload_hash=run.payload_hash,
            campaign_id=36,
            business_task_id=55,
            error=AdapterError(
                kind=AdapterErrorKind.TIMEOUT,
                message="timed out",
                reconciliation_required=True,
            ),
        ),
    )
    assert unknown.status == RecommendationRunStatus.UNKNOWN
    still_unknown = apply_task_poll(
        unknown, JavaTask(id=55, campaignId=36, status="pending"), poll_sequence=1
    )
    assert still_unknown.status == RecommendationRunStatus.RECONCILIATION_REQUIRED
    assert still_unknown.reconciliation_required is True
    with pytest.raises(ValueError, match="not ready"):
        build_recommendation_start_request(still_unknown)


def test_explicit_recommend_rejection_is_failed_without_reconciliation():
    draft, _ = _previewed()
    run = draft.recommendation_run
    rejected = apply_start_result(
        run,
        OperationReceipt(
            status=OperationStatus.REJECTED,
            operation_id=run.operation_id,
            request_id=run.request_id,
            payload_hash=run.payload_hash,
            campaign_id=36,
            business_task_id=55,
            error=AdapterError(kind=AdapterErrorKind.BUSINESS_CODE, message="rejected"),
        ),
    )
    assert rejected.status == RecommendationRunStatus.FAILED
    assert rejected.reconciliation_required is False


def test_poll_sequence_is_monotonic_and_completed_run_is_immutable():
    draft, _ = _previewed()
    run = draft.recommendation_run.model_copy(update={
        "status": RecommendationRunStatus.ACCEPTED,
        "start_attempted": True,
    })
    progressed = apply_task_poll(
        run, JavaTask(id=55, campaignId=36, status="in_progress", progress=30), poll_sequence=2
    )
    assert progressed.status == RecommendationRunStatus.IN_PROGRESS
    assert apply_task_poll(
        progressed, JavaTask(id=55, campaignId=36, status="pending"), poll_sequence=1
    ).status == RecommendationRunStatus.IN_PROGRESS
    completed = apply_task_poll(
        progressed,
        JavaTask(id=55, campaignId=36, status="recommend_completed", progress=100, downstreamTaskId="down-1"),
        poll_sequence=3,
    )
    completed.result_ref = uuid4()
    unchanged = apply_task_poll(
        completed, JavaTask(id=55, campaignId=36, status="failed"), poll_sequence=4
    )
    assert unchanged.status == RecommendationRunStatus.RECOMMEND_COMPLETED


def test_results_require_completed_run_and_create_bounded_non_durable_projection():
    draft, _ = _previewed()
    run = draft.recommendation_run.model_copy(update={
        "status": RecommendationRunStatus.RECOMMEND_COMPLETED,
        "downstream_task_id": "down-1",
    })
    results = JavaRecommendationResults(kols=[
        JavaRecommendationCandidate(
            kolBackendId="kol-1", source="recommend",
            kolName="Alice", kolDetail={"tier": "T3", "homepage_url": "javascript:bad"},
        )
    ])
    updated, snapshot, projection = validate_results_and_make_snapshot(run, results)
    assert updated.result_ref == snapshot.snapshot_ref
    assert projection.candidate_universe_complete is False
    assert projection.durable is False
    assert projection.items[0].homepage_url is None


def test_unknown_and_transient_reads_have_bounded_recovery_policy():
    draft, _ = _previewed()
    run = draft.recommendation_run.model_copy(update={
        "status": RecommendationRunStatus.ACCEPTED,
        "start_attempted": True,
    })
    error = AdapterError(kind=AdapterErrorKind.TIMEOUT, message="timeout", retryable=True)
    for _ in range(3):
        run = mark_read_error(run, error)
        assert run.status == RecommendationRunStatus.ACCEPTED
    run = mark_read_error(run, error)
    assert run.status == RecommendationRunStatus.RECONCILIATION_REQUIRED
    assert decide_recommendation_recovery(run).action == RecommendationRecoveryAction.MANUAL_RECONCILIATION


def test_graph_keeps_recommend_preview_and_start_as_two_separate_events():
    class Services:
        def __init__(self):
            self.calls = []

        async def read_task(self, request):
            self.calls.append("read_task")
            return AdapterResponse(
                value=JavaTask(
                    id=55, campaignId=36, status="pending", platform="Douyin"
                ),
                http_status=200, business_code=200,
                operation_id=request.operation_id, request_id=request.request_id,
            )

        async def start_recommendation(self, request):
            self.calls.append("start_recommendation")
            return OperationReceipt(
                status=OperationStatus.UNKNOWN,
                operation_id=request.operation_id,
                request_id=request.request_id,
                payload_hash=request.payload_hash,
                campaign_id=36,
                business_task_id=55,
            )

        async def read_recommendation_results(self, request):
            raise AssertionError("results must not be read before a poll")

    draft, _ = _draft_and_task()
    services = Services()
    graph = build_graph(services)
    previewed = __import__("asyncio").run(graph.ainvoke({
        "draft": draft,
        "incoming_event": RecommendationStartRequested(
            client_event_id=uuid4(), task_id=draft.task_id,
            expected_form_revision=draft.form_revision,
        ),
        "actor_scope": SCOPE,
        "trusted_brand": BRAND,
    }))
    assert services.calls == ["read_task"]
    assert previewed["draft"].recommendation_run.status == RecommendationRunStatus.AWAITING_CONFIRMATION
    operation = previewed["draft"].pending_business_operation
    confirmed = __import__("asyncio").run(graph.ainvoke({
        "draft": previewed["draft"],
        "incoming_event": RecommendationOperationConfirmation(
            client_event_id=uuid4(), task_id=draft.task_id,
            operation_id=operation.operation_id, request_id=operation.request_id,
            confirmation_id=operation.confirmation_id, campaign_id=36,
            business_task_id=55, form_revision=draft.form_revision,
            payload_hash=operation.payload_hash,
        ),
        "actor_scope": SCOPE,
        "trusted_brand": BRAND,
    }))
    assert services.calls == ["read_task", "read_task", "start_recommendation"]
    assert confirmed["draft"].recommendation_run.status == RecommendationRunStatus.UNKNOWN


def test_graph_polls_read_only_then_projects_results_only_after_completed():
    from app.schemas.recommendation import RecommendationPollEvent
    from app.services.recommendation_snapshots import VolatileRecommendationSnapshotStore

    class Services:
        def __init__(self):
            self.calls = []

        async def read_task(self, request):
            self.calls.append(("task", request.downstream_task_id))
            return AdapterResponse(
                value=JavaTask(
                    id=55, campaignId=36, status="recommend_completed", progress=100,
                    downstreamTaskId="down-1",
                ),
                http_status=200, business_code=200,
                operation_id=request.operation_id, request_id=request.request_id,
            )

        async def read_recommendation_results(self, request):
            self.calls.append(("results", request.downstream_task_id))
            return AdapterResponse(
                value=JavaRecommendationResults(kols=[
                    JavaRecommendationCandidate(kolBackendId="kol-1", source="recommend")
                ]),
                http_status=200, business_code=200,
                operation_id=request.operation_id, request_id=request.request_id,
            )

        async def start_recommendation(self, request):
            raise AssertionError("polling must never issue recommend POST")

    draft, _ = _previewed()
    run = draft.recommendation_run
    run.status = RecommendationRunStatus.ACCEPTED
    run.start_attempted = True
    draft.recommendation_run = run
    services = Services()
    store = VolatileRecommendationSnapshotStore()
    graph = build_graph(services, store)
    import asyncio
    result = asyncio.run(graph.ainvoke({
        "draft": draft,
        "incoming_event": RecommendationPollEvent(
            client_event_id=uuid4(), task_id=draft.task_id,
            operation_id=run.operation_id, request_id=run.request_id,
            poll_sequence=1,
        ),
        "actor_scope": SCOPE,
        "trusted_brand": BRAND,
    }))
    assert services.calls == [("task", None), ("results", "down-1")]
    assert result["recommendation_result"].disposition == PollDisposition.COMPLETED
    assert result["candidate_projection"].candidate_universe_complete is False
    assert result["draft"].selection.snapshot_ref == result["candidate_projection"].snapshot_ref
    assert "candidate_universe_incomplete" in result["draft"].selection.final_operation.preview.blocked_reasons
    assert store.get_results(result["draft"].recommendation_run.result_ref) is not None
