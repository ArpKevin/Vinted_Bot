from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from .config import AppConfig, WatchRule
from .database import Database
from .discord import (
    DiscordWebhookClient,
    PermanentWebhookError,
    WebhookError,
    build_discord_payload,
)
from .providers import (
    ListingProvider,
    ProviderAccessDeniedError,
    ProviderAuthorizationError,
    ProviderRateLimitError,
)
from .scoring import evaluate_listing, evaluate_market_listing

logger = structlog.get_logger(__name__)


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class RuntimeState:
    configuration_ready: bool = True
    database_ready: bool = False
    provider_ready: bool = False
    webhook_ready: bool = False
    last_successful_poll: datetime | None = None
    last_successful_discord_delivery: datetime | None = None
    last_error: str | None = None


class DealFinderService:
    def __init__(
        self,
        config: AppConfig,
        provider: ListingProvider,
        database: Database,
        discord: DiscordWebhookClient,
    ) -> None:
        self.config = config
        self.provider = provider
        self.database = database
        self.discord = discord
        self.state = RuntimeState(
            database_ready=database.ready,
            provider_ready=provider.ready,
            webhook_ready=discord.ready,
        )
        self._stop_event = asyncio.Event()
        self._provider_semaphore = asyncio.Semaphore(config.max_provider_concurrency)
        self._next_allowed_poll: dict[str, datetime] = {}

    async def run_forever(self) -> None:
        await logger.ainfo(
            "service_started",
            poll_interval_seconds=self.config.poll_interval_seconds,
            provider=self.provider.name,
            dry_run=self.discord.dry_run,
        )
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                await self._record_error(f"unexpected polling cycle failure: {exc}")
                await logger.aexception("polling_cycle_failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.config.poll_interval_seconds
                )
            except TimeoutError:
                pass
        await logger.ainfo("service_stopped")

    async def stop(self) -> None:
        self._stop_event.set()

    async def poll_once(self) -> None:
        removed = await self.database.prune_seen(self.config.seen_retention_days)
        if removed:
            await logger.ainfo("seen_listings_pruned", count=removed)

        enabled_watches = [watch for watch in self.config.watches if watch.enabled]
        tasks = [self._poll_watch(watch) for watch in enabled_watches if self._watch_is_due(watch)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                await self._record_error(f"watch polling task failed: {result}")
                await logger.aerror("watch_task_failed", error=str(result))
        await self.flush_pending_alerts()

    def _watch_is_due(self, watch: WatchRule) -> bool:
        due_at = self._next_allowed_poll.get(watch.id)
        return due_at is None or now_utc() >= due_at

    async def _poll_watch(self, watch: WatchRule) -> None:
        async with self._provider_semaphore:
            cursor = await self.database.get_cursor(watch.id)
            result = None
            for attempt in range(1, 4):
                try:
                    result = await self.provider.fetch_latest(watch, cursor)
                    break
                except ProviderAuthorizationError as exc:
                    self.state.provider_ready = False
                    await self._record_error(str(exc))
                    await logger.awarning(
                        "provider_authorization_required", watch_id=watch.id, error=str(exc)
                    )
                    return
                except ProviderRateLimitError as exc:
                    self._next_allowed_poll[watch.id] = now_utc() + timedelta(
                        seconds=exc.retry_after_seconds
                    )
                    await self._record_error(str(exc))
                    await logger.awarning(
                        "provider_rate_limited",
                        watch_id=watch.id,
                        retry_after_seconds=exc.retry_after_seconds,
                    )
                    return
                except ProviderAccessDeniedError as exc:
                    self.state.provider_ready = False
                    await self._record_error(str(exc))
                    await logger.aerror(
                        "provider_access_denied", watch_id=watch.id, error=str(exc)
                    )
                    return
                except Exception as exc:
                    if attempt == 3:
                        self.state.provider_ready = False
                        await self._record_error(
                            f"provider failed for watch {watch.id} after 3 attempts: {exc}"
                        )
                        await logger.aerror(
                            "provider_fetch_failed",
                            watch_id=watch.id,
                            attempt=attempt,
                            error=str(exc),
                        )
                        return
                    await logger.awarning(
                        "provider_fetch_retry",
                        watch_id=watch.id,
                        attempt=attempt,
                        error=str(exc),
                    )
                    await asyncio.sleep(float(2 ** (attempt - 1)))

            if result is None:
                return

            try:
                for listing in result.listings:
                    if watch.valuation_mode == "market":
                        evaluation = evaluate_market_listing(
                            listing,
                            watch,
                            result.comparables,
                            self.config.market_defaults,
                        )
                    else:
                        evaluation = evaluate_listing(
                            listing, watch, self.config.score_defaults
                        )
                    await self.database.record_seen(listing, watch.id, evaluation)
                    if evaluation.qualifies and not await self.database.was_alerted(
                        listing.provider, listing.listing_id
                    ):
                        payload = build_discord_payload(
                            listing, watch, evaluation, self.config.discord.username
                        )
                        await self.database.queue_alert(
                            listing.provider, listing.listing_id, watch.id, payload
                        )
                await self.database.set_cursor(watch.id, result.next_cursor)
            except Exception as exc:
                await self._record_error(f"processing failed for watch {watch.id}: {exc}")
                await logger.aexception("listing_processing_failed", watch_id=watch.id)
                return

            if result.retry_after_seconds is not None:
                self._next_allowed_poll[watch.id] = now_utc() + timedelta(
                    seconds=result.retry_after_seconds
                )
            self.state.provider_ready = self.provider.ready
            self.state.last_successful_poll = now_utc()
            await self.database.set_service_state(
                "last_successful_poll", self.state.last_successful_poll.isoformat()
            )
            await logger.ainfo(
                "watch_polled",
                watch_id=watch.id,
                listing_count=len(result.listings),
                next_cursor=result.next_cursor,
            )

    async def flush_pending_alerts(self) -> None:
        if self.discord.disabled:
            self.state.webhook_ready = False
            return
        for alert in await self.database.pending_alerts():
            try:
                receipt = await self.discord.send(alert.payload)
            except PermanentWebhookError as exc:
                await self.database.mark_alert_failed(alert, str(exc))
                self.state.webhook_ready = False
                await self._record_error(str(exc))
                await logger.aerror("discord_delivery_disabled", error=str(exc))
                break
            except WebhookError as exc:
                await self.database.mark_alert_failed(alert, str(exc))
                self.state.webhook_ready = False
                await self._record_error(str(exc))
                await logger.awarning(
                    "discord_delivery_failed",
                    provider=alert.provider,
                    listing_id=alert.listing_id,
                    error=str(exc),
                )
                continue

            await self.database.mark_alert_sent(alert, receipt.message_id)
            self.state.webhook_ready = self.discord.ready
            self.state.last_successful_discord_delivery = now_utc()
            await self.database.set_service_state(
                "last_successful_discord_delivery",
                self.state.last_successful_discord_delivery.isoformat(),
            )
            await logger.ainfo(
                "discord_alert_sent",
                provider=alert.provider,
                listing_id=alert.listing_id,
                dry_run=receipt.dry_run,
            )

    async def _record_error(self, error: str) -> None:
        sanitized = error[:1000]
        self.state.last_error = sanitized
        if self.database.ready:
            await self.database.set_service_state("last_error", sanitized)

    async def health(self) -> dict[str, object]:
        pending = await self.database.pending_alert_count() if self.database.ready else 0
        healthy = all(
            [
                self.state.configuration_ready,
                self.state.database_ready,
                self.state.provider_ready,
                self.state.webhook_ready,
            ]
        )
        return {
            "status": "healthy" if healthy else "degraded",
            "configuration_ready": self.state.configuration_ready,
            "database_ready": self.state.database_ready,
            "provider_ready": self.state.provider_ready,
            "webhook_ready": self.state.webhook_ready,
            "last_successful_poll": _iso_or_none(self.state.last_successful_poll),
            "last_successful_discord_delivery": _iso_or_none(
                self.state.last_successful_discord_delivery
            ),
            "pending_alert_count": pending,
            "last_error": self.state.last_error,
        }

    async def readiness(self) -> dict[str, object]:
        ready = all(
            [
                self.state.configuration_ready,
                self.state.database_ready,
                self.state.provider_ready,
            ]
        )
        return {
            "status": "ready" if ready else "not_ready",
            "configuration_ready": self.state.configuration_ready,
            "database_ready": self.state.database_ready,
            "provider_ready": self.state.provider_ready,
        }


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
