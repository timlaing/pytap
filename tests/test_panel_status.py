"""Panel availability timeout, status-flag entities, and impossible-reading faults."""

from unittest.mock import AsyncMock, patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import CONF_AUTO_DISCOVERY, DOMAIN, REPORT_TIMEOUT_SECONDS
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator, reading_faults
from custom_components.pytap.pytap.core.events import PowerReportEvent
from custom_components.pytap.pytap.core.types import PowerReport

BARCODE = "A-1234567B"
MODULE = {"barcode": BARCODE, "name": "East Panel", "string": "East", "peak_power": 500}


def _event(*, vin=31.0, vout=30.7, iin=1.9, temp=18.0, status=0x64, high=True, low=False):
    return PowerReportEvent(
        gateway_id=1,
        node_id=10,
        barcode=BARCODE,
        voltage_in=vin,
        voltage_out=vout,
        current_in=iin,
        temperature=temp,
        dc_dc_duty_cycle=1.0,
        rssi=99,
        timestamp=dt_util.now(),
        high_output_mode=high,
        status_code=status,
        low_voltage_flag=low,
    )


# The corrupt node 10 report captured at 2026-11-18 10:02:44 UTC.
IMPOSSIBLE = {"vin": 69.7, "vout": 154.3, "iin": 7.87, "status": 0x50, "high": True, "low": False}


def test_plausible_readings_have_no_faults() -> None:
    assert reading_faults(_event()) == []
    # Dawn readings: low voltage, output just under input.
    assert reading_faults(_event(vin=11.3, vout=5.1, iin=0.02, status=0xE4, low=True)) == []
    # Rounding can put output fractionally above input.
    assert reading_faults(_event(vin=30.0, vout=31.5)) == []


def test_impossible_readings_are_flagged() -> None:
    assert reading_faults(_event(**IMPOSSIBLE)) == [
        "voltage_out_out_of_range",
        "voltage_out_above_input",
    ]
    assert reading_faults(_event(temp=150.0)) == ["temperature_out_of_range"]
    assert reading_faults(_event(vin=-1.0, vout=0.0)) == ["voltage_in_out_of_range"]


def test_power_report_status_bits() -> None:
    def report(byte7: int, byte9: int) -> PowerReport:
        return PowerReport.from_bytes(
            bytes([0x26, 0x41, 0x33, 0xFF, 0x17, 0x60, 0xB4, byte7, 0, byte9, 0, 0, 99])
        )

    production = report(0x93, 0x64)
    assert production.high_output_mode is True
    assert production.status_code == 0x64
    assert production.low_voltage_flag is False

    dawn = report(0x10, 0xE4)
    assert dawn.high_output_mode is False
    assert dawn.status_code == 0xE4
    assert dawn.low_voltage_flag is True


async def _setup(hass: HomeAssistant) -> tuple[MockConfigEntry, PyTapDataUpdateCoordinator]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=7,
        unique_id="status-entry",
        data={
            "host": "gateway",
            "port": 502,
            "connection_type": "tcp",
            "modules": [MODULE],
            CONF_AUTO_DISCOVERY: False,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


async def _feed(
    hass: HomeAssistant, coordinator: PyTapDataUpdateCoordinator, event: PowerReportEvent
) -> None:
    await hass.async_add_executor_job(coordinator._process_event, event)
    coordinator.async_set_updated_data({**coordinator.data, "nodes": dict(coordinator.data["nodes"])})
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, domain: str, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return hass.states.get(entity_id)


async def test_timeout_fault_and_recovery(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    power = f"{DOMAIN}_{BARCODE}_power"
    energy = f"{DOMAIN}_{BARCODE}_total_energy"
    status = f"{DOMAIN}_{BARCODE}_status_code"
    high = f"{DOMAIN}_{BARCODE}_high_output_mode"
    low = f"{DOMAIN}_{BARCODE}_low_voltage_flag"
    fault = f"{DOMAIN}_{BARCODE}_reading_fault"
    installation_power = f"{DOMAIN}_{entry.entry_id}_installation_power"

    # No report yet this session: live values are unavailable.
    assert _state(hass, "sensor", power).state == STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", high).state == STATE_UNAVAILABLE

    await _feed(hass, coordinator, _event())
    assert float(_state(hass, "sensor", power).state) > 0
    assert _state(hass, "sensor", status).state == "0x64"
    assert _state(hass, "binary_sensor", high).state == STATE_ON
    assert _state(hass, "binary_sensor", low).state == STATE_OFF
    assert _state(hass, "binary_sensor", fault).state == STATE_OFF
    assert _state(hass, "sensor", installation_power).state != STATE_UNAVAILABLE

    # Reports stop (darkness): after the timeout live values go unavailable,
    # without waiting for new data, but energy and the fault entity remain.
    coordinator.data["nodes"][BARCODE]["report_monotonic"] -= REPORT_TIMEOUT_SECONDS + 1
    coordinator._async_check_freshness()
    await hass.async_block_till_done()
    assert coordinator.is_reporting(BARCODE) is False
    for domain, unique_id in (
        ("sensor", power),
        ("sensor", status),
        ("binary_sensor", high),
        ("binary_sensor", low),
        ("sensor", installation_power),
    ):
        assert _state(hass, domain, unique_id).state == STATE_UNAVAILABLE, unique_id
    energy_before = _state(hass, "sensor", energy).state
    assert energy_before != STATE_UNAVAILABLE
    assert _state(hass, "binary_sensor", fault).state == STATE_OFF

    # An impossible reading raises the fault but does not replace the last good
    # measurement, add energy, or count as a fresh report.
    good_voltage = coordinator.data["nodes"][BARCODE]["voltage_in"]
    await _feed(hass, coordinator, _event(**IMPOSSIBLE))
    fault_state = _state(hass, "binary_sensor", fault)
    assert fault_state.state == STATE_ON
    assert fault_state.attributes["fault_reasons"] == ["voltage_out_out_of_range", "voltage_out_above_input"]
    assert fault_state.attributes["last_fault_at"] is not None
    assert coordinator.data["nodes"][BARCODE]["voltage_in"] == good_voltage
    assert coordinator.is_reporting(BARCODE) is False
    assert _state(hass, "sensor", power).state == STATE_UNAVAILABLE
    assert _state(hass, "sensor", energy).state == energy_before

    # The next valid reading clears the fault and restores live values.
    await _feed(hass, coordinator, _event(vin=13.2, vout=9.9, iin=0.03, status=0xE4, high=False, low=True))
    fault_state = _state(hass, "binary_sensor", fault)
    assert fault_state.state == STATE_OFF
    assert fault_state.attributes["fault_reasons"] == []
    assert fault_state.attributes["last_fault_at"] is not None
    assert _state(hass, "sensor", power).state != STATE_UNAVAILABLE
    assert _state(hass, "sensor", status).state == "0xE4"
    assert _state(hass, "binary_sensor", high).state == STATE_OFF
    assert _state(hass, "binary_sensor", low).state == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_fault_persists_but_live_timing_does_not(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass)
    await _feed(hass, coordinator, _event())
    await _feed(hass, coordinator, _event(**IMPOSSIBLE))

    saved = AsyncMock()
    with patch.object(coordinator._store, "async_save", saved):
        await coordinator._async_save_coordinator_state()
    snapshot = saved.call_args.args[0]["node_snapshots"][BARCODE]
    assert snapshot["reading_fault"] is True
    assert snapshot["fault_reasons"] == ["voltage_out_out_of_range", "voltage_out_above_input"]
    assert "report_monotonic" not in snapshot
    assert "status_code" not in snapshot

    assert await hass.config_entries.async_unload(entry.entry_id)
