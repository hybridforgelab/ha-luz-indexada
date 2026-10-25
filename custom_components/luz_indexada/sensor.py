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
from homeassistant.const import CURRENCY_EURO, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .compare import Comparison
from .const import CONF_MODE, LISBON, MODE_RAPIDAPI
from .coordinator import LuzConfigEntry, LuzCoordinator
from .entity import LuzEntity
from .prices import PriceState, raw_slots

PRICE_UNIT = "EUR/kWh"

# Large per-slot lists: shown in the UI/templates, never written to the recorder database.
UNRECORDED = frozenset(
    {"raw_today", "raw_tomorrow", "today", "tomorrow", "status_reason", "formula", "tariffs", "notes"}
)


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
        "valid_until": today.extra.get("valid_until"),
        "terms_expired": today.extra.get("terms_expired"),
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


class LuzComparisonEntity(LuzEntity, SensorEntity):
    """Base for the supplier-comparison sensors (unavailable while there is nothing to compare)."""

    _unrecorded_attributes = UNRECORDED

    @property
    def comparison(self) -> Comparison | None:
        """Comparison of the current refresh."""
        data = self.coordinator.data
        return data.comparison if data else None


class LuzTariffAverageSensor(LuzComparisonEntity):
    """Today's average price of one comparable tariff, with its curves for ApexCharts overlays."""

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: LuzCoordinator, tariff: str, name: str) -> None:
        super().__init__(coordinator, f"average_today_{tariff}")
        self._attr_translation_key = "tariff_average_today"
        self._attr_translation_placeholders = {"tariff": name}
        self.tariff = tariff

    @property
    def available(self) -> bool:
        """Only while the tariff is in today's comparison."""
        c = self.comparison
        return super().available and c is not None and self.tariff in c.curves

    @property
    def native_value(self) -> float | None:
        """Average of today's priced slots."""
        c = self.comparison
        curve = c.curves.get(self.tariff) if c else None
        return curve.today_avg if curve else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Curves and tariff details."""
        c = self.comparison
        curve = c.curves.get(self.tariff) if c else None
        if c is None or curve is None:
            return None
        return {
            "tariff": curve.name,
            "tariff_id": curve.tariff,
            "status": curve.status,
            "estimate": curve.status == "estimate",
            "selected": curve.tariff == c.selected,
            "today_min": curve.today_min,
            "today_max": curve.today_max,
            "vat_included": c.include_vat,
            "raw_today": raw_slots(curve.today, c.include_vat),
            "raw_tomorrow": raw_slots(curve.tomorrow, c.include_vat),
        }


class LuzTariffCostSensor(LuzComparisonEntity):
    """Estimated monthly cost of one comparable tariff (/v1/compare, standard ERSE profile)."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = CURRENCY_EURO
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: LuzCoordinator, tariff: str, name: str) -> None:
        super().__init__(coordinator, f"monthly_cost_{tariff}")
        self._attr_translation_key = "tariff_monthly_cost"
        self._attr_translation_placeholders = {"tariff": name}
        self.tariff = tariff

    @property
    def available(self) -> bool:
        """Only while the tariff is in the cost comparison."""
        c = self.comparison
        return super().available and c is not None and self.tariff in c.costs

    @property
    def native_value(self) -> float | None:
        """Monthly cost (VAT per the option, with the real 6%/23% split)."""
        c = self.comparison
        return c.cost(self.tariff) if c else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Cost details."""
        c = self.comparison
        cost = c.costs.get(self.tariff) if c else None
        if c is None or cost is None:
            return None
        cmp = c.compare
        return {
            "tariff": cost.name,
            "tariff_id": cost.tariff,
            "status": cost.status,
            "estimate": cost.status == "estimate",
            "selected": cost.tariff == c.selected,
            "saving_vs_selected": c.saving(cost.tariff),
            "total_excl_vat": cost.total_excl_vat,
            "vat": cost.vat,
            "total_incl_vat": cost.total,
            "vat_included": c.include_vat,
            "avg_energy_price": cost.avg_energy_price,
            "monthly_kwh": cmp.kwh if cmp else None,
            "kva": cmp.kva if cmp else None,
            "month": cmp.month if cmp else None,
            "days_priced": cost.days_priced,
            "coverage": cost.coverage,
            "flags": cost.flags,
        }


class LuzCheapestSensor(LuzComparisonEntity):
    """Name of the cheapest comparable tariff for the configured consumption."""

    def __init__(self, coordinator: LuzCoordinator) -> None:
        super().__init__(coordinator, "cheapest_supplier")

    @property
    def native_value(self) -> str | None:
        """Cheapest tariff name."""
        c = self.comparison
        cheapest = c.cheapest if c else None
        return cheapest.name if cheapest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Every tariff with its cost and the saving versus the selected one."""
        c = self.comparison
        if c is None:
            return None
        cmp = self.coordinator
        return {
            **c.summary(),
            "fetched_day": cmp.compare_result.fetched_day if cmp.compare_result else None,
            "next_attempt": _local(cmp.compare_next_attempt),
            "last_error": cmp.compare_error,
        }


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
    if coordinator.compare:
        entities.append(LuzCheapestSensor(coordinator))
    async_add_entities(entities)
    if not coordinator.compare:
        return

    known: set[str] = set()

    @callback
    def _add_tariffs() -> None:
        """One average and one cost sensor per comparable tariff, added as tariffs show up."""
        c = coordinator.data.comparison if coordinator.data else None
        if c is None:
            return
        new: list[SensorEntity] = []
        for tariff, curve in c.curves.items():
            if (key := f"avg:{tariff}") not in known:
                known.add(key)
                new.append(LuzTariffAverageSensor(coordinator, tariff, curve.name))
        for tariff, cost in c.costs.items():
            if (key := f"cost:{tariff}") not in known:
                known.add(key)
                new.append(LuzTariffCostSensor(coordinator, tariff, cost.name))
        if new:
            async_add_entities(new)

    _add_tariffs()
    entry.async_on_unload(coordinator.async_add_listener(_add_tariffs))
