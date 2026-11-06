"""Four-level reversibility Saga algebra (Phase 12B).

Every tool action is classified into a reversibility level before it is
trusted to a rollback plan:

- **Level 0 — Pure Read**: observation probes; inverse action is the empty
  set, a rewind only moves the DAG pointer.
- **Level 1 — Logical State**: transient desktop/pointer state (window focus,
  mouse/keyboard, clipboard, git branch pointers); rolled back logically.
- **Level 2 — Compensable Write**: file creations/patches with a captured
  pre-state backup; rolled back by the deterministic inverse action (delete
  created files, restore original bytes).
- **Level 3 — Irreversible External**: arbitrary shell execution and writes
  into architecture redline files; rollback must NEVER silently compensate —
  the saga halts and issues a human confirmation ticket.

The journal is an append-only JSONL per task (``.agent/tasks/<id>/``); a
rollback walks it in reverse and mechanically executes Level-2 compensations
until it either finishes or hits the Level-3 boundary.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core import persistence
from core.models import ROOT

JOURNAL_FILENAME = "action_journal.jsonl"
CONFIRMATION_PREFIX = "saga_confirmation_"

LEVEL_PURE_READ = 0
LEVEL_LOGICAL = 1
LEVEL_COMPENSABLE = 2
LEVEL_IRREVERSIBLE = 3

# Level 0: pure observation probes (zero side effects).
LEVEL0_PROBES = frozenset(
    {
        "read_file", "list_dir", "screenshot", "pic_zoom", "ui_tree",
        "grounding", "read_skill", "search", "read_checkpoint_node",
        "task_done", "task_blocked",
    }
)
# Level 2: mechanically compensable writes.
LEVEL2_WRITES = frozenset({"write_file", "patch", "add_skill", "modify_skill", "crystallize_macro"})
# Level 3: unbounded / redline actions requiring operator confirmation.
LEVEL3_CRITICAL = frozenset({"shell"})

# Overwriting an architecture redline file is never silently compensated.
REDLINE_WRITE_PATHS = frozenset({"requirements.txt", "docs/SPEC.md", "package.json"})

_BEFORE_STATE_CONTENT_CAP = 1024 * 1024


def classify_action_reversibility(tool_name: str, args: dict[str, Any]) -> int:
    """Map one tool action to its Saga reversibility level (0..3).

    Patch-12B-1 additions:
    1. Writes to files exceeding _BEFORE_STATE_CONTENT_CAP (1MB) are promoted to Level 3
       because no full content backup can be preserved for deterministic rollback.
    2. Target paths are fully resolved to detect symlinks/junctions targeting redline files.
    """
    name = str(tool_name or "").strip()
    if name in LEVEL0_PROBES:
        return LEVEL_PURE_READ
    if name in LEVEL3_CRITICAL:
        return LEVEL_IRREVERSIBLE
    if name in LEVEL2_WRITES:
        target = str((args or {}).get("path") or "")
        if name in ("add_skill", "modify_skill"):
            skill_name = str((args or {}).get("skill_name") or "").strip()
            if skill_name:
                target = f"skills/{skill_name.removesuffix('.py')}.py"

        resolved = _resolve_repo_path(target)
        if resolved is None:
            return LEVEL_IRREVERSIBLE
        root = Path(ROOT).resolve()
        try:
            rel = resolved.relative_to(root).as_posix()
        except ValueError:
            return LEVEL_IRREVERSIBLE
        if rel.lower() in {p.lower() for p in REDLINE_WRITE_PATHS}:
            return LEVEL_IRREVERSIBLE
        try:
            cand = Path(target)
            if not cand.is_absolute():
                cand = ROOT / cand
            if cand.is_symlink():
                real_target = cand.resolve()
                if real_target != root and root not in real_target.parents:
                    return LEVEL_IRREVERSIBLE
                if real_target.relative_to(root).as_posix().lower() in {p.lower() for p in REDLINE_WRITE_PATHS}:
                    return LEVEL_IRREVERSIBLE
        except (OSError, ValueError):
            pass
        try:
            if resolved.is_file() and resolved.stat().st_size > _BEFORE_STATE_CONTENT_CAP:
                return LEVEL_IRREVERSIBLE
        except OSError:
            pass
        return LEVEL_COMPENSABLE
    # Mouse/keyboard/window focus/clipboard/git pointers and any other
    # transient logical state: reset by logical rollback, not file surgery.
    return LEVEL_LOGICAL


def action_journal_path(task_id: str) -> Path:
    persistence._validate_identifier(task_id, "task id")
    return persistence.TASKS_ROOT / task_id / JOURNAL_FILENAME


def _resolve_repo_path(path: str) -> Path | None:
    """Resolve a tool path against the repo root; None when it escapes it."""
    raw = str(path or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        resolved = candidate.resolve()
        root = Path(ROOT).resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def capture_action_before_state(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Snapshot the pre-action file state for compensable write tools."""
    name = str(tool_name or "").strip()
    if name not in ("write_file", "patch", "add_skill", "modify_skill"):
        return None
    target = str((args or {}).get("path") or "")
    if name in ("add_skill", "modify_skill"):
        skill_name = str((args or {}).get("skill_name") or "").strip()
        if not skill_name:
            return None
        target = f"skills/{skill_name.removesuffix('.py')}.py"
    resolved = _resolve_repo_path(target)
    if resolved is None:
        return None
    existed = resolved.is_file()
    entry: dict[str, Any] = {
        "path": str(target).replace("\\", "/"),
        "existed": existed,
        "sha256": None,
        "content": None,
    }
    if existed:
        try:
            raw = resolved.read_bytes()
        except OSError:
            return entry
        entry["sha256"] = hashlib.sha256(raw).hexdigest()
        if len(raw) <= _BEFORE_STATE_CONTENT_CAP:
            entry["content"] = raw.decode("utf-8", errors="replace")
    return entry


def record_action_journal(
    task_id: str,
    step_index: int,
    tool_name: str,
    args: dict[str, Any],
    before_state: dict[str, Any] | None = None,
) -> None:
    """Atomically append one reversibility journal entry for a committed action."""
    persistence._validate_identifier(task_id, "task id")
    if before_state is None:
        before_state = capture_action_before_state(tool_name, args)
    entry = {
        "ts": datetime.now().isoformat(timespec="microseconds"),
        "task_id": task_id,
        "step_index": int(step_index),
        "tool_name": str(tool_name),
        "level": classify_action_reversibility(tool_name, args),
        "args": args if isinstance(args, dict) else {},
        "before_state": before_state,
    }
    journal = action_journal_path(task_id)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        try:
            import os

            os.fsync(handle.fileno())
        except OSError:
            pass


def load_action_journal(task_id: str) -> list[dict[str, Any]]:
    """Read the full append-only journal; unreadable lines are skipped."""
    journal = action_journal_path(task_id)
    if not journal.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries


def _write_confirmation_ticket(
    task_id: str, entry: dict[str, Any], reason: str
) -> Path:
    """Persist a human-confirmation ticket at a Level-3 saga boundary."""
    ticket_dir = persistence.TASKS_ROOT / task_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    ticket = {
        "kind": "SAGA_LEVEL3_CONFIRMATION_REQUIRED",
        "task_id": task_id,
        "created_at": datetime.now().isoformat(timespec="microseconds"),
        "reason": reason,
        "blocked_action": entry,
    }
    target = ticket_dir / f"{CONFIRMATION_PREFIX}{stamp}.json"
    persistence.atomic_write_text(
        target, json.dumps(ticket, ensure_ascii=False, indent=2) + "\n"
    )
    return target


def _compensate_file_action(entry: dict[str, Any]) -> str:
    """Execute the deterministic inverse of one compensable write action."""
    before = entry.get("before_state") or {}
    path = str(before.get("path") or (entry.get("args") or {}).get("path") or "")
    resolved = _resolve_repo_path(path)
    if resolved is None:
        return f"skipped (path escapes repository: {path!r})"

    if before.get("existed"):
        content = before.get("content")
        if content is None:
            return f"failed (no content backup for {path!r})"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        persistence.atomic_write_text(resolved, content)
        return f"restored original content of {path}"
    try:
        resolved.unlink()
        return f"deleted created file {path}"
    except FileNotFoundError:
        return f"already absent {path}"
    except OSError as exc:
        return f"failed ({exc})"


def execute_saga_rollback(task_id: str, target_step_index: int) -> dict[str, Any]:
    """Reverse-walk the journal and compensate actions after the target step.

    Level 2 actions receive their deterministic inverse; Level 3 actions halt
    the rollback and emit a confirmation ticket instead of any silent
    compensation.
    """
    persistence._validate_identifier(task_id, "task id")
    entries = load_action_journal(task_id)
    compensated: list[dict[str, Any]] = []
    skipped_logical = 0

    for entry in reversed(entries):
        step_index = int(entry.get("step_index", 0))
        if step_index <= int(target_step_index):
            continue
        level = int(entry.get("level", 1))
        if level <= LEVEL_LOGICAL:
            skipped_logical += 1
            continue
        if level >= LEVEL_IRREVERSIBLE:
            ticket = _write_confirmation_ticket(
                task_id,
                entry,
                "Level-3 irreversible action blocks silent rollback; "
                "operator confirmation is required.",
            )
            return {
                "ok": False,
                "halted": True,
                "compensated": compensated,
                "skipped_logical": skipped_logical,
                "halted_at": {
                    "step_index": step_index,
                    "tool_name": entry.get("tool_name"),
                    "level": level,
                },
                "confirmation_ticket": f".agent/tasks/{task_id}/{ticket.name}",
            }
        detail = _compensate_file_action(entry)
        compensated.append(
            {
                "step_index": step_index,
                "tool_name": entry.get("tool_name"),
                "result": detail,
            }
        )

    return {
        "ok": True,
        "halted": False,
        "compensated": compensated,
        "skipped_logical": skipped_logical,
        "target_step_index": int(target_step_index),
    }
