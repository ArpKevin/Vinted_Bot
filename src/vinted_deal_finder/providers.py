from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from .config import ProviderConfig, WatchRule
from .models import Condition, FetchResult, Listing


class ListingProvider(Protocol):
    name: str
    ready: bool

    async def fetch_latest(self, watch: WatchRule, cursor: str | None) -> FetchResult: ...


class ProviderError(RuntimeError):
    """Base error for listing-provider failures."""


class ProviderAuthorizationError(ProviderError):
    """Raised when approved provider access has not been configured."""


class ProviderRateLimitError(ProviderError):
    """Raised when the public catalog asks the monitor to wait."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(retry_after_seconds, 0.0)
        super().__init__(
            f"Vinted rate limited the catalog request for {self.retry_after_seconds:g}s"
        )


class ProviderAccessDeniedError(ProviderError):
    """Raised when the public site refuses the catalog request."""


class FixtureListingProvider:
    name = "fixture"
    ready = True

    def __init__(self, path: Path) -> None:
        self.path = path
        self._listings = self._load(path)

    @staticmethod
    def _load(path: Path) -> list[tuple[Listing, set[str]]]:
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"cannot load fixture provider data from {path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("listings"), list):
            raise ProviderError("fixture data must contain a top-level 'listings' array")

        adapter = TypeAdapter(Listing)
        parsed: list[tuple[Listing, set[str]]] = []
        for index, item in enumerate(raw["listings"]):
            if not isinstance(item, dict):
                raise ProviderError(f"fixture listing at index {index} must be an object")
            listing_data = dict(item)
            watch_ids_raw = listing_data.pop("watch_ids", [])
            if not isinstance(watch_ids_raw, list) or not all(
                isinstance(watch_id, str) for watch_id in watch_ids_raw
            ):
                raise ProviderError(f"fixture listing at index {index} has invalid watch_ids")
            try:
                parsed.append((adapter.validate_python(listing_data), set(watch_ids_raw)))
            except ValidationError as exc:
                raise ProviderError(f"invalid fixture listing at index {index}: {exc}") from exc
        return parsed

    async def fetch_latest(self, watch: WatchRule, cursor: str | None) -> FetchResult:
        start = 0
        if cursor is not None:
            try:
                start = max(0, int(cursor))
            except ValueError as exc:
                raise ProviderError(f"invalid fixture cursor: {cursor!r}") from exc
        relevant = [
            listing
            for listing, watch_ids in self._listings
            if not watch_ids or watch.id in watch_ids
        ]
        return FetchResult(
            listings=relevant[start:], comparables=relevant, next_cursor=str(len(relevant))
        )


class _CatalogHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.order: list[str] = []
        self.images: dict[str, tuple[str, str]] = {}
        self.links: dict[str, str] = {}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "img":
            test_id = attributes.get("data-testid") or ""
            match = re.fullmatch(r"product-item-id-(\d+)--image--img", test_id)
            if match is None:
                return
            listing_id = match.group(1)
            source = attributes.get("src") or attributes.get("data-src") or ""
            alt = attributes.get("alt") or ""
            if listing_id not in self.images:
                self.order.append(listing_id)
                self.images[listing_id] = (source, alt)
            return
        if tag != "a":
            return
        href = attributes.get("href") or ""
        match = re.match(r"/items/(\d+)(?:-|\?|$)", href)
        if match is not None:
            self.links.setdefault(match.group(1), href)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


_CONDITION_ALIASES: dict[str, Condition] = {
    "új, címkékkel": Condition.NEW_WITH_TAGS,
    "új címkékkel": Condition.NEW_WITH_TAGS,
    "új, címkék nélkül": Condition.NEW_WITHOUT_TAGS,
    "új címkék nélkül": Condition.NEW_WITHOUT_TAGS,
    "kiváló": Condition.VERY_GOOD,
    "nagyon jó": Condition.VERY_GOOD,
    "jó": Condition.GOOD,
    "kielégítő": Condition.SATISFACTORY,
}


def _parse_huf(value: str) -> int:
    compact = re.sub(r"[^0-9,.]", "", value)
    if not compact:
        raise ValueError("price is empty")
    if "," in compact and "." in compact:
        if compact.rfind(",") > compact.rfind("."):
            compact = compact.replace(".", "").replace(",", ".")
        else:
            compact = compact.replace(",", "")
    elif "," in compact:
        head, tail = compact.rsplit(",", 1)
        compact = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else head + tail
    elif "." in compact:
        head, tail = compact.rsplit(".", 1)
        compact = f"{head.replace('.', '')}.{tail}" if len(tail) <= 2 else head + tail
    try:
        return int(Decimal(compact))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid HUF price: {value!r}") from exc


def _parse_card_alt(alt: str) -> tuple[str, str | None, str, Condition, int, int]:
    price_match = re.search(
        r",\s*([0-9\s\u00a0.,]+)\s*Ft,\s*([0-9\s\u00a0.,]+)\s*Ft\s*$",
        alt,
    )
    if price_match is None:
        raise ValueError("catalog card is missing its two HUF prices")
    item_price = _parse_huf(price_match.group(1))
    all_in_price = _parse_huf(price_match.group(2))
    details = alt[: price_match.start()]
    try:
        before_size, size = details.rsplit(", Méret: ", 1)
        before_condition, condition_text = before_size.rsplit(", Állapot: ", 1)
    except ValueError as exc:
        raise ValueError("catalog card is missing its condition or size") from exc
    if ", Márka: " in before_condition:
        title, brand = before_condition.rsplit(", Márka: ", 1)
    else:
        title, brand = before_condition, None
    condition = _CONDITION_ALIASES.get(_normalize(condition_text), Condition.UNKNOWN)
    return (
        title.strip(),
        brand.strip() if brand else None,
        size.strip(),
        condition,
        item_price,
        all_in_price,
    )


def parse_vinted_catalog(html: str, watch: WatchRule) -> list[Listing]:
    parser = _CatalogHTMLParser()
    parser.feed(html)
    category = watch.categories[0] if watch.categories else watch.id
    catalog_url = str(watch.catalog_url) if watch.catalog_url is not None else ""
    listings: list[Listing] = []
    for rank, listing_id in enumerate(parser.order, start=1):
        href = parser.links.get(listing_id)
        image = parser.images.get(listing_id)
        if href is None or image is None:
            continue
        image_url, alt = image
        try:
            title, brand, size, condition, item_price, all_in_price = _parse_card_alt(alt)
            listing = Listing.model_validate(
                {
                    "provider": "public_vinted_html",
                    "listing_id": listing_id,
                    "url": urljoin("https://www.vinted.hu", href),
                    "title": title,
                    "item_price_huf": item_price,
                    "brand": brand,
                    "category": category,
                    "size": size,
                    "condition": condition,
                    "image_url": (
                        image_url
                        if image_url.startswith(("http://", "https://"))
                        else None
                    ),
                    "buyer_fee_huf": max(all_in_price - item_price, 0),
                    "raw_metadata": {
                        "catalog_url": catalog_url,
                        "catalog_rank": rank,
                        "buyer_protection_inclusive_price_huf": all_in_price,
                        "shipping_included": False,
                    },
                }
            )
        except (ValueError, ValidationError):
            continue
        listings.append(listing)
    return listings


class PublicVintedHTMLProvider:
    name = "public_vinted_html"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.ready = True
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout_seconds), follow_redirects=True
        )
        self._owns_client = client is None
        self._headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "hu-HU,hu;q=0.9",
            "User-Agent": config.user_agent,
        }
        self._market_page_count = config.market_page_count
        self._market_cache_seconds = config.market_cache_seconds
        self._market_cache: dict[str, tuple[float, list[Listing]]] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_latest(self, watch: WatchRule, cursor: str | None) -> FetchResult:
        if watch.catalog_url is None:
            raise ProviderError(f"market watch {watch.id!r} has no catalog_url")
        url = _newest_first_url(str(watch.catalog_url))
        newest_catalog = await self._fetch_catalog(url, watch)
        cached = self._market_cache.get(watch.id)
        cache_fresh = (
            cached is not None and monotonic() - cached[0] < self._market_cache_seconds
        )
        if self._market_page_count == 1:
            older_comparables: list[Listing] = []
        elif cache_fresh and cached is not None:
            older_comparables = cached[1]
        else:
            older_comparables = []
            for page in range(2, self._market_page_count + 1):
                older_comparables.extend(
                    await self._fetch_catalog(_url_with_page(url, page), watch)
                )
            self._market_cache[watch.id] = (monotonic(), older_comparables)

        catalog = list(
            {
                (listing.provider, listing.listing_id): listing
                for listing in [*newest_catalog, *older_comparables]
            }.values()
        )
        top_listing_id = newest_catalog[0].listing_id
        if cursor is None:
            new_listings = newest_catalog if watch.notify_existing_on_first_run else []
        else:
            known_index = next(
                (
                    index
                    for index, listing in enumerate(newest_catalog)
                    if listing.listing_id == cursor
                ),
                None,
            )
            # If the previous top item disappeared from the first page, reset safely instead of
            # treating the whole page as new and flooding Discord.
            new_listings = [] if known_index is None else newest_catalog[:known_index]
        return FetchResult(
            listings=new_listings,
            comparables=catalog,
            next_cursor=top_listing_id,
        )

    async def _fetch_catalog(self, url: str, watch: WatchRule) -> list[Listing]:
        response = await self._client.get(url, headers=self._headers)
        if response.status_code == 429:
            raise ProviderRateLimitError(_retry_after(response))
        if response.status_code in {401, 403}:
            raise ProviderAccessDeniedError(
                f"public Vinted catalog returned HTTP {response.status_code}; "
                "the monitor will not attempt to bypass access controls"
            )
        if response.status_code >= 400:
            raise ProviderError(f"public Vinted catalog returned HTTP {response.status_code}")

        catalog = parse_vinted_catalog(response.text, watch)
        if not catalog:
            raise ProviderError(
                "public Vinted catalog contained no parseable listing cards; the page layout "
                "may have changed or access may be restricted"
            )
        return catalog


class AuthorizedVintedProvider:
    name = "authorized_vinted"
    ready = False

    async def fetch_latest(self, watch: WatchRule, cursor: str | None) -> FetchResult:
        del watch, cursor
        raise ProviderAuthorizationError(
            "live Vinted access is disabled: supply approved API documentation or another "
            "authorized listing feed before implementing this provider"
        )


def build_provider(config: ProviderConfig) -> ListingProvider:
    if config.kind == "fixture":
        return FixtureListingProvider(config.fixture_path)
    if config.kind == "public_vinted_html":
        return PublicVintedHTMLProvider(config)
    return AuthorizedVintedProvider()


def _newest_first_url(url: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["order"] = "newest_first"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _url_with_page(url: str, page: int) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _retry_after(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    if header is not None:
        try:
            return max(float(header), 0.0)
        except ValueError:
            pass
    return 60.0
