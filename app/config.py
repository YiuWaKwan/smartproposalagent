"""Application settings; integration credentials are not configured in the base."""

from functools import lru_cache
import os

from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_name: str = "Smart Proposal Agent"
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings(environment=os.getenv("AGENT_ENVIRONMENT", "development"))
