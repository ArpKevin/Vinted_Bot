from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import Condition


class ScoreWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: float = Field(default=0.75, ge=0, le=1)
    condition: float = Field(default=0.15, ge=0, le=1)
    seller: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> ScoreWeights:
        total = self.price + self.condition + self.seller
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"score weights must sum to 1.0, got {total:g}")
        return self


class ScoreDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    target_discount_pct: float = Field(default=40, gt=0, le=100)
    min_discount_pct: float = Field(default=20, ge=0, le=100)
    min_score: float = Field(default=70, ge=0, le=100)


class MarketDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_discount_pct: float = Field(default=40, gt=0, le=100)
    min_discount_pct: float = Field(default=25, ge=0, le=100)
    min_confidence: float = Field(default=0.55, ge=0, le=1)
    min_comparables: int = Field(default=5, ge=3, le=100)
    max_comparables: int = Field(default=30, ge=3, le=100)

    @model_validator(mode="after")
    def comparable_bounds_are_valid(self) -> MarketDefaults:
        if self.max_comparables < self.min_comparables:
            raise ValueError("max_comparables must be at least min_comparables")
        return self


class WatchRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    enabled: bool = False
    valuation_mode: Literal["manual", "market"] = "manual"
    catalog_url: AnyHttpUrl | None = None
    notify_existing_on_first_run: bool = False
    query: str = ""
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    sizes: list[str] = Field(default_factory=list)
    allowed_conditions: list[Condition] = Field(default_factory=list)
    currency: Literal["HUF"] = "HUF"
    max_item_price_huf: int | None = Field(default=None, ge=0)
    reference_all_in_value_huf: int | None = Field(default=None, gt=0)
    fallback_buyer_fee_huf: int | None = Field(default=None, ge=0)
    fallback_shipping_huf: int | None = Field(default=None, ge=0)
    target_discount_pct: float | None = Field(default=None, gt=0, le=100)
    min_discount_pct: float | None = Field(default=None, ge=0, le=100)
    min_score: float | None = Field(default=None, ge=0, le=100)
    score_weights: ScoreWeights | None = None
    market_target_discount_pct: float | None = Field(default=None, gt=0, le=100)
    min_market_discount_pct: float | None = Field(default=None, ge=0, le=100)
    min_market_confidence: float | None = Field(default=None, ge=0, le=1)
    min_market_comparables: int | None = Field(default=None, ge=3, le=100)
    max_market_comparables: int | None = Field(default=None, ge=3, le=100)

    @model_validator(mode="after")
    def validate_enabled_rule(self) -> WatchRule:
        if not self.enabled:
            return self
        if self.valuation_mode == "market":
            if self.catalog_url is None:
                raise ValueError("enabled market watch is missing: catalog_url")
            if self.catalog_url.host != "www.vinted.hu" or not (
                self.catalog_url.path or ""
            ).startswith("/catalog"):
                raise ValueError(
                    "catalog_url must be a public https://www.vinted.hu/catalog... URL"
                )
            if (
                self.min_market_comparables is not None
                and self.max_market_comparables is not None
                and self.max_market_comparables < self.min_market_comparables
            ):
                raise ValueError(
                    "max_market_comparables must be at least min_market_comparables"
                )
            return self
        missing: list[str] = []
        for field_name in (
            "reference_all_in_value_huf",
            "fallback_buyer_fee_huf",
            "fallback_shipping_huf",
        ):
            if getattr(self, field_name) is None:
                missing.append(field_name)
        if missing:
            raise ValueError(f"enabled watch rule is missing: {', '.join(missing)}")
        if not any(
            [
                self.query.strip(),
                self.include_keywords,
                self.brands,
                self.categories,
                self.sizes,
            ]
        ):
            raise ValueError("enabled watch rule needs a query or at least one hard filter")
        return self


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fixture", "public_vinted_html", "authorized_vinted"] = "fixture"
    fixture_path: Path = Path("fixtures/listings.json")
    request_timeout_seconds: float = Field(default=20, ge=5, le=60)
    market_page_count: int = Field(default=1, ge=1, le=5)
    market_cache_seconds: float = Field(default=1800, ge=120, le=86400)
    user_agent: str = Field(
        default="VintedDealFinder/0.2 (+local personal-use monitor)",
        min_length=10,
        max_length=200,
    )


class DiscordConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(default="Vinted Deal Finder", min_length=1, max_length=80)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_interval_seconds: float = Field(default=120, ge=1)
    database_path: Path = Path("data/deals.db")
    seen_retention_days: int = Field(default=30, ge=1)
    max_provider_concurrency: int = Field(default=2, ge=1, le=20)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    score_defaults: ScoreDefaults = Field(default_factory=ScoreDefaults)
    market_defaults: MarketDefaults = Field(default_factory=MarketDefaults)
    watches: list[WatchRule]

    @model_validator(mode="after")
    def watch_ids_are_unique(self) -> AppConfig:
        ids = [watch.id for watch in self.watches]
        duplicate_ids = sorted({watch_id for watch_id in ids if ids.count(watch_id) > 1})
        if duplicate_ids:
            raise ValueError(f"duplicate watch IDs: {', '.join(duplicate_ids)}")
        return self


class EnvironmentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    discord_webhook_url: SecretStr | None = None
    authorized_provider_token: SecretStr | None = None
    dry_run: bool = False
    config_path: Path = Path("config.yaml")


def load_config(path: Path) -> AppConfig:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read configuration file {path}: {exc}") from exc

    try:
        raw: Any = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"configuration file {path} must contain a YAML mapping")

    config = AppConfig.model_validate(raw)
    base = path.resolve().parent
    if not config.database_path.is_absolute():
        config.database_path = base / config.database_path
    if not config.provider.fixture_path.is_absolute():
        config.provider.fixture_path = base / config.provider.fixture_path
    return config
