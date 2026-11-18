"""Binary sensor platform for PyTap: per-optimizer status flags and reading faults.

The two status flags come from undocumented power-report bits. Their names
describe what captured data shows, not a meaning confirmed by Tigo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MODULE_BARCODE, DOMAIN
from .coordinator import PyTapDataUpdateCoordinator
from .entity import module_device_info


@dataclass(frozen=True, kw_only=True)
class PyTapBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a PyTap binary sensor entity."""

    value_key: str
    # Live flags are unavailable once a panel stops reporting.
    live: bool = True


BINARY_SENSOR_DESCRIPTIONS: tuple[PyTapBinarySensorEntityDescription, ...] = (
    PyTapBinarySensorEntityDescription(
        key="high_output_mode",
        translation_key="high_output_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="high_output_mode",
    ),
    PyTapBinarySensorEntityDescription(
        key="low_voltage_flag",
        translation_key="low_voltage_flag",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="low_voltage_flag",
    ),
    PyTapBinarySensorEntityDescription(
        key="reading_fault",
        translation_key="reading_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="reading_fault",
        # Stays set until the next valid reading, even after reports stop.
        live=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PyTap binary sensors for configured and discovered optimizers."""
    coordinator: PyTapDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    known_barcodes: set[str] = set()

    @callback
    def add_discovered_entities() -> None:
        if len(known_barcodes) == coordinator.module_count:
            return
        entities: list[PyTapBinarySensor] = []
        for module_config in coordinator.modules:
            barcode = module_config.get(CONF_MODULE_BARCODE, "")
            if not barcode or barcode in known_barcodes:
                continue
            known_barcodes.add(barcode)
            entities.extend(
                PyTapBinarySensor(coordinator, description, module_config)
                for description in BINARY_SENSOR_DESCRIPTIONS
            )
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(add_discovered_entities))
    add_discovered_entities()


class PyTapBinarySensor(CoordinatorEntity[PyTapDataUpdateCoordinator], BinarySensorEntity):
    """A per-optimizer status flag or fault indicator."""

    _attr_has_entity_name = True
    entity_description: PyTapBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: PyTapDataUpdateCoordinator,
        description: PyTapBinarySensorEntityDescription,
        module_config: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._barcode = module_config[CONF_MODULE_BARCODE]
        self._attr_unique_id = f"{DOMAIN}_{self._barcode}_{description.key}"
        self._attr_device_info = module_device_info(module_config)

    def _node(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get("nodes", {}).get(self._barcode)

    @property
    def available(self) -> bool:
        node = self._node()
        if node is None:
            return False
        if self.entity_description.live:
            return bool(self.coordinator.is_reporting(self._barcode))
        return True

    @property
    def is_on(self) -> bool | None:
        node = self._node()
        value = node.get(self.entity_description.value_key) if node else None
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "reading_fault" or (node := self._node()) is None:
            return None
        return {
            "fault_reasons": list(node.get("fault_reasons") or []),
            "last_fault_at": node.get("last_fault_at"),
        }
