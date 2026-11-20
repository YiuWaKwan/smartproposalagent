"""Java SmartProposal HTTP adapter: mapping, transport and envelope parsing only."""

import hashlib
import json
from typing import Any, TypeVar
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.schemas.business_api import (
    AdapterError,
    AdapterErrorKind,
    AdapterResponse,
    ActorScope,
    BusinessOperationType,
    CampaignCreated,
    CampaignDetails,
    CampaignCreateRequest,
    JavaEnvelope,
    OperationReceipt,
    OperationStatus,
    TaskCreated,
    TaskCreateRequest,
    TrustedBrand,
)
from app.schemas.domain import CampaignDraft, RecommendationTaskDraft
from app.schemas.domain.selection import SelectionOperationStatus, SelectionReceipt
from app.schemas.recommendation import (
    JavaRecommendationResults,
    JavaTask,
    JavaTaskList,
    RecommendationReadRequest,
    RecommendationStartAccepted,
    RecommendationStartRequest,
)
from app.schemas.selection import SelectionConfirmRequest, SelectionSaveRequest

T = TypeVar("T", bound=BaseModel)


def canonical_json(payload: BaseModel) -> str:
    """Stable compact representation used for operation integrity hashes."""

    return json.dumps(
        payload.model_dump(mode="json", by_alias=True, exclude_unset=True, exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_payload_hash(payload: BaseModel) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def campaign_payload_from_draft(draft: CampaignDraft, trusted_brand: TrustedBrand):
    """Map existing domain fields without defaults, trimming or inference."""

    from app.schemas.business_api import CampaignCreatePayload

    return CampaignCreatePayload(
        name=draft.name,
        title=draft.title,
        brandName=trusted_brand.brand_name,
        platform=draft.platform,
        type=draft.campaign_type,
        budget=draft.budget,
        product=draft.product,
        sellingPoint=draft.selling_point,
        competitors=draft.competitors,
        startDate=draft.start_date,
        endDate=draft.end_date,
        persona=draft.persona,
        emotion=draft.emotion,
        coreSellingPoint=draft.core_selling_point,
    )


def task_payload_from_draft(draft: RecommendationTaskDraft):
    from app.schemas.business_api import TaskCreatePayload

    return TaskCreatePayload(
        name=draft.name,
        kolCount=draft.kol_count,
        budget=draft.budget,
        tierDistribution=draft.tier_distribution,
        talkingAngle=draft.talking_angle,
        talentType=draft.talent_type,
        scene=draft.scene,
        contentPreference=draft.content_preference,
        talentAbility=draft.talent_ability,
        remark=draft.remark,
    )


class SmartProposalAdapter:
    """Does not retry writes, resolve brands, or authorize operations."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self.client is not None and self._owns_client:
            await self.client.aclose()

    async def create_campaign(
        self, request: CampaignCreateRequest
    ) -> AdapterResponse[CampaignCreated] | OperationReceipt:
        return await self._post(
            self.settings.smartproposal_campaign_create_path,
            request.payload,
            request.operation_id,
            request.request_id,
            request.payload_hash,
            CampaignCreated,
            actor_scope=request.actor_scope,
            operation_type=BusinessOperationType.CREATE_CAMPAIGN,
            confirmation_id=request.confirmation_id,
            form_revision=request.form_revision,
            campaign_id_field=True,
        )

    async def create_task(
        self, request: TaskCreateRequest
    ) -> AdapterResponse[TaskCreated] | OperationReceipt:
        path = self.settings.smartproposal_task_create_path.format(campaign_id=request.campaign_id)
        return await self._post(
            path,
            request.payload,
            request.operation_id,
            request.request_id,
            request.payload_hash,
            TaskCreated,
            actor_scope=request.actor_scope,
            operation_type=BusinessOperationType.CREATE_TASK,
            confirmation_id=request.confirmation_id,
            form_revision=request.form_revision,
            campaign_id=request.campaign_id,
        )

    async def read_campaign(self, campaign_id: int, actor_scope: ActorScope):
        """Read a Campaign for a caller that already has trusted auth context."""

        if not self.settings.smartproposal_identity_contract_verified or not all((
            self.settings.smartproposal_tenant_id_header,
            self.settings.smartproposal_user_id_header,
            self.settings.smartproposal_brand_id_header,
        )):
            return AdapterError(kind=AdapterErrorKind.CONFIGURATION, message="Campaign identity propagation contract has not been verified")
        if not self.settings.smartproposal_api_base_url or not self.settings.smartproposal_api_bearer_token:
            return AdapterError(kind=AdapterErrorKind.CONFIGURATION, message="SmartProposal API is not configured")
        path = self.settings.smartproposal_campaign_detail_path.format(campaign_id=campaign_id)
        client = self.client or httpx.AsyncClient(timeout=30.0)
        owned = self.client is None
        try:
            response = await client.get(
                self.settings.smartproposal_api_base_url.rstrip("/") + "/" + path.lstrip("/"),
                headers={
                    "Authorization": f"Bearer {self.settings.smartproposal_api_bearer_token}",
                    str(self.settings.smartproposal_tenant_id_header): actor_scope.tenant_id,
                    str(self.settings.smartproposal_user_id_header): actor_scope.user_id,
                    str(self.settings.smartproposal_brand_id_header): actor_scope.brand_id,
                },
            )
        except httpx.TimeoutException:
            return AdapterError(kind=AdapterErrorKind.TIMEOUT, message="Campaign lookup timed out", reconciliation_required=True)
        except httpx.RequestError:
            return AdapterError(kind=AdapterErrorKind.CONNECTION, message="Campaign lookup failed")
        finally:
            if owned:
                await client.aclose()
        if not 200 <= response.status_code < 300:
            return AdapterError(kind=AdapterErrorKind.HTTP_STATUS, message="Campaign lookup returned non-success status", http_status=response.status_code)
        try:
            envelope = JavaEnvelope[CampaignDetails].model_validate(response.json())
            if envelope.code != 200:
                return AdapterError(kind=AdapterErrorKind.BUSINESS_CODE, message="Campaign lookup was rejected", http_status=response.status_code, business_code=envelope.code)
            details = CampaignDetails.model_validate(envelope.data)
            if details.campaign_id != campaign_id:
                return AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="Campaign detail ID does not match requested ID", http_status=response.status_code)
        except (ValueError, TypeError, ValidationError):
            return AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="Campaign details response is invalid", http_status=response.status_code)
        return AdapterResponse(value=details, http_status=response.status_code, business_code=envelope.code)

    async def save_selection(self, request: SelectionSaveRequest) -> SelectionReceipt:
        """Typed gate for batch-select. This slice never constructs an HTTP request."""
        if not isinstance(request, SelectionSaveRequest):
            raise TypeError("batch-select requires SelectionSaveRequest")
        return SelectionReceipt.from_preview(request.preview, SelectionOperationStatus.BLOCKED, "save_authorization_unverified")

    async def confirm_selection(self, request: SelectionConfirmRequest) -> SelectionReceipt:
        """Typed gate for batch-confirm-selection pending Java snapshot/revision support."""
        if not isinstance(request, SelectionConfirmRequest):
            raise TypeError("batch-confirm-selection requires SelectionConfirmRequest")
        return SelectionReceipt.from_preview(request.preview, SelectionOperationStatus.BLOCKED, "java_snapshot_and_revision_unverified")

    async def start_recommendation(self, request: RecommendationStartRequest):
        """Issue one POST only when the dedicated recommendation contract gate is enabled."""

        required = (
            self.settings.smartproposal_recommendation_contract_verified,
            self.settings.smartproposal_recommendation_idempotency_verified,
            self.settings.smartproposal_recommendation_reconciliation_verified,
            self.settings.smartproposal_identity_contract_verified,
            self.settings.smartproposal_recommendation_operation_header,
            self.settings.smartproposal_recommendation_request_header,
            self.settings.smartproposal_recommendation_idempotency_header,
            (self.settings.smartproposal_confirmation_id_header or "").strip(),
            (self.settings.smartproposal_form_revision_header or "").strip(),
            (self.settings.smartproposal_payload_hash_header or "").strip(),
            self.settings.smartproposal_tenant_id_header,
            self.settings.smartproposal_user_id_header,
            self.settings.smartproposal_brand_id_header,
        )
        if not all(required):
            return self._recommendation_receipt(
                request, OperationStatus.REJECTED,
                AdapterError(kind=AdapterErrorKind.CONFIGURATION, message="Recommend write, identity, idempotency and reconciliation contracts must be verified"),
            )
        if request.idempotency_key != request.operation_id:
            return self._recommendation_receipt(
                request, OperationStatus.REJECTED,
                AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="recommendation idempotency key must equal operation ID"),
            )
        if not self.settings.smartproposal_api_base_url or not self.settings.smartproposal_api_bearer_token:
            return self._recommendation_receipt(
                request, OperationStatus.REJECTED,
                AdapterError(kind=AdapterErrorKind.CONFIGURATION, message="SmartProposal API is not configured"),
            )
        path = self.settings.smartproposal_recommend_path.format(
            campaign_id=request.campaign_id, task_id=request.business_task_id
        )
        client = self._client()
        owned = self.client is None
        try:
            response = await client.post(
                self._url(path),
                headers=self._recommendation_headers(request),
            )
        except httpx.TimeoutException:
            return self._recommendation_receipt(request, OperationStatus.UNKNOWN, AdapterError(
                kind=AdapterErrorKind.TIMEOUT, message="Recommend start timed out", reconciliation_required=True
            ))
        except httpx.RequestError:
            return self._recommendation_receipt(request, OperationStatus.UNKNOWN, AdapterError(
                kind=AdapterErrorKind.CONNECTION, message="Recommend start connection failed", reconciliation_required=True
            ))
        finally:
            if owned:
                await client.aclose()
        if not 200 <= response.status_code < 300:
            uncertain = response.status_code >= 500 or response.status_code in {408, 409, 429}
            return self._recommendation_receipt(request,
                OperationStatus.UNKNOWN if uncertain else OperationStatus.REJECTED,
                AdapterError(kind=AdapterErrorKind.HTTP_STATUS, message="Recommend start returned non-success HTTP status",
                             http_status=response.status_code, reconciliation_required=uncertain,
                             retryable=False))
        try:
            envelope = JavaEnvelope[object].model_validate(response.json())
        except (ValueError, ValidationError):
            return self._recommendation_receipt(request, OperationStatus.UNKNOWN, AdapterError(
                kind=AdapterErrorKind.INVALID_ENVELOPE, message="Recommend response envelope is invalid",
                http_status=response.status_code, reconciliation_required=True
            ))
        if envelope.code != 200:
            return self._recommendation_receipt(request, OperationStatus.REJECTED, AdapterError(
                kind=AdapterErrorKind.BUSINESS_CODE, message="Java rejected recommendation start",
                http_status=response.status_code, business_code=envelope.code
            ))
        # Java's source controller returns code=200 with null data. It means accepted only.
        return AdapterResponse(
            value=RecommendationStartAccepted(
                campaign_id=request.campaign_id,
                business_task_id=request.business_task_id,
                operation_id=request.operation_id,
                request_id=request.request_id,
                accepted=True,
            ),
            http_status=response.status_code,
            business_code=envelope.code,
            operation_id=request.operation_id,
            request_id=request.request_id,
            payload_hash=request.payload_hash,
        )

    async def read_task(self, request: RecommendationReadRequest):
        path = self.settings.smartproposal_task_detail_path.format(
            campaign_id=request.campaign_id, task_id=request.business_task_id
        )
        return await self._read_task_path(path, request)

    async def list_tasks(self, request: RecommendationReadRequest):
        path = self.settings.smartproposal_task_list_path.format(campaign_id=request.campaign_id)
        result = await self._get_json(path, request)
        if isinstance(result, AdapterError):
            return result
        response, envelope = result
        try:
            raw_tasks = envelope.data
            if not isinstance(raw_tasks, list):
                raise ValueError("task list data is not a list")
            tasks = [JavaTask.model_validate(item) for item in raw_tasks]
            if any(t.campaign_id != request.campaign_id for t in tasks):
                raise ValueError("task list contains a foreign Campaign task")
        except (ValueError, TypeError, ValidationError):
            return AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="Task list response is invalid", http_status=response.status_code)
        return AdapterResponse(value=JavaTaskList(items=tasks), http_status=response.status_code, business_code=envelope.code,
                               operation_id=request.operation_id, request_id=request.request_id)

    async def _read_task_path(self, path: str, request: RecommendationReadRequest):
        result = await self._get_json(path, request)
        if isinstance(result, AdapterError):
            return result
        response, envelope = result
        try:
            task = JavaTask.model_validate(envelope.data)
            if task.campaign_id != request.campaign_id or task.business_task_id != request.business_task_id:
                raise ValueError("Task response identity does not match request")
        except (ValueError, TypeError, ValidationError):
            return AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="Task detail response is invalid", http_status=response.status_code)
        return AdapterResponse(value=task, http_status=response.status_code, business_code=envelope.code,
                               operation_id=request.operation_id, request_id=request.request_id)

    async def read_recommendation_results(self, request: RecommendationReadRequest):
        path = self.settings.smartproposal_results_path.format(
            campaign_id=request.campaign_id, task_id=request.business_task_id
        )
        result = await self._get_json(
            path, request, max_response_bytes=self.settings.smartproposal_max_result_bytes
        )
        if isinstance(result, AdapterError):
            return result
        response, envelope = result
        if not isinstance(envelope.data, dict):
            return AdapterError(kind=AdapterErrorKind.MISSING_DATA, message="Recommendation results are missing", http_status=response.status_code, retryable=True)
        if not isinstance(envelope.data.get("kols"), list):
            return AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="Recommendation results lack the required KOL collection", http_status=response.status_code)
        raw = json.dumps(envelope.data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > self.settings.smartproposal_max_result_bytes:
            return AdapterError(kind=AdapterErrorKind.RESULT_TOO_LARGE, message="Recommendation results exceed configured safe parsing limit", http_status=response.status_code)
        if len(envelope.data.get("kols", [])) > self.settings.smartproposal_max_result_candidates:
            return AdapterError(kind=AdapterErrorKind.RESULT_TOO_LARGE, message="Recommendation candidate count exceeds configured limit", http_status=response.status_code)
        try:
            results = JavaRecommendationResults.model_validate(envelope.data)
        except (TypeError, ValidationError):
            return AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="Recommendation result schema is invalid", http_status=response.status_code)
        return AdapterResponse(value=results, http_status=response.status_code, business_code=envelope.code,
                               operation_id=request.operation_id, request_id=request.request_id)

    async def _get_json(
        self, path: str, request: RecommendationReadRequest, *, max_response_bytes: int | None = None
    ):
        if not self.settings.smartproposal_identity_contract_verified or not all((
            self.settings.smartproposal_tenant_id_header,
            self.settings.smartproposal_user_id_header,
            self.settings.smartproposal_brand_id_header,
        )):
            return AdapterError(kind=AdapterErrorKind.CONFIGURATION, message="Java identity propagation contract has not been verified")
        if not self.settings.smartproposal_api_base_url or not self.settings.smartproposal_api_bearer_token:
            return AdapterError(kind=AdapterErrorKind.CONFIGURATION, message="SmartProposal API is not configured")
        client = self._client()
        owned = self.client is None
        try:
            async with client.stream(
                "GET", self._url(path), headers=self._read_headers(request)
            ) as streamed:
                body = bytearray()
                byte_limit = max_response_bytes or self.settings.smartproposal_max_result_bytes
                async for chunk in streamed.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > byte_limit:
                        return AdapterError(
                            kind=AdapterErrorKind.RESULT_TOO_LARGE,
                            message="SmartProposal response exceeds configured safe parsing limit",
                            http_status=streamed.status_code,
                        )
                response = httpx.Response(
                    status_code=streamed.status_code,
                    headers=streamed.headers,
                    content=bytes(body),
                    request=streamed.request,
                )
        except httpx.TimeoutException:
            return AdapterError(kind=AdapterErrorKind.TIMEOUT, message="SmartProposal read timed out", retryable=True)
        except httpx.RequestError:
            return AdapterError(kind=AdapterErrorKind.CONNECTION, message="SmartProposal read failed", retryable=True)
        finally:
            if owned:
                await client.aclose()
        if not 200 <= response.status_code < 300:
            transient = response.status_code >= 500 or response.status_code in {408, 429}
            return AdapterError(kind=AdapterErrorKind.HTTP_STATUS, message="SmartProposal read returned non-success status",
                                http_status=response.status_code, retryable=transient)
        try:
            envelope = JavaEnvelope[object].model_validate(response.json())
        except (ValueError, ValidationError):
            return AdapterError(kind=AdapterErrorKind.INVALID_ENVELOPE, message="SmartProposal read envelope is invalid",
                                http_status=response.status_code, retryable=True)
        if envelope.code != 200:
            return AdapterError(kind=AdapterErrorKind.BUSINESS_CODE, message="SmartProposal read was rejected",
                                http_status=response.status_code, business_code=envelope.code)
        return response, envelope

    def _client(self):
        return self.client or httpx.AsyncClient(timeout=30.0)

    def _url(self, path: str) -> str:
        return self.settings.smartproposal_api_base_url.rstrip("/") + "/" + path.lstrip("/")

    def _read_headers(self, request: RecommendationReadRequest) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.smartproposal_api_bearer_token}",
            str(self.settings.smartproposal_tenant_id_header): request.actor_tenant_id,
            str(self.settings.smartproposal_user_id_header): request.actor_user_id,
            str(self.settings.smartproposal_brand_id_header): request.actor_brand_id,
        }

    def _recommendation_headers(self, request: RecommendationStartRequest) -> dict[str, str]:
        return {
            **self._read_headers(RecommendationReadRequest(
                actor_tenant_id=request.actor_tenant_id,
                actor_user_id=request.actor_user_id,
                actor_brand_id=request.actor_brand_id,
                campaign_id=request.campaign_id,
                business_task_id=request.business_task_id,
                operation_id=request.operation_id,
                request_id=request.request_id,
            )),
            str(self.settings.smartproposal_recommendation_operation_header): str(request.operation_id),
            str(self.settings.smartproposal_recommendation_request_header): str(request.request_id),
            str(self.settings.smartproposal_recommendation_idempotency_header): str(request.idempotency_key),
            str(self.settings.smartproposal_confirmation_id_header): str(request.confirmation_id),
            str(self.settings.smartproposal_form_revision_header): str(request.form_revision),
            str(self.settings.smartproposal_payload_hash_header): request.payload_hash,
        }

    @staticmethod
    def _recommendation_receipt(request, status, error):
        return OperationReceipt(status=status, operation_id=request.operation_id, request_id=request.request_id,
                                payload_hash=request.payload_hash, campaign_id=request.campaign_id,
                                business_task_id=request.business_task_id, error=error)

    async def _post(
        self,
        path: str,
        payload: BaseModel,
        operation_id,
        request_id,
        payload_hash: str,
        result_model: type[T],
        *,
        actor_scope: ActorScope,
        operation_type: BusinessOperationType,
        confirmation_id: UUID | None,
        form_revision: int,
        campaign_id: int | None = None,
        campaign_id_field: bool = False,
    ) -> AdapterResponse[T] | OperationReceipt:
        if canonical_payload_hash(payload) != payload_hash:
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.REJECTED,
                AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="operation payload hash does not match canonical payload"),
                campaign_id,
            )
        if not all((
            self.settings.smartproposal_write_contract_verified,
            self.settings.smartproposal_identity_contract_verified,
            self.settings.smartproposal_idempotency_contract_verified,
            self.settings.smartproposal_reconciliation_contract_verified,
            self.settings.smartproposal_operation_id_header,
            self.settings.smartproposal_request_id_header,
            self.settings.smartproposal_idempotency_key_header,
            self.settings.smartproposal_tenant_id_header,
            self.settings.smartproposal_user_id_header,
            self.settings.smartproposal_brand_id_header,
            self.settings.smartproposal_confirmation_id_header,
            self.settings.smartproposal_form_revision_header,
            self.settings.smartproposal_payload_hash_header,
            self.settings.smartproposal_operation_type_header,
        )):
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.REJECTED,
                AdapterError(
                    kind=AdapterErrorKind.CONFIGURATION,
                    message="SmartProposal write, identity, idempotency and reconciliation contracts must be verified",
                ),
                campaign_id,
            )
        if not self.settings.smartproposal_api_base_url or not self.settings.smartproposal_api_bearer_token:
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.REJECTED,
                AdapterError(kind=AdapterErrorKind.CONFIGURATION, message="SmartProposal API is not configured"),
                campaign_id,
            )
        client = self.client or httpx.AsyncClient(timeout=30.0)
        owned = self.client is None
        try:
            response = await client.post(
                self.settings.smartproposal_api_base_url.rstrip("/") + "/" + path.lstrip("/"),
                json=payload.model_dump(mode="json", by_alias=True, exclude_unset=True, exclude_none=True),
                headers={
                    "Authorization": f"Bearer {self.settings.smartproposal_api_bearer_token}",
                    str(self.settings.smartproposal_operation_id_header): str(operation_id),
                    str(self.settings.smartproposal_request_id_header): str(request_id),
                    str(self.settings.smartproposal_idempotency_key_header): str(operation_id),
                    str(self.settings.smartproposal_tenant_id_header): actor_scope.tenant_id,
                    str(self.settings.smartproposal_user_id_header): actor_scope.user_id,
                    str(self.settings.smartproposal_brand_id_header): actor_scope.brand_id,
                    str(self.settings.smartproposal_confirmation_id_header): str(confirmation_id),
                    str(self.settings.smartproposal_form_revision_header): str(form_revision),
                    str(self.settings.smartproposal_payload_hash_header): payload_hash,
                    str(self.settings.smartproposal_operation_type_header): operation_type.value,
                },
            )
        except httpx.TimeoutException:
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.UNKNOWN,
                AdapterError(kind=AdapterErrorKind.TIMEOUT, message="SmartProposal request timed out", reconciliation_required=True),
                campaign_id,
            )
        except httpx.RequestError:
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.UNKNOWN,
                AdapterError(kind=AdapterErrorKind.CONNECTION, message="SmartProposal connection failed", reconciliation_required=True),
                campaign_id,
            )
        finally:
            if owned:
                await client.aclose()

        if not 200 <= response.status_code < 300:
            business_code = None
            try:
                error_envelope = JavaEnvelope[object].model_validate(response.json())
                business_code = error_envelope.code
            except (ValueError, ValidationError):
                pass
            return self._receipt(
                operation_id, request_id, payload_hash,
                OperationStatus.UNKNOWN
                if response.status_code >= 500 or response.status_code in {408, 409, 429}
                else OperationStatus.REJECTED,
                AdapterError(
                    kind=AdapterErrorKind.HTTP_STATUS,
                    message="SmartProposal returned a non-success HTTP status",
                    http_status=response.status_code,
                    business_code=business_code,
                    reconciliation_required=response.status_code >= 500 or response.status_code in {408, 409, 429},
                ),
                campaign_id,
            )
        try:
            envelope = JavaEnvelope[object].model_validate(response.json())
        except (ValueError, ValidationError):
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.UNKNOWN,
                AdapterError(kind=AdapterErrorKind.INVALID_ENVELOPE, message="SmartProposal response envelope is invalid", http_status=response.status_code, reconciliation_required=True),
                campaign_id,
            )
        if envelope.code != 200:
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.REJECTED,
                AdapterError(kind=AdapterErrorKind.BUSINESS_CODE, message="SmartProposal rejected the operation", http_status=response.status_code, business_code=envelope.code),
                campaign_id,
            )
        if envelope.data is None or isinstance(envelope.data, bool):
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.UNKNOWN,
                AdapterError(kind=AdapterErrorKind.MISSING_DATA, message="SmartProposal success response has no identifier", http_status=response.status_code, business_code=envelope.code, reconciliation_required=True),
                campaign_id,
            )
        try:
            result = result_model.model_validate(
                {"campaign_id" if campaign_id_field else "business_task_id": envelope.data}
            )
        except ValidationError:
            return self._receipt(
                operation_id, request_id, payload_hash, OperationStatus.UNKNOWN,
                AdapterError(kind=AdapterErrorKind.RESPONSE_PARSE, message="SmartProposal response identifier is invalid", http_status=response.status_code, business_code=envelope.code, reconciliation_required=True),
                campaign_id,
            )
        return AdapterResponse(
            value=result,
            http_status=response.status_code,
            business_code=envelope.code,
            operation_id=operation_id,
            request_id=request_id,
            payload_hash=payload_hash,
        )

    @staticmethod
    def _receipt(operation_id, request_id, payload_hash, status, error, campaign_id):
        return OperationReceipt(
            status=status,
            operation_id=operation_id,
            request_id=request_id,
            payload_hash=payload_hash,
            campaign_id=campaign_id,
            error=error,
        )
