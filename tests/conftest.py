from __future__ import annotations

from collections.abc import Callable

import pytest

from vinted_deal_finder.config import AppConfig, WatchRule
from vinted_deal_finder.models import Condition, Listing


@pytest.fixture
def listing_factory() -> Callable[..., Listing]:
    def factory(**overrides: object) -> Listing:
        values: dict[str, object] = {
            "provider": "test",
            "listing_id": "listing-1",
            "url": "https://example.invalid/listings/1",
            "title": "Nike Air Max 90 white trainers",
            "item_price_huf": 14_000,
            "currency": "HUF",
            "brand": "Nike",
            "category": "Shoes",
            "size": "42",
            "condition": Condition.VERY_GOOD,
            "seller_name": "Seller",
            "seller_rating": 4.9,
            "seller_review_count": 20,
            "buyer_fee_huf": 900,
            "shipping_huf": 1_300,
        }
        values.update(overrides)
        return Listing.model_validate(values)

    return factory


@pytest.fixture
def watch_factory() -> Callable[..., WatchRule]:
    def factory(**overrides: object) -> WatchRule:
        values: dict[str, object] = {
            "id": "test_watch",
            "enabled": True,
            "query": "Nike Air Max 90",
            "brands": ["Nike"],
            "categories": ["Shoes"],
            "sizes": ["42"],
            "allowed_conditions": [
                Condition.NEW_WITH_TAGS,
                Condition.NEW_WITHOUT_TAGS,
                Condition.VERY_GOOD,
            ],
            "reference_all_in_value_huf": 30_000,
            "fallback_buyer_fee_huf": 1_000,
            "fallback_shipping_huf": 1_500,
        }
        values.update(overrides)
        return WatchRule.model_validate(values)

    return factory


@pytest.fixture
def app_config_factory(watch_factory: Callable[..., WatchRule]) -> Callable[..., AppConfig]:
    def factory(**overrides: object) -> AppConfig:
        values: dict[str, object] = {
            "poll_interval_seconds": 120,
            "database_path": "data/test.db",
            "provider": {"kind": "fixture", "fixture_path": "fixtures/listings.json"},
            "watches": [watch_factory()],
        }
        values.update(overrides)
        return AppConfig.model_validate(values)

    return factory

