from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
import respx
from pydantic import SecretStr

from vinted_deal_finder.config import ScoreDefaults, WatchRule
from vinted_deal_finder.discord import (
    DiscordWebhookClient,
    PermanentWebhookError,
    build_discord_payload,
)
from vinted_deal_finder.models import Listing
from vinted_deal_finder.scoring import evaluate_listing


def test_embed_contains_required_fields_and_disables_mentions(
    listing_factory: Callable[..., Listing], watch_factory: Callable[..., WatchRule]
) -> None:
    listing = listing_factory(
        title="Great deal @everyone\x00",
        buyer_fee_huf=None,
        shipping_huf=None,
    )
    watch = watch_factory(query="Great deal")
    evaluation = evaluate_listing(listing, watch, ScoreDefaults())

    payload = build_discord_payload(listing, watch, evaluation)

    assert payload["allowed_mentions"] == {"parse": []}
    embed = payload["embeds"][0]
    assert "\x00" not in embed["title"]
    assert embed["url"] == str(listing.url)
    assert embed["footer"]["text"] == "Watch: test_watch · Listing: listing-1"
    field_names = {field["name"] for field in embed["fields"]}
    assert {"Item price", "Effective all-in cost", "Reference value", "Deal score"} <= field_names
    effective_cost_field = next(
        field for field in embed["fields"] if field["name"] == "Effective all-in cost"
    )
    assert "configured buyer fee, shipping estimate used" in effective_cost_field["value"]


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_honors_retry_after_then_succeeds() -> None:
    url = "https://discord.com/api/webhooks/1/token"
    route = respx.post(url, params={"wait": "true"}).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2.5"}),
            httpx.Response(200, json={"id": "message-1"}),
        ]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = DiscordWebhookClient(
        SecretStr(url), dry_run=False, sleeper=record_sleep
    )
    try:
        receipt = await client.send({"embeds": [{"title": "Deal"}]})
    finally:
        await client.close()

    assert route.call_count == 2
    assert delays == [2.5]
    assert receipt.message_id == "message-1"


@pytest.mark.asyncio
@respx.mock
async def test_server_errors_retry_three_times() -> None:
    url = "https://discord.com/api/webhooks/1/token"
    route = respx.post(url, params={"wait": "true"}).mock(
        return_value=httpx.Response(503)
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = DiscordWebhookClient(SecretStr(url), dry_run=False, sleeper=record_sleep)
    try:
        with pytest.raises(Exception, match="503"):
            await client.send({"embeds": [{"title": "Deal"}]})
    finally:
        await client.close()

    assert route.call_count == 3
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
@respx.mock
async def test_network_failure_retries_and_can_recover() -> None:
    url = "https://discord.com/api/webhooks/1/token"
    route = respx.post(url, params={"wait": "true"}).mock(
        side_effect=[
            httpx.ConnectError("connection failed"),
            httpx.Response(200, json={"id": "recovered"}),
        ]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = DiscordWebhookClient(SecretStr(url), dry_run=False, sleeper=record_sleep)
    try:
        receipt = await client.send({"embeds": [{"title": "Deal"}]})
    finally:
        await client.close()

    assert route.call_count == 2
    assert delays == [1.0]
    assert receipt.message_id == "recovered"


@pytest.mark.asyncio
@respx.mock
async def test_invalid_webhook_disables_future_attempts() -> None:
    url = "https://discord.com/api/webhooks/1/bad-token"
    route = respx.post(url, params={"wait": "true"}).mock(return_value=httpx.Response(404))
    client = DiscordWebhookClient(SecretStr(url), dry_run=False)
    try:
        with pytest.raises(PermanentWebhookError, match="delivery disabled"):
            await client.send({"embeds": [{"title": "Deal"}]})
        with pytest.raises(PermanentWebhookError, match="is disabled"):
            await client.send({"embeds": [{"title": "Deal"}]})
    finally:
        await client.close()

    assert route.call_count == 1
