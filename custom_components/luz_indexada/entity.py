"""Base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_TARIFF_NAME, DOMAIN, MANUFACTURER, WEBSITE
from .coordinator import LuzCoordinator


class LuzEntity(CoordinatorEntity[LuzCoordinator]):
    """Common device info and naming."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: LuzCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        name = entry.data.get(CONF_TARIFF_NAME) or coordinator.tariff
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Luz Indexada {name} ({coordinator.cycle})",
            manufacturer=MANUFACTURER,
            model=f"{name} · {coordinator.cycle}",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=WEBSITE,
        )
