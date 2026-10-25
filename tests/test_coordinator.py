"""Setup, fetch schedule (API call budget), cache, errors and entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.luz_indexada.api import parse_prices
from custom_components.luz_indexada.const import (
    CONF_MODE,
    CONF_RAPIDAPI_HOST,
    DEFAULT_RAPIDAPI_HOST,
    DOMAIN,
    MODE_RAPIDAPI,
    STORAGE_VERSION,
)
from custom_components.luz_indexada.diagnostics import async_get_config_entry_diagnostics

from .conftest import TARIFF, TODAY, TOMORROW, complete, load, prices_url


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: str) -> None:
    """Jump to a quarter-hour tick and let the coordinator run."""
    moment = _utc(when)
    freezer.move_to(moment)
    async_fire_time_changed(hass, moment)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str, platform: str = "sensor") -> Any:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None, key
    return hass.states.get(entity_id)


def _not_yet(day: str) -> dict[str, Any]:
    return {"detail": "not published yet", "date": day, "next_publication_utc": None, "latest_published": TODAY}


async def test_day_schedule(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, mock_entry
) -> None:
    """One call at startup, tomorrow from 13:00 retried hourly, nothing else."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))  # Lisbon = UTC after the DST change
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    aioclient_mock.get(prices_url(TOMORROW), status=404, json=_not_yet(TOMORROW))
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 1

    price = _state(hass, mock_entry, "current_price")
    assert price.attributes["unit_of_measurement"] == "EUR/kWh"
    assert float(price.state) == pytest.approx(parse_prices(load(f"prices_{TODAY}.json"), TARIFF).slots[44].total_vat)
    assert price.attributes["status"] == "exact" and price.attributes["estimate"] is False
    assert len(price.attributes["raw_today"]) == 100 and price.attributes["raw_tomorrow"] == []
    assert price.attributes["vat_included"] is True
    assert _state(hass, mock_entry, "tomorrow_available", "binary_sensor").state == "off"
    assert _state(hass, mock_entry, "cheap_now", "binary_sensor").state in ("on", "off")
    assert _state(hass, mock_entry, "cheapest_window_start").state.startswith("2026-10-25T")
    assert _state(hass, mock_entry, "api_calls_month").state == "1"

    # Quarter-hour ticks before 13:00: the current price moves, no API call.
    await _at(hass, freezer, "2026-10-25T10:15:02Z")
    await _at(hass, freezer, "2026-10-25T12:45:02Z")
    assert aioclient_mock.call_count == 1
    assert _state(hass, mock_entry, "current_price").state != price.state

    # 13:00: tomorrow not out yet → one call, then wait an hour.
    await _at(hass, freezer, "2026-10-25T13:00:02Z")
    await _at(hass, freezer, "2026-10-25T13:30:02Z")
    assert aioclient_mock.call_count == 2

    aioclient_mock.clear_requests()
    aioclient_mock.get(prices_url(TOMORROW), json=load(f"prices_{TOMORROW}.json"))
    await _at(hass, freezer, "2026-10-25T14:00:02Z")
    assert aioclient_mock.call_count == 1  # today is complete: no refresh needed
    assert _state(hass, mock_entry, "tomorrow_available", "binary_sensor").state == "on"
    assert len(_state(hass, mock_entry, "current_price").attributes["raw_tomorrow"]) == 96

    # Rest of the day and midnight: tomorrow is promoted, still no call.
    await _at(hass, freezer, "2026-10-25T20:00:02Z")
    await _at(hass, freezer, "2026-10-26T00:00:02Z")
    assert aioclient_mock.call_count == 1
    price = _state(hass, mock_entry, "current_price")
    assert price.attributes["today_complete"] is False and price.attributes["raw_tomorrow"] == []
    assert float(price.state) == pytest.approx(0.419364)

    # Next afternoon: tomorrow + today's missing last hour.
    aioclient_mock.clear_requests()
    aioclient_mock.get(prices_url("2026-10-27"), json={**load(f"prices_{TOMORROW}.json"), "date": "2026-10-27"})
    aioclient_mock.get(prices_url(TOMORROW), json=complete(load(f"prices_{TOMORROW}.json")))
    await _at(hass, freezer, "2026-10-26T13:00:02Z")
    await _at(hass, freezer, "2026-10-26T15:00:02Z")
    assert aioclient_mock.call_count == 2
    assert _state(hass, mock_entry, "current_price").attributes["today_complete"] is True
    assert _state(hass, mock_entry, "api_calls_month").state == "5"


async def test_restart_uses_cache(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, mock_entry
) -> None:
    """A reload/restart doesn't spend calls when today and tomorrow are cached."""
    freezer.move_to(_utc("2026-10-25T15:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    aioclient_mock.get(prices_url(TOMORROW), json=load(f"prices_{TOMORROW}.json"))
    await _setup(hass, mock_entry)
    assert aioclient_mock.call_count == 2
    assert await hass.config_entries.async_reload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 2
    assert _state(hass, mock_entry, "tomorrow_available", "binary_sensor").state == "on"
    assert _state(hass, mock_entry, "api_calls_month").attributes["total"] == 2


async def test_cache_ignored_for_other_tariff_or_corrupt(
    hass: HomeAssistant, hass_storage: dict[str, Any], aioclient_mock: AiohttpClientMocker, freezer, mock_entry
) -> None:
    """A cache for a different tariff, or an unreadable one, is not used."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    key = f"{DOMAIN}.{mock_entry.entry_id}"
    hass_storage[key] = {
        "version": STORAGE_VERSION,
        "key": key,
        "data": {"tariff": TARIFF, "cycle": "simples", "today": {"day": "x"}},
    }
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    await _setup(hass, mock_entry)
    assert aioclient_mock.call_count == 1
    await hass.config_entries.async_remove(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert await Store(hass, STORAGE_VERSION, key).async_load() is None


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, mock_entry
) -> None:
    """401 → setup error + reauth flow."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), status=401, json={"detail": "Unauthorized"})
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_no_data_retries_setup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, mock_entry
) -> None:
    """Network error without a cache → setup retried later."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), exc=aiohttp.ClientError("down"))
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


async def test_today_not_published_retries_setup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, mock_entry
) -> None:
    """Today missing on the API → setup retried later."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), status=404, json=_not_yet(TODAY))
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


async def test_errors_back_off_and_keep_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, mock_entry
) -> None:
    """Errors on tomorrow keep today's entities alive and back off (429: at least an hour)."""
    freezer.move_to(_utc("2026-10-25T12:50:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    await _setup(hass, mock_entry)

    aioclient_mock.clear_requests()
    aioclient_mock.get(prices_url(TOMORROW), status=429, json={"message": "quota"})
    await _at(hass, freezer, "2026-10-25T13:00:02Z")
    await _at(hass, freezer, "2026-10-25T13:45:02Z")
    assert aioclient_mock.call_count == 1
    assert _state(hass, mock_entry, "current_price").state not in ("unavailable", "unknown")
    calls = _state(hass, mock_entry, "api_calls_month").attributes
    assert calls["last_error"] == "rate limited (429)" and calls["backoff_until"].startswith("2026-10-25T14:00")

    aioclient_mock.clear_requests()
    aioclient_mock.get(prices_url(TOMORROW), status=500)
    await _at(hass, freezer, "2026-10-25T14:00:02Z")
    await _at(hass, freezer, "2026-10-25T14:15:02Z")  # error backoff: 15 min
    assert aioclient_mock.call_count == 2
    await _at(hass, freezer, "2026-10-25T14:30:02Z")  # then 30 min
    assert aioclient_mock.call_count == 2

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        prices_url(TOMORROW), status=404, json={**_not_yet(TOMORROW), "next_publication_utc": "2026-10-25T17:00:00Z"}
    )
    await _at(hass, freezer, "2026-10-25T14:45:02Z")
    await _at(hass, freezer, "2026-10-25T16:00:02Z")  # waits for the announced publication time
    assert aioclient_mock.call_count == 1

    aioclient_mock.clear_requests()
    aioclient_mock.get(prices_url(TOMORROW), status=401)
    await _at(hass, freezer, "2026-10-25T17:00:02Z")
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_today_refresh_still_incomplete(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, mock_entry
) -> None:
    """If today's last hour is still pending after tomorrow is out, retry hourly (not every 15 min)."""
    freezer.move_to(_utc("2026-10-26T15:00:00Z"))
    incomplete = load(f"prices_{TOMORROW}.json")
    aioclient_mock.get(prices_url(TOMORROW), json=incomplete)
    aioclient_mock.get(prices_url("2026-10-27"), json={**incomplete, "date": "2026-10-27"})
    await _setup(hass, mock_entry)
    assert aioclient_mock.call_count == 3  # today, tomorrow, today again
    await _at(hass, freezer, "2026-10-26T15:15:02Z")
    assert aioclient_mock.call_count == 3
    aioclient_mock.clear_requests()
    aioclient_mock.get(prices_url(TOMORROW), status=404, json=_not_yet(TOMORROW))
    await _at(hass, freezer, "2026-10-26T16:00:02Z")
    assert aioclient_mock.call_count == 1
    assert _state(hass, mock_entry, "current_price").attributes["today_complete"] is False


async def test_wrong_day_in_response(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, mock_entry
) -> None:
    """A response for another date is treated as not published."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TOMORROW}.json"))
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rapidapi_entry_has_quota_sensor(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, entry_data: dict[str, Any]
) -> None:
    """RapidAPI mode uses the host option and exposes the remaining quota."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    host = "custom.p.rapidapi.com"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data, CONF_MODE: MODE_RAPIDAPI, CONF_RAPIDAPI_HOST: DEFAULT_RAPIDAPI_HOST},
        options={CONF_RAPIDAPI_HOST: host},
    )
    aioclient_mock.get(
        prices_url(TODAY, f"https://{host}"),
        json=load(f"prices_{TODAY}.json"),
        headers={"x-ratelimit-requests-limit": "150", "x-ratelimit-requests-remaining": "120"},
    )
    await _setup(hass, entry)
    quota = _state(hass, entry, "api_quota_remaining")
    assert quota.state == "120" and quota.attributes["limit"] == 150


async def test_diagnostics_redact_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, mock_entry
) -> None:
    """Diagnostics never contain the API key."""
    freezer.move_to(_utc("2026-10-25T15:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    aioclient_mock.get(prices_url(TOMORROW), status=404, json=_not_yet(TOMORROW))
    await _setup(hass, mock_entry)
    diag = await async_get_config_entry_diagnostics(hass, mock_entry)
    assert diag["entry"]["data"]["api_key"] == "**REDACTED**"
    assert "test-key" not in str(diag)
    assert diag["today"]["slots"] == 100 and diag["tomorrow"] is None
    assert diag["schedule"]["tomorrow_next_attempt"].startswith("2026-10-25T16:00")
    assert diag["calls"]["month_count"] == 2


async def test_unload(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, mock_entry) -> None:
    """Unload removes the entities' updates."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    await _setup(hass, mock_entry)
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    assert mock_entry.state is ConfigEntryState.NOT_LOADED


async def test_options_change_reloads_without_calls(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, mock_entry
) -> None:
    """Switching VAT off reloads from the cache and changes the price, with no API call."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    await _setup(hass, mock_entry)
    with_vat = float(_state(hass, mock_entry, "current_price").state)
    hass.config_entries.async_update_entry(mock_entry, options={**mock_entry.options, "include_vat": False})
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1
    assert float(_state(hass, mock_entry, "current_price").state) == pytest.approx(with_vat / 1.23, abs=1e-5)


async def test_stale_cache_after_days_offline(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, mock_entry
) -> None:
    """Two days later both cached days are dropped and today is fetched."""
    freezer.move_to(_utc("2026-10-25T15:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    aioclient_mock.get(prices_url(TOMORROW), json=load(f"prices_{TOMORROW}.json"))
    aioclient_mock.get(prices_url("2026-10-27"), json={**load(f"prices_{TOMORROW}.json"), "date": "2026-10-27"})
    await _setup(hass, mock_entry)
    await _at(hass, freezer, "2026-10-27T10:00:02Z")
    assert aioclient_mock.call_count == 3
    assert _state(hass, mock_entry, "tomorrow_available", "binary_sensor").state == "off"


async def test_entity_ids(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, mock_entry) -> None:
    """Entity ids documented in the README."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    await _setup(hass, mock_entry)
    assert hass.states.get("sensor.luz_indexada_galp_plano_flexivel_simples_current_price") is not None
    assert hass.states.get("sensor.luz_indexada_galp_plano_flexivel_simples_cheapest_window_start") is not None
    assert hass.states.get("binary_sensor.luz_indexada_galp_plano_flexivel_simples_cheap_now") is not None
