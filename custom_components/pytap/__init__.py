"""The PyTap integration.

Lifecycle management for the PyTap Home Assistant custom component.
Creates the push-based streaming coordinator, starts the background listener,
and wires up platform forwarding and teardown.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .capture_services import async_remove_captures, register_capture_services
from .connection import InvalidConnectionSettings
from .const import (
    CONF_AUTO_DISCOVERY,
    CONF_CONNECTION_TYPE,
    CONF_EXCLUDED_BARCODES,
    CONF_LIVE_REPORTING,
    CONF_MODULE_BARCODE,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    CONF_WRITE_INTERVAL,
    CONNECTION_TCP,
    DEFAULT_LIVE_REPORTING,
    DEFAULT_PEAK_POWER,
    DEFAULT_STRING_NAME,
    DEFAULT_WRITE_INTERVAL,
    DOMAIN,
    MAX_WRITE_INTERVAL,
    MIN_WRITE_INTERVAL,
)
from .coordinator import PyTapConfigEntry, PyTapDataUpdateCoordinator, coordinator_store

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Bumped from 1 → 2 when voltage/current were split into in/out variants.
# Bumped from 2 → 3 when module string labels became mandatory.
# Bumped from 3 → 4 when module peak_power became mandatory in config.
# Bumped from 4 → 5 when live_reporting toggle was added.
# Bumped from 5 → 6 when serial/proxy connections were added.
# Bumped from 6 → 7 when automatic module discovery became configurable.
CONFIG_ENTRY_VERSION = 7

# Old unique IDs that were replaced by the _in/_out split.
_LEGACY_UNIQUE_ID_RENAMES: dict[str, str] = {
    "voltage": "voltage_in",
    "current": "current_in",
}
# Old unique IDs that no longer have a corresponding entity at all.
_LEGACY_UNIQUE_IDS_TO_REMOVE: set[str] = {"voltage", "current"}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions once, independent of an individual config entry."""
    register_capture_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PyTapConfigEntry) -> bool:
    """Set up PyTap from a config entry.

    1. Wait for any previous listener for this entry to release its port.
    2. Clean up legacy entity unique IDs from pre-v0.2.0.
    3. Create the coordinator and start the background listener (no
       connectivity check: an offline gateway is retried in the background).
    4. Remove devices for modules that are no longer configured.
    5. Store the coordinator and forward platform setup.
    """
    # A timed-out stop must not allow a new coordinator to claim the same port.
    # Raising NotReady makes Home Assistant retry setup until it has stopped.
    stopping = _stopping_coordinators(hass)
    if previous := stopping.get(entry.entry_id):
        if not await previous.async_stop_listener():
            raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="previous_listener_stopping")
        stopping.pop(entry.entry_id, None)

    # --- Legacy entity migration (voltage/current → voltage_in/out, current_in/out) ---
    await _async_cleanup_legacy_entities(hass, entry)

    try:
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
    except InvalidConnectionSettings as err:
        # Older versions could save e.g. an out-of-range port. Ask the user
        # to fix it in the options flow rather than retrying forever.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="invalid_connection_settings",
            translation_placeholders={"field": err.field},
        ) from err

    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_listener()
    entry.runtime_data = coordinator

    # Home Assistant doesn't unload entries on shutdown: stop the listener and
    # finalise any packet capture explicitly so its files stay verifiable.
    async def _async_on_stop(_event: Event) -> None:
        await coordinator.async_stop_listener()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop))

    if coordinator.store_loaded:
        _async_remove_orphaned_devices(hass, entry, coordinator.modules)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates to reload module configuration
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PyTapConfigEntry) -> bool:
    """Unload a config entry.

    A listener that doesn't stop in time parks its coordinator in the stopping
    registry so the next setup waits (``ConfigEntryNotReady``) instead of
    the entry getting stuck as failed-unload until Home Assistant restarts.
    """
    coordinator = entry.runtime_data
    stopped = await coordinator.async_stop_listener()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not stopped:
        _stopping_coordinators(hass)[entry.entry_id] = coordinator
        _LOGGER.warning("PyTap listener is still stopping; setup will wait for it")
    return unload_ok


def _stopping_coordinators(hass: HomeAssistant) -> dict[str, PyTapDataUpdateCoordinator]:
    """Coordinators whose listener timed out on unload, by entry ID (never live ones)."""
    return hass.data.setdefault(DOMAIN, {})


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this entry's private packet captures and saved state with the entry."""
    await async_remove_captures(hass, entry.entry_id)
    await coordinator_store(hass, entry.entry_id).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: PyTapConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Let users delete optimizer and empty string devices from the UI.

    Deleting an optimizer excludes its barcode (so discovery doesn't re-add it)
    and reloads the entry. The installation device and strings that still
    contain modules can't be deleted.
    """
    identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
    modules = list(entry.data.get(CONF_MODULES, []))
    known = entry.runtime_data.modules if entry.state is ConfigEntryState.LOADED else modules
    barcodes = {module.get(CONF_MODULE_BARCODE) for module in known}
    if removed := identifiers & barcodes:
        excluded = set(entry.data.get(CONF_EXCLUDED_BARCODES, [])) | removed
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_MODULES: [m for m in modules if m.get(CONF_MODULE_BARCODE) not in removed],
                CONF_EXCLUDED_BARCODES: sorted(excluded),
            },
        )
        return True
    used_strings = {f"{entry.entry_id}_string_{m.get(CONF_MODULE_STRING)}" for m in known}
    return not identifiers & ({f"{entry.entry_id}_installation"} | used_strings)


def _async_remove_orphaned_devices(
    hass: HomeAssistant, entry: ConfigEntry, modules: list[dict[str, Any]]
) -> None:
    """Remove devices (and their entities) for removed modules or empty strings."""
    keep = {f"{entry.entry_id}_installation"}
    for module in modules:
        keep.add(module.get(CONF_MODULE_BARCODE, ""))
        keep.add(f"{entry.entry_id}_string_{module.get(CONF_MODULE_STRING, '')}")
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
        if identifiers and not identifiers & keep:
            _LOGGER.info("Removing PyTap device %s: no longer configured", device.name)
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current version.

    v1 → v2: voltage/current sensors split into _in/_out variants.
    v2 → v3: module string labels are mandatory.
    v3 → v4: module peak power is added with default value.
    v4 → v5: live_reporting toggle added (default True).
    v5 → v6: existing connections explicitly marked as TCP.
    v6 → v7: preserve manual selection on existing entries.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        return False
    if entry.version == 1:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version 1 to 2",
            entry.entry_id,
        )
        hass.config_entries.async_update_entry(entry, version=2)

    if entry.version == 2:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version 2 to 3",
            entry.entry_id,
        )
        modules = list(entry.data.get(CONF_MODULES, []))
        updated_modules: list[dict[str, str]] = []
        updated = False

        for module in modules:
            normalized = dict(module)
            if not normalized.get(CONF_MODULE_STRING):
                normalized[CONF_MODULE_STRING] = DEFAULT_STRING_NAME
                updated = True
            updated_modules.append(normalized)

        if updated:
            new_data = {**entry.data, CONF_MODULES: updated_modules}
            hass.config_entries.async_update_entry(entry, data=new_data, version=3)
        else:
            hass.config_entries.async_update_entry(entry, version=3)

    if entry.version == 3:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version 3 to 4",
            entry.entry_id,
        )
        modules = list(entry.data.get(CONF_MODULES, []))
        updated_modules: list[dict[str, str | int]] = []

        for module in modules:
            normalized = dict(module)
            if CONF_MODULE_PEAK_POWER not in normalized:
                normalized[CONF_MODULE_PEAK_POWER] = DEFAULT_PEAK_POWER
            updated_modules.append(normalized)

        new_data = {**entry.data, CONF_MODULES: updated_modules}
        hass.config_entries.async_update_entry(entry, data=new_data, version=4)

    if entry.version == 4:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version 4 to 5",
            entry.entry_id,
        )
        new_data = {**entry.data}
        if CONF_LIVE_REPORTING not in new_data:
            new_data[CONF_LIVE_REPORTING] = DEFAULT_LIVE_REPORTING
        # The v5 reporting form constrains write_interval to
        # [MIN_WRITE_INTERVAL, MAX_WRITE_INTERVAL].  Earlier versions allowed
        # smaller values, so clamp any stale value to keep the options form
        # submittable after upgrade.
        if CONF_WRITE_INTERVAL in new_data:
            try:
                interval = int(new_data[CONF_WRITE_INTERVAL])
            except (TypeError, ValueError):
                interval = DEFAULT_WRITE_INTERVAL
            new_data[CONF_WRITE_INTERVAL] = max(MIN_WRITE_INTERVAL, min(MAX_WRITE_INTERVAL, interval))
        hass.config_entries.async_update_entry(entry, data=new_data, version=5)

    if entry.version == 5:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_CONNECTION_TYPE: CONNECTION_TCP},
            version=6,
        )

    if entry.version == 6:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_AUTO_DISCOVERY: entry.data.get(CONF_AUTO_DISCOVERY, False),
                CONF_EXCLUDED_BARCODES: entry.data.get(CONF_EXCLUDED_BARCODES, []),
            },
            version=7,
        )

    return True


async def _async_cleanup_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove legacy entity registry entries left over from the voltage/current rename.

    Before v0.2.0, sensors used unique IDs like ``pytap_BARCODE_voltage`` and
    ``pytap_BARCODE_current``. These were replaced by ``_voltage_in``,
    ``_voltage_out``, ``_current_in``, and ``_current_out``. The old entries
    linger in the entity registry as orphaned/unavailable entities.
    """
    ent_reg = er.async_get(hass)
    modules: list[dict[str, str]] = entry.data.get(CONF_MODULES, [])
    removed = 0

    for module in modules:
        barcode = module.get(CONF_MODULE_BARCODE, "")
        if not barcode:
            continue
        for old_suffix in _LEGACY_UNIQUE_IDS_TO_REMOVE:
            old_unique_id = f"{DOMAIN}_{barcode}_{old_suffix}"
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, old_unique_id)
            if entity_id is not None:
                _LOGGER.info(
                    "Removing legacy entity %s (unique_id=%s)",
                    entity_id,
                    old_unique_id,
                )
                ent_reg.async_remove(entity_id)
                removed += 1

    if removed:
        _LOGGER.info("Cleaned up %d legacy sensor entities", removed)
