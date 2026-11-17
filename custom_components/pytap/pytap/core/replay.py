"""Offline, bounded frame inspection and timestamp-aware parser replay."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from .capture import CapturedChunk
from .crc import crc
from .parser import Parser
from .state import PersistentState
from .types import FrameType, PacketType, PowerReport

_UNESCAPE = {0: 0x7E, 1: 0x24, 2: 0x23, 3: 0x25, 4: 0xA4, 5: 0xA3, 6: 0xA5}


def _name(enum: type[FrameType] | type[PacketType], value: int) -> str:
    try:
        return enum(value).name
    except ValueError:
        return "UNKNOWN"


def _pv_packets(payload: bytes) -> dict[str, Any]:
    if len(payload) < 2:
        return {"receive_header_truncated": True}
    flags = int.from_bytes(payload[:2], "big")
    offset = 2 + sum(size for flag, size in ((1, 1), (2, 1), (4, 2), (8, 2)) if not flags & flag)
    offset += 1 if flags & 16 else 2
    offset += 2  # slot counter
    if offset > len(payload):
        return {"receive_header_truncated": True, "status_flags": flags}
    header_end = offset
    packets: list[dict[str, Any]] = []
    while offset < len(payload):
        remaining = payload[offset:]
        if len(remaining) < 7:
            packets.append({"truncated_header": True, "raw_hex": remaining.hex()})
            break
        length = remaining[6]
        data = remaining[7 : 7 + length]
        packet: dict[str, Any] = {
            "packet_type": remaining[0],
            "packet_name": _name(PacketType, remaining[0]),
            "node_address": int.from_bytes(remaining[1:3], "big"),
            "short_address": int.from_bytes(remaining[3:5], "big"),
            "sequence": remaining[5],
            "declared_length": length,
            "header_hex": remaining[:7].hex(),
            "payload_hex": data.hex(),
            "truncated": len(data) != length,
        }
        if remaining[0] == PacketType.POWER_REPORT and len(data) >= 13:
            report = PowerReport.from_bytes(data[:13])
            packet["measurement"] = {
                "voltage_in": report.voltage_in,
                "voltage_out": report.voltage_out,
                "current_in": report.current_in,
                "temperature": report.temperature,
                "duty_cycle": report.duty_cycle,
                "rssi_raw": report.rssi,
                "slot_counter_raw": report.slot_counter.raw,
                "unknown_hex": data[7:10].hex(),
                "extra_hex": data[13:].hex(),
            }
        packets.append(packet)
        if len(data) != length:
            break
        offset += 7 + length
    return {"status_flags": flags, "receive_header_hex": payload[:header_end].hex(), "packets": packets}


def decode_wire_frame(wire: bytes) -> dict[str, Any]:
    """Report unknown types, malformed escapes and CRCs instead of discarding."""
    result: dict[str, Any] = {"kind": "frame", "wire_hex": wire.hex(), "wire_length": len(wire)}
    decoded = bytearray()
    offset = 2
    while offset < len(wire) - 2:
        byte = wire[offset]
        if byte == 0x7E:
            offset += 1
            if offset >= len(wire) - 2 or wire[offset] not in _UNESCAPE:
                return {**result, "framing_error": "unknown_escape", "crc_valid": None}
            byte = _UNESCAPE[wire[offset]]
        decoded.append(byte)
        offset += 1
    result["decoded_hex"] = decoded.hex()
    if len(decoded) < 6:
        return {**result, "framing_error": "runt", "crc_valid": None}
    body = bytes(decoded[:-2])
    address = int.from_bytes(body[:2], "big")
    frame_type = int.from_bytes(body[2:4], "big")
    expected_crc = int.from_bytes(decoded[-2:], "little")
    calculated_crc = crc(body)
    result.update(
        gateway_id=address & 0x7FFF,
        direction="from_gateway" if address & 0x8000 else "to_gateway",
        frame_type=frame_type,
        frame_name=_name(FrameType, frame_type),
        payload_hex=body[4:].hex(),
        crc_expected=expected_crc,
        crc_calculated=calculated_crc,
        crc_valid=calculated_crc == expected_crc,
    )
    # Inspect bad-CRC payloads too, but retain validity so corrupt measurements
    # cannot be confused with trusted telemetry. Wire bytes always remain intact.
    if frame_type == FrameType.RECEIVE_RESPONSE:
        result["receive"] = _pv_packets(body[4:])
    elif frame_type in (FrameType.COMMAND_REQUEST, FrameType.COMMAND_RESPONSE) and len(body[4:]) >= 5:
        payload = body[4:]
        result["command"] = {
            "header_hex": payload[:5].hex(),
            "packet_type": payload[3],
            "packet_name": _name(PacketType, payload[3]),
            "sequence": payload[4],
            "payload_hex": payload[5:].hex(),
        }
    return result


class FrameScanner:
    """Keep framing across reads/segments, never across connections or gaps."""

    def __init__(self, max_frame_bytes: int = 65536) -> None:
        if max_frame_bytes < 6:
            raise ValueError("Inspection buffer is too small")
        self._buffer = bytearray()
        self._offset = 0
        self._next_offset = 0
        self._connection: int | None = None
        self._clock: dict[str, int] = {}
        self._limit = max_frame_bytes

    def _record(self, kind: str, length: int, **details: Any) -> dict[str, Any]:
        wire = bytes(self._buffer[:length])
        result = {
            "kind": kind,
            "stream_offset": self._offset,
            "connection_id": self._connection,
            **self._clock,
            "wire_hex": wire.hex(),
            "wire_length": length,
            **details,
        }
        if kind == "frame":
            result.update(decode_wire_frame(wire))
        del self._buffer[:length]
        self._offset += length
        return result

    def feed(self, chunk: CapturedChunk) -> Iterator[dict[str, Any]]:
        if self._connection is not None and (
            self._connection != chunk.connection_id or self._next_offset != chunk.stream_offset
        ):
            if self._buffer:
                yield self._record("incomplete", len(self._buffer), reason="connection_or_capture_gap")
        if not self._buffer:
            self._offset = chunk.stream_offset
        self._connection = chunk.connection_id
        self._clock = {"utc_ns": chunk.utc_ns, "monotonic_ns": chunk.monotonic_ns}
        self._next_offset = chunk.stream_offset + len(chunk.data)
        self._buffer.extend(chunk.data)
        while self._buffer:
            start = self._buffer.find(b"\x7e\x07")
            if start < 0:
                length = len(self._buffer) - (self._buffer[-1] == 0x7E)
                if length:
                    yield self._record("noise", length)
                break
            if start:
                yield self._record("noise", start)
            end = self._buffer.find(b"\x7e\x08", 2)
            restart = self._buffer.find(b"\x7e\x07", 2)
            if restart >= 0 and (end < 0 or restart < end):
                yield self._record("incomplete", restart, reason="restart_marker")
            elif end >= 0:
                yield self._record("frame", end + 2)
            elif len(self._buffer) > self._limit:
                yield self._record("oversized_fragment", self._limit, reason="inspection_buffer_limit")
            else:
                break

    def finish(self) -> Iterator[dict[str, Any]]:
        if self._buffer:
            yield self._record("incomplete", len(self._buffer), reason="capture_end")


def replay_events(
    chunks: Iterator[CapturedChunk], state: PersistentState | None = None
) -> Iterator[dict[str, Any]]:
    """Use recorded UTC time without sleeping or modifying Home Assistant."""
    observed = datetime.now(UTC)
    persistent = state or PersistentState()
    parser = Parser(persistent_state=persistent, clock=lambda: observed)
    connection = None
    next_offset = 0
    for chunk in chunks:
        observed = datetime.fromtimestamp(chunk.utc_ns / 1_000_000_000, UTC)
        if connection is not None and (
            connection != chunk.connection_id or next_offset != chunk.stream_offset
        ):
            parser = Parser(persistent_state=persistent, clock=lambda: observed)
        connection = chunk.connection_id
        next_offset = chunk.stream_offset + len(chunk.data)
        for event in parser.feed(chunk.data):
            yield {
                "stream_offset": chunk.stream_offset,
                "connection_id": connection,
                "observed_utc_ns": chunk.utc_ns,
                "event": event.to_dict(),
            }
