"""Explicit user actions for private, opt-in packet recording."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.service import async_register_admin_service
import voluptuous as vol

from .const import DOMAIN

_ENTRY = {vol.Required("config_entry_id"): str}
_CAPTURE = {**_ENTRY, vol.Optional("capture_id"): str}


def register_capture_services(hass: HomeAssistant) -> None:
    async def handle(call: ServiceCall) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        coordinator = hass.data.get(DOMAIN, {}).get(call.data["config_entry_id"])
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
            or coordinator is None
        ):
            raise ServiceValidationError("Select a loaded PyTap config entry")
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
            return await coordinator.async_export_capture(call.data.get("capture_id"))
        except (ValueError, OSError, KeyError) as err:
            # Exception text may include filesystem or raw-data details.
            raise ServiceValidationError(
                f"Packet capture action failed ({type(err).__name__}); "
                "check capture status and available storage"
            ) from None

    for name, schema in {
        "start_capture": {
            **_ENTRY,
            vol.Optional("duration_minutes", default=1440): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10080)
            ),
            vol.Optional("max_storage_mib", default=1024): vol.All(
                vol.Coerce(int), vol.Range(min=16, max=4096)
            ),
        },
        "stop_capture": _ENTRY,
        "capture_status": _CAPTURE,
        "list_captures": _ENTRY,
        "export_capture": _CAPTURE,
    }.items():
        async_register_admin_service(
            hass, DOMAIN, name, handle, schema=vol.Schema(schema), supports_response=SupportsResponse.OPTIONAL
        )
