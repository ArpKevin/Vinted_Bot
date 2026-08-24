from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from statistics import median

from .config import MarketDefaults, ScoreDefaults, ScoreWeights, WatchRule
from .models import Condition, Listing

CONDITION_SCORES: dict[Condition, float] = {
    Condition.NEW_WITH_TAGS: 100.0,
    Condition.NEW_WITHOUT_TAGS: 90.0,
    Condition.VERY_GOOD: 80.0,
    Condition.GOOD: 60.0,
    Condition.SATISFACTORY: 30.0,
    Condition.UNKNOWN: 50.0,
}


@dataclass(frozen=True, slots=True)
class DealEvaluation:
    hard_filters_match: bool
    qualifies: bool
    effective_cost_huf: int | None
    discount_pct: float | None
    price_score: float | None
    condition_score: float | None
    seller_score: float | None
    final_score: float | None
    used_fallback_buyer_fee: bool
    used_fallback_shipping: bool
    reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    estimated_market_value_huf: float | None = None
    comparable_count: int = 0
    market_confidence: float | None = None
    comparable_basis: str | None = None
    price_dispersion_pct: float | None = None
    shipping_included: bool = True


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _matches_one(value: str | None, allowed: list[str]) -> bool:
    if not allowed:
        return True
    if value is None:
        return False
    normalized = normalize_text(value)
    return normalized in {normalize_text(candidate) for candidate in allowed}


def hard_filter_rejections(listing: Listing, watch: WatchRule) -> list[str]:
    rejections: list[str] = []
    haystack = normalize_text(
        " ".join(
            part
            for part in [
                listing.title,
                listing.brand,
                listing.category,
                listing.size,
            ]
            if part
        )
    )
    query_tokens = [normalize_text(token) for token in watch.query.split() if token.strip()]
    if query_tokens and not all(token in haystack for token in query_tokens):
        rejections.append("query does not match")
    missing_includes = [
        keyword for keyword in watch.include_keywords if normalize_text(keyword) not in haystack
    ]
    if missing_includes:
        rejections.append("missing required keyword(s)")
    if any(normalize_text(keyword) in haystack for keyword in watch.exclude_keywords):
        rejections.append("contains excluded keyword")
    if not _matches_one(listing.brand, watch.brands):
        rejections.append("brand does not match")
    if not _matches_one(listing.category, watch.categories):
        rejections.append("category does not match")
    if not _matches_one(listing.size, watch.sizes):
        rejections.append("size does not match")
    if watch.allowed_conditions and listing.condition not in watch.allowed_conditions:
        rejections.append("condition does not match")
    if watch.max_item_price_huf is not None and listing.item_price_huf > watch.max_item_price_huf:
        rejections.append("item price exceeds maximum")
    if listing.currency != watch.currency:
        rejections.append("currency does not match")
    return rejections


def seller_score(listing: Listing) -> float:
    if listing.seller_rating is None or listing.seller_review_count is None:
        return 50.0
    rating_score = min(max((listing.seller_rating - 3.5) / 1.5 * 100.0, 0.0), 100.0)
    confidence = min(listing.seller_review_count / 10.0, 1.0)
    return 50.0 * (1.0 - confidence) + rating_score * confidence


def _comparison_cost(listing: Listing) -> int:
    return listing.item_price_huf + (listing.buyer_fee_huf or 0)


def _same(left: str | None, right: str | None) -> bool:
    return left is not None and right is not None and normalize_text(left) == normalize_text(right)


def _select_comparables(
    listing: Listing,
    candidates: list[Listing],
    minimum: int,
) -> tuple[list[Listing], str, float]:
    others = [
        candidate
        for candidate in candidates
        if not (
            candidate.provider == listing.provider
            and candidate.listing_id == listing.listing_id
        )
        and candidate.currency == "HUF"
        and _comparison_cost(candidate) > 0
    ]
    def same_brand(item: Listing) -> bool:
        return _same(item.brand, listing.brand)

    def same_size(item: Listing) -> bool:
        return _same(item.size, listing.size)

    def same_condition(item: Listing) -> bool:
        return item.condition == listing.condition

    def same_category(item: Listing) -> bool:
        return _same(item.category, listing.category)

    tiers: list[tuple[str, float, Callable[[Listing], bool]]] = []
    if listing.brand:
        tiers.extend(
            [
                (
                    "same brand, size and condition",
                    1.0,
                    lambda item: same_brand(item) and same_size(item) and same_condition(item),
                ),
                (
                    "same brand and condition",
                    0.94,
                    lambda item: same_brand(item) and same_condition(item),
                ),
                (
                    "same brand and size",
                    0.90,
                    lambda item: same_brand(item) and same_size(item),
                ),
                ("same brand", 0.82, same_brand),
            ]
        )
    cross_brand_quality = (0.50, 0.42, 0.30) if listing.brand else (0.72, 0.62, 0.45)
    tiers.extend(
        [
            (
                "same category, size and condition",
                cross_brand_quality[0],
                lambda item: same_category(item)
                and same_size(item)
                and same_condition(item),
            ),
            (
                "same category and condition",
                cross_brand_quality[1],
                lambda item: same_category(item) and same_condition(item),
            ),
            ("same category", cross_brand_quality[2], same_category),
        ]
    )
    largest: tuple[list[Listing], str, float] = ([], "no comparable group", 0.0)
    for basis, quality, predicate in tiers:
        matches = [candidate for candidate in others if predicate(candidate)]
        if len(matches) > len(largest[0]):
            largest = (matches, basis, quality)
        if len(matches) >= minimum:
            return matches, basis, quality
    return largest


def _trim_outliers(values: list[int]) -> list[int]:
    if len(values) < 4:
        return values
    center = float(median(values))
    absolute_deviations = [abs(value - center) for value in values]
    mad = float(median(absolute_deviations))
    if mad == 0:
        return [value for value in values if value == center] or values
    trimmed = [value for value in values if abs(value - center) <= 3.5 * mad]
    return trimmed if len(trimmed) >= 3 else values


def evaluate_market_listing(
    listing: Listing,
    watch: WatchRule,
    comparables: list[Listing],
    defaults: MarketDefaults,
) -> DealEvaluation:
    rejections = hard_filter_rejections(listing, watch)
    if rejections:
        return DealEvaluation(
            hard_filters_match=False,
            qualifies=False,
            effective_cost_huf=None,
            discount_pct=None,
            price_score=None,
            condition_score=None,
            seller_score=None,
            final_score=None,
            used_fallback_buyer_fee=False,
            used_fallback_shipping=False,
            reasons=(),
            rejection_reasons=tuple(rejections),
            shipping_included=False,
        )

    minimum = watch.min_market_comparables or defaults.min_comparables
    maximum = watch.max_market_comparables or defaults.max_comparables
    selected, basis, basis_quality = _select_comparables(listing, comparables, minimum)
    selected = selected[:maximum]
    values = _trim_outliers([_comparison_cost(candidate) for candidate in selected])
    effective_cost = _comparison_cost(listing)
    if len(values) < minimum:
        return DealEvaluation(
            hard_filters_match=True,
            qualifies=False,
            effective_cost_huf=effective_cost,
            discount_pct=None,
            price_score=None,
            condition_score=None,
            seller_score=None,
            final_score=None,
            used_fallback_buyer_fee=False,
            used_fallback_shipping=False,
            reasons=(),
            rejection_reasons=(
                f"only {len(values)} suitable comparables; at least {minimum} required",
            ),
            comparable_count=len(values),
            comparable_basis=basis,
            shipping_included=False,
        )

    market_value = float(median(values))
    discount = 100.0 * (market_value - effective_cost) / market_value
    absolute_deviation = float(median([abs(value - market_value) for value in values]))
    dispersion = absolute_deviation / market_value
    sample_score = min(len(values) / 12.0, 1.0)
    dispersion_score = max(0.0, 1.0 - dispersion / 0.50)
    confidence = basis_quality * (0.50 * sample_score + 0.50 * dispersion_score)
    target_discount = watch.market_target_discount_pct or defaults.target_discount_pct
    price_score = min(max(100.0 * discount / target_discount, 0.0), 100.0)
    final_score = 0.80 * price_score + 0.20 * confidence * 100.0
    minimum_discount = (
        watch.min_market_discount_pct
        if watch.min_market_discount_pct is not None
        else defaults.min_discount_pct
    )
    minimum_confidence = (
        watch.min_market_confidence
        if watch.min_market_confidence is not None
        else defaults.min_confidence
    )

    qualification_rejections: list[str] = []
    if discount < minimum_discount:
        qualification_rejections.append("market discount is below minimum")
    if confidence < minimum_confidence:
        qualification_rejections.append("market confidence is below minimum")

    reasons = (
        f"Price is {discount:.1f}% below the median of {len(values)} active comparables",
        f"Comparable basis: {basis}",
        f"Market confidence is {confidence * 100:.0f}% after robust outlier filtering",
    )
    return DealEvaluation(
        hard_filters_match=True,
        qualifies=not qualification_rejections,
        effective_cost_huf=effective_cost,
        discount_pct=discount,
        price_score=price_score,
        condition_score=None,
        seller_score=None,
        final_score=final_score,
        used_fallback_buyer_fee=False,
        used_fallback_shipping=False,
        reasons=reasons,
        rejection_reasons=tuple(qualification_rejections),
        estimated_market_value_huf=market_value,
        comparable_count=len(values),
        market_confidence=confidence,
        comparable_basis=basis,
        price_dispersion_pct=dispersion * 100.0,
        shipping_included=False,
    )


def evaluate_listing(
    listing: Listing,
    watch: WatchRule,
    defaults: ScoreDefaults,
) -> DealEvaluation:
    rejections = hard_filter_rejections(listing, watch)
    if rejections:
        return DealEvaluation(
            hard_filters_match=False,
            qualifies=False,
            effective_cost_huf=None,
            discount_pct=None,
            price_score=None,
            condition_score=None,
            seller_score=None,
            final_score=None,
            used_fallback_buyer_fee=False,
            used_fallback_shipping=False,
            reasons=(),
            rejection_reasons=tuple(rejections),
        )

    if (
        watch.reference_all_in_value_huf is None
        or watch.fallback_buyer_fee_huf is None
        or watch.fallback_shipping_huf is None
    ):
        raise ValueError(f"watch {watch.id!r} is incomplete and cannot be scored")

    buyer_fee = (
        listing.buyer_fee_huf
        if listing.buyer_fee_huf is not None
        else watch.fallback_buyer_fee_huf
    )
    shipping = (
        listing.shipping_huf
        if listing.shipping_huf is not None
        else watch.fallback_shipping_huf
    )
    effective_cost = listing.item_price_huf + buyer_fee + shipping
    discount = (
        100.0
        * (watch.reference_all_in_value_huf - effective_cost)
        / watch.reference_all_in_value_huf
    )
    target_discount = watch.target_discount_pct or defaults.target_discount_pct
    computed_price_score = min(max(100.0 * discount / target_discount, 0.0), 100.0)
    computed_condition_score = CONDITION_SCORES[listing.condition]
    computed_seller_score = seller_score(listing)
    weights: ScoreWeights = watch.score_weights or defaults.weights
    final_score = (
        weights.price * computed_price_score
        + weights.condition * computed_condition_score
        + weights.seller * computed_seller_score
    )
    min_discount = (
        watch.min_discount_pct
        if watch.min_discount_pct is not None
        else defaults.min_discount_pct
    )
    min_score = watch.min_score if watch.min_score is not None else defaults.min_score

    qualification_rejections: list[str] = []
    if discount < min_discount:
        qualification_rejections.append("discount is below minimum")
    if final_score < min_score:
        qualification_rejections.append("deal score is below minimum")

    seller_reason = "Seller data unavailable; neutral seller score used"
    if listing.seller_rating is not None and listing.seller_review_count is not None:
        seller_reason = (
            f"Seller rated {listing.seller_rating:.1f}/5 across "
            f"{listing.seller_review_count} review(s)"
        )
    reasons = (
        f"Effective cost is {discount:.1f}% below the configured reference value",
        f"Condition is {listing.condition.value.replace('_', ' ')}",
        seller_reason,
    )
    return DealEvaluation(
        hard_filters_match=True,
        qualifies=not qualification_rejections,
        effective_cost_huf=effective_cost,
        discount_pct=discount,
        price_score=computed_price_score,
        condition_score=computed_condition_score,
        seller_score=computed_seller_score,
        final_score=final_score,
        used_fallback_buyer_fee=listing.buyer_fee_huf is None,
        used_fallback_shipping=listing.shipping_huf is None,
        reasons=reasons,
        rejection_reasons=tuple(qualification_rejections),
    )
