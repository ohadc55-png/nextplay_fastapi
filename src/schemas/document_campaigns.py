"""Pydantic shapes for /org/api/document-campaigns/* (Phase 2.4, lands in 2.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.schemas.common import ORMModel

RecipientFilterType = Literal["all", "region", "branch", "team", "specific_players"]
DeliveryChannel = Literal["email", "sms"]
CampaignStatus = Literal["DRAFT", "QUEUED", "SENDING", "COMPLETED", "CANCELLED"]


class RecipientFilter(BaseModel):
    """Resolution rules for who receives this campaign. The shape mirrors
    the JSON column on document_campaigns; the actual resolver lives in
    Part B (Sub-Phase 2.4) and reads this back."""

    type: RecipientFilterType
    region_id: int | None = None
    branch_id: int | None = None
    team_ids: list[int] = Field(default_factory=list)
    player_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_consistency(self) -> RecipientFilter:
        # Cheap up-front check; the service layer also enforces scoping.
        if self.type == "region" and self.region_id is None:
            raise ValueError("region_id required when type='region'")
        if self.type == "branch" and self.branch_id is None:
            raise ValueError("branch_id required when type='branch'")
        if self.type == "team" and not self.team_ids:
            raise ValueError("team_ids required when type='team'")
        if self.type == "specific_players" and not self.player_ids:
            raise ValueError("player_ids required when type='specific_players'")
        return self


class DocumentCampaignCreate(BaseModel):
    template_id: int
    title: str = Field(min_length=1, max_length=200)
    recipient_filter: RecipientFilter
    delivery_channels: list[DeliveryChannel] = Field(min_length=1)
    scheduled_at: datetime | None = None
    expires_at: datetime
    reminder_after_days: int | None = Field(default=7, ge=1, le=90)


class DocumentCampaignOut(ORMModel):
    id: int
    organization_id: int
    template_id: int
    title: str
    recipient_filter: dict
    delivery_channels: list[str]
    scheduled_at: datetime | None = None
    status: str
    expires_at: datetime
    reminder_after_days: int | None = None
    total_recipients: int
    total_signed: int
    total_opened: int
    total_failed: int
    created_by_user_id: int | None = None
    sent_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "CampaignStatus",
    "DeliveryChannel",
    "DocumentCampaignCreate",
    "DocumentCampaignOut",
    "RecipientFilter",
    "RecipientFilterType",
]
