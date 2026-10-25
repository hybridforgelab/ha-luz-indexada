"""Luz Indexada: 15-minute final electricity prices for Portuguese indexed tariffs."""

from __future__ import annotations

from datetime import datetime

from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store

from .api import LuzApiClient
from .const import (
    CONF_BASE_URL,
    CONF_MODE,
    CONF_RAPIDAPI_HOST,
    DEFAULT_RAPIDAPI_HOST,
    DOMAIN,
    MODE_RAPIDAPI,
    STORAGE_VERSION,
)
from .coordinator import LuzConfigEntry, LuzCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


def build_client(hass: HomeAssistant, data: dict, options: dict | None = None) -> LuzApiClient:
    """Client for a config entry's data (options may override the RapidAPI host)."""
    host = (options or {}).get(CONF_RAPIDAPI_HOST) or data.get(CONF_RAPIDAPI_HOST) or DEFAULT_RAPIDAPI_HOST
    return LuzApiClient(
        async_get_clientsession(hass),
        data.get(CONF_MODE, MODE_RAPIDAPI),
        data[CONF_API_KEY],
        base_url=data.get(CONF_BASE_URL),
        rapidapi_host=host,
    )


async def async_setup_entry(hass: HomeAssistant, entry: LuzConfigEntry) -> bool:
    """Set up a tariff from a config entry."""
    client = build_client(hass, dict(entry.data), dict(entry.options))
    coordinator = LuzCoordinator(hass, entry, client)
    client.on_call = coordinator.count_call
    await coordinator.async_load_cache()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    @callback
    def _tick(_now: datetime) -> None:
        entry.async_create_background_task(hass, coordinator.async_refresh(), "luz_indexada quarter-hour refresh")

    # Every quarter-hour: recompute from the cache (and fetch only when the schedule says so).
    entry.async_on_unload(async_track_time_change(hass, _tick, minute=(0, 15, 30, 45), second=2))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: LuzConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LuzConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: LuzConfigEntry) -> None:
    """Delete the price cache when the entry is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
