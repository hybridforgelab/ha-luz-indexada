"""Shared fixtures. Fixtures under tests/fixtures/ are real API responses (no keys)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.luz_indexada.const import (
    CONF_BASE_URL,
    CONF_CYCLE,
    CONF_INCLUDE_VAT,
    CONF_MODE,
    CONF_TARIFF,
    CONF_TARIFF_NAME,
    CONF_WINDOW_HOURS,
    DOMAIN,
    MODE_DIRECT,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://luz.example.test"
TARIFF = "galp-plano-flexivel"
TODAY = "2026-10-25"
TOMORROW = "2026-10-26"


def load(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text())


def prices_url(day: str, base: str = BASE) -> str:
    """URL matcher for /v1/prices of one day."""
    return f"{base}/v1/prices?date={day}"


def complete(day_body: dict[str, Any]) -> dict[str, Any]:
    """Same day with every pending slot priced (what the API publishes the next day)."""
    body = json.loads(json.dumps(day_body))
    body["complete"], body["pending_from_utc"], body["version"] = True, None, 2
    for row in body["tariffs"][0]["prices"]:
        if row["total"] is None:
            row["total"], row["total_vat23"] = 0.2, 0.246
    return body


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def entry_data() -> dict[str, Any]:
    """Direct-mode entry data."""
    return {
        CONF_MODE: MODE_DIRECT,
        CONF_BASE_URL: BASE,
        "api_key": "test-key",
        CONF_TARIFF: TARIFF,
        CONF_TARIFF_NAME: "Galp Plano Flexível",
        CONF_CYCLE: "simples",
    }


@pytest.fixture
def mock_entry(entry_data: dict[str, Any]) -> MockConfigEntry:
    """A configured tariff."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Galp Plano Flexível (simples)",
        data=entry_data,
        options={CONF_INCLUDE_VAT: True, CONF_WINDOW_HOURS: 2.0},
        unique_id=f"{TARIFF}_simples",
    )
