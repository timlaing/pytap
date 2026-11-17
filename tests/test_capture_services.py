"""HA capture actions, lifecycle, privacy, and capture-before-parser ordering."""

import asyncio
import json
from pathlib import Path
import threading
from unittest.mock import MagicMock, patch

from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError, Unauthorized
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import DOMAIN
from custom_components.pytap.pytap.core.capture import iter_capture, list_captures


@pytest.fixture
async def capture_entry(hass, tmp_path):
    entry = MockConfigEntry(
        domain=DOMAIN, version=7, data={"host": "private-host", "port": 502, "modules": []}
    )
    entry.add_to_hass(hass)

    def no_network(self):
        pass

    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", new=no_network):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # Model an offline/retrying listener while avoiding a network dependency.
    coordinator._stop_event.clear()
    coordinator._capture_root = tmp_path / "captures" / entry.entry_id
    yield entry, coordinator
    await coordinator.async_stop_capture()
    await hass.config_entries.async_unload(entry.entry_id)


async def _call(hass, service, entry, **data):
    return await hass.services.async_call(
        DOMAIN, service, {"config_entry_id": entry.entry_id, **data}, blocking=True, return_response=True
    )


async def test_actions_start_stop_list_export_and_private_metadata(hass, capture_entry):
    entry, coordinator = capture_entry
    assert coordinator.capture_status == {"state": "idle"}
    result = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    assert result["state"] == "capturing"
    coordinator._capture.record(b"all raw bytes\x00\xff", 1)
    result = await _call(hass, "stop_capture", entry)
    assert result["state"] == "stopped" and result["complete"] is True
    assert result["bytes_written"] == 15
    manifest = json.loads((Path(result["directory"]) / "manifest.json").read_text())
    assert "private-host" not in json.dumps(manifest)
    assert manifest["metadata"]["integration_version"] == "1.5.0"
    assert "parser_state" in manifest["metadata"]
    listed = await _call(hass, "list_captures", entry)
    assert listed["captures"][0]["capture_id"] == result["capture_id"]
    exported = await _call(hass, "export_capture", entry)
    assert Path(exported["archive"]).is_file()


async def test_running_export_and_duplicate_start_are_rejected(hass, capture_entry):
    entry, _ = capture_entry
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    for service in ("start_capture", "export_capture"):
        with pytest.raises(ServiceValidationError):
            await _call(hass, service, entry)


async def test_unloaded_or_wrong_entry_cannot_capture(hass):
    from custom_components.pytap import async_setup

    await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "start_capture", {"config_entry_id": "missing"}, blocking=True)


async def test_non_admin_cannot_start_capture(hass, capture_entry, hass_read_only_user):
    entry, coordinator = capture_entry
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "start_capture",
            {"config_entry_id": entry.entry_id},
            context=Context(user_id=hass_read_only_user.id),
            blocking=True,
        )
    assert coordinator.capture_status == {"state": "idle"}


async def test_capture_disabled_after_listener_restart_but_evidence_remains(hass, capture_entry):
    entry, coordinator = capture_entry
    started = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    coordinator._capture.record(b"evidence", 1)
    assert await coordinator.async_stop_listener()
    assert coordinator.capture_status["stop_reason"] == "unload"
    with pytest.raises(ValueError, match="not running"):
        await coordinator.async_start_capture()
    assert list_captures(coordinator._capture_root)[0]["capture_id"] == started["capture_id"]


async def test_read_bytes_are_captured_before_parser_can_discard_them(hass, capture_entry):
    entry, coordinator = capture_entry
    raw = b"\x00noise-and-unknown-invalid-frames\xff"
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    source, parser = MagicMock(), MagicMock()
    parser.infrastructure = {}

    def feed(data):
        assert coordinator.capture_status["bytes_observed"] == len(raw)
        assert data == raw
        return []  # The live parser is free to reject every byte.

    parser.feed.side_effect = feed

    reads = iter([raw, b""])

    def read(size):
        data = next(reads)
        if not data:
            coordinator._stop_event.set()
        return data

    source.read.side_effect = read
    with (
        patch("custom_components.pytap.coordinator.open_source", return_value=source),
        patch("custom_components.pytap.pytap.api.create_parser", return_value=parser),
    ):
        await coordinator._async_listen()
        assert await coordinator.async_stop_listener()
    assert b"".join(c.data for c in iter_capture(coordinator._capture.directory)) == raw
    source.close.assert_called_once()


async def test_capture_shutdown_is_off_loop(hass, capture_entry):
    entry, coordinator = capture_entry
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    stopped = asyncio.create_task(coordinator.async_stop_capture())
    await asyncio.sleep(0)
    assert await stopped
    assert not coordinator._capture.busy


async def test_cancelled_export_keeps_executor_ownership_and_blocks_new_capture(hass, capture_entry):
    entry, coordinator = capture_entry
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    await _call(hass, "stop_capture", entry)
    entered, release = asyncio.Event(), threading.Event()

    def delayed_export(*args, **kwargs):
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return Path("private.zip")

    with patch("custom_components.pytap.coordinator.export_archive", new=delayed_export):
        task = asyncio.create_task(coordinator.async_export_capture())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not coordinator._capture_export_future.done()
        try:
            with pytest.raises(ValueError, match="already running"):
                await coordinator.async_start_capture(duration_minutes=1, max_storage_mib=16)
        finally:
            release.set()
            await coordinator._capture_export_future


async def test_cancelled_capture_start_cannot_leave_a_late_writer_running(hass, capture_entry):
    _, coordinator = capture_entry
    from custom_components.pytap.pytap.core.capture import CaptureWriter

    entered, release = asyncio.Event(), threading.Event()
    original = CaptureWriter.start

    def delayed_start(writer):
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        original(writer)

    with patch.object(CaptureWriter, "start", new=delayed_start):
        task = asyncio.create_task(coordinator.async_start_capture(1, 16))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        stopped = await coordinator.async_stop_capture()
    assert stopped["state"] == "stopped"
    assert stopped["stop_reason"] == "start_cancelled"
    assert not coordinator._capture.busy


async def test_unload_deadline_includes_capture_lock_wait(hass, capture_entry):
    _, coordinator = capture_entry
    await coordinator._capture_lock.acquire()
    try:
        with patch("custom_components.pytap.coordinator.LISTENER_STOP_TIMEOUT", 0.01):
            assert await coordinator.async_stop_listener() is False
        assert coordinator._worker_future is not None  # Retained, not discarded.
    finally:
        coordinator._capture_lock.release()
    assert await coordinator.async_stop_listener()


async def test_bootstrap_is_snapshotted_in_listener_at_first_admitted_read(hass, capture_entry):
    entry, coordinator = capture_entry
    from custom_components.pytap.pytap.core.types import LongAddress

    old = LongAddress.from_str("00:00:00:00:00:00:00:01")
    new = LongAddress.from_str("00:00:00:00:00:00:00:02")
    coordinator._persistent_state.gateway_node_tables = {1: {2: old}}
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    # A reassignment occurs after async preparation but before the first read.
    coordinator._persistent_state.gateway_node_tables[1][2] = new
    raw = b"first captured data"
    source, parser = MagicMock(), MagicMock()
    parser.infrastructure = {}
    parser.feed.return_value = []
    reads = iter([raw, b""])

    def read(size):
        data = next(reads)
        if not data:
            coordinator._stop_event.set()
        return data

    source.read.side_effect = read
    with (
        patch("custom_components.pytap.coordinator.open_source", return_value=source),
        patch("custom_components.pytap.pytap.api.create_parser", return_value=parser),
    ):
        await coordinator._async_listen()
        await coordinator.async_stop_listener()
    manifest = json.loads((coordinator._capture.directory / "manifest.json").read_text())
    assert manifest["metadata"]["parser_state"]["gateway_node_tables"]["1"]["2"] == str(new)
    assert manifest["metadata"]["parser_state_at_capture_boundary"] is True


async def test_capture_marker_failure_cannot_prevent_port_release(hass, capture_entry):
    _, coordinator = capture_entry
    source = MagicMock()

    def read(size):
        coordinator._stop_event.set()
        return b""

    source.read.side_effect = read
    recorder = MagicMock()

    def mark(event, *args, **kwargs):
        if event == "connection_closed":
            raise ValueError("marker failure")

    recorder.mark.side_effect = mark
    with (
        patch.object(coordinator, "_capture", recorder),
        patch("custom_components.pytap.coordinator.open_source", return_value=source),
    ):
        with pytest.raises(ValueError, match="marker failure"):
            await coordinator._async_listen()
    source.close.assert_called_once()
    assert coordinator._source is None
