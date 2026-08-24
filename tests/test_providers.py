from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from vinted_deal_finder.config import ProviderConfig, WatchRule
from vinted_deal_finder.providers import (
    AuthorizedVintedProvider,
    FixtureListingProvider,
    ProviderAuthorizationError,
    ProviderError,
    ProviderRateLimitError,
    PublicVintedHTMLProvider,
    build_provider,
    parse_vinted_catalog,
)


def catalog_card(
    listing_id: str,
    *,
    title: str = "Levi's női farmer",
    condition: str = "Kiváló",
    price: str = "5 000,00",
    all_in: str = "5 590,00",
) -> str:
    return (
        f'<img src="https://images.example/{listing_id}.webp" '
        f'data-testid="product-item-id-{listing_id}--image--img" '
        f'alt="{title}, Márka: Levi&#x27;s, Állapot: {condition}, '
        f'Méret: M / 38 / 10, {price} Ft, {all_in} Ft">'
        f'<a href="/items/{listing_id}-levis-noi-farmer?referrer=catalog"></a>'
    )


def market_watch() -> WatchRule:
    return WatchRule.model_validate(
        {
            "id": "womens_jeans",
            "enabled": True,
            "valuation_mode": "market",
            "catalog_url": "https://www.vinted.hu/catalog/183-jeans?order=price_low_to_high",
            "categories": ["Women's jeans"],
        }
    )


@pytest.mark.asyncio
async def test_fixture_provider_filters_watch_ids_and_advances_cursor(tmp_path: Path) -> None:
    path = tmp_path / "listings.json"
    path.write_text(
        json.dumps(
            {
                "listings": [
                    {
                        "watch_ids": ["watch_a"],
                        "provider": "fixture",
                        "listing_id": "1",
                        "url": "https://example.invalid/1",
                        "title": "First",
                        "item_price_huf": 1,
                    },
                    {
                        "watch_ids": ["watch_b"],
                        "provider": "fixture",
                        "listing_id": "2",
                        "url": "https://example.invalid/2",
                        "title": "Second",
                        "item_price_huf": 2,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    provider = FixtureListingProvider(path)
    watch = WatchRule(id="watch_a", enabled=False)

    first = await provider.fetch_latest(watch, None)
    second = await provider.fetch_latest(watch, first.next_cursor)

    assert [listing.listing_id for listing in first.listings] == ["1"]
    assert first.next_cursor == "1"
    assert second.listings == []


def test_invalid_fixture_fails_with_actionable_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"wrong": []}', encoding="utf-8")
    with pytest.raises(ProviderError, match="top-level 'listings' array"):
        FixtureListingProvider(path)


@pytest.mark.asyncio
async def test_authorized_provider_is_deliberately_disabled() -> None:
    provider = build_provider(ProviderConfig(kind="authorized_vinted"))
    assert isinstance(provider, AuthorizedVintedProvider)
    assert provider.ready is False
    with pytest.raises(ProviderAuthorizationError, match="approved API documentation"):
        await provider.fetch_latest(WatchRule(id="disabled"), None)


def test_public_catalog_parser_extracts_localized_card_fields() -> None:
    listings = parse_vinted_catalog(
        catalog_card("9724928583", condition="Új, címkékkel"), market_watch()
    )

    assert len(listings) == 1
    listing = listings[0]
    assert listing.listing_id == "9724928583"
    assert listing.brand == "Levi's"
    assert listing.size == "M / 38 / 10"
    assert listing.condition.value == "new_with_tags"
    assert listing.item_price_huf == 5_000
    assert listing.buyer_fee_huf == 590


@pytest.mark.asyncio
async def test_public_provider_seeds_cursor_then_returns_only_new_arrivals() -> None:
    responses = [
        httpx.Response(200, text=catalog_card("2") + catalog_card("1")),
        httpx.Response(200, text=catalog_card("3") + catalog_card("2") + catalog_card("1")),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = PublicVintedHTMLProvider(ProviderConfig(kind="public_vinted_html"), client=client)
    try:
        first = await provider.fetch_latest(market_watch(), None)
        second = await provider.fetch_latest(market_watch(), first.next_cursor)
    finally:
        await client.aclose()

    assert first.listings == []
    assert first.next_cursor == "2"
    assert [listing.listing_id for listing in second.listings] == ["3"]
    assert len(second.comparables) == 3
    assert requests[0].url.params["order"] == "newest_first"


@pytest.mark.asyncio
async def test_public_provider_surfaces_retry_after_without_bypass() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12.5"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = PublicVintedHTMLProvider(ProviderConfig(kind="public_vinted_html"), client=client)
    try:
        with pytest.raises(ProviderRateLimitError) as error:
            await provider.fetch_latest(market_watch(), None)
    finally:
        await client.aclose()

    assert error.value.retry_after_seconds == 12.5


@pytest.mark.asyncio
async def test_public_provider_caches_older_market_pages() -> None:
    responses = [
        httpx.Response(200, text=catalog_card("2") + catalog_card("1")),
        httpx.Response(200, text=catalog_card("0")),
        httpx.Response(200, text=catalog_card("3") + catalog_card("2")),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ProviderConfig(kind="public_vinted_html", market_page_count=2)
    provider = PublicVintedHTMLProvider(config, client=client)
    try:
        first = await provider.fetch_latest(market_watch(), None)
        second = await provider.fetch_latest(market_watch(), first.next_cursor)
    finally:
        await client.aclose()

    assert [listing.listing_id for listing in first.comparables] == ["2", "1", "0"]
    assert [listing.listing_id for listing in second.comparables] == ["3", "2", "0"]
    assert [request.url.params.get("page") for request in requests] == [None, "2", None]
