"""Bounded, opt-in byte recording. No Home Assistant or serial dependencies.

Chunk indexes use little-endian <QQQII: UTC nanoseconds, monotonic nanoseconds,
logical stream offset, byte length, connection generation. Raw data is never
decoded or filtered on the producer thread.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import struct
import tempfile
import threading
import time
from typing import Any, BinaryIO
import uuid
import warnings
import zipfile

INDEX = struct.Struct("<QQQII")
MAX_RECORD_BYTES = 65536
FORMAT_VERSION = 1
MANIFEST_RESERVE = 1024 * 1024
FREE_SPACE_RESERVE = 64 * 1024 * 1024
CAPTURE_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z_[a-f0-9]{12}$")
_SEGMENT_PATTERN = re.compile(r"^bus-\d{6}\.bin$")


def _private_file(path: Path) -> BinaryIO:
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    if len(data) > MANIFEST_RESERVE:
        raise ValueError("Capture manifest exceeds reserved storage")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".manifest-", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _tree_size(root: Path) -> int:
    """Count allocated evidence, without following links or removing anything."""
    return sum(p.stat().st_size for p in root.rglob("*") if not p.is_symlink() and p.is_file())


@dataclass(frozen=True)
class CapturedChunk:
    utc_ns: int
    monotonic_ns: int
    stream_offset: int
    connection_id: int
    data: bytes
    declared_length: int | None = None


class CaptureWriter:
    """One session, one writer. Producers only enqueue or record explicit loss."""

    def __init__(
        self,
        root: Path,
        metadata: Mapping[str, Any],
        *,
        duration_seconds: float = 86400,
        max_storage_bytes: int = 1024 * 1024 * 1024,
        segment_bytes: int = 16 * 1024 * 1024,
        queue_records: int = 2048,
    ) -> None:
        if (
            duration_seconds <= 0
            or max_storage_bytes <= MANIFEST_RESERVE
            or segment_bytes < 1
            or queue_records < 1
        ):
            raise ValueError("Invalid capture limits")
        self.root = Path(root)
        self.capture_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:12]
        self.directory = self.root / self.capture_id
        self.metadata = dict(metadata)
        self.duration_seconds = duration_seconds
        self.max_storage_bytes = max_storage_bytes
        self.segment_bytes = segment_bytes
        self._queue: queue.Queue[CapturedChunk | dict[str, Any]] = queue.Queue(queue_records)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._bootstrap_done = False
        self._state = "idle"
        self._reason: str | None = None
        self._error: str | None = None
        self._started_ns: int | None = None
        self._ended_ns: int | None = None
        self._deadline = 0.0
        self._observed_bytes = 0
        self._written_bytes = 0
        self._written_records = 0
        self._dropped_bytes = 0
        self._dropped_records = 0
        self._used_bytes = 0
        self._session_budget = 0
        self._segments: list[dict[str, Any]] = []
        self._raw: BinaryIO | None = None
        self._index: BinaryIO | None = None
        self._events: BinaryIO | None = None
        self._raw_hash = hashlib.sha256()
        self._index_hash = hashlib.sha256()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "capture_id": self.capture_id,
                "directory": str(self.directory),
                "state": self._state,
                "stop_reason": self._reason,
                "error": self._error,
                "started_utc_ns": self._started_ns,
                "ended_utc_ns": self._ended_ns,
                "bytes_observed": self._observed_bytes,
                "bytes_written": self._written_bytes,
                "records_written": self._written_records,
                "dropped_bytes": self._dropped_bytes,
                "dropped_records": self._dropped_records,
                "complete": self._state == "stopped"
                and self._started_ns is not None
                and not self._dropped_records
                and self._error is None,
                "duration_seconds": self.duration_seconds,
                "max_storage_bytes": self.max_storage_bytes,
                "session_budget_bytes": self._session_budget,
                "storage_bytes_used": self._used_bytes,
                "queue_depth": self._queue.qsize(),
            }

    def start(self) -> None:
        """Prepare private files and start the writer; call off an event loop."""
        with self._lock:
            if self._state != "idle":
                raise ValueError("Capture has already started")
            self._state = "starting"
        try:
            if self.root.is_symlink():
                raise ValueError("Capture root must not be a symlink")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._session_budget = min(
                self.max_storage_bytes - _tree_size(self.root),
                shutil.disk_usage(self.root).free - FREE_SPACE_RESERVE,
            )
            if self._session_budget <= MANIFEST_RESERVE:
                raise OSError("Capture storage budget exhausted")
            self.directory.mkdir(mode=0o700)
            self._events = _private_file(self.directory / "events.jsonl")
            self._open_segment()
            with self._lock:
                if not self._stop.is_set():
                    self._started_ns = time.time_ns()
                    self._deadline = time.monotonic() + self.duration_seconds
                    self._state = "capturing"
            self._write_manifest()
            # The coordinator publishes this object before start(). Only admit
            # reads after all files/metadata are ready, avoiding a startup gap.
            with self._lock:
                cancelled = self._stop.is_set()
                self._accepting = not cancelled
            if cancelled:
                self._close_files()
                with self._lock:
                    if self._state != "error":
                        self._state = "stopped"
                self._try_manifest()
                return
            self._thread = threading.Thread(target=self._run, name="pytap_capture", daemon=True)
            self._thread.start()
        except BaseException as err:
            self._fail(err, "start_failed")
            self._close_files()
            self._try_manifest()
            raise

    def _stop_locked(self, reason: str) -> None:
        self._accepting = False
        if self._reason is None:
            self._reason = reason
            self._ended_ns = time.time_ns()
        if self._state != "error":
            self._state = "stopping"
        self._stop.set()

    def request_stop(self, reason: str = "user") -> None:
        with self._lock:
            if self._state == "idle":
                # An async caller can cancel after publishing the writer but
                # before its executor starts preparation. Latch that stop.
                if self._reason is None:
                    self._reason = reason
                    self._ended_ns = time.time_ns()
                self._stop.set()
            elif self._state in ("capturing", "starting", "stopping"):
                self._stop_locked(reason)

    def join(self, timeout: float = 10) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.busy

    def record(
        self, data: bytes, connection_id: int, bootstrap: Callable[[], Mapping[str, Any]] | None = None
    ) -> None:
        """Copy/queue bytes before parsing. Never wait on the writer or disk."""
        with self._lock:
            if not self._accepting or not data:
                return
            if time.monotonic() >= self._deadline:
                self._stop_locked("duration_limit")
                return
            if not self._bootstrap_done and bootstrap is not None:
                try:
                    # This callback runs in the sole listener before parsing
                    # the first admitted read, not during async file preparation.
                    self.metadata["parser_state"] = dict(bootstrap())
                    self.metadata["parser_state_at_capture_boundary"] = True
                    self._bootstrap_done = True
                except Exception as err:
                    self._error = type(err).__name__
                    self._dropped_records += 1
                    self._dropped_bytes += len(data)
                    self._observed_bytes += len(data)
                    self._stop_locked("bootstrap_error")
                    self._state = "error"
                    return
            chunk = CapturedChunk(
                time.time_ns(), time.monotonic_ns(), self._observed_bytes, connection_id, bytes(data)
            )
            self._observed_bytes += len(data)
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                self._dropped_records += 1
                self._dropped_bytes += len(data)
                self._stop_locked("queue_overflow")

    def mark(self, event: str, connection_id: int, **details: str | bool) -> None:
        """Connection boundaries, without endpoints or backend exception text."""
        with self._lock:
            if not self._accepting:
                return
            record = {
                "event": event,
                "utc_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "connection_id": connection_id,
                **details,
            }
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._dropped_records += 1
                self._stop_locked("queue_overflow")

    def _fail(self, err: BaseException, reason: str) -> None:
        with self._lock:
            self._error = type(err).__name__  # Messages may contain private data.
            self._stop_locked(reason)
            self._state = "error"

    def _open_segment(self) -> None:
        number = len(self._segments)
        raw_name, index_name = f"bus-{number:06d}.bin", f"chunks-{number:06d}.bin"
        self._raw = _private_file(self.directory / raw_name)
        self._index = _private_file(self.directory / index_name)
        self._raw_hash, self._index_hash = hashlib.sha256(), hashlib.sha256()
        self._segments.append({"file": raw_name, "index": index_name, "bytes": 0, "records": 0})

    def _close_segment(self) -> None:
        error = None
        for handle in (self._raw, self._index):
            if handle is not None:
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError as err:
                    error = err
                finally:
                    try:
                        handle.close()
                    except OSError as err:
                        error = err
        self._raw = self._index = None
        if self._segments:
            self._segments[-1].update(
                sha256=self._raw_hash.hexdigest(), index_sha256=self._index_hash.hexdigest()
            )
        if error is not None:
            raise error

    def _close_files(self) -> None:
        error = None
        try:
            self._close_segment()
        except OSError as err:
            error = err
        if self._events is not None:
            try:
                self._events.flush()
                os.fsync(self._events.fileno())
            except OSError as err:
                error = err
            finally:
                try:
                    self._events.close()
                except OSError as err:
                    error = err
                self._events = None
        if error is not None:
            self._fail(error, "disk_error")

    def _budget(self, required: int) -> bool:
        if self._used_bytes + required + MANIFEST_RESERVE <= self._session_budget:
            return True
        self.request_stop("storage_limit")
        return False

    def _drop(self, item: CapturedChunk | dict[str, Any], byte_count: int | None = None) -> None:
        with self._lock:
            self._dropped_records += 1
            if isinstance(item, CapturedChunk):
                self._dropped_bytes += len(item.data) if byte_count is None else byte_count

    def _write_chunk(self, chunk: CapturedChunk) -> bool:
        assert self._raw is not None and self._index is not None
        remaining, space, extra_records = len(chunk.data), self.segment_bytes - self._segments[-1]["bytes"], 0
        while remaining:
            space = space or self.segment_bytes
            length = min(remaining, space, MAX_RECORD_BYTES)
            remaining -= length
            space -= length
            extra_records += 1
        if not self._budget(len(chunk.data) + INDEX.size * extra_records):
            self._drop(chunk)
            return False
        consumed = 0
        while consumed < len(chunk.data):
            segment = self._segments[-1]
            if segment["bytes"] == self.segment_bytes:
                self._close_segment()
                self._open_segment()
                segment = self._segments[-1]
            length = min(len(chunk.data) - consumed, self.segment_bytes - segment["bytes"], MAX_RECORD_BYTES)
            data = chunk.data[consumed : consumed + length]
            index = INDEX.pack(
                chunk.utc_ns, chunk.monotonic_ns, chunk.stream_offset + consumed, length, chunk.connection_id
            )
            self._raw.write(data)
            self._index.write(index)
            self._raw_hash.update(data)
            self._index_hash.update(index)
            segment["bytes"] += length
            segment["records"] += 1
            consumed += length
            self._used_bytes += length + INDEX.size
            with self._lock:
                self._written_bytes += length
                self._written_records += 1
        return True

    def _write_manifest(self) -> None:
        with self._lock:
            metadata = dict(self.metadata)
        segments = [dict(segment) for segment in self._segments]
        if segments:
            segments[-1].update(sha256=self._raw_hash.hexdigest(), index_sha256=self._index_hash.hexdigest())
        _atomic_json(
            self.directory / "manifest.json",
            {
                **self.status(),
                "format_version": FORMAT_VERSION,
                "index_format": INDEX.format,
                "metadata": metadata,
                "segments": segments,
            },
        )

    def _try_manifest(self) -> None:
        try:
            if self.directory.is_dir():
                self._write_manifest()
        except (OSError, ValueError) as err:
            self._fail(err, "disk_error")

    def _run(self) -> None:
        last_flush = time.monotonic()
        current = None
        written_before = self._written_bytes
        try:
            while True:
                if time.monotonic() >= self._deadline:
                    self.request_stop("duration_limit")
                if self._stop.is_set() and self._queue.empty():
                    break
                try:
                    current = self._queue.get(timeout=0.25)
                except queue.Empty:
                    current = None
                if isinstance(current, CapturedChunk):
                    written_before = self._written_bytes
                    if not self._write_chunk(current):
                        current = None
                        break
                elif current is not None:
                    data = (json.dumps(current, separators=(",", ":")) + "\n").encode()
                    if not self._budget(len(data)):
                        self._drop(current)
                        current = None
                        break
                    assert self._events is not None
                    self._events.write(data)
                    self._used_bytes += len(data)
                current = None
                if time.monotonic() - last_flush >= 1:
                    for handle in (self._raw, self._index, self._events):
                        if handle is not None:
                            handle.flush()
                    self._write_manifest()
                    last_flush = time.monotonic()
                    if shutil.disk_usage(self.directory).free < FREE_SPACE_RESERVE // 2:
                        self.request_stop("disk_space_low")
                        break
        except BaseException as err:
            if current is not None:
                remaining = (
                    max(0, len(current.data) - (self._written_bytes - written_before))
                    if isinstance(current, CapturedChunk)
                    else None
                )
                self._drop(current, remaining)
            self._fail(err, "disk_error")
        finally:
            while True:
                try:
                    self._drop(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._close_files()
            with self._lock:
                self._accepting = False
                if self._state != "error":
                    self._state = "stopped"
                self._ended_ns = self._ended_ns or time.time_ns()
            self._try_manifest()


def capture_directory(root: Path, capture_id: str) -> Path:
    """Resolve a generated session ID, never an arbitrary caller-supplied path."""
    if not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise ValueError("Invalid capture ID")
    directory = root / capture_id
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Capture directory is unavailable")
    return directory


def read_manifest(directory: Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    path = directory / "manifest.json"
    if path.is_symlink():
        raise ValueError("Capture manifest must not be a symlink")
    if path.stat().st_size > MANIFEST_RESERVE:
        raise ValueError("Capture manifest is too large")
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Invalid capture manifest")
    if manifest.get("format_version") != FORMAT_VERSION or manifest.get("index_format") != INDEX.format:
        raise ValueError("Unsupported capture format")
    if not allow_incomplete and (manifest.get("state") != "stopped" or not manifest.get("complete")):
        raise ValueError("Capture is incomplete; explicitly allow incomplete evidence to inspect it")
    return manifest


def _segment_paths(directory: Path, segment: Mapping[str, Any]) -> tuple[Path, Path]:
    name = segment.get("file", "")
    if not isinstance(name, str) or not _SEGMENT_PATTERN.fullmatch(name):
        raise ValueError("Unsafe segment filename")
    expected_index = name.replace("bus-", "chunks-")
    if segment.get("index") != expected_index:
        raise ValueError("Invalid segment index filename")
    paths = directory / name, directory / expected_index
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("Capture segments must be regular files")
    return paths


def iter_capture(directory: Path, *, allow_incomplete: bool = False) -> Iterator[CapturedChunk]:
    """Validate and stream a stopped capture. No packet type is filtered."""
    manifest = read_manifest(directory, allow_incomplete=allow_incomplete)
    strict = not allow_incomplete or bool(manifest["complete"])
    previous_offset = 0
    total_bytes = 0
    for segment in manifest["segments"]:
        raw_path, index_path = _segment_paths(directory, segment)
        for path, count, digest in (
            (raw_path, segment["bytes"], segment.get("sha256")),
            (index_path, segment["records"] * INDEX.size, segment.get("index_sha256")),
        ):
            if path.stat().st_size != count:
                if strict:
                    raise ValueError("Capture size mismatch (possibly an interrupted write)")
                warnings.warn("Incomplete evidence has stale file sizes", RuntimeWarning, stacklevel=2)
            with path.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
                    if strict:
                        raise ValueError("Capture checksum mismatch")
                    warnings.warn(
                        "Incomplete evidence has unverified checksums", RuntimeWarning, stacklevel=2
                    )
        with raw_path.open("rb") as raw, index_path.open("rb") as index:
            while record := index.read(INDEX.size):
                if len(record) != INDEX.size:
                    if strict:
                        raise ValueError("Truncated chunk index")
                    warnings.warn(
                        "Incomplete evidence ends with a partial index row", RuntimeWarning, stacklevel=2
                    )
                    break
                utc_ns, mono_ns, offset, length, connection = INDEX.unpack(record)
                if not 0 < length <= MAX_RECORD_BYTES or offset < previous_offset:
                    raise ValueError("Invalid chunk ordering or length")
                data = raw.read(length)
                if len(data) != length:
                    if strict:
                        raise ValueError("Truncated capture data")
                    warnings.warn(
                        "Incomplete evidence ends with a partial data chunk", RuntimeWarning, stacklevel=2
                    )
                previous_offset = offset + len(data)
                total_bytes += len(data)
                if data:
                    yield CapturedChunk(utc_ns, mono_ns, offset, connection, data, length)
                if len(data) != length:
                    break
            if raw.read(1):
                if strict:
                    raise ValueError("Unindexed capture bytes")
                warnings.warn(
                    "Unindexed tail retained in raw file, not replayed", RuntimeWarning, stacklevel=2
                )
    if total_bytes != manifest["bytes_written"]:
        if strict:
            raise ValueError("Capture byte count mismatch")
        warnings.warn("Incomplete evidence has a stale byte count", RuntimeWarning, stacklevel=2)


def list_captures(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    result = []
    for path in sorted(root.iterdir(), reverse=True):
        if path.is_symlink() or not path.is_dir() or not CAPTURE_ID_PATTERN.fullmatch(path.name):
            continue
        try:
            manifest = read_manifest(path, allow_incomplete=True)
            result.append(
                {
                    key: manifest.get(key)
                    for key in (
                        "capture_id",
                        "state",
                        "stop_reason",
                        "complete",
                        "bytes_written",
                        "dropped_bytes",
                        "dropped_records",
                        "started_utc_ns",
                        "ended_utc_ns",
                    )
                }
            )
        except (OSError, ValueError):
            result.append({"capture_id": path.name, "state": "invalid_manifest", "complete": False})
        if len(result) >= limit:
            break
    return result


def export_archive(directory: Path, *, allow_interrupted: bool = False) -> Path:
    """Archive stopped evidence, including incomplete captures with clear flags."""
    manifest = read_manifest(directory, allow_incomplete=True)
    if manifest["state"] not in ("stopped", "error") and not allow_interrupted:
        raise ValueError("Stop capture before exporting")
    # The HA controller rejects a live writer. A stale 'capturing' manifest
    # after a crash is incomplete evidence and must still be recoverable.
    # Validate every byte; incomplete evidence is explicitly marked/warned and
    # preserved verbatim, never "repaired" or declared complete.
    for _ in iter_capture(directory, allow_incomplete=True):
        pass
    paths = [directory / "manifest.json", directory / "events.jsonl"]
    for segment in manifest["segments"]:
        paths.extend(_segment_paths(directory, segment))
    unverified = []
    if not manifest["complete"]:
        listed = {path.name for path in paths}
        for path in sorted(directory.iterdir()):
            if re.fullmatch(r"(?:bus|chunks)-\d{6}\.bin", path.name) and path.name not in listed:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Unsafe unlisted segment")
                paths.append(path)
                unverified.append(path.name)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("Unsafe archive input")
    if shutil.disk_usage(directory).free < sum(path.stat().st_size for path in paths) + FREE_SPACE_RESERVE:
        raise OSError("Insufficient export space")
    output = directory.parent / f"{directory.name}-{uuid.uuid4().hex[:8]}.zip"
    try:
        with (
            _private_file(output) as handle,
            zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive,
        ):
            for path in paths:
                archive.write(path, arcname=f"{directory.name}/{path.name}")
            if unverified:
                archive.writestr(
                    f"{directory.name}/recovery.json",
                    json.dumps(
                        {
                            "complete": False,
                            "unverified_files": unverified,
                            "note": "Unlisted crash segments preserved verbatim; "
                            "unverified and not timestamp-replayed",
                        }
                    ),
                )
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output
