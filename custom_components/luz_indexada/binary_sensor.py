"""Binary sensors: cheap now, tomorrow available."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import LISBON
from .coordinator import LuzConfigEntry, LuzCoordinator
from .entity import LuzEntity
from .prices import PriceState


@dataclass(frozen=True, kw_only=True)
class LuzBinaryDescription(BinarySensorEntityDescription):
    """Describes a binary sensor."""

    is_on_fn: Callable[[PriceState], bool | None]
    attrs_fn: Callable[[PriceState], dict[str, Any]] | None = None


def _cheap_attrs(s: PriceState) -> dict[str, Any]:
    return {
        "current_price": s.current_price,
        "today_average": s.today_avg,
        "below_average": s.current_price is not None and s.today_avg is not None and s.current_price < s.today_avg,
        "window_start": s.window.start.astimezone(LISBON).isoformat() if s.window else None,
        "window_end": s.window.end.astimezone(LISBON).isoformat() if s.window else None,
    }


def _tomorrow_attrs(s: PriceState) -> dict[str, Any]:
    t = s.tomorrow
    return {
        "date": t.day.isoformat() if t else None,
        "complete": t.complete if t else None,
        "slots_priced": t.priced if t else 0,
    }


BINARY_SENSORS: tuple[LuzBinaryDescription, ...] = (
    LuzBinaryDescription(key="cheap_now", is_on_fn=lambda s: s.cheap_now, attrs_fn=_cheap_attrs),
    LuzBinaryDescription(key="tomorrow_available", is_on_fn=lambda s: s.tomorrow_available, attrs_fn=_tomorrow_attrs),
)


class LuzBinarySensor(LuzEntity, BinarySensorEntity):
    """A binary sensor."""

    entity_description: LuzBinaryDescription

    def __init__(self, coordinator: LuzCoordinator, description: LuzBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """State."""
        return self.entity_description.is_on_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes."""
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LuzConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    async_add_entities(LuzBinarySensor(entry.runtime_data, d) for d in BINARY_SENSORS)
