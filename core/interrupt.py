"""Real-time decoding and execution interrupt gates (step_state & abort.flag)."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any

from core import persistence
from core.persistence import _read_text_tolerant as _read_tolerant
from core.persistence import _task_directory, atomic_write_text

ABORT_FLAG_FILE = "abort.flag"
STEP_STATE_FILE = "step_state"
STEP_MODE_FILE = "step_mode"
STEP_CONTINUE_FILE = "step_continue.flag"
STEP_MODES = ("auto", "step_risky", "step_all")
DEFAULT_STEP_MODE = "auto"


def _resolve_target_dir(task_id: str, base_root: Path | None = None) -> Path:
    if base_root is not None:
        persistence._validate_identifier(task_id, "task id")
        return Path(base_root) / task_id
    return _task_directory(task_id)


def interrupt_archive_dir() -> Path:
    return persistence.TASKS_ROOT.parent / "interrupt_processed"


def write_step_state(
    task_id: str,
    step_id: str,
    round_i: int,
    phase: str,
    rejected: bool = False,
    active_tool: str | None = None,
    base_root: Path | None = None,
) -> Path:
    """Explicitly persist the current ReAct step lifecycle phase to disk."""
    task_dir = _resolve_target_dir(task_id, base_root=base_root)
    task_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="microseconds")

    started_at = now
    step_file = task_dir / STEP_STATE_FILE
    if step_file.is_file():
        try:
            prev = json.loads(step_file.read_text(encoding="utf-8"))
            if isinstance(prev, dict) and prev.get("step_id") == step_id and prev.get("started_at"):
                started_at = prev["started_at"]
            if isinstance(prev, dict) and prev.get("rejected") and not rejected:
                rejected = True
        except Exception:
            pass

    payload = {
        "step_id": step_id,
        "round": round_i,
        "phase": phase,
        "rejected": bool(rejected),
        "started_at": started_at,
        "updated_at": now,
        "active_tool": active_tool,
    }
    return atomic_write_text(
        step_file,
        json.dumps(payload, ensure_ascii=False) + "\n",
    )


def read_step_state(task_id: str | None, base_root: Path | None = None) -> dict[str, Any] | None:
    """Read the current step_state JSON for task_id, or return None."""
    if not task_id:
        return None
    try:
        step_file = _resolve_target_dir(task_id, base_root=base_root) / STEP_STATE_FILE
    except ValueError:
        return None
    if not step_file.is_file():
        return None
    try:
        data = json.loads(step_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def set_abort_flag(
    task_id: str,
    source: str = "operator",
    reason: str = "User interrupted",
    mode: str = "reject_execution",
    halt_loop: bool = False,
    base_root: Path | None = None,
) -> dict[str, Any]:
    """Create abort.flag and mark step_state.rejected = True immediately."""
    task_dir = _resolve_target_dir(task_id, base_root=base_root)
    task_dir.mkdir(parents=True, exist_ok=True)
    step_file = task_dir / STEP_STATE_FILE
    current_step: dict[str, Any] = {}
    if step_file.is_file():
        try:
            current_step = json.loads(step_file.read_text(encoding="utf-8"))
        except Exception:
            current_step = {}
    step_id = current_step.get("step_id", "unknown_step")
    flag_data = {
        "target_step_id": step_id,
        "rejected": True,
        "source": str(source or "operator"),
        "reason": str(reason or "User interrupted"),
        "mode": str(mode or "reject_execution"),
        "halt_loop": bool(halt_loop),
        "timestamp": datetime.now().isoformat(timespec="microseconds"),
    }
    atomic_write_text(
        task_dir / ABORT_FLAG_FILE,
        json.dumps(flag_data, ensure_ascii=False) + "\n",
    )
    if current_step:
        current_step["rejected"] = True
        current_step["updated_at"] = flag_data["timestamp"]
        atomic_write_text(
            step_file,
            json.dumps(current_step, ensure_ascii=False) + "\n",
        )
    return flag_data


def check_abort_flag(task_id: str | None, base_root: Path | None = None) -> dict[str, Any] | None:
    """Return abort.flag data if present on disk for task_id, else None."""
    if not task_id:
        return None
    try:
        flag_path = _resolve_target_dir(task_id, base_root=base_root) / ABORT_FLAG_FILE
    except ValueError:
        return None
    if not flag_path.is_file():
        return None
    try:
        return json.loads(flag_path.read_text(encoding="utf-8"))
    except Exception:
        return {"rejected": True, "source": "unknown", "reason": "abort.flag present", "mode": "reject_execution"}


def is_step_rejected(task_id: str | None, base_root: Path | None = None) -> dict[str, Any] | None:
    """Check whether either abort.flag is present OR step_state.rejected is True."""
    abort_info = check_abort_flag(task_id, base_root=base_root)
    if abort_info is not None:
        return abort_info
    step_state = read_step_state(task_id, base_root=base_root)
    if isinstance(step_state, dict) and step_state.get("rejected") is True:
        return {
            "target_step_id": step_state.get("step_id", "unknown_step"),
            "rejected": True,
            "source": "step_state",
            "reason": "step_state marked rejected",
            "mode": "reject_execution",
            "halt_loop": False,
        }
    return None


def consume_abort_flag(
    task_id: str | None,
    archive_root: Path | None = None,
    base_root: Path | None = None,
) -> dict[str, Any] | None:
    """Atomically archive abort.flag to .agent/interrupt_processed/ and return its info."""
    if not task_id:
        return None
    try:
        task_dir = _resolve_target_dir(task_id, base_root=base_root)
    except ValueError:
        return None
    flag_path = task_dir / ABORT_FLAG_FILE
    info = is_step_rejected(task_id, base_root=base_root)
    if not flag_path.is_file():
        return info

    target_archive = archive_root or interrupt_archive_dir()
    target_archive.mkdir(parents=True, exist_ok=True)
    step_id = (info or {}).get("target_step_id", "step")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = target_archive / f"abort_{task_id}_{step_id}_{ts}.json"
    try:
        flag_path.replace(dest)
    except OSError:
        try:
            atomic_write_text(dest, json.dumps(info or {}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        flag_path.unlink(missing_ok=True)
    return info


def get_step_mode(task_id: str | None, base_root: Path | None = None) -> str:
    """Read the single-line step gate mode from disk; defaults to ``auto``."""
    if not task_id:
        return DEFAULT_STEP_MODE
    try:
        mode_file = _resolve_target_dir(task_id, base_root=base_root) / STEP_MODE_FILE
    except ValueError:
        return DEFAULT_STEP_MODE
    raw = (_read_tolerant(mode_file) or "").strip().lower()
    return raw if raw in STEP_MODES else DEFAULT_STEP_MODE


def set_step_mode(task_id: str, mode: str, base_root: Path | None = None) -> str:
    """Persist the step gate mode as a single plain-text word."""
    normalized = str(mode or "").strip().lower()
    if normalized not in STEP_MODES:
        raise ValueError(f"invalid step mode: {mode!r} (expected one of {', '.join(STEP_MODES)})")
    task_dir = _resolve_target_dir(task_id, base_root=base_root)
    task_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(task_dir / STEP_MODE_FILE, f"{normalized}\n")
    return normalized


def write_step_continue(task_id: str, base_root: Path | None = None) -> Path:
    """Create the one-shot operator release flag for a paused pre-dispatch breakpoint."""
    task_dir = _resolve_target_dir(task_id, base_root=base_root)
    task_dir.mkdir(parents=True, exist_ok=True)
    return atomic_write_text(task_dir / STEP_CONTINUE_FILE, "continue\n")


def consume_step_continue(task_id: str | None, base_root: Path | None = None) -> bool:
    """Return True exactly once when a step_continue.flag was present, deleting it."""
    if not task_id:
        return False
    try:
        flag_path = _resolve_target_dir(task_id, base_root=base_root) / STEP_CONTINUE_FILE
    except ValueError:
        return False
    if not flag_path.is_file():
        return False
    try:
        flag_path.unlink()
    except OSError:
        pass
    return True


def wait_for_step_continue(
    task_id: str,
    poll_interval: float = 0.1,
    base_root: Path | None = None,
) -> dict[str, Any] | None:
    """Block until the operator releases the breakpoint or aborts the step.

    Returns the abort info when an abort won the race, otherwise None.
    """
    while True:
        if consume_step_continue(task_id, base_root=base_root):
            return None
        abort_info = is_step_rejected(task_id, base_root=base_root)
        if abort_info is not None:
            return abort_info
        time.sleep(poll_interval)
