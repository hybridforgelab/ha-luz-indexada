"""Config, reauth and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.luz_indexada.const import (
    CONF_BASE_URL,
    CONF_CYCLE,
    CONF_INCLUDE_VAT,
    CONF_MODE,
    CONF_RAPIDAPI_HOST,
    CONF_TARIFF,
    CONF_WINDOW_HOURS,
    DEFAULT_RAPIDAPI_HOST,
    DOMAIN,
    MODE_DIRECT,
    MODE_RAPIDAPI,
)

from .conftest import BASE, TARIFF, load

RAPID = f"https://{DEFAULT_RAPIDAPI_HOST}"


@pytest.fixture(autouse=True)
def no_setup():
    """Don't set the entry up after the flow."""
    with patch("custom_components.luz_indexada.async_setup_entry", return_value=True) as mock:
        yield mock


async def _start(hass: HomeAssistant, step: str, advanced: bool = False) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER, "show_advanced_options": advanced}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [MODE_RAPIDAPI, MODE_DIRECT]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": step})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == step
    return result


async def test_direct_flow(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Direct mode: URL + bearer, then tariff."""
    aioclient_mock.get(f"{BASE}/v1/tariffs", json=load("tariffs.json"))
    result = await _start(hass, MODE_DIRECT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: BASE + "/", "api_key": " secret "}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "tariff"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TARIFF: TARIFF, CONF_CYCLE: "bi-diario", CONF_INCLUDE_VAT: False, CONF_WINDOW_HOURS: 3},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Galp Plano Flexível (bi-diario)"
    assert result["data"] == {
        CONF_MODE: MODE_DIRECT,
        CONF_BASE_URL: BASE,
        "api_key": "secret",
        CONF_TARIFF: TARIFF,
        "tariff_name": "Galp Plano Flexível",
        CONF_CYCLE: "bi-diario",
    }
    assert result["options"] == {CONF_INCLUDE_VAT: False, CONF_WINDOW_HOURS: 3.0}
    assert result["result"].unique_id == f"{TARIFF}_bi-diario"


async def test_rapidapi_flow_advanced_host(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """RapidAPI mode with a custom host under advanced options."""
    aioclient_mock.get("https://other.p.rapidapi.com/v1/tariffs", json=load("tariffs.json"))
    result = await _start(hass, MODE_RAPIDAPI, advanced=True)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": "k", CONF_RAPIDAPI_HOST: "other.p.rapidapi.com"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TARIFF: "coopernico-base-2", CONF_CYCLE: "simples", CONF_INCLUDE_VAT: True, CONF_WINDOW_HOURS: 2},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_RAPIDAPI_HOST] == "other.p.rapidapi.com"
    assert aioclient_mock.mock_calls[0][3]["X-RapidAPI-Key"] == "k"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"status": 401}, "invalid_auth"),
        ({"status": 403}, "invalid_auth"),
        ({"status": 429}, "rate_limited"),
        ({"exc": aiohttp.ClientError()}, "cannot_connect"),
        ({"status": 500}, "unknown"),
    ],
)
async def test_rapidapi_errors_then_recover(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kwargs: dict[str, Any], error: str
) -> None:
    """Errors are shown on the form; a retry works."""
    aioclient_mock.get(f"{RAPID}/v1/tariffs", **kwargs)
    result = await _start(hass, MODE_RAPIDAPI)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "k"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": error}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{RAPID}/v1/tariffs", json=load("tariffs.json"))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "k"})
    assert result["step_id"] == "tariff"


async def test_direct_invalid_url(hass: HomeAssistant) -> None:
    """The base URL needs a scheme."""
    result = await _start(hass, MODE_DIRECT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_URL: "api.example", "api_key": "k"}
    )
    assert result["errors"] == {CONF_BASE_URL: "invalid_url"}


async def test_cycle_not_offered_and_duplicate(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A cycle missing from the tariff is rejected; the same tariff+cycle twice aborts."""
    tariffs = load("tariffs.json")
    tariffs["tariffs"][3]["cycles"] = ["simples"]
    aioclient_mock.get(f"{BASE}/v1/tariffs", json=tariffs)
    MockConfigEntry(domain=DOMAIN, unique_id=f"{TARIFF}_simples").add_to_hass(hass)
    result = await _start(hass, MODE_DIRECT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_BASE_URL: BASE, "api_key": "k"})
    choice = {CONF_TARIFF: TARIFF, CONF_INCLUDE_VAT: True, CONF_WINDOW_HOURS: 2}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**choice, CONF_CYCLE: "tri-diario"})
    assert result["errors"] == {CONF_CYCLE: "cycle_not_offered"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**choice, CONF_CYCLE: "simples"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_entry: MockConfigEntry) -> None:
    """A new key is validated and stored."""
    mock_entry.add_to_hass(hass)
    aioclient_mock.get(f"{BASE}/v1/tariffs", status=401)
    result = await mock_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "bad"})
    assert result["errors"] == {"base": "invalid_auth"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/v1/tariffs", json=load("tariffs.json"))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "new-key"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert mock_entry.data["api_key"] == "new-key" and mock_entry.data[CONF_TARIFF] == TARIFF


async def test_options_direct(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Direct mode: VAT + window length only."""
    mock_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_entry.entry_id, context={"show_advanced_options": True})
    assert CONF_RAPIDAPI_HOST not in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_INCLUDE_VAT: False, CONF_WINDOW_HOURS: 1.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options == {CONF_INCLUDE_VAT: False, CONF_WINDOW_HOURS: 1.5}


async def test_options_rapidapi_host(hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
    """RapidAPI mode: the host can be overridden (advanced)."""
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data, CONF_MODE: MODE_RAPIDAPI})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id, context={"show_advanced_options": True})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_INCLUDE_VAT: True, CONF_WINDOW_HOURS: 2, CONF_RAPIDAPI_HOST: " new.p.rapidapi.com "}
    )
    assert entry.options[CONF_RAPIDAPI_HOST] == "new.p.rapidapi.com"
