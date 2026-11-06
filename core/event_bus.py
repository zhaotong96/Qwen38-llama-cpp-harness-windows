"""Event-driven file bus on ``.agent/inbox/`` (Phase 12A).

Every external reality event (operator interrupt, cron trigger, filesystem
watchdog, window focus change) is persisted as a lightweight JSON envelope so
the resident daemon can consume it after a crash or restart — the bus is the
filesystem, never a hidden in-memory queue.

Envelope contract:
- Path:    ``.agent/inbox/<timestamp>_<priority>_<event_id>.json``
- Timestamp: 14+ digit local time ``YYYYMMDD_HHMMSS_ffffff``
- Priority: single digit ``0``..``9`` (0 = emergency, 9 = idle maintenance)
- Event id: 8 hex chars unique per emission

The daemon's single-flight arbitration uses an O_EXCL lock file
(``.agent/daemon_flight.lock``): at most one task dispatch runs at any time,
in this process or any other. Legacy operator messages (``{"message": ...}``)
written by CLI/API share the directory untouched; event envelopes are
distinguishable by their ``event_id``/``event_type`` fields.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import threading
import time
from typing import Any

from core import persistence

INBOX_DIRNAME = "inbox"
PROCESSED_DIRNAME = "processed"
FLIGHT_LOCK_FILENAME = "daemon_flight.lock"

DEFAULT_PRIORITY = 3
MIN_PRIORITY = 0
MAX_PRIORITY = 9

_BUS_LOCK = threading.Lock()

_ENVELOPE_FILENAME_SUFFIX = ".json"


def event_inbox_dir() -> Path:
    """Resolve the event inbox directory under the active .agent state root."""
    return persistence.TASKS_ROOT.parent / INBOX_DIRNAME


def event_processed_dir() -> Path:
    """Resolve the processed-archive directory (audit trail of consumed events)."""
    return event_inbox_dir() / PROCESSED_DIRNAME


def _event_id(event_type: str, payload: dict[str, Any], now: datetime) -> str:
    raw = f"{event_type}|{now.isoformat(timespec='microseconds')}|{json.dumps(payload, sort_keys=True, default=str)}"
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:8]


def _filename_timestamp(now: datetime) -> str:
    return now.strftime("%Y%m%d_%H%M%S_%f")


def _scan_inbox_files() -> list[Path]:
    inbox = event_inbox_dir()
    if not inbox.is_dir():
        return []
    try:
        return sorted(p for p in inbox.glob(f"*{_ENVELOPE_FILENAME_SUFFIX}") if p.is_file())
    except OSError:
        return []


def _read_envelope(path: Path) -> dict[str, Any] | None:
    """Parse one envelope file; None when unreadable or not an event envelope."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if "event_id" not in data or "event_type" not in data:
        return None
    data["_filename"] = path.name
    return data


def _dequeue_key(meta: dict[str, Any]) -> tuple[int, str, str]:
    """Total order: priority ascending, then insertion timestamp, then id."""
    filename = str(meta.get("_filename") or "")
    stem = filename.removesuffix(_ENVELOPE_FILENAME_SUFFIX)
    # Stem layout: <YYYYMMDD_HHMMSS_ffffff>_<priority>_<event_id>. rsplit(…, 2)
    # peels off the priority digit and event id, leaving the insertion ts.
    parts = stem.rsplit("_", 2)
    ts = parts[0] if len(parts) == 3 else str(meta.get("created_at") or "")
    return (int(meta.get("priority", DEFAULT_PRIORITY)), ts, str(meta.get("event_id", "")))


def emit_event(
    event_type: str,
    payload: dict[str, Any],
    priority: int = DEFAULT_PRIORITY,
    debounce_key: str | None = None,
    debounce_window_seconds: float = 1.0,
    source: str = "system",
) -> str:
    """Persist one event envelope atomically; returns the event id.

    When ``debounce_key`` matches an envelope emitted within
    ``debounce_window_seconds``, the existing envelope is merged/updated in
    place instead of creating a duplicate file (high-frequency coalescing).
    """
    priority = max(MIN_PRIORITY, min(MAX_PRIORITY, int(priority)))
    now = datetime.now()
    payload = dict(payload or {})

    with _BUS_LOCK:
        inbox = event_inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)

        if debounce_key:
            merged_into = _merge_debounced_event(
                str(debounce_key), payload, now, float(debounce_window_seconds)
            )
            if merged_into is not None:
                return merged_into

        event_id = _event_id(str(event_type), payload, now)
        envelope: dict[str, Any] = {
            "event_id": event_id,
            "priority": priority,
            "event_type": str(event_type),
            "source": str(source or "system"),
            "created_at": now.isoformat(timespec="microseconds"),
            "debounce_key": str(debounce_key) if debounce_key else None,
            "payload": payload,
        }
        filename = f"{_filename_timestamp(now)}_{priority}_{event_id}.json"
        persistence.atomic_write_text(
            inbox / filename,
            json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        )
        return event_id


def _merge_debounced_event(
    debounce_key: str,
    payload: dict[str, Any],
    now: datetime,
    window_seconds: float,
) -> str | None:
    """Fold a repeat emission into the live debounced envelope; None when expired."""
    for path in _scan_inbox_files():
        data = _read_envelope(path)
        if data is None or data.get("debounce_key") != debounce_key:
            continue
        try:
            first_at = datetime.fromisoformat(str(data.get("created_at")))
        except Exception:
            continue
        if (now - first_at).total_seconds() > window_seconds:
            continue
        merged_payload = dict(data.get("payload") or {})
        merged_payload.update(payload)
        data["payload"] = merged_payload
        data["updated_at"] = now.isoformat(timespec="microseconds")
        data["merge_count"] = int(data.get("merge_count") or 0) + 1
        persistence.atomic_write_text(
            path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )
        return str(data.get("event_id"))
    return None


def peek_highest_priority_event() -> dict[str, Any] | None:
    """Return the highest-priority event metadata without removing it."""
    best: tuple[tuple[int, str, str], dict[str, Any]] | None = None
    with _BUS_LOCK:
        for path in _scan_inbox_files():
            meta = _read_envelope(path)
            if meta is None:
                continue
            key = _dequeue_key(meta)
            if best is None or key < best[0]:
                best = (key, meta)
    return best[1] if best else None


def pop_highest_priority_event() -> dict[str, Any] | None:
    """Atomically dequeue the highest-priority event into the processed archive."""
    with _BUS_LOCK:
        best: tuple[tuple[int, str, str], Path, dict[str, Any]] | None = None
        for path in _scan_inbox_files():
            meta = _read_envelope(path)
            if meta is None:
                continue
            key = _dequeue_key(meta)
            if best is None or key < best[0]:
                best = (key, path, meta)
        if best is None:
            return None
        _, src_path, meta = best
        processed = event_processed_dir()
        processed.mkdir(parents=True, exist_ok=True)
        dest = processed / src_path.name
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        if not _atomic_replace_retry(src_path, dest):
            return None
        return meta


def _atomic_replace_retry(src: Path, dst: Path, max_attempts: int = 4) -> bool:
    """Replace src into dst with exponential backoff for Windows NTFS lock contention."""
    backoffs = (0.010, 0.030, 0.080, 0.200)
    for attempt in range(max_attempts):
        try:
            src.replace(dst)
            return True
        except OSError as err:
            winerror = getattr(err, "winerror", None)
            # WinError 5 (access denied), 32 (sharing violation), 33 (lock violation)
            if winerror in (5, 32, 33) or isinstance(err, PermissionError):
                if attempt < max_attempts - 1:
                    delay = backoffs[attempt] + random.uniform(0.002, 0.010)
                    time.sleep(delay)
                    continue
            return False
    return False


def purge_stale_events(max_age_seconds: float = 86400) -> int:
    """Delete processed-archive envelopes older than ``max_age_seconds``."""
    processed = event_processed_dir()
    if not processed.is_dir():
        return 0
    cutoff = time.time() - max(0.0, float(max_age_seconds))
    purged = 0
    for path in processed.glob(f"*{_ENVELOPE_FILENAME_SUFFIX}"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                purged += 1
        except OSError:
            continue
    return purged


def is_event_envelope_data(data: Any) -> bool:
    """True when a parsed inbox JSON object is a Phase 12A event envelope."""
    return isinstance(data, dict) and "event_id" in data and "event_type" in data


# --- single-flight arbitration --------------------------------------------

def flight_lock_path() -> Path:
    return persistence.TASKS_ROOT.parent / FLIGHT_LOCK_FILENAME


def acquire_flight_slot(marker: str) -> bool:
    """Atomically claim the daemon single-flight slot (O_EXCL create).

    Returns False while any other daemon/task dispatch owns the slot, so no
    second task can ever start concurrently (single-flight red line).
    """
    path = flight_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, (str(marker) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return True


def flight_slot_held() -> bool:
    return flight_lock_path().is_file()


def touch_flight_slot(marker: str) -> bool:
    """Refresh the daemon flight lock mtime (heartbeat) during long-horizon tasks."""
    path = flight_lock_path()
    if not path.is_file():
        return False
    try:
        owner = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if owner != str(marker):
        return False
    try:
        os.utime(str(path), None)
        return True
    except OSError:
        return False


def release_flight_slot(marker: str) -> bool:
    """Release the flight slot only when the owning marker matches."""
    path = flight_lock_path()
    if not path.is_file():
        return False
    try:
        owner = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if owner != str(marker):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def clear_stale_flight_lock() -> bool:
    """Operator-level recovery: unconditionally drop a leftover flight lock."""
    path = flight_lock_path()
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def break_stale_flight_lock(max_age_seconds: float = 21600.0) -> bool:
    """Break the flight lock only when it is provably older than the budget.

    Active resident daemons call ``touch_flight_slot`` on every loop iteration,
    refreshing its mtime so a live daemon is NEVER misidentified as a stale lock.
    """
    path = flight_lock_path()
    if not path.is_file():
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age < max(0.0, float(max_age_seconds)):
        return False
    return clear_stale_flight_lock()
