"""Readiness of this base, without exposing unauthenticated business operations."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    workflow: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", workflow="not_implemented")


# TODO(api, design §5): add authenticated conversations, events, SSE, results
# and memory endpoints only after trusted identity and durable acceptance exist.
