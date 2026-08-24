from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


class Condition(StrEnum):
    NEW_WITH_TAGS = "new_with_tags"
    NEW_WITHOUT_TAGS = "new_without_tags"
    VERY_GOOD = "very_good"
    GOOD = "good"
    SATISFACTORY = "satisfactory"
    UNKNOWN = "unknown"


class Listing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=100)
    listing_id: str = Field(min_length=1, max_length=200)
    url: AnyHttpUrl
    title: str = Field(min_length=1, max_length=1000)
    item_price_huf: int = Field(ge=0)
    currency: str = "HUF"
    brand: str | None = None
    category: str | None = None
    size: str | None = None
    condition: Condition = Condition.UNKNOWN
    image_url: AnyHttpUrl | None = None
    seller_name: str | None = None
    seller_rating: float | None = Field(default=None, ge=0, le=5)
    seller_review_count: int | None = Field(default=None, ge=0)
    buyer_fee_huf: int | None = Field(default=None, ge=0)
    shipping_huf: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def only_huf_is_supported(cls, value: str) -> str:
        normalized = value.upper()
        if normalized != "HUF":
            raise ValueError("only HUF listings are supported")
        return normalized

    @field_validator("created_at")
    @classmethod
    def make_timestamp_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class FetchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listings: list[Listing]
    comparables: list[Listing] = Field(default_factory=list)
    next_cursor: str | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)
