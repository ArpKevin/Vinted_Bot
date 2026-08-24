from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from vinted_deal_finder.config import EnvironmentSettings
from vinted_deal_finder.main import create_app


def write_runtime_files(tmp_path: Path, *, provider_kind: str = "fixture") -> Path:
    fixture_path = tmp_path / "listings.json"
    fixture_path.write_text(json.dumps({"listings": []}), encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "poll_interval_seconds: 60",
                "database_path: deals.db",
                "provider:",
                f"  kind: {provider_kind}",
                "  fixture_path: listings.json",
                "watches: []",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints_are_live(tmp_path: Path) -> None:
    config_path = write_runtime_files(tmp_path)
    settings = EnvironmentSettings(dry_run=True, config_path=config_path)
    app = create_app(config_path=config_path, settings=settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            readiness = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_disabled_live_provider_degrades_endpoints(tmp_path: Path) -> None:
    config_path = write_runtime_files(tmp_path, provider_kind="authorized_vinted")
    settings = EnvironmentSettings(dry_run=True, config_path=config_path)
    app = create_app(config_path=config_path, settings=settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            readiness = await client.get("/readyz")

    assert health.status_code == 503
    assert health.json()["detail"]["provider_ready"] is False
    assert readiness.status_code == 503
    assert readiness.json()["detail"]["status"] == "not_ready"
