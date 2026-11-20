from decimal import Decimal
from uuid import uuid4

import pytest

from app.graphs.campaign.graph import build_graph
from app.graphs.campaign.nodes import WorkflowNotImplemented
from app.graphs.campaign.state import AgentState
from app.schemas.domain import AgentWorkflowStage, CampaignBinding, CampaignBindingMode, WorkflowDraft
from app.schemas.business_api import (
    AdapterResponse,
    ActorScope,
    BusinessOperationConfirmation,
    BusinessOperationType,
    CampaignCreatePayload,
    CampaignCreateRequest,
    CampaignCreated,
    CampaignDetails,
    OperationStatus,
    TaskCreated,
    TaskCreateRequest,
    TrustedBrand,
)
from app.schemas.events import CampaignSelectionEvent, FormPatchEvent
from app.schemas.patch import FieldPatch, FormPatch
from app.services.draft_service import (
    apply_campaign_selection,
    apply_form_patch,
    build_campaign_create_request,
    confirm_business_operation,
    prepare_business_operation,
    validate_and_prepare,
)
from app.services.adapters.smartproposal import canonical_payload_hash, campaign_payload_from_draft


def _state(event, draft=None) -> AgentState:
    return {
        "draft": draft or WorkflowDraft(task_id=uuid4()),
        "incoming_event": event,
    }


def _complete_patch(task_id):
    return FormPatchEvent(
        client_event_id=uuid4(),
        task_id=task_id,
        expected_form_revision=0,
        patch=FormPatch(
            operations=[
                FieldPatch(target="campaign", field="platform", op="set", value="Douyin"),
                FieldPatch(target="campaign", field="persona", op="set", value="都市女性"),
                FieldPatch(target="campaign", field="product", op="set", value="测试产品"),
                FieldPatch(target="campaign", field="selling_point", op="set", value="测试卖点"),
                FieldPatch(target="recommendation_task", field="kol_count", op="set", value=2),
                FieldPatch(target="recommendation_task", field="budget", op="set", value="20000"),
                FieldPatch(target="recommendation_task", field="tier_distribution", op="set", value="Tier3:2人"),
                FieldPatch(target="recommendation_task", field="talent_type", op="set", value="单品种草型达人"),
            ]
        ),
    )


def test_existing_campaign_selection_does_not_require_campaign_creation_fields():
    draft = WorkflowDraft(
        task_id=uuid4(),
        campaign_binding=CampaignBinding(mode=CampaignBindingMode.EXISTING, campaign_id=36),
    )
    updated = validate_and_prepare(draft)
    assert updated.stage == AgentWorkflowStage.AWAITING_CLARIFICATION
    assert "campaign.persona" not in {item.field for item in updated.validation_errors}
    assert "recommendation_task.kol_count" in {item.field for item in updated.validation_errors}


def test_patch_is_typed_revisioned_and_validated():
    event = _complete_patch(uuid4())
    draft = apply_form_patch(WorkflowDraft(task_id=event.task_id), event)
    assert draft.form_revision == 1
    assert draft.campaign.platform == "Douyin"
    assert draft.recommendation_task.budget == Decimal("20000")
    ready = validate_and_prepare(draft)
    assert ready.stage == AgentWorkflowStage.CAMPAIGN_CREATING
    assert ready.next_action == "wait_for_operation_confirmation"


def test_stale_revision_and_unknown_field_are_rejected():
    event = _complete_patch(uuid4())
    draft = WorkflowDraft(task_id=event.task_id, form_revision=1)
    with pytest.raises(ValueError, match="stale form revision"):
        apply_form_patch(draft, event)

    bad = event.model_copy(update={"patch": FormPatch(operations=[FieldPatch(target="campaign", field="kpi", op="set", value="x")])})
    with pytest.raises(ValueError, match="unsupported campaign field"):
        apply_form_patch(WorkflowDraft(task_id=event.task_id), bad)


def test_existing_campaign_graph_prepares_confirmable_task_operation():
    task_id = uuid4()
    event = _complete_patch(task_id)
    draft = WorkflowDraft(
        task_id=task_id,
        campaign_binding=CampaignBinding(mode=CampaignBindingMode.EXISTING, campaign_id=36),
    )
    event = event.model_copy(update={"expected_form_revision": 0})
    state = _state(event, draft)
    graph = build_graph()
    scope = ActorScope(tenant_id="tenant", user_id="user", brand_id="brand")
    result = graph.invoke({**state, "actor_scope": scope})
    assert result["draft"].pending_business_operation.operation_type == BusinessOperationType.CREATE_TASK
    assert result["draft"].pending_business_operation.confirmed is False
    assert result["draft"].pending_ui.type == "business_operation_confirmation"


def _complete_campaign_draft():
    task_id = uuid4()
    event = FormPatchEvent(
        client_event_id=uuid4(), task_id=task_id, expected_form_revision=0,
        patch=FormPatch(operations=[
            FieldPatch(target="campaign", field="platform", op="set", value="Douyin"),
            FieldPatch(target="campaign", field="persona", op="set", value="都市女性"),
            FieldPatch(target="campaign", field="product", op="set", value="测试产品"),
            FieldPatch(target="campaign", field="selling_point", op="set", value="测试卖点"),
        ]),
    )
    draft = apply_form_patch(WorkflowDraft(task_id=task_id), event)
    draft.recommendation_task.kol_count = 2
    draft.recommendation_task.budget = Decimal("20000.00")
    draft.recommendation_task.tier_distribution = "Tier3:2人"
    draft.recommendation_task.talent_type = "单品种草型达人"
    return draft


def test_wrong_task_id_rejected_for_patch_and_campaign_binding():
    task_id = uuid4()
    wrong_event = _complete_patch(uuid4()).model_copy(update={"task_id": task_id})
    with pytest.raises(ValueError, match="does not match workflow"):
        apply_form_patch(WorkflowDraft(task_id=uuid4()), wrong_event)
    wrong_binding = CampaignSelectionEvent(
        client_event_id=uuid4(), task_id=uuid4(), expected_form_revision=0, campaign_id=36
    )
    with pytest.raises(ValueError, match="does not match workflow"):
        apply_campaign_selection(WorkflowDraft(task_id=uuid4()), wrong_binding)


def test_campaign_operation_requires_matching_explicit_confirmation_and_revision():
    scope = ActorScope(tenant_id="t", user_id="u", brand_id="brand-1")
    brand = TrustedBrand(brand_id="brand-1", brand_name="Trusted Brand", brand_code="trusted-code")
    draft = validate_and_prepare(_complete_campaign_draft())
    preview = prepare_business_operation(draft, actor_scope=scope, trusted_brand=brand)
    pending = preview.pending_business_operation
    assert pending is not None
    with pytest.raises(ValueError, match="lacks a valid explicit"):
        build_campaign_create_request(preview, actor_scope=scope, trusted_brand=brand)

    confirmed = confirm_business_operation(
        preview,
        BusinessOperationConfirmation(
            client_event_id=uuid4(), task_id=preview.task_id, operation_id=pending.operation_id,
            operation_type=pending.operation_type, form_revision=pending.form_revision,
            payload_hash=pending.payload_hash, confirmation_id=pending.confirmation_id,
        ),
        actor_scope=scope,
    )
    request = build_campaign_create_request(confirmed, actor_scope=scope, trusted_brand=brand)
    assert request.payload_hash == pending.payload_hash
    assert request.payload.brand_name == "Trusted Brand"
    assert request.payload.budget is None
    assert request.idempotency_key == request.operation_id
    with pytest.raises(ValueError, match="does not match"):
        confirm_business_operation(
            preview,
            BusinessOperationConfirmation(
                client_event_id=uuid4(), task_id=preview.task_id, operation_id=pending.operation_id,
                operation_type=pending.operation_type, form_revision=pending.form_revision - 1,
                payload_hash=pending.payload_hash, confirmation_id=pending.confirmation_id,
            ),
            actor_scope=scope,
        )
    edited_event = FormPatchEvent(
        client_event_id=uuid4(), task_id=confirmed.task_id, expected_form_revision=confirmed.form_revision,
        patch=FormPatch(operations=[FieldPatch(target="campaign", field="emotion", op="set", value="新调性")]),
    )
    edited = validate_and_prepare(apply_form_patch(confirmed, edited_event))
    with pytest.raises(ValueError, match="lacks a valid explicit"):
        build_campaign_create_request(edited, actor_scope=scope, trusted_brand=brand)
    other_actor = ActorScope(tenant_id="t", user_id="other", brand_id="brand-1")
    with pytest.raises(ValueError, match="does not match"):
        confirm_business_operation(
            preview,
            BusinessOperationConfirmation(
                client_event_id=uuid4(), task_id=preview.task_id, operation_id=pending.operation_id,
                operation_type=pending.operation_type, form_revision=preview.form_revision,
                payload_hash=pending.payload_hash, confirmation_id=pending.confirmation_id,
            ),
            actor_scope=other_actor,
        )


def test_campaign_payload_preserves_decimal_date_and_omits_absent_optional_fields():
    draft = _complete_campaign_draft()
    draft.campaign.budget = Decimal("1234.50")
    draft.campaign.start_date = __import__("datetime").date(2026, 2, 1)
    payload = campaign_payload_from_draft(
        draft.campaign,
        TrustedBrand(brand_id="brand", brand_name="Trusted Brand", brand_code="brand-code"),
    )
    assert payload.model_dump(mode="json", by_alias=True, exclude_unset=True, exclude_none=True) == {
        "brandName": "Trusted Brand", "platform": "Douyin", "persona": "都市女性",
        "product": "测试产品", "sellingPoint": "测试卖点", "budget": "1234.50",
        "startDate": "2026-02-01",
    }
    assert canonical_payload_hash(payload) == canonical_payload_hash(payload.model_copy())


def test_use_default_only_unblocks_default_and_set_none_differs_from_clear():
    task_id = uuid4()
    draft = WorkflowDraft(task_id=task_id, suppressed_defaults={"campaign.emotion"})
    use_default = FormPatchEvent(
        client_event_id=uuid4(), task_id=task_id, expected_form_revision=0,
        patch=FormPatch(operations=[FieldPatch(target="campaign", field="emotion", op="use_default")]),
    )
    updated = apply_form_patch(draft, use_default)
    assert updated.campaign.emotion is None
    assert "campaign.emotion" not in updated.suppressed_defaults
    assert updated.form_revision == 1

    set_none = use_default.model_copy(update={
        "client_event_id": uuid4(),
        "patch": FormPatch(operations=[FieldPatch(target="campaign", field="emotion", op="set", value=None)]),
    })
    with pytest.raises(ValueError, match="set null is ambiguous"):
        apply_form_patch(draft, set_none)
    clear = use_default.model_copy(update={
        "client_event_id": uuid4(),
        "patch": FormPatch(operations=[FieldPatch(target="campaign", field="emotion", op="clear")]),
    })
    clear_result = apply_form_patch(draft, clear)
    assert "campaign.emotion" in clear_result.suppressed_defaults

    blank = use_default.model_copy(update={
        "client_event_id": uuid4(),
        "patch": FormPatch(operations=[FieldPatch(target="campaign", field="emotion", op="set", value="")]),
    })
    blank_result = apply_form_patch(draft, blank)
    assert blank_result.campaign.emotion == ""
    assert "campaign.emotion" not in blank_result.suppressed_defaults

    empty_list = use_default.model_copy(update={
        "client_event_id": uuid4(),
        "patch": FormPatch(operations=[FieldPatch(target="campaign", field="emotion", op="set", value=[])]),
    })
    with pytest.raises(ValueError, match="invalid value"):
        apply_form_patch(draft, empty_list)


def test_campaign_create_then_task_create_requires_two_explicit_confirmations():
    class LocalContractService:
        def __init__(self):
            self.calls = []

        async def create_campaign(self, request):
            self.calls.append(("campaign", request))
            return AdapterResponse(
                value=CampaignCreated(campaign_id=36), http_status=200, business_code=200,
                operation_id=request.operation_id, request_id=request.request_id,
                payload_hash=request.payload_hash,
            )

        async def read_campaign(self, campaign_id, actor_scope):
            self.calls.append(("read", campaign_id, actor_scope))
            return AdapterResponse(
                value=CampaignDetails(id=campaign_id, brandName="Trusted Brand", brandCode="trusted-code", platform="Douyin"),
                http_status=200,
                business_code=200,
            )

        async def create_task(self, request):
            self.calls.append(("task", request))
            return AdapterResponse(
                value=TaskCreated(business_task_id=55), http_status=200, business_code=200,
                operation_id=request.operation_id, request_id=request.request_id,
                payload_hash=request.payload_hash,
            )

    import asyncio

    scope = ActorScope(tenant_id="tenant", user_id="user", brand_id="brand-1")
    brand = TrustedBrand(brand_id="brand-1", brand_name="Trusted Brand", brand_code="trusted-code")
    services = LocalContractService()
    graph = build_graph(services)
    draft = WorkflowDraft(task_id=uuid4())
    preview_state = graph.invoke({
        "draft": draft,
        "incoming_event": _complete_patch(draft.task_id),
        "actor_scope": scope,
        "trusted_brand": brand,
    })
    campaign_operation = preview_state["draft"].pending_business_operation
    assert campaign_operation.operation_type == BusinessOperationType.CREATE_CAMPAIGN
    assert services.calls == []

    campaign_confirmed = BusinessOperationConfirmation(
        client_event_id=uuid4(), task_id=draft.task_id, operation_id=campaign_operation.operation_id,
        operation_type=campaign_operation.operation_type, form_revision=preview_state["draft"].form_revision,
        payload_hash=campaign_operation.payload_hash, confirmation_id=campaign_operation.confirmation_id,
    )
    after_campaign = asyncio.run(graph.ainvoke({
        "draft": preview_state["draft"], "incoming_event": campaign_confirmed,
        "actor_scope": scope, "trusted_brand": brand,
    }))
    assert [call[0] for call in services.calls] == ["campaign"]
    assert after_campaign["draft"].business_campaign_id == 36
    task_operation = after_campaign["draft"].pending_business_operation
    assert task_operation.operation_type == BusinessOperationType.CREATE_TASK
    assert task_operation.campaign_id == 36

    task_confirmed = BusinessOperationConfirmation(
        client_event_id=uuid4(), task_id=draft.task_id, operation_id=task_operation.operation_id,
        operation_type=task_operation.operation_type, form_revision=after_campaign["draft"].form_revision,
        payload_hash=task_operation.payload_hash, confirmation_id=task_operation.confirmation_id,
    )
    finished = asyncio.run(graph.ainvoke({
        "draft": after_campaign["draft"], "incoming_event": task_confirmed,
        "actor_scope": scope, "trusted_brand": brand,
    }))
    assert [call[0] for call in services.calls] == ["campaign", "read", "task"]
    assert finished["draft"].business_task_id == 55
    assert finished["draft"].business_task_status.value == "pending"


def test_existing_campaign_must_match_trusted_brand_before_task_create():
    class WrongBrandService:
        async def read_campaign(self, campaign_id, actor_scope):
            return AdapterResponse(
                value=CampaignDetails(id=campaign_id, brandName="Other", brandCode="other", platform="Douyin"),
                http_status=200,
                business_code=200,
            )

        async def create_task(self, request):
            raise AssertionError("Task must not be created for a mismatched Campaign")

    import asyncio
    from app.graphs.campaign.nodes import reserve_business_operation

    task_id = uuid4()
    draft = WorkflowDraft(
        task_id=task_id,
        campaign_binding=CampaignBinding(mode=CampaignBindingMode.EXISTING, campaign_id=36),
    )
    draft.recommendation_task.kol_count = 2
    draft.recommendation_task.budget = Decimal("20000")
    draft.recommendation_task.tier_distribution = "Tier3:2人"
    draft.recommendation_task.talent_type = "种草"
    scope = ActorScope(tenant_id="tenant", user_id="user", brand_id="brand-1")
    brand = TrustedBrand(brand_id="brand-1", brand_name="Trusted Brand", brand_code="trusted-code")
    draft = prepare_business_operation(draft, actor_scope=scope, trusted_brand=brand)
    operation = draft.pending_business_operation
    draft = confirm_business_operation(draft, BusinessOperationConfirmation(
        client_event_id=uuid4(), task_id=task_id, operation_id=operation.operation_id,
        operation_type=operation.operation_type, form_revision=operation.form_revision,
        payload_hash=operation.payload_hash, confirmation_id=operation.confirmation_id,
    ), actor_scope=scope)
    with pytest.raises(ValueError, match="does not match current authorized brand"):
        asyncio.run(reserve_business_operation(
            {"draft": draft, "actor_scope": scope, "trusted_brand": brand},
            business_services=WrongBrandService(),
        ))
