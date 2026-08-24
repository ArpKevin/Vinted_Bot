from __future__ import annotations

from collections.abc import Callable

import pytest

from vinted_deal_finder.config import MarketDefaults, ScoreDefaults
from vinted_deal_finder.models import Condition, Listing
from vinted_deal_finder.scoring import (
    CONDITION_SCORES,
    evaluate_listing,
    evaluate_market_listing,
    seller_score,
)


def test_hard_filters_support_unicode_and_casefold(
    listing_factory: Callable[..., Listing], watch_factory: Callable[..., object]
) -> None:
    listing = listing_factory(title="NIKE Air Max 90 – fehér cipő")
    watch = watch_factory(query="nike AIR max 90", include_keywords=["FEHÉR CIPŐ"])

    evaluation = evaluate_listing(listing, watch, ScoreDefaults())

    assert evaluation.hard_filters_match is True
    assert evaluation.qualifies is True


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"exclude_keywords": ["white"]}, "contains excluded keyword"),
        ({"brands": ["Adidas"]}, "brand does not match"),
        ({"categories": ["Jackets"]}, "category does not match"),
        ({"sizes": ["41"]}, "size does not match"),
        ({"max_item_price_huf": 10_000}, "item price exceeds maximum"),
    ],
)
def test_each_hard_filter_rejects(
    listing_factory: Callable[..., Listing],
    watch_factory: Callable[..., object],
    overrides: dict[str, object],
    expected_reason: str,
) -> None:
    evaluation = evaluate_listing(
        listing_factory(), watch_factory(**overrides), ScoreDefaults()
    )
    assert evaluation.qualifies is False
    assert expected_reason in evaluation.rejection_reasons


@pytest.mark.parametrize("condition", list(Condition))
def test_every_condition_uses_documented_score(
    listing_factory: Callable[..., Listing],
    watch_factory: Callable[..., object],
    condition: Condition,
) -> None:
    listing = listing_factory(condition=condition)
    watch = watch_factory(allowed_conditions=[])
    evaluation = evaluate_listing(listing, watch, ScoreDefaults())
    assert evaluation.condition_score == CONDITION_SCORES[condition]


@pytest.mark.parametrize(
    ("rating", "reviews", "expected"),
    [
        (None, None, 50.0),
        (5.0, 0, 50.0),
        (5.0, 5, 75.0),
        (5.0, 10, 100.0),
        (3.5, 10, 0.0),
    ],
)
def test_seller_score_confidence(
    listing_factory: Callable[..., Listing],
    rating: float | None,
    reviews: int | None,
    expected: float,
) -> None:
    assert seller_score(
        listing_factory(seller_rating=rating, seller_review_count=reviews)
    ) == pytest.approx(expected)


def test_actual_costs_are_preferred_over_fallbacks(
    listing_factory: Callable[..., Listing], watch_factory: Callable[..., object]
) -> None:
    evaluation = evaluate_listing(listing_factory(), watch_factory(), ScoreDefaults())
    assert evaluation.effective_cost_huf == 16_200
    assert evaluation.used_fallback_buyer_fee is False
    assert evaluation.used_fallback_shipping is False


def test_missing_costs_use_configured_fallbacks(
    listing_factory: Callable[..., Listing], watch_factory: Callable[..., object]
) -> None:
    listing = listing_factory(buyer_fee_huf=None, shipping_huf=None)
    evaluation = evaluate_listing(listing, watch_factory(), ScoreDefaults())
    assert evaluation.effective_cost_huf == 16_500
    assert evaluation.used_fallback_buyer_fee is True
    assert evaluation.used_fallback_shipping is True


def test_discount_and_score_thresholds_are_both_required(
    listing_factory: Callable[..., Listing], watch_factory: Callable[..., object]
) -> None:
    listing = listing_factory(item_price_huf=25_000)
    evaluation = evaluate_listing(listing, watch_factory(), ScoreDefaults())
    assert evaluation.discount_pct == pytest.approx(9.3333333333)
    assert evaluation.qualifies is False
    assert "discount is below minimum" in evaluation.rejection_reasons


def test_market_analysis_uses_robust_median_and_rejects_outlier(
    listing_factory: Callable[..., Listing], watch_factory: Callable[..., object]
) -> None:
    target = listing_factory(item_price_huf=5_000, buyer_fee_huf=500)
    watch = watch_factory(
        valuation_mode="market",
        catalog_url="https://www.vinted.hu/catalog/183-jeans",
        min_market_comparables=5,
    )
    prices = [9_500, 10_500, 11_500, 12_000, 12_500, 99_500]
    comparables = [
        listing_factory(
            listing_id=f"comparable-{index}",
            item_price_huf=price,
            buyer_fee_huf=500,
        )
        for index, price in enumerate(prices)
    ]

    evaluation = evaluate_market_listing(target, watch, comparables, MarketDefaults())

    assert evaluation.estimated_market_value_huf == pytest.approx(12_000)
    assert evaluation.comparable_count == 5
    assert evaluation.discount_pct == pytest.approx(54.166667)
    assert evaluation.market_confidence is not None
    assert evaluation.qualifies is True


def test_market_analysis_requires_enough_relevant_comparables(
    listing_factory: Callable[..., Listing], watch_factory: Callable[..., object]
) -> None:
    target = listing_factory(brand="Rare Brand")
    watch = watch_factory(
        valuation_mode="market",
        catalog_url="https://www.vinted.hu/catalog/183-jeans",
        min_market_comparables=5,
        brands=[],
    )
    comparables = [
        listing_factory(listing_id=f"other-{index}", brand="Other") for index in range(4)
    ]

    evaluation = evaluate_market_listing(target, watch, comparables, MarketDefaults())

    assert evaluation.qualifies is False
    assert "at least 5 required" in evaluation.rejection_reasons[0]
