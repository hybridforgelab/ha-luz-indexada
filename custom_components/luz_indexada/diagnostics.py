"""Diagnostics (the API key is redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .api import DayPrices
from .coordinator import LuzConfigEntry

TO_REDACT = {CONF_API_KEY}


def _day(day: DayPrices | None) -> dict[str, Any] | None:
    if day is None:
        return None
    return {
        "date": day.day.isoformat(),
        "complete": day.complete,
        "slots": len(day.slots),
        "slots_priced": day.priced,
        "status": day.status,
        "flags": day.flags,
        "version": day.version,
        "fetched_at": day.fetched_at.isoformat() if day.fetched_at else None,
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LuzConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    c = entry.runtime_data
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "today": _day(c.today),
        "tomorrow": _day(c.tomorrow),
        "schedule": {
            "tomorrow_next_attempt": c.tomorrow_next_attempt.isoformat() if c.tomorrow_next_attempt else None,
            "today_next_attempt": c.today_next_attempt.isoformat() if c.today_next_attempt else None,
            "backoff_until": c.backoff_until.isoformat() if c.backoff_until else None,
            "last_error": c.last_error,
        },
        "calls": {"month": c.calls_month, "month_count": c.calls_month_count, "total": c.calls_total},
        "quota": {"limit": c.client.quota_limit, "remaining": c.client.quota_remaining},
    }
