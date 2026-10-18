"""Explicit File-Backed Unified Message Queue (.agent/queue/).

Provides dual-control (Web UI + HTTP API + CLI) visibility, hold/resume control,
in-place content & priority modification, cancellation, and crash recovery.
Every queued message is an explicit JSON file on disk under ``.agent/queue/``.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import threading
import uuid
from typing import Any

from core import persistence


_queue_lock = threading.Lock()
_seq_counter = 100


def queue_dir() -> Path:
    return persistence.TASKS_ROOT.parent / "queue"


def queue_processed_dir() -> Path:
    return persistence.TASKS_ROOT.parent / "queue_processed"


def _next_seq() -> int:
    global _seq_counter
    with _queue_lock:
        _seq_counter += 1
        return _seq_counter


def _format_filename(priority: int, created_iso: str, item_id: str) -> str:
    try:
        dt = datetime.fromisoformat(created_iso)
        ts_str = dt.strftime("%Y%m%d_%H%M%S_%f")
    except Exception:
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{int(priority):02d}_{ts_str}_{item_id}.json"


def _find_item_file(item_id: str) -> Path | None:
    qdir = queue_dir()
    if not qdir.is_dir():
        return None
    for p in qdir.glob(f"*_{item_id}.json"):
        if p.is_file():
            return p
    for p in qdir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("id") == item_id:
                return p
        except Exception:
            continue
    return None


def enqueue(
    content: str,
    priority: int = 50,
    source: str = "api",
    author: str = "operator",
    mode: str = "steer_or_next_turn",
    target_branch: str = "main",
    status: str = "queued",
    item_id: str | None = None,
) -> dict[str, Any]:
    """Create a new explicit queue message file in .agent/queue/."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("queue message content must be a non-empty string")
    if status not in ("queued", "held"):
        raise ValueError(f"invalid initial queue status: {status!r}")

    qdir = queue_dir()
    qdir.mkdir(parents=True, exist_ok=True)

    short_id = item_id or f"q{uuid.uuid4().hex[:6]}"
    now = datetime.now().isoformat(timespec="microseconds")
    prio = max(1, min(999, int(priority)))
    seq = _next_seq()

    payload: dict[str, Any] = {
        "id": short_id,
        "seq": seq,
        "priority": prio,
        "source": str(source or "api"),
        "author": str(author or "operator"),
        "mode": str(mode or "steer_or_next_turn"),
        "target_branch": str(target_branch or "main"),
        "content": content.strip(),
        "status": status,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "audit_log": [
            {
                "at": now,
                "by": str(source or "api"),
                "action": "enqueue",
            }
        ],
    }

    filename = _format_filename(prio, now, short_id)
    file_path = qdir / filename
    persistence.atomic_write_text(
        file_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return payload


def list_queue(
    include_held: bool = True,
    target_branch: str | None = None,
) -> list[dict[str, Any]]:
    """List all active messages in .agent/queue/ ordered by priority then timestamp."""
    qdir = queue_dir()
    if not qdir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for p in sorted(qdir.glob("*.json")):
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            status = data.get("status", "queued")
            if not include_held and status == "held":
                continue
            if target_branch and data.get("target_branch") not in (None, "", target_branch):
                continue
            items.append(data)
        except Exception:
            continue

    items.sort(
        key=lambda x: (
            int(x.get("priority", 50)),
            str(x.get("created_at", "")),
            int(x.get("seq", 0)),
        )
    )
    return items


def get_item(item_id: str) -> dict[str, Any] | None:
    """Return a single queued item by ID, or None if not found."""
    p = _find_item_file(item_id)
    if p is None or not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def modify_item(
    item_id: str,
    content: str | None = None,
    priority: int | None = None,
    status: str | None = None,
    mode: str | None = None,
    target_branch: str | None = None,
    by: str = "operator",
) -> dict[str, Any] | None:
    """Modify a queued message's content, priority, or status (queued <-> held)."""
    with _queue_lock:
        p = _find_item_file(item_id)
        if p is None or not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

        if data.get("status") not in ("queued", "held"):
            raise ValueError(f"cannot modify item in status {data.get('status')!r}")

        actions: list[str] = []
        if content is not None:
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content must be a non-empty string")
            data["content"] = content.strip()
            actions.append("edit_content")

        old_priority = int(data.get("priority", 50))
        new_priority = old_priority
        if priority is not None:
            new_priority = max(1, min(999, int(priority)))
            data["priority"] = new_priority
            actions.append(f"reprioritize:{old_priority}->{new_priority}")

        if status is not None:
            if status not in ("queued", "held"):
                raise ValueError(f"invalid queue status transition: {status!r}")
            old_status = data.get("status")
            data["status"] = status
            actions.append(f"status:{old_status}->{status}")

        if mode is not None:
            data["mode"] = str(mode)
            actions.append("edit_mode")

        if target_branch is not None:
            data["target_branch"] = str(target_branch)
            actions.append("edit_branch")

        now = datetime.now().isoformat(timespec="microseconds")
        data["version"] = int(data.get("version", 1)) + 1
        data["updated_at"] = now
        audit = list(data.get("audit_log") or [])
        audit.append(
            {
                "at": now,
                "by": str(by or "operator"),
                "action": ",".join(actions) if actions else "touch",
            }
        )
        data["audit_log"] = audit

        new_filename = _format_filename(new_priority, data.get("created_at", now), data["id"])
        new_path = p.parent / new_filename
        persistence.atomic_write_text(
            new_path,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )
        if new_path != p and p.exists():
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        return data


def cancel_item(item_id: str, by: str = "operator") -> dict[str, Any] | None:
    """Cancel a queued/held message and move it to .agent/queue_processed/."""
    with _queue_lock:
        p = _find_item_file(item_id)
        if p is None or not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

        now = datetime.now().isoformat(timespec="microseconds")
        data["status"] = "cancelled"
        data["version"] = int(data.get("version", 1)) + 1
        data["updated_at"] = now
        audit = list(data.get("audit_log") or [])
        audit.append({"at": now, "by": str(by or "operator"), "action": "cancel"})
        data["audit_log"] = audit

        proc_dir = queue_processed_dir()
        proc_dir.mkdir(parents=True, exist_ok=True)
        dest = proc_dir / p.name
        persistence.atomic_write_text(
            dest,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return data


def consume_ready(
    target_branch: str | None = None,
    by: str = "agent",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Claim and archive all 'queued' messages (skipping 'held' ones) in priority order."""
    with _queue_lock:
        qdir = queue_dir()
        if not qdir.is_dir():
            return []

        candidates: list[tuple[Path, dict[str, Any]]] = []
        for p in sorted(qdir.glob("*.json")):
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("status") != "queued":
                continue
            if target_branch and data.get("target_branch") not in (None, "", target_branch):
                continue
            candidates.append((p, data))

        candidates.sort(
            key=lambda pair: (
                int(pair[1].get("priority", 50)),
                str(pair[1].get("created_at", "")),
                int(pair[1].get("seq", 0)),
            )
        )
        if limit is not None and limit > 0:
            candidates = candidates[:limit]

        consumed: list[dict[str, Any]] = []
        proc_dir = queue_processed_dir()
        proc_dir.mkdir(parents=True, exist_ok=True)

        for p, data in candidates:
            now = datetime.now().isoformat(timespec="microseconds")
            data["status"] = "processing"
            data["updated_at"] = now
            persistence.atomic_write_text(
                p,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )

            data["status"] = "processed"
            data["version"] = int(data.get("version", 1)) + 1
            audit = list(data.get("audit_log") or [])
            audit.append({"at": now, "by": by, "action": "consume"})
            data["audit_log"] = audit

            dest = proc_dir / p.name
            persistence.atomic_write_text(
                dest,
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            )
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            consumed.append(data)

        return consumed


def recover_interrupted_items() -> int:
    """Startup self-healing: reset any queue item left in 'processing' back to 'queued'."""
    with _queue_lock:
        qdir = queue_dir()
        if not qdir.is_dir():
            return 0
        recovered = 0
        for p in sorted(qdir.glob("*.json")):
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("status") == "processing":
                now = datetime.now().isoformat(timespec="microseconds")
                data["status"] = "queued"
                data["updated_at"] = now
                audit = list(data.get("audit_log") or [])
                audit.append({"at": now, "by": "self_healing", "action": "reset_processing_to_queued"})
                data["audit_log"] = audit
                persistence.atomic_write_text(
                    p,
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                )
                recovered += 1
        return recovered
