"""Campaign binding and Campaign create draft models."""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from app.schemas.domain.common import DomainModel


class CampaignBindingMode(StrEnum):
    NEW = "new"
    EXISTING = "existing"


class CampaignBinding(DomainModel):
    """One Agent task may create a Campaign or attach to an existing one."""

    mode: CampaignBindingMode = CampaignBindingMode.NEW
    campaign_id: int | None = None


class CampaignDraft(DomainModel):
    """Fields sent to the existing Campaign create/update service."""

    name: str | None = None
    title: str | None = None
    campaign_type: str | None = None
    platform: str | None = None
    budget: Decimal | None = None
    start_date: date | None = None
    end_date: date | None = None
    persona: str | None = None
    emotion: str | None = None
    product: str | None = None
    selling_point: str | None = None
    core_selling_point: str | None = None
    competitors: str | None = None
