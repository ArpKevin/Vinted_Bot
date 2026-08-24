from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from pydantic import SecretStr

from .config import WatchRule
from .models import Listing
from .scoring import DealEvaluation

logger = structlog.get_logger(__name__)


class WebhookError(RuntimeError):
    """Base webhook delivery error."""


class PermanentWebhookError(WebhookError):
    """A webhook error that must not be retried until configuration changes."""


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    message_id: str | None
    dry_run: bool = False


def clean_text(value: str, limit: int) -> str:
    without_controls = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
    if len(without_controls) <= limit:
        return without_controls
    return without_controls[: max(limit - 1, 0)] + "…"


def format_huf(value: int | float) -> str:
    return f"{round(value):,}".replace(",", " ") + " Ft"


def build_discord_payload(
    listing: Listing,
    watch: WatchRule,
    evaluation: DealEvaluation,
    username: str = "Vinted Deal Finder",
) -> dict[str, Any]:
    if (
        evaluation.effective_cost_huf is None
        or evaluation.discount_pct is None
        or evaluation.final_score is None
    ):
        raise ValueError("cannot build a Discord alert from an unscored listing")
    is_market_analysis = evaluation.estimated_market_value_huf is not None
    if not is_market_analysis and watch.reference_all_in_value_huf is None:
        raise ValueError("manual scoring requires a reference value")

    fallback_parts: list[str] = []
    if evaluation.used_fallback_buyer_fee:
        fallback_parts.append("buyer fee")
    if evaluation.used_fallback_shipping:
        fallback_parts.append("shipping")
    cost_label = (
        "Cost incl. buyer protection" if is_market_analysis else "Effective all-in cost"
    )
    cost_value = format_huf(evaluation.effective_cost_huf)
    if fallback_parts:
        cost_value += f" (configured {', '.join(fallback_parts)} estimate used)"

    seller = "Not provided"
    if listing.seller_name:
        seller = clean_text(listing.seller_name, 900)
    if listing.seller_rating is not None and listing.seller_review_count is not None:
        seller += f" — {listing.seller_rating:.1f}/5 ({listing.seller_review_count} reviews)"

    attributes = " · ".join(
        clean_text(value, 250)
        for value in [
            listing.brand or "Unknown brand",
            listing.size or "Unknown size",
            listing.condition.value.replace("_", " ").title(),
        ]
    )
    description = clean_text("\n".join(f"• {reason}" for reason in evaluation.reasons), 4096)
    score = evaluation.final_score
    color = 0x2ECC71 if score >= 85 else 0xF1C40F if score >= 70 else 0xE67E22
    timestamp = datetime.now(UTC).isoformat()

    reference_name = "Estimated active-market value" if is_market_analysis else "Reference value"
    reference_value = (
        evaluation.estimated_market_value_huf
        if is_market_analysis
        else watch.reference_all_in_value_huf
    )
    if reference_value is None:
        raise ValueError("scored listing has no reference value")
    fields: list[dict[str, Any]] = [
        {"name": "Item price", "value": format_huf(listing.item_price_huf), "inline": True},
        {"name": cost_label, "value": clean_text(cost_value, 1024), "inline": True},
        {"name": reference_name, "value": format_huf(reference_value), "inline": True},
        {
            "name": "Market discount" if is_market_analysis else "Discount",
            "value": f"{evaluation.discount_pct:.1f}%",
            "inline": True,
        },
        {"name": "Deal score", "value": f"{score:.1f}/100", "inline": True},
    ]
    if is_market_analysis:
        confidence = evaluation.market_confidence or 0.0
        fields.extend(
            [
                {
                    "name": "Market evidence",
                    "value": clean_text(
                        f"{evaluation.comparable_count} comparables · "
                        f"{confidence * 100:.0f}% confidence",
                        1024,
                    ),
                    "inline": True,
                },
                {
                    "name": "Price basis",
                    "value": "Buyer protection included; shipping excluded",
                    "inline": False,
                },
            ]
        )
    fields.append({"name": "Item", "value": clean_text(attributes, 1024), "inline": False})
    if listing.seller_name or (
        listing.seller_rating is not None and listing.seller_review_count is not None
    ):
        fields.append({"name": "Seller", "value": clean_text(seller, 1024), "inline": False})

    embed: dict[str, Any] = {
        "title": clean_text(listing.title, 256),
        "url": str(listing.url),
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {
            "text": clean_text(f"Watch: {watch.id} · Listing: {listing.listing_id}", 2048)
        },
        "timestamp": timestamp,
    }
    if listing.image_url is not None:
        embed["image"] = {"url": str(listing.image_url)}
    return {
        "username": clean_text(username, 80),
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }


class DiscordWebhookClient:
    def __init__(
        self,
        webhook_url: SecretStr | None,
        *,
        dry_run: bool,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._webhook_url = webhook_url
        self.dry_run = dry_run
        self.disabled = False
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self._owns_client = client is None
        self._sleeper = sleeper

    @property
    def ready(self) -> bool:
        return self.dry_run or (self._webhook_url is not None and not self.disabled)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, payload: dict[str, Any]) -> DeliveryReceipt:
        if self.dry_run:
            await logger.ainfo("discord_dry_run", payload=payload)
            return DeliveryReceipt(message_id="dry-run", dry_run=True)
        if self.disabled:
            raise PermanentWebhookError("Discord webhook delivery is disabled")
        if self._webhook_url is None:
            self.disabled = True
            raise PermanentWebhookError("DISCORD_WEBHOOK_URL is required unless DRY_RUN=true")

        url = self._webhook_url.get_secret_value()
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = await self._client.post(url, params={"wait": "true"}, json=payload)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 3:
                    break
                await self._sleeper(float(2 ** (attempt - 1)))
                continue

            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                message_id = body.get("id") if isinstance(body, dict) else None
                return DeliveryReceipt(message_id=str(message_id) if message_id else None)

            if response.status_code == 429:
                retry_after = _retry_after_seconds(response)
                last_error = WebhookError(f"Discord rate limited delivery for {retry_after:g}s")
                if attempt == 3:
                    break
                await self._sleeper(retry_after)
                continue

            if response.status_code in {401, 403, 404}:
                self.disabled = True
                raise PermanentWebhookError(
                    f"Discord webhook returned HTTP {response.status_code}; delivery disabled"
                )

            if response.status_code >= 500:
                last_error = WebhookError(f"Discord webhook returned HTTP {response.status_code}")
                if attempt == 3:
                    break
                await self._sleeper(float(2 ** (attempt - 1)))
                continue

            raise WebhookError(f"Discord webhook rejected payload with HTTP {response.status_code}")

        if last_error is None:
            last_error = WebhookError("Discord webhook delivery failed")
        raise WebhookError(str(last_error)) from last_error


def _retry_after_seconds(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    if header is not None:
        try:
            return max(float(header), 0.0)
        except ValueError:
            pass
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        retry_after = body.get("retry_after")
        if isinstance(retry_after, int | float):
            return max(float(retry_after), 0.0)
    return 1.0
