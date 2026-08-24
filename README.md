# Vinted Deal Finder

A Python 3.12 service for Hungary that checks public Vinted women’s jeans and trousers catalogs,
detects newly arrived listings, estimates their value from comparable active listings, and posts
qualified deals to Discord. It uses newest-first ordering, SQLite deduplication, retries, health
checks, Docker, and English rich embeds.

## Important Vinted terms and operating boundary

Vinted’s Hungarian terms prohibit external bots, scraping, crawling, and similar data collection
unless Vinted has permitted it. The `public_vinted_html` provider therefore carries account/IP and
breakage risk even though it only reads unauthenticated public catalog HTML. Use it only if you
accept that risk and have any permission required for your use.

The implementation deliberately does not sign in, use account cookies, automate a browser, call
undocumented endpoints, solve CAPTCHAs, rotate proxies or identities, evade blocking, make offers,
buy items, favorite listings, or message sellers. It stops and reports an error on access-control
responses instead of attempting a bypass.

## Ready-to-use configuration

`config.example.yaml` is configured for local use and `config.docker.yaml` for Docker. Both enable:

- women’s jeans: `https://www.vinted.hu/catalog/183-jeans?order=newest_first`;
- women’s trousers and leggings, with leggings excluded by keywords;
- new with tags, new without tags, very good, and good condition;
- a poll every 120 seconds;
- a 25% minimum estimated market discount;
- a 55% minimum statistical confidence;
- five or more suitable comparables.

On the first poll, the service stores the current newest listing as its cursor and sends nothing.
Later polls alert only on items above that cursor. This prevents an initial Discord flood. If the
old cursor disappears from the first catalog page, the monitor safely resets instead of treating
the whole page as new.

## Market-value calculation

The service does not use a manually entered price ceiling or reference price.

For each new listing it:

1. Reads the item price and the buyer-protection-inclusive price shown on the public catalog card.
2. Selects increasingly broad comparable groups: same brand, size, and condition first; then same
   brand and condition; same brand and size; and same brand. Unbranded items may use matched
   category groups. Cross-brand estimates for branded items receive low confidence and cannot meet
   the default alert threshold by themselves.
3. Requires at least five comparables, keeps at most 30, removes extreme prices with a robust
   median-absolute-deviation filter, and uses the remaining median as estimated market value.
4. Calculates:

   ```text
   market_discount_pct =
       100 × (estimated_market_value − listing_cost) / estimated_market_value

   price_score = clamp(100 × market_discount_pct / 40, 0, 100)

   deal_score = 0.80 × price_score + 0.20 × market_confidence × 100
   ```

5. Alerts only when the discount is at least 25% and confidence is at least 55%.

The first newest-first page is fetched every two minutes. Pages two and three provide a broader
comparison pool and are cached for 30 minutes, so the normal poll does not repeatedly fetch all
three pages.

This is an active-listing estimate, not a sold-price valuation. Asking prices may be optimistic,
the public page has no complete sales history, and shipping is not included. Discord embeds state
that buyer protection is included and shipping is excluded.

## Start with Docker

The supplied `.env` is already pointed at `config.example.yaml`; Docker mounts
`config.docker.yaml`. Ensure the webhook in `.env` is current and set:

```dotenv
DRY_RUN=false
```

Then start the service:

```powershell
docker compose up --build -d
docker compose logs -f deal-finder
```

Check it locally:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/readyz
Invoke-RestMethod http://127.0.0.1:8080/healthz
```

Stop it without deleting the persistent database volume:

```powershell
docker compose down
```

Do not use `docker compose down -v` unless you intend to delete the deduplication database.

## Start without Docker

```powershell
.\.venv\Scripts\uvicorn.exe vinted_deal_finder.main:app --host 127.0.0.1 --port 8080
```

The `.env` file supplies `CONFIG_PATH`, `DRY_RUN`, and `DISCORD_WEBHOOK_URL` automatically.

## Adjust alert frequency and selectivity

Edit `market_defaults` in the selected YAML file:

```yaml
market_defaults:
  target_discount_pct: 40
  min_discount_pct: 25
  min_confidence: 0.55
  min_comparables: 5
  max_comparables: 30
```

- Lower `min_discount_pct` for more alerts; raise it for fewer, stronger discounts.
- Lowering `min_confidence` increases false-positive risk, especially for uncommon brands.
- Do not reduce `min_comparables` below five unless you accept noisier estimates.
- Change `poll_interval_seconds` carefully; more frequent requests increase the likelihood of rate
  limits or access restrictions.
- Set `notify_existing_on_first_run: true` only if you intentionally want to evaluate the current
  catalog after starting with a fresh SQLite database.

To limit sizes or brands, add exact normalized filters to a watch:

```yaml
brands: [Levi's, Zara, Mango]
sizes: ["S / 36 / 8", "M / 38 / 10", "L / 40 / 12"]
```

## Discord behavior

Every qualifying listing gets one embed containing the listing link and image, item price,
buyer-protection-inclusive cost, estimated active-market value, discount, deal score, comparable
count, confidence, matching basis, item attributes, watch ID, and listing ID. Mentions are disabled
and untrusted strings are sanitized and truncated.

The webhook follows Discord’s `wait=true` delivery behavior. Network failures, `5xx`, and `429`
responses are retried up to three times; `Retry-After` is honored. A `401`, `403`, or `404` disables
delivery until the service restarts or configuration is corrected.

The webhook URL is a secret. Anyone who has it can post to the channel. Rotate a URL that has been
shared in chat, source control, logs, or screenshots, and replace only the value in `.env`.

## Persistence and health

SQLite stores seen listings, provider cursors, alert attempts, delivery state, and service state.
A successfully delivered listing is not delivered again after restart. Failed webhook deliveries
stay retryable. Delivery is at-least-once, so a lost response after Discord accepted a message can
rarely produce a duplicate.

Docker stores the database in the `deal-finder-data` volume. Back it up while the container is
stopped or with SQLite’s online backup facilities; do not copy only the main file during active WAL
writes.

`GET /readyz` checks configuration, database, and provider initialization. `GET /healthz` also
reports webhook health, last successful poll, last successful Discord delivery, pending alerts,
and a sanitized recent error.

## Development and validation

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe
docker build -t vinted-deal-finder:local .
```

Automated tests use synthetic HTML, fixture listings, mocked HTTP, and dry-run Discord delivery.
They never contact Vinted or a real Discord webhook.

## Troubleshooting

- **No notification after startup:** expected; the first poll seeds the newest-item cursor.
- **No later alerts:** new arrivals may not be at least 25% below a sufficiently reliable market
  estimate. Review logs and thresholds.
- **No estimate for a rare brand:** this is intentional when too few same-brand comparables exist.
- **Catalog contains no parseable cards:** Vinted may have changed the HTML or restricted access.
- **Provider rate limited:** the requested wait is honored and the cursor is not advanced.
- **Webhook unhealthy:** rotate/correct the webhook, replace it in `.env`, and restart.
- **Reset monitoring intentionally:** stop the service, back up the database, then remove only the
  selected SQLite database before restarting. The current catalog will be seeded again by default.
