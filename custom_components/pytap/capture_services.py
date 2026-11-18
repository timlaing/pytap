"""Explicit user actions for private, opt-in packet recording."""

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.service import async_register_admin_service
import voluptuous as vol

from .const import DOMAIN
from .pytap.core.capture import CaptureError, remove_capture_root

_LOGGER = logging.getLogger(__name__)

_ENTRY = {vol.Required("config_entry_id"): str}
_CAPTURE = {**_ENTRY, vol.Optional("capture_id"): str}
_STORAGE = vol.All(vol.Coerce(int), vol.Range(min=16, max=4096))


def capture_root(hass: HomeAssistant, entry_id: str) -> Path:
    """Private per-instance capture directory (included in Home Assistant backups)."""
    return Path(hass.config.path("pytap_captures", entry_id))


async def async_remove_captures(hass: HomeAssistant, entry_id: str) -> None:
    """Delete all captures/exports of a removed config entry; never raises."""
    try:
        await hass.async_add_executor_job(remove_capture_root, capture_root(hass, entry_id))
    except (OSError, ValueError) as err:
        # Exception text may include filesystem paths.
        _LOGGER.warning("Could not remove PyTap packet captures (%s)", type(err).__name__)


def register_capture_services(hass: HomeAssistant) -> None:
    async def handle(call: ServiceCall) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="capture_entry_not_loaded"
            )
        coordinator = entry.runtime_data
        try:
            if call.service == "start_capture":
                return await coordinator.async_start_capture(
                    call.data["duration_minutes"], call.data["max_storage_mib"]
                )
            if call.service == "stop_capture":
                return await coordinator.async_stop_capture()
            if call.service == "capture_status":
                return await coordinator.async_capture_info(call.data.get("capture_id"))
            if call.service == "list_captures":
                return {"captures": await coordinator.async_list_captures()}
            if call.service == "delete_capture":
                return await coordinator.async_delete_capture(call.data["capture_id"])
            return await coordinator.async_export_capture(
                call.data.get("capture_id"), call.data.get("max_storage_mib")
            )
        except CaptureError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=err.translation_key,
                translation_placeholders=err.translation_placeholders,
            ) from None
        except (ValueError, OSError, KeyError) as err:
            # Exception text may include filesystem or raw-data details.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="capture_failed",
                translation_placeholders={"error": type(err).__name__},
            ) from None

    for name, schema in {
        "start_capture": {
            **_ENTRY,
            vol.Optional("duration_minutes", default=1440): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10080)
            ),
            vol.Optional("max_storage_mib", default=1024): _STORAGE,
        },
        "stop_capture": _ENTRY,
        "capture_status": _CAPTURE,
        "list_captures": _ENTRY,
        "export_capture": {**_CAPTURE, vol.Optional("max_storage_mib"): _STORAGE},
        "delete_capture": {**_ENTRY, vol.Required("capture_id"): str},
    }.items():
        async_register_admin_service(
            hass, DOMAIN, name, handle, schema=vol.Schema(schema), supports_response=SupportsResponse.OPTIONAL
        )
