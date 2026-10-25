"""Price sensors (all computed locally from cached slots)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MODE, LISBON, MODE_RAPIDAPI
from .coordinator import LuzConfigEntry, LuzCoordinator
from .entity import LuzEntity
from .prices import PriceState

PRICE_UNIT = "EUR/kWh"

# Large per-slot lists: shown in the UI/templates, never written to the recorder database.
UNRECORDED = frozenset({"raw_today", "raw_tomorrow", "today", "tomorrow", "status_reason", "formula"})


def _local(value: datetime | None) -> str | None:
    return value.astimezone(LISBON).isoformat() if value else None


@dataclass(frozen=True, kw_only=True)
class LuzSensorDescription(SensorEntityDescription):
    """Describes a price sensor."""

    value_fn: Callable[[PriceState], float | datetime | None]
    attrs_fn: Callable[[PriceState], dict[str, Any]] | None = None


def _current_attrs(s: PriceState) -> dict[str, Any]:
    today = s.today
    return {
        "tariff": today.tariff_name,
        "status": today.status,
        "estimate": today.status == "estimate",
        "status_reason": today.extra.get("status_reason"),
        "formula": today.formula,
        "period": s.current.period if s.current else None,
        "vat_included": s.include_vat,
        "today_complete": today.complete,
        "flags": today.flags,
        "reference_kva": today.reference_kva,
        "tomorrow_valid": s.tomorrow_available,
        "raw_today": s.raw(today),
        "raw_tomorrow": s.raw(s.tomorrow),
        "today": s.values(today),
        "tomorrow": s.values(s.tomorrow),
    }


SENSORS: tuple[LuzSensorDescription, ...] = (
    LuzSensorDescription(
        key="current_price",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda s: s.current_price,
        attrs_fn=_current_attrs,
    ),
    LuzSensorDescription(
        key="next_price",
        native_unit_of_measurement=PRICE_UNIT,
        suggested_display_precision=4,
        value_fn=lambda s: s.next_price,
        attrs_fn=lambda s: {"start": _local(s.next.start if s.next else None)},
    ),
    LuzSensorDescription(
        key="today_min",
        native_unit_of_measurement=PRICE_UNIT,
        suggested_display_precision=4,
        value_fn=lambda s: s.today_min,
        attrs_fn=lambda s: {"at": _local(s.today_min_at)},
    ),
    LuzSensorDescription(
        key="today_max",
        native_unit_of_measurement=PRICE_UNIT,
        suggested_display_precision=4,
        value_fn=lambda s: s.today_max,
        attrs_fn=lambda s: {"at": _local(s.today_max_at)},
    ),
    LuzSensorDescription(
        key="today_avg",
        native_unit_of_measurement=PRICE_UNIT,
        suggested_display_precision=4,
        value_fn=lambda s: s.today_avg,
    ),
    LuzSensorDescription(
        key="cheapest_window_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.window.start if s.window else None,
        attrs_fn=lambda s: {
            "end": _local(s.window.end if s.window else None),
            "average_price": round(s.window.average, 6) if s.window else None,
            "hours": s.window_hours,
        },
    ),
)


class LuzSensor(LuzEntity, SensorEntity):
    """A price sensor."""

    entity_description: LuzSensorDescription
    _unrecorded_attributes = UNRECORDED

    def __init__(self, coordinator: LuzCoordinator, description: LuzSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | datetime | None:
        """State."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes."""
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None


class LuzCallsSensor(LuzEntity, SensorEntity):
    """API calls this integration made in the current month (counted locally)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "requests"

    def __init__(self, coordinator: LuzCoordinator) -> None:
        super().__init__(coordinator, "api_calls_month")

    @property
    def native_value(self) -> int:
        """Calls this month."""
        return self.coordinator.calls_month_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Schedule details."""
        c = self.coordinator
        return {
            "month": c.calls_month,
            "total": c.calls_total,
            "next_tomorrow_attempt": _local(c.tomorrow_next_attempt),
            "backoff_until": _local(c.backoff_until),
            "last_error": c.last_error,
        }


class LuzQuotaSensor(LuzEntity, SensorEntity):
    """Requests left on the RapidAPI plan, as reported by RapidAPI on the last response."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "requests"

    def __init__(self, coordinator: LuzCoordinator) -> None:
        super().__init__(coordinator, "api_quota_remaining")

    @property
    def native_value(self) -> int | None:
        """Remaining requests."""
        return self.coordinator.client.quota_remaining

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Plan limit."""
        return {"limit": self.coordinator.client.quota_limit}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LuzConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [LuzSensor(coordinator, d) for d in SENSORS]
    entities.append(LuzCallsSensor(coordinator))
    if entry.data.get(CONF_MODE, MODE_RAPIDAPI) == MODE_RAPIDAPI:
        entities.append(LuzQuotaSensor(coordinator))
    async_add_entities(entities)
