from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vinted_deal_finder.config import AppConfig, WatchRule
from vinted_deal_finder.database import Database
from vinted_deal_finder.discord import DeliveryReceipt, DiscordWebhookClient, WebhookError
from vinted_deal_finder.models import FetchResult, Listing
from vinted_deal_finder.providers import ProviderAccessDeniedError
from vinted_deal_finder.service import DealFinderService


class StaticProvider:
    name = "static"
    ready = True

    def __init__(
        self,
        listings: list[Listing],
        *,
        comparables: list[Listing] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.listings = listings
        self.comparables = comparables or []
        self.error = error
        self.calls = 0

    async def fetch_latest(self, watch: WatchRule, cursor: str | None) -> FetchResult:
        del watch, cursor
        self.calls += 1
        if self.error is not None:
            raise self.error
        return FetchResult(
            listings=self.listings, comparables=self.comparables, next_cursor=None
        )


class RecordingDiscord(DiscordWebhookClient):
    def __init__(self, *, error: WebhookError | None = None) -> None:
        super().__init__(None, dry_run=True)
        self.payloads: list[dict[str, Any]] = []
        self.error = error

    async def send(self, payload: dict[str, Any]) -> DeliveryReceipt:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return DeliveryReceipt(message_id=f"message-{len(self.payloads)}", dry_run=True)


async def make_service(
    tmp_path: Path,
    config: AppConfig,
    provider: StaticProvider,
    discord: RecordingDiscord,
) -> tuple[DealFinderService, Database]:
    config.database_path = tmp_path / "deals.db"
    database = Database(config.database_path)
    await database.initialize()
    return DealFinderService(config, provider, database, discord), database


@pytest.mark.asyncio
async def test_qualifying_listing_alerts_once_and_survives_restart(
    tmp_path: Path,
    listing_factory: Callable[..., Listing],
    app_config_factory: Callable[..., AppConfig],
) -> None:
    listing = listing_factory()
    config = app_config_factory()
    first_discord = RecordingDiscord()
    service, database = await make_service(
        tmp_path, config, StaticProvider([listing]), first_discord
    )

    await service.poll_once()
    await service.poll_once()
    assert len(first_discord.payloads) == 1
    assert await database.was_alerted(listing.provider, listing.listing_id)
    assert await database.pending_alert_count() == 0
    await database.close()
    await first_discord.close()

    restarted_database = Database(tmp_path / "deals.db")
    await restarted_database.initialize()
    restarted_discord = RecordingDiscord()
    restarted_service = DealFinderService(
        config, StaticProvider([listing]), restarted_database, restarted_discord
    )
    await restarted_service.poll_once()
    assert restarted_discord.payloads == []
    await restarted_database.close()
    await restarted_discord.close()


@pytest.mark.asyncio
async def test_nonqualifying_listing_is_not_alerted(
    tmp_path: Path,
    listing_factory: Callable[..., Listing],
    app_config_factory: Callable[..., AppConfig],
) -> None:
    discord = RecordingDiscord()
    service, database = await make_service(
        tmp_path,
        app_config_factory(),
        StaticProvider([listing_factory(item_price_huf=29_000)]),
        discord,
    )
    await service.poll_once()
    assert discord.payloads == []
    assert await database.pending_alert_count() == 0
    await database.close()
    await discord.close()


@pytest.mark.asyncio
async def test_market_valued_arrival_sends_market_evidence_embed(
    tmp_path: Path,
    listing_factory: Callable[..., Listing],
    watch_factory: Callable[..., WatchRule],
    app_config_factory: Callable[..., AppConfig],
) -> None:
    target = listing_factory(item_price_huf=5_000, buyer_fee_huf=500)
    comparables = [
        listing_factory(
            listing_id=f"market-{index}",
            item_price_huf=price,
            buyer_fee_huf=500,
        )
        for index, price in enumerate([9_500, 10_500, 11_500, 12_000, 12_500, 99_500])
    ]
    watch = watch_factory(
        valuation_mode="market",
        catalog_url="https://www.vinted.hu/catalog/183-jeans",
        min_market_comparables=5,
    )
    config = app_config_factory(watches=[watch])
    discord = RecordingDiscord()
    service, database = await make_service(
        tmp_path,
        config,
        StaticProvider([target], comparables=comparables),
        discord,
    )

    await service.poll_once()

    assert len(discord.payloads) == 1
    fields = {field["name"] for field in discord.payloads[0]["embeds"][0]["fields"]}
    assert {"Estimated active-market value", "Market evidence", "Price basis"} <= fields
    await database.close()
    await discord.close()


@pytest.mark.asyncio
async def test_failed_delivery_remains_pending(
    tmp_path: Path,
    listing_factory: Callable[..., Listing],
    app_config_factory: Callable[..., AppConfig],
) -> None:
    discord = RecordingDiscord(error=WebhookError("temporary Discord failure"))
    service, database = await make_service(
        tmp_path, app_config_factory(), StaticProvider([listing_factory()]), discord
    )
    await service.poll_once()
    assert await database.pending_alert_count() == 1
    health = await service.health()
    assert health["status"] == "degraded"
    assert health["webhook_ready"] is False
    assert health["last_error"] == "temporary Discord failure"
    await database.close()
    await discord.close()


@pytest.mark.asyncio
async def test_provider_failure_does_not_advance_cursor(
    tmp_path: Path,
    app_config_factory: Callable[..., AppConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("vinted_deal_finder.service.asyncio.sleep", no_sleep)
    discord = RecordingDiscord()
    provider = StaticProvider([], error=RuntimeError("provider unavailable"))
    service, database = await make_service(tmp_path, app_config_factory(), provider, discord)
    await database.set_cursor("test_watch", "old-cursor")
    await service.poll_once()
    assert provider.calls == 3
    assert await database.get_cursor("test_watch") == "old-cursor"
    assert (await service.readiness())["status"] == "not_ready"
    await database.close()
    await discord.close()


@pytest.mark.asyncio
async def test_provider_access_denial_is_not_retried(
    tmp_path: Path,
    app_config_factory: Callable[..., AppConfig],
) -> None:
    discord = RecordingDiscord()
    provider = StaticProvider([], error=ProviderAccessDeniedError("HTTP 403"))
    service, database = await make_service(tmp_path, app_config_factory(), provider, discord)

    await service.poll_once()

    assert provider.calls == 1
    assert (await service.readiness())["status"] == "not_ready"
    await database.close()
    await discord.close()


@pytest.mark.asyncio
async def test_health_and_readiness_reflect_runtime_state(
    tmp_path: Path,
    app_config_factory: Callable[..., AppConfig],
) -> None:
    discord = RecordingDiscord()
    service, database = await make_service(
        tmp_path, app_config_factory(), StaticProvider([]), discord
    )
    assert (await service.readiness())["status"] == "ready"
    assert (await service.health())["status"] == "healthy"
    service.state.provider_ready = False
    assert (await service.readiness())["status"] == "not_ready"
    assert (await service.health())["status"] == "degraded"
    await database.close()
    await discord.close()
