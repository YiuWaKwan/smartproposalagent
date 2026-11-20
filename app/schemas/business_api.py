"""Typed Java SmartProposal API contracts and operation outcomes."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import Field, StrictInt, StrictStr, model_validator

from app.schemas.domain.common import DomainModel
from app.schemas.domain.workflow import BusinessOperationType, PendingBusinessOperation

T = TypeVar("T")


class OperationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class AdapterErrorKind(StrEnum):
    CONFIGURATION = "configuration"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    HTTP_STATUS = "http_status"
    BUSINESS_CODE = "business_code"
    INVALID_ENVELOPE = "invalid_envelope"
    MISSING_DATA = "missing_data"
    RESPONSE_PARSE = "response_parse"
    RESULT_TOO_LARGE = "result_too_large"


class TrustedBrand(DomainModel):
    """Brand entry resolved from the authenticated user's authorized brand list."""

    brand_id: StrictStr = Field(min_length=1)
    brand_name: StrictStr = Field(min_length=1)
    brand_code: StrictStr = Field(min_length=1)


class ActorScope(DomainModel):
    """Trusted identity scope; never populated from an event or form payload."""

    tenant_id: StrictStr = Field(min_length=1)
    user_id: StrictStr = Field(min_length=1)
    brand_id: StrictStr = Field(min_length=1)


class CampaignCreatePayload(DomainModel):
    """Java CampaignCreateDTO; absent optional fields are omitted from JSON."""

    name: str | None = None
    title: str | None = None
    brand_name: StrictStr = Field(alias="brandName", min_length=1)
    platform: StrictStr = Field(min_length=1)
    campaign_type: str | None = Field(default=None, alias="type")
    budget: Decimal | None = Field(default=None, ge=0)
    product: StrictStr = Field(min_length=1)
    selling_point: StrictStr = Field(alias="sellingPoint", min_length=1)
    competitors: str | None = None
    start_date: date | None = Field(default=None, alias="startDate")
    end_date: date | None = Field(default=None, alias="endDate")
    persona: StrictStr = Field(min_length=1)
    emotion: str | None = None
    core_selling_point: str | None = Field(default=None, alias="coreSellingPoint")


class TaskCreatePayload(DomainModel):
    """Java TaskCreateDTO; campaign ID and platform are path/inherited data."""

    name: str | None = None
    kol_count: Annotated[StrictInt, Field(ge=1)] = Field(alias="kolCount")
    budget: Decimal = Field(gt=0)
    tier_distribution: StrictStr = Field(alias="tierDistribution", min_length=1)
    talking_angle: str | None = Field(default=None, alias="talkingAngle")
    talent_type: StrictStr = Field(alias="talentType", min_length=1)
    scene: str | None = None
    content_preference: str | None = Field(default=None, alias="contentPreference")
    talent_ability: str | None = Field(default=None, alias="talentAbility")
    remark: str | None = None


class CampaignCreateRequest(DomainModel):
    actor_scope: ActorScope
    trusted_brand: TrustedBrand
    operation_id: UUID
    request_id: UUID
    idempotency_key: UUID
    agent_task_id: UUID
    form_revision: int = Field(ge=0)
    confirmation_id: UUID
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: CampaignCreatePayload

    @model_validator(mode="after")
    def validate_actor_and_brand(self):
        if self.actor_scope.brand_id != self.trusted_brand.brand_id:
            raise ValueError("actor and trusted brand scopes must match")
        if self.payload.brand_name != self.trusted_brand.brand_name:
            raise ValueError("Campaign payload brand must come from the trusted brand entry")
        if self.idempotency_key != self.operation_id:
            raise ValueError("Campaign idempotency key must equal operation ID")
        return self


class TaskCreateRequest(DomainModel):
    actor_scope: ActorScope
    campaign_id: Annotated[StrictInt, Field(gt=0)]
    operation_id: UUID
    request_id: UUID
    idempotency_key: UUID
    agent_task_id: UUID
    form_revision: int = Field(ge=0)
    confirmation_id: UUID
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: TaskCreatePayload

    @model_validator(mode="after")
    def validate_idempotency_key(self):
        if self.idempotency_key != self.operation_id:
            raise ValueError("Task idempotency key must equal operation ID")
        return self


class JavaEnvelope(DomainModel, Generic[T]):
    code: StrictInt
    message: str | None = None
    data: T | None = None
    timestamp: int | str | None = None


class AdapterError(DomainModel):
    kind: AdapterErrorKind
    message: str
    http_status: int | None = None
    business_code: int | None = None
    retryable: bool = False
    reconciliation_required: bool = False


class OperationReceipt(DomainModel):
    status: OperationStatus
    operation_id: UUID
    request_id: UUID
    payload_hash: str
    campaign_id: int | None = None
    business_task_id: int | None = None
    error: AdapterError | None = None


class CampaignDetails(DomainModel):
    model_config = {"extra": "ignore", "populate_by_name": True}

    campaign_id: Annotated[StrictInt, Field(gt=0)] = Field(alias="id")
    brand_name: StrictStr = Field(alias="brandName", min_length=1)
    brand_code: StrictStr = Field(alias="brandCode", min_length=1)
    platform: StrictStr = Field(min_length=1)


TDomain = TypeVar("TDomain", bound=DomainModel)


class AdapterResponse(DomainModel, Generic[TDomain]):
    status: OperationStatus = OperationStatus.SUCCEEDED
    value: TDomain
    http_status: int
    business_code: int
    operation_id: UUID | None = None
    request_id: UUID | None = None
    payload_hash: str | None = None


class CampaignCreated(DomainModel):
    campaign_id: Annotated[StrictInt, Field(gt=0)]


class TaskCreated(DomainModel):
    business_task_id: Annotated[StrictInt, Field(gt=0)]


class BusinessOperationConfirmation(DomainModel):
    """Explicit user confirmation bound to a rendered operation preview."""

    type: Literal["business_operation_confirmed"] = "business_operation_confirmed"
    client_event_id: UUID
    task_id: UUID
    operation_id: UUID
    operation_type: BusinessOperationType
    form_revision: int = Field(ge=0)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_id: UUID
