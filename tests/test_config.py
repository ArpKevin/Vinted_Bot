from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vinted_deal_finder.config import AppConfig, ScoreWeights, WatchRule, load_config


def test_example_configuration_loads_and_resolves_paths() -> None:
    path = Path("config.example.yaml")
    config = load_config(path)

    assert config.poll_interval_seconds == 120
    assert config.watches[0].enabled is True
    assert config.watches[0].valuation_mode == "market"
    assert config.provider.kind == "public_vinted_html"
    assert config.database_path.is_absolute()


def test_enabled_watch_requires_cost_inputs() -> None:
    with pytest.raises(ValidationError, match="fallback_buyer_fee_huf"):
        WatchRule.model_validate(
            {
                "id": "broken",
                "enabled": True,
                "query": "shoes",
                "reference_all_in_value_huf": 10_000,
            }
        )


def test_duplicate_watch_ids_are_rejected(watch_factory: object) -> None:
    assert callable(watch_factory)
    watch = watch_factory()
    with pytest.raises(ValidationError, match="duplicate watch IDs"):
        AppConfig.model_validate({"watches": [watch, watch]})


def test_invalid_weight_total_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must sum to 1.0"):
        ScoreWeights(price=0.7, condition=0.2, seller=0.2)


def test_unsupported_listing_currency_is_rejected(listing_factory: object) -> None:
    assert callable(listing_factory)
    with pytest.raises(ValidationError, match="only HUF"):
        listing_factory(currency="EUR")


def test_invalid_yaml_has_actionable_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("watches: [", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_config(path)
