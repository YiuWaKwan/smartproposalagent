"""Application settings; integration credentials are not configured in the base."""

from functools import lru_cache
import os

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_name: str = "Smart Proposal Agent"
    environment: str = "development"
    smartproposal_api_base_url: str | None = None
    smartproposal_api_bearer_token: str | None = None
    smartproposal_write_contract_verified: bool = False
    smartproposal_identity_contract_verified: bool = False
    smartproposal_idempotency_contract_verified: bool = False
    smartproposal_reconciliation_contract_verified: bool = False
    smartproposal_operation_id_header: str | None = None
    smartproposal_request_id_header: str | None = None
    smartproposal_idempotency_key_header: str | None = None
    smartproposal_tenant_id_header: str | None = None
    smartproposal_user_id_header: str | None = None
    smartproposal_brand_id_header: str | None = None
    smartproposal_confirmation_id_header: str | None = None
    smartproposal_form_revision_header: str | None = None
    smartproposal_payload_hash_header: str | None = None
    smartproposal_operation_type_header: str | None = None
    smartproposal_campaign_create_path: str = "/api/campaigns"
    smartproposal_campaign_detail_path: str = "/api/campaigns/{campaign_id}"
    smartproposal_task_create_path: str = "/api/campaigns/{campaign_id}/tasks"
    smartproposal_task_list_path: str = "/api/campaigns/{campaign_id}/tasks"
    smartproposal_task_detail_path: str = "/api/campaigns/{campaign_id}/tasks/{task_id}"
    smartproposal_recommend_path: str = "/api/campaigns/{campaign_id}/tasks/{task_id}/recommend"
    smartproposal_results_path: str = "/api/campaigns/{campaign_id}/tasks/{task_id}/results"
    smartproposal_recommendation_contract_verified: bool = False
    smartproposal_recommendation_idempotency_verified: bool = False
    smartproposal_recommendation_reconciliation_verified: bool = False
    smartproposal_recommendation_operation_header: str | None = None
    smartproposal_recommendation_request_header: str | None = None
    smartproposal_recommendation_idempotency_header: str | None = None
    smartproposal_max_result_bytes: int = Field(default=5_000_000, gt=0)
    smartproposal_max_result_candidates: int = Field(default=100_000, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        environment=os.getenv("AGENT_ENVIRONMENT", "development"),
        smartproposal_api_base_url=os.getenv("SMARTPROPOSAL_API_BASE_URL"),
        smartproposal_api_bearer_token=os.getenv("SMARTPROPOSAL_API_BEARER_TOKEN"),
        smartproposal_write_contract_verified=_env_bool("SMARTPROPOSAL_WRITE_CONTRACT_VERIFIED"),
        smartproposal_identity_contract_verified=_env_bool("SMARTPROPOSAL_IDENTITY_CONTRACT_VERIFIED"),
        smartproposal_idempotency_contract_verified=_env_bool("SMARTPROPOSAL_IDEMPOTENCY_CONTRACT_VERIFIED"),
        smartproposal_reconciliation_contract_verified=_env_bool("SMARTPROPOSAL_RECONCILIATION_CONTRACT_VERIFIED"),
        smartproposal_operation_id_header=os.getenv("SMARTPROPOSAL_OPERATION_ID_HEADER"),
        smartproposal_request_id_header=os.getenv("SMARTPROPOSAL_REQUEST_ID_HEADER"),
        smartproposal_idempotency_key_header=os.getenv("SMARTPROPOSAL_IDEMPOTENCY_KEY_HEADER"),
        smartproposal_tenant_id_header=os.getenv("SMARTPROPOSAL_TENANT_ID_HEADER"),
        smartproposal_user_id_header=os.getenv("SMARTPROPOSAL_USER_ID_HEADER"),
        smartproposal_brand_id_header=os.getenv("SMARTPROPOSAL_BRAND_ID_HEADER"),
        smartproposal_confirmation_id_header=os.getenv("SMARTPROPOSAL_CONFIRMATION_ID_HEADER"),
        smartproposal_form_revision_header=os.getenv("SMARTPROPOSAL_FORM_REVISION_HEADER"),
        smartproposal_payload_hash_header=os.getenv("SMARTPROPOSAL_PAYLOAD_HASH_HEADER"),
        smartproposal_operation_type_header=os.getenv("SMARTPROPOSAL_OPERATION_TYPE_HEADER"),
        smartproposal_campaign_create_path=os.getenv(
            "SMARTPROPOSAL_CAMPAIGN_CREATE_PATH", "/api/campaigns"
        ),
        smartproposal_campaign_detail_path=os.getenv(
            "SMARTPROPOSAL_CAMPAIGN_DETAIL_PATH", "/api/campaigns/{campaign_id}"
        ),
        smartproposal_task_create_path=os.getenv(
            "SMARTPROPOSAL_TASK_CREATE_PATH", "/api/campaigns/{campaign_id}/tasks"
        ),
        smartproposal_task_list_path=os.getenv("SMARTPROPOSAL_TASK_LIST_PATH", "/api/campaigns/{campaign_id}/tasks"),
        smartproposal_task_detail_path=os.getenv("SMARTPROPOSAL_TASK_DETAIL_PATH", "/api/campaigns/{campaign_id}/tasks/{task_id}"),
        smartproposal_recommend_path=os.getenv("SMARTPROPOSAL_RECOMMEND_PATH", "/api/campaigns/{campaign_id}/tasks/{task_id}/recommend"),
        smartproposal_results_path=os.getenv("SMARTPROPOSAL_RESULTS_PATH", "/api/campaigns/{campaign_id}/tasks/{task_id}/results"),
        smartproposal_recommendation_contract_verified=_env_bool("SMARTPROPOSAL_RECOMMENDATION_CONTRACT_VERIFIED"),
        smartproposal_recommendation_idempotency_verified=_env_bool("SMARTPROPOSAL_RECOMMENDATION_IDEMPOTENCY_VERIFIED"),
        smartproposal_recommendation_reconciliation_verified=_env_bool("SMARTPROPOSAL_RECOMMENDATION_RECONCILIATION_VERIFIED"),
        smartproposal_recommendation_operation_header=os.getenv("SMARTPROPOSAL_RECOMMENDATION_OPERATION_HEADER"),
        smartproposal_recommendation_request_header=os.getenv("SMARTPROPOSAL_RECOMMENDATION_REQUEST_HEADER"),
        smartproposal_recommendation_idempotency_header=os.getenv("SMARTPROPOSAL_RECOMMENDATION_IDEMPOTENCY_HEADER"),
        smartproposal_max_result_bytes=int(os.getenv("SMARTPROPOSAL_MAX_RESULT_BYTES", "5000000")),
        smartproposal_max_result_candidates=int(os.getenv("SMARTPROPOSAL_MAX_RESULT_CANDIDATES", "100000")),
    )


def _env_bool(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"
