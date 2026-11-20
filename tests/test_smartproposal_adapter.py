from datetime import date
from decimal import Decimal
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.schemas.business_api import (
    ActorScope,
    AdapterError,
    AdapterErrorKind,
    AdapterResponse,
    CampaignCreated,
    CampaignDetails,
    CampaignCreatePayload,
    CampaignCreateRequest,
    OperationReceipt,
    OperationStatus,
    TaskCreated,
    TaskCreatePayload,
    TaskCreateRequest,
    TrustedBrand,
)
from app.schemas.recommendation import RecommendationReadRequest, RecommendationStartRequest
from app.services.adapters.smartproposal import SmartProposalAdapter, canonical_payload_hash


def _settings(**overrides):
    values = {
        "smartproposal_api_base_url": "https://api.invalid/context",
        "smartproposal_api_bearer_token": "test-only",
        "smartproposal_write_contract_verified": True,
        "smartproposal_identity_contract_verified": True,
        "smartproposal_idempotency_contract_verified": True,
        "smartproposal_reconciliation_contract_verified": True,
        "smartproposal_operation_id_header": "X-Operation-Id",
        "smartproposal_request_id_header": "X-Request-Id",
        "smartproposal_idempotency_key_header": "X-Idempotency-Key",
        "smartproposal_tenant_id_header": "X-Tenant",
        "smartproposal_user_id_header": "X-User",
        "smartproposal_brand_id_header": "X-Brand",
        "smartproposal_confirmation_id_header": "X-Confirmation-Id",
        "smartproposal_form_revision_header": "X-Form-Revision",
        "smartproposal_payload_hash_header": "X-Payload-Hash",
        "smartproposal_operation_type_header": "X-Operation-Type",
        "smartproposal_recommendation_contract_verified": True,
        "smartproposal_recommendation_idempotency_verified": True,
        "smartproposal_recommendation_reconciliation_verified": True,
        "smartproposal_recommendation_operation_header": "X-Operation-Id",
        "smartproposal_recommendation_request_header": "X-Request-Id",
        "smartproposal_recommendation_idempotency_header": "X-Idempotency-Key",
    }
    values.update(overrides)
    return Settings(**values)


def _campaign_request():
    payload = CampaignCreatePayload(
        brandName="Authorized Brand",
        platform="Douyin",
        persona="Audience",
        product="Product",
        sellingPoint="Selling point",
        budget=Decimal("1200.50"),
        startDate=date(2026, 1, 2),
    )
    operation_id = uuid4()
    return CampaignCreateRequest(
        actor_scope=ActorScope(tenant_id="tenant", user_id="user", brand_id="brand"),
        trusted_brand=TrustedBrand(brand_id="brand", brand_name="Authorized Brand", brand_code="brand-code"),
        operation_id=operation_id,
        request_id=uuid4(),
        idempotency_key=operation_id,
        agent_task_id=uuid4(),
        form_revision=3,
        confirmation_id=uuid4(),
        payload_hash=canonical_payload_hash(payload),
        payload=payload,
    )


def test_campaign_adapter_maps_canonical_payload_and_typed_response():
    request = _campaign_request()
    observed = {}

    async def handler(http_request):
        observed["request"] = http_request
        return httpx.Response(200, json={"code": 200, "data": 321, "message": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SmartProposalAdapter(_settings(), client)
    result = asyncio.run(adapter.create_campaign(request))

    assert isinstance(result, AdapterResponse)
    assert result.value == CampaignCreated(campaign_id=321)
    assert result.operation_id == request.operation_id
    sent = observed["request"]
    assert sent.url.path == "/context/api/campaigns"
    assert sent.headers["X-Operation-Id"] == str(request.operation_id)
    assert sent.headers["X-Request-Id"] == str(request.request_id)
    assert sent.headers["X-Idempotency-Key"] == str(request.operation_id)
    assert sent.headers["X-Brand"] == "brand"
    assert sent.headers["X-Confirmation-Id"] == str(request.confirmation_id)
    assert sent.headers["X-Form-Revision"] == str(request.form_revision)
    assert sent.headers["X-Payload-Hash"] == request.payload_hash
    assert sent.headers["X-Operation-Type"] == "create_campaign"
    assert sent.headers["Authorization"] == "Bearer test-only"
    assert json.loads(sent.read()) == {
        "brandName": "Authorized Brand",
        "platform": "Douyin",
        "product": "Product",
        "sellingPoint": "Selling point",
        "persona": "Audience",
        "budget": "1200.50",
        "startDate": "2026-01-02",
    }
    asyncio.run(client.aclose())


@pytest.mark.parametrize(
    ("status_code", "body", "expected_status", "expected_kind"),
    [
        (422, {"code": 422, "message": "bad field", "data": None}, OperationStatus.REJECTED, AdapterErrorKind.HTTP_STATUS),
        (200, {"code": 40001, "message": "business reject", "data": None}, OperationStatus.REJECTED, AdapterErrorKind.BUSINESS_CODE),
        (200, {"code": 200, "message": "ok", "data": None}, OperationStatus.UNKNOWN, AdapterErrorKind.MISSING_DATA),
        (200, {"code": 200, "message": "ok", "data": "not-long"}, OperationStatus.UNKNOWN, AdapterErrorKind.RESPONSE_PARSE),
        (200, {"oops": True}, OperationStatus.UNKNOWN, AdapterErrorKind.INVALID_ENVELOPE),
        (503, {"code": 503, "message": "unavailable"}, OperationStatus.UNKNOWN, AdapterErrorKind.HTTP_STATUS),
        (408, {"code": 408, "message": "timeout"}, OperationStatus.UNKNOWN, AdapterErrorKind.HTTP_STATUS),
        (429, {"code": 429, "message": "throttled"}, OperationStatus.UNKNOWN, AdapterErrorKind.HTTP_STATUS),
        (409, {"code": 409, "message": "conflict"}, OperationStatus.UNKNOWN, AdapterErrorKind.HTTP_STATUS),
    ],
)
def test_write_response_envelopes_have_typed_classification(
    status_code, body, expected_status, expected_kind
):
    async def handler(_):
        return httpx.Response(status_code, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SmartProposalAdapter(_settings(), client)
    result = asyncio.run(adapter.create_campaign(_campaign_request()))
    assert isinstance(result, OperationReceipt)
    assert result.status == expected_status
    assert result.error.kind == expected_kind
    asyncio.run(client.aclose())


def test_timeout_is_unknown_and_is_never_retried():
    calls = 0

    async def handler(_):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("request timed out")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SmartProposalAdapter(_settings(), client)
    result = asyncio.run(adapter.create_campaign(_campaign_request()))
    assert isinstance(result, OperationReceipt)
    assert result.status == OperationStatus.UNKNOWN
    assert result.error.kind == AdapterErrorKind.TIMEOUT
    assert result.error.reconciliation_required is True
    assert calls == 1
    asyncio.run(client.aclose())


def test_write_gate_blocks_request_before_http_when_external_contract_not_verified():
    calls = 0

    async def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 200, "data": 321})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SmartProposalAdapter(_settings(smartproposal_idempotency_contract_verified=False), client)
    result = asyncio.run(adapter.create_campaign(_campaign_request()))
    assert isinstance(result, OperationReceipt)
    assert result.status == OperationStatus.REJECTED
    assert result.error.kind == AdapterErrorKind.CONFIGURATION
    assert calls == 0
    asyncio.run(client.aclose())


def test_task_mapping_uses_campaign_path_and_decimal_string():
    payload = TaskCreatePayload(
        kolCount=3,
        budget=Decimal("20000.25"),
        tierDistribution="Tier3:3人",
        talentType="种草型",
    )
    operation_id = uuid4()
    request_id = uuid4()
    scope = ActorScope(tenant_id="tenant", user_id="user", brand_id="brand")
    request = TaskCreateRequest(
        actor_scope=scope,
        campaign_id=321,
        operation_id=operation_id,
        request_id=request_id,
        idempotency_key=operation_id,
        agent_task_id=uuid4(),
        form_revision=5,
        confirmation_id=uuid4(),
        payload_hash=canonical_payload_hash(payload),
        payload=payload,
    )

    async def handler(http_request):
        assert http_request.url.path == "/context/api/campaigns/321/tasks"
        assert b'"kolCount":3' in http_request.read()
        assert b'"budget":"20000.25"' in http_request.read()
        return httpx.Response(200, json={"code": 200, "data": 654})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(SmartProposalAdapter(_settings(), client).create_task(request))
    assert isinstance(result, AdapterResponse)
    assert result.value == TaskCreated(business_task_id=654)
    asyncio.run(client.aclose())


def test_read_campaign_parses_typed_detail_and_rejects_wrong_id():
    scope = ActorScope(tenant_id="tenant", user_id="user", brand_id="brand")
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert request.url.path.endswith("/api/campaigns/36")
        assert request.headers["X-Tenant"] == "tenant"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "id": 36,
                    "brandName": "Authorized Brand",
                    "brandCode": "brand-code",
                    "platform": "Douyin",
                    "name": "Campaign",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SmartProposalAdapter(_settings(), client)
    result = asyncio.run(adapter.read_campaign(36, scope))
    assert isinstance(result, AdapterResponse)
    assert result.value == CampaignDetails(
        id=36, brandName="Authorized Brand", brandCode="brand-code", platform="Douyin"
    )
    assert calls == 1
    asyncio.run(client.aclose())


def test_campaign_read_refuses_unverified_identity_contract_without_request():
    calls = 0

    async def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 200, "data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = _settings(smartproposal_identity_contract_verified=False)
    result = asyncio.run(SmartProposalAdapter(settings, client).read_campaign(
        36, ActorScope(tenant_id="t", user_id="u", brand_id="b")
    ))
    assert isinstance(result, AdapterError)
    assert result.kind == AdapterErrorKind.CONFIGURATION
    assert calls == 0
    asyncio.run(client.aclose())


def _recommendation_start_request():
    from app.services.adapters.smartproposal import canonical_json
    from app.schemas.recommendation import RecommendationExecutionPayload
    payload = RecommendationExecutionPayload(
        campaign_id=36, business_task_id=55, task_status="pending",
        downstream_task_id=None, task_name="task", platform="Douyin",
        kol_count=2, budget=Decimal("20000"), tier_distribution="Tier3:2人",
        talent_type="种草",
    )
    operation_id = uuid4()
    canonical = canonical_json(payload)
    return RecommendationStartRequest(
        actor_tenant_id="tenant", actor_user_id="user", actor_brand_id="brand",
        campaign_id=36, business_task_id=55, agent_task_id=uuid4(),
        operation_id=operation_id, request_id=uuid4(), idempotency_key=operation_id,
        confirmation_id=uuid4(), form_revision=3,
        payload_hash=__import__("hashlib").sha256(canonical.encode()).hexdigest(),
        canonical_payload=canonical,
    )


def test_recommend_start_is_bodyless_post_and_returns_accepted_only():
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 200, "data": None, "message": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = _recommendation_start_request()
    result = asyncio.run(SmartProposalAdapter(_settings(), client).start_recommendation(request))
    assert isinstance(result, AdapterResponse)
    assert result.value.accepted is True
    sent = calls[0]
    assert sent.method == "POST"
    assert sent.url.path == "/context/api/campaigns/36/tasks/55/recommend"
    assert sent.content == b""
    assert sent.headers["X-Operation-Id"] == str(request.operation_id)
    assert sent.headers["X-Request-Id"] == str(request.request_id)
    assert sent.headers["X-Idempotency-Key"] == str(request.idempotency_key)
    assert sent.headers["X-Confirmation-Id"] == str(request.confirmation_id)
    assert sent.headers["X-Form-Revision"] == str(request.form_revision)
    assert sent.headers["X-Payload-Hash"] == request.payload_hash
    asyncio.run(client.aclose())


@pytest.mark.parametrize("header", [
    "smartproposal_confirmation_id_header",
    "smartproposal_form_revision_header",
    "smartproposal_payload_hash_header",
])
def test_recommend_requires_all_confirmation_headers_before_http(header):
    async def scenario():
        def unexpected_request(_):
            pytest.fail("a missing confirmation header must block HTTP")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            adapter = SmartProposalAdapter(_settings(**{header: None}), client)
            receipt = await adapter.start_recommendation(_recommendation_start_request())
            assert receipt.status == OperationStatus.REJECTED
            assert receipt.error.kind == AdapterErrorKind.CONFIGURATION

    asyncio.run(scenario())


def test_recommend_start_timeout_is_unknown_and_never_retried():
    calls = 0

    async def handler(_):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(SmartProposalAdapter(_settings(), client).start_recommendation(_recommendation_start_request()))
    assert isinstance(result, OperationReceipt)
    assert result.status == OperationStatus.UNKNOWN
    assert calls == 1
    asyncio.run(client.aclose())


def test_task_and_result_reads_are_typed_and_result_size_is_bounded():
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("/results"):
            return httpx.Response(200, json={"code": 200, "data": {"kols": [{"kolBackendId": "k1", "source": "recommend"}]}})
        return httpx.Response(200, json={"code": 200, "data": {
            "id": 55, "campaignId": 36, "status": "in_progress",
            "progress": 50, "downstreamTaskId": "down-1", "platform": "Douyin",
        }})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = SmartProposalAdapter(_settings(), client)
    read = RecommendationReadRequest(
        actor_tenant_id="tenant", actor_user_id="user", actor_brand_id="brand",
        campaign_id=36, business_task_id=55, downstream_task_id="down-1",
        operation_id=uuid4(), request_id=uuid4(),
    )
    task = asyncio.run(adapter.read_task(read))
    assert isinstance(task, AdapterResponse) and task.value.status == "in_progress"
    settings = _settings(smartproposal_max_result_bytes=20)
    oversized = asyncio.run(SmartProposalAdapter(settings, client).read_recommendation_results(read))
    assert isinstance(oversized, AdapterError)
    assert oversized.kind == AdapterErrorKind.RESULT_TOO_LARGE
    assert requests[0].method == "GET"
    asyncio.run(client.aclose())
