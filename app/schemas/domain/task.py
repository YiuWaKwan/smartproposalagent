"""The recommendation task draft owned by one Agent workflow."""

from decimal import Decimal

from pydantic import Field

from app.schemas.domain.common import DomainModel


class RecommendationTaskDraft(DomainModel):
    name: str | None = None
    kol_count: int | None = Field(default=None, ge=1)
    budget: Decimal | None = None
    tier_distribution: str | None = None
    talking_angle: str | None = None
    talent_type: str | None = None
    scene: str | None = None
    content_preference: str | None = None
    talent_ability: str | None = None
    remark: str | None = None
