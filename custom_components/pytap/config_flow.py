"""Config flow for PyTap integration.

Implements menu-driven setup with passive automatic discovery or a manual
optimizer allowlist. Discovered metadata can be edited through options.

Flow: user (transport menu) → tcp / serial → modules_menu → add_module → finish
Options: init (menu) → module edits / discovery / connection / reporting → done
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
import uuid

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import SerialPortSelector
import serialx
import voluptuous as vol

from .connection import (
    CONNECTION_KEYS,
    InvalidConnectionSettings,
    connection_title,
    normalize_connection,
    open_source,
    serial_endpoint_key,
)
from .const import (
    CONF_AUTO_DISCOVERY,
    CONF_BAUD_RATE,
    CONF_CONNECTION_TYPE,
    CONF_EXCLUDED_BARCODES,
    CONF_LIVE_REPORTING,
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    CONF_SERIAL_PORT,
    CONF_WRITE_INTERVAL,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
    DEFAULT_BAUD_RATE,
    DEFAULT_LIVE_REPORTING,
    DEFAULT_PEAK_POWER,
    DEFAULT_PORT,
    DEFAULT_WRITE_INTERVAL,
    DOMAIN,
    MAX_WRITE_INTERVAL,
    MIN_WRITE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Barcode format: X-NNNNNNN[C] where X is hex digit, N is hex, C is alpha
_BARCODE_PATTERN = re.compile(r"^[0-9A-Fa-f]-[0-9A-Fa-f]{1,7}[A-Za-z]$")


def connection_schema(connection_type: str, current: dict[str, Any] | None = None) -> vol.Schema:
    """Build connection forms for setup and options with the same defaults."""
    current = current or {}
    if connection_type == CONNECTION_TCP:
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
                vol.Optional(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): int,
            }
        )
    return vol.Schema(
        {
            vol.Required(CONF_SERIAL_PORT, default=current.get(CONF_SERIAL_PORT, "")): SerialPortSelector(),
            vol.Optional(CONF_BAUD_RATE, default=current.get(CONF_BAUD_RATE, DEFAULT_BAUD_RATE)): int,
        }
    )


ADD_MODULE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODULE_STRING): str,
        vol.Required(CONF_MODULE_NAME): str,
        vol.Required(CONF_MODULE_BARCODE): str,
        vol.Optional(CONF_MODULE_PEAK_POWER, default=DEFAULT_PEAK_POWER): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1000)
        ),
    }
)


def validate_barcode(barcode: str) -> None:
    """Validate a single barcode format."""
    if barcode and not _BARCODE_PATTERN.match(barcode):
        raise InvalidBarcodeFormat(
            f"Invalid barcode format: '{barcode}'. Expected format like A-1234567B (X-NNNNNNNC)."
        )


async def validate_connection(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Probe without consuming bytes; executor cleanup also runs on cancellation."""
    settings = normalize_connection(data)

    def _test_connection() -> None:
        source = open_source(hass.loop, settings)
        source.close()

    # Shield the executor job: cancelling a flow must not cancel ownership of
    # its open attempt. The bounded backend operation still closes in finally.
    await asyncio.shield(hass.async_add_executor_job(_test_connection))
    return {"title": connection_title(settings)}


def serial_port_claimed(hass: HomeAssistant, settings: dict[str, Any], entry_id: str | None = None) -> bool:
    """Avoid probing or saving a serial port owned by another PyTap entry."""
    if settings[CONF_CONNECTION_TYPE] != CONNECTION_SERIAL:
        return False
    return any(
        entry.entry_id != entry_id
        and entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_SERIAL
        and serial_endpoint_key(entry.data.get(CONF_SERIAL_PORT, ""))
        == serial_endpoint_key(settings[CONF_SERIAL_PORT])
        for entry in hass.config_entries.async_entries(DOMAIN)
    )


async def connection_errors(
    hass: HomeAssistant,
    settings: dict[str, Any],
    entry: ConfigEntry | None = None,
) -> dict[str, str]:
    """Reject invalid settings but keep temporary connectivity warn-only."""
    if serial_port_claimed(hass, settings, entry.entry_id if entry else None):
        return {CONF_SERIAL_PORT: "serial_port_in_use"}
    if (
        entry is not None
        and settings[CONF_CONNECTION_TYPE] == CONNECTION_SERIAL
        and entry.data.get(CONF_CONNECTION_TYPE) == CONNECTION_SERIAL
        and serial_endpoint_key(entry.data.get(CONF_SERIAL_PORT, ""))
        == serial_endpoint_key(settings[CONF_SERIAL_PORT])
    ):
        # The running listener owns this port; reload applies any baud change.
        return {}
    try:
        await validate_connection(hass, settings)
    except (ValueError, serialx.UnsupportedSetting):
        return {"base": "unsupported_serial_settings"}
    except Exception as err:
        # Backend messages can contain complete URLs and hardware identities.
        _LOGGER.warning(
            "Could not connect using %s (%s) — saving for retry",
            settings[CONF_CONNECTION_TYPE],
            type(err).__name__,
        )
    return {}


def _modules_description(modules: list[dict[str, Any]]) -> str:
    """Build a human-readable summary of currently added modules."""
    if not modules:
        return "No modules added yet."
    lines = []
    for i, m in enumerate(modules, 1):
        parts = []
        if m.get(CONF_MODULE_STRING):
            parts.append(f"string={m[CONF_MODULE_STRING]}")
        parts.append(m[CONF_MODULE_NAME])
        parts.append(m.get(CONF_MODULE_BARCODE, ""))
        peak_power = m.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER)
        rating = f"{peak_power}Wp" if peak_power else "peak power unknown"
        lines.append(f"  {i}. {' / '.join(parts)} ({rating})")
    return f"**Modules ({len(modules)}):**\n" + "\n".join(lines)


class PyTapConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PyTap."""

    VERSION = 7

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_data: dict[str, Any] = {}
        self._modules: list[dict[str, Any]] = []
        self._auto_discovery = True

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PyTapOptionsFlow:
        """Get the options flow for this handler."""
        return PyTapOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose how to receive the passive byte stream."""
        return self.async_show_menu(step_id="user", menu_options=["tcp", "serial"])

    async def async_step_tcp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure an existing TCP gateway."""
        return await self._async_connection_step(CONNECTION_TCP, user_input)

    async def async_step_serial(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select a local serial port or Home Assistant proxy."""
        return await self._async_connection_step(CONNECTION_SERIAL, user_input)

    async def _async_connection_step(
        self, connection_type: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                settings = normalize_connection({**user_input, CONF_CONNECTION_TYPE: connection_type})
            except InvalidConnectionSettings as err:
                errors[err.field] = err.error
            else:
                errors = await connection_errors(self.hass, settings)
                if not errors:
                    self._user_data = settings
                    await self.async_set_unique_id(uuid.uuid4().hex)
                    return await self.async_step_modules_menu()
        return self.async_show_form(
            step_id=connection_type,
            data_schema=connection_schema(connection_type, user_input),
            errors=errors,
        )

    async def async_step_modules_menu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the modules menu: add another or finish."""
        return self.async_show_menu(
            step_id="modules_menu",
            menu_options=["add_module", "discovery", "finish"],
            description_placeholders={
                "modules_list": _modules_description(self._modules),
                "error": "",
                "discovery_mode": "Automatic" if self._auto_discovery else "Manual",
            },
        )

    async def async_step_discovery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose passive automatic discovery or a manual allowlist."""
        if user_input is not None:
            self._auto_discovery = user_input[CONF_AUTO_DISCOVERY]
            return await self.async_step_modules_menu()
        return self.async_show_form(
            step_id="discovery",
            data_schema=vol.Schema({vol.Required(CONF_AUTO_DISCOVERY, default=self._auto_discovery): bool}),
        )

    async def async_step_add_module(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle adding a single module (string, name, barcode)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            barcode = user_input.get(CONF_MODULE_BARCODE, "").strip().upper()
            name = user_input.get(CONF_MODULE_NAME, "").strip()
            string_group = user_input.get(CONF_MODULE_STRING, "").strip()

            if not string_group:
                errors[CONF_MODULE_STRING] = "missing_string"
            elif not name:
                errors[CONF_MODULE_NAME] = "missing_name"
            elif not barcode:
                errors[CONF_MODULE_BARCODE] = "missing_barcode"
            else:
                try:
                    validate_barcode(barcode)
                except InvalidBarcodeFormat:
                    errors[CONF_MODULE_BARCODE] = "invalid_barcode"

            # Check for duplicate barcode
            if not errors and any(m[CONF_MODULE_BARCODE] == barcode for m in self._modules):
                errors[CONF_MODULE_BARCODE] = "duplicate_barcode"

            if not errors:
                self._modules.append(
                    {
                        CONF_MODULE_STRING: string_group,
                        CONF_MODULE_NAME: name,
                        CONF_MODULE_BARCODE: barcode,
                        CONF_MODULE_PEAK_POWER: user_input.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER),
                    }
                )
                return await self.async_step_modules_menu()

        return self.async_show_form(
            step_id="add_module",
            data_schema=ADD_MODULE_SCHEMA,
            errors=errors,
            description_placeholders={
                "modules_list": _modules_description(self._modules),
            },
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Finish the config flow — create the entry."""
        if not self._modules and not self._auto_discovery:
            return self.async_show_form(
                step_id="add_module",
                data_schema=ADD_MODULE_SCHEMA,
                errors={"base": "no_modules"},
                description_placeholders={"modules_list": _modules_description(self._modules)},
            )
        if serial_port_claimed(self.hass, self._user_data):
            # Another flow may have claimed the port while modules were added.
            return self.async_show_form(
                step_id="serial",
                data_schema=connection_schema(CONNECTION_SERIAL, self._user_data),
                errors={CONF_SERIAL_PORT: "serial_port_in_use"},
            )
        data = {
            **self._user_data,
            CONF_MODULES: self._modules,
            CONF_AUTO_DISCOVERY: self._auto_discovery,
            CONF_EXCLUDED_BARCODES: [],
        }
        title = connection_title(self._user_data)
        return self.async_create_entry(title=title, data=data)


class PyTapOptionsFlow(OptionsFlow):
    """Handle PyTap options — add/remove modules after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._modules: list[dict[str, Any]] = []
        self._initial_barcodes: set[str] = set()
        self._modules_initialized = False
        self._auto_discovery = config_entry.data.get(CONF_AUTO_DISCOVERY, False)
        self._excluded_barcodes = set(config_entry.data.get(CONF_EXCLUDED_BARCODES, []))
        self._editing_barcode: str | None = None

    def _current_modules(self) -> list[dict[str, Any]]:
        """Read the live discovery catalogue without opening or changing a port."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        if coordinator is not None:
            return coordinator.modules
        return [dict(module) for module in self._config_entry.data.get(CONF_MODULES, [])]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options menu: change connection / add / remove / done."""
        if not self._modules_initialized:
            self._modules = self._current_modules()
            self._initial_barcodes = {m[CONF_MODULE_BARCODE] for m in self._modules}
            self._modules_initialized = True
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "change_connection",
                "change_reporting",
                "change_discovery",
                "add_module",
                "edit_module",
                "remove_module",
                "done",
            ],
            description_placeholders={
                "modules_list": _modules_description(self._modules),
                "discovery_mode": "Automatic" if self._auto_discovery else "Manual",
            },
        )

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Save modules and close the options flow."""
        # A listener can discover more modules while this options flow is open.
        # Merge late arrivals, but don't overwrite edits or undo explicit removals.
        selected = {m[CONF_MODULE_BARCODE] for m in self._modules}
        removed = self._initial_barcodes - selected
        self._excluded_barcodes.update(removed)
        self._excluded_barcodes.difference_update(selected)
        modules = list(self._modules)
        modules.extend(
            module
            for module in self._current_modules()
            if module[CONF_MODULE_BARCODE] not in selected | self._excluded_barcodes
        )
        if not modules and not self._auto_discovery:
            return self.async_show_form(
                step_id="change_discovery",
                data_schema=vol.Schema({vol.Required(CONF_AUTO_DISCOVERY, default=False): bool}),
                errors={"base": "no_modules"},
            )
        new_data = {
            **self._config_entry.data,
            CONF_MODULES: modules,
            CONF_AUTO_DISCOVERY: self._auto_discovery,
            CONF_EXCLUDED_BARCODES: sorted(self._excluded_barcodes),
        }
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        return self.async_create_entry(title="", data={})

    async def async_step_change_discovery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Discovery changes are saved together with module edits on Done."""
        if user_input is not None:
            self._auto_discovery = user_input[CONF_AUTO_DISCOVERY]
            return await self.async_step_init()
        return self.async_show_form(
            step_id="change_discovery",
            data_schema=vol.Schema({vol.Required(CONF_AUTO_DISCOVERY, default=self._auto_discovery): bool}),
        )

    async def async_step_edit_module(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select a configured or automatically discovered optimizer to edit."""
        if not self._modules:
            return await self.async_step_init()
        if user_input is not None:
            self._editing_barcode = user_input[CONF_MODULE_BARCODE]
            return await self.async_step_module_details()
        choices = {
            m[CONF_MODULE_BARCODE]: f"{m[CONF_MODULE_NAME]} ({m[CONF_MODULE_BARCODE]})" for m in self._modules
        }
        return self.async_show_form(
            step_id="edit_module",
            data_schema=vol.Schema({vol.Required(CONF_MODULE_BARCODE): vol.In(choices)}),
        )

    async def async_step_module_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit metadata, never the barcode or its stable sensor identities."""
        module = next((m for m in self._modules if m[CONF_MODULE_BARCODE] == self._editing_barcode), None)
        if module is None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_MODULE_NAME].strip()
            string_group = user_input[CONF_MODULE_STRING].strip()
            if not name:
                errors[CONF_MODULE_NAME] = "missing_name"
            if not string_group:
                errors[CONF_MODULE_STRING] = "missing_string"
            if not errors:
                updated = {**module, **user_input, CONF_MODULE_NAME: name, CONF_MODULE_STRING: string_group}
                self._modules = [
                    updated if m[CONF_MODULE_BARCODE] == self._editing_barcode else m for m in self._modules
                ]
                return await self.async_step_init()
        current = user_input if user_input is not None else module
        return self.async_show_form(
            step_id="module_details",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODULE_NAME, default=current[CONF_MODULE_NAME]): str,
                    vol.Required(CONF_MODULE_STRING, default=current[CONF_MODULE_STRING]): str,
                    vol.Required(
                        CONF_MODULE_PEAK_POWER, default=current.get(CONF_MODULE_PEAK_POWER, 0)
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=1000)),
                }
            ),
            errors=errors,
        )

    async def async_step_change_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the transport to use after reloading this entry."""
        return self.async_show_menu(step_id="change_connection", menu_options=["change_tcp", "change_serial"])

    async def async_step_change_tcp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit the TCP endpoint."""
        return await self._async_connection_step(CONNECTION_TCP, user_input)

    async def async_step_change_serial(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit the serial endpoint using the native picker."""
        return await self._async_connection_step(CONNECTION_SERIAL, user_input)

    async def _async_connection_step(
        self, connection_type: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                settings = normalize_connection({**user_input, CONF_CONNECTION_TYPE: connection_type})
            except InvalidConnectionSettings as err:
                errors[err.field] = err.error
            else:
                errors = await connection_errors(self.hass, settings, self._config_entry)
                if not errors:
                    new_data = {
                        key: value
                        for key, value in self._config_entry.data.items()
                        if key not in CONNECTION_KEYS
                    }
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data={**new_data, **settings},
                        title=connection_title(settings),
                    )
                    return await self.async_step_init()
        return self.async_show_form(
            step_id=f"change_{connection_type}",
            data_schema=connection_schema(
                connection_type,
                user_input if user_input is not None else dict(self._config_entry.data),
            ),
            errors=errors,
        )

    async def async_step_change_reporting(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Toggle live reporting.

        Live reporting is presented on its own step with just the checkbox.
        Home Assistant's static forms cannot grey out a field reactively, so
        the reporting interval is collected on a separate ``set_interval``
        step that is only reached when live reporting is turned off. When it
        is left on, the interval is irrelevant and never shown.
        """
        if user_input is not None:
            live = user_input.get(CONF_LIVE_REPORTING, DEFAULT_LIVE_REPORTING)
            if live:
                # Live mode — clear any stored interval and finish.
                new_data = {**self._config_entry.data}
                new_data[CONF_LIVE_REPORTING] = True
                new_data.pop(CONF_WRITE_INTERVAL, None)
                self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
                return await self.async_step_init()
            # Interval mode — collect the interval on the next step.
            return await self.async_step_set_interval()

        current_live = self._config_entry.data.get(CONF_LIVE_REPORTING, DEFAULT_LIVE_REPORTING)
        schema = vol.Schema(
            {
                vol.Optional(CONF_LIVE_REPORTING, default=current_live): bool,
            }
        )

        return self.async_show_form(
            step_id="change_reporting",
            data_schema=schema,
        )

    async def async_step_set_interval(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the reporting interval (only reached when live reporting is off)."""
        if user_input is not None:
            new_data = {**self._config_entry.data}
            new_data[CONF_LIVE_REPORTING] = False
            new_data[CONF_WRITE_INTERVAL] = user_input.get(CONF_WRITE_INTERVAL, DEFAULT_WRITE_INTERVAL)
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return await self.async_step_init()

        current_write_interval = self._config_entry.data.get(CONF_WRITE_INTERVAL, DEFAULT_WRITE_INTERVAL)
        schema = vol.Schema(
            {
                vol.Optional(CONF_WRITE_INTERVAL, default=current_write_interval): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_WRITE_INTERVAL, max=MAX_WRITE_INTERVAL),
                ),
            }
        )

        return self.async_show_form(
            step_id="set_interval",
            data_schema=schema,
        )

    async def async_step_add_module(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a single module in options flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            barcode = user_input.get(CONF_MODULE_BARCODE, "").strip().upper()
            name = user_input.get(CONF_MODULE_NAME, "").strip()
            string_group = user_input.get(CONF_MODULE_STRING, "").strip()

            if not string_group:
                errors[CONF_MODULE_STRING] = "missing_string"
            elif not name:
                errors[CONF_MODULE_NAME] = "missing_name"
            elif not barcode:
                errors[CONF_MODULE_BARCODE] = "missing_barcode"
            else:
                try:
                    validate_barcode(barcode)
                except InvalidBarcodeFormat:
                    errors[CONF_MODULE_BARCODE] = "invalid_barcode"

            if not errors and any(m[CONF_MODULE_BARCODE] == barcode for m in self._modules):
                errors[CONF_MODULE_BARCODE] = "duplicate_barcode"

            if not errors:
                self._modules.append(
                    {
                        CONF_MODULE_STRING: string_group,
                        CONF_MODULE_NAME: name,
                        CONF_MODULE_BARCODE: barcode,
                        CONF_MODULE_PEAK_POWER: user_input.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER),
                    }
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id="add_module",
            data_schema=ADD_MODULE_SCHEMA,
            errors=errors,
            description_placeholders={
                "modules_list": _modules_description(self._modules),
            },
        )

    async def async_step_remove_module(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Remove a module by selecting from a list."""
        if not self._modules:
            return await self.async_step_init()

        if user_input is not None:
            remove_barcode = user_input.get("remove_barcode")
            if remove_barcode:
                self._modules = [m for m in self._modules if m[CONF_MODULE_BARCODE] != remove_barcode]
                self._excluded_barcodes.add(remove_barcode)
            return await self.async_step_init()

        # Build selection list from current modules
        barcode_options = {
            m[CONF_MODULE_BARCODE]: f"{m[CONF_MODULE_NAME]} ({m[CONF_MODULE_BARCODE]})"
            for m in self._modules
            if m.get(CONF_MODULE_BARCODE)
        }

        schema = vol.Schema(
            {
                vol.Required("remove_barcode"): vol.In(barcode_options),
            }
        )

        return self.async_show_form(
            step_id="remove_module",
            data_schema=schema,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class InvalidModuleFormat(HomeAssistantError):
    """Error to indicate invalid module format."""


class InvalidBarcodeFormat(HomeAssistantError):
    """Error to indicate invalid barcode format."""
