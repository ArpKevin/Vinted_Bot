from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException

from .config import EnvironmentSettings, load_config
from .database import Database
from .discord import DiscordWebhookClient
from .logging_config import configure_logging
from .providers import build_provider
from .service import DealFinderService


def create_app(
    *,
    config_path: Path | None = None,
    settings: EnvironmentSettings | None = None,
) -> FastAPI:
    environment = settings or EnvironmentSettings()
    selected_config_path = config_path or environment.config_path

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        config = load_config(selected_config_path)
        database = Database(config.database_path)
        await database.initialize()
        provider = build_provider(config.provider)
        discord = DiscordWebhookClient(
            environment.discord_webhook_url,
            dry_run=environment.dry_run,
        )
        service = DealFinderService(config, provider, database, discord)
        app.state.deal_finder_service = service
        service_task = asyncio.create_task(service.run_forever(), name="deal-finder-poller")
        try:
            yield
        finally:
            await service.stop()
            try:
                await asyncio.wait_for(service_task, timeout=5)
            except TimeoutError:
                service_task.cancel()
                await asyncio.gather(service_task, return_exceptions=True)
            provider_close = getattr(provider, "close", None)
            if provider_close is not None:
                await provider_close()
            await discord.close()
            await database.close()

    application = FastAPI(
        title="Vinted Deal Finder",
        version="0.2.0",
        lifespan=lifespan,
    )

    @application.get("/healthz")
    async def health() -> dict[str, object]:
        service: DealFinderService | None = getattr(
            application.state, "deal_finder_service", None
        )
        if service is None:
            raise HTTPException(status_code=503, detail="service has not initialized")
        result = await service.health()
        if result["status"] != "healthy":
            raise HTTPException(status_code=503, detail=result)
        return result

    @application.get("/readyz")
    async def readiness() -> dict[str, object]:
        service: DealFinderService | None = getattr(
            application.state, "deal_finder_service", None
        )
        if service is None:
            raise HTTPException(status_code=503, detail="service has not initialized")
        result = await service.readiness()
        if result["status"] != "ready":
            raise HTTPException(status_code=503, detail=result)
        return result

    return application


app = create_app()


def run() -> None:
    uvicorn.run(
        "vinted_deal_finder.main:app",
        host="0.0.0.0",
        port=8080,
        workers=1,
    )


if __name__ == "__main__":
    run()
