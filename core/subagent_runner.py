"""Isolated SubAgent Sandbox Runner (.agent/subagents/)."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import threading
from typing import Any
from unittest import mock

from core import config as config_mod
from core import interrupt
from core import persistence


ACTIVE_LOCK_FILE = "active.lock"
EXCLUDED_META_SKILLS = frozenset({"subagent", "add_skill", "modify_skill"})
MAX_REPORT_CHARS = 2000
_subagent_mutex = threading.Lock()


def subagents_root() -> Path:
    """Resolve .agent/subagents/ dynamically under TASKS_ROOT.parent."""
    return persistence.TASKS_ROOT.parent / "subagents"


def active_lock_path() -> Path:
    return subagents_root() / ACTIVE_LOCK_FILE


def generate_subagent_id() -> str:
    return f"sub_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


def _map_subagent_terminal_status(final_text: str, was_aborted: bool = False) -> str:
    """Map internal Agent conclusions to AGENTS.md terminal states: completed, failed, cancelled."""
    if was_aborted or "TASK_ABORTED" in final_text or "REJECTED_BY_" in final_text or "EXECUTION_REJECTED" in final_text:
        if "TASK_DONE" not in final_text:
            return "cancelled"
    if "TASK_DONE" in final_text:
        return "completed"
    return "failed"


def get_active_subagent() -> str | None:
    """Return the currently running SubAgent ID if active.lock points to a running SubAgent."""
    lock_file = active_lock_path()
    if not lock_file.is_file():
        return None
    sub_id = lock_file.read_text(encoding="utf-8").strip()
    if not sub_id:
        return None
    status_file = subagents_root() / sub_id / persistence.STATUS_FILE
    if status_file.is_file():
        status = status_file.read_text(encoding="utf-8").strip()
        if status == "running":
            return sub_id
    return None


def heal_orphan_subagent_locks() -> list[str]:
    """Startup self-healing: mark orphan 'running' subagents as 'cancelled' and remove active.lock."""
    healed: list[str] = []
    root = subagents_root()
    lock_file = active_lock_path()
    if lock_file.is_file():
        sub_id = lock_file.read_text(encoding="utf-8").strip()
        if sub_id:
            status_file = root / sub_id / persistence.STATUS_FILE
            if not status_file.is_file() or status_file.read_text(encoding="utf-8").strip() == "running":
                persistence.atomic_write_text(status_file, "cancelled\n")
                healed.append(sub_id)
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    if root.is_dir():
        for sub_dir in sorted(root.iterdir()):
            if not sub_dir.is_dir():
                continue
            status_file = sub_dir / persistence.STATUS_FILE
            if status_file.is_file() and status_file.read_text(encoding="utf-8").strip() == "running":
                persistence.atomic_write_text(status_file, "cancelled\n")
                if sub_dir.name not in healed:
                    healed.append(sub_dir.name)
    return healed


def list_subagents() -> list[dict[str, Any]]:
    """List all SubAgent sandboxes under .agent/subagents/."""
    root = subagents_root()
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for sub_dir in sorted(root.iterdir(), reverse=True):
        if not sub_dir.is_dir() or not persistence._TASK_ID_PATTERN.fullmatch(sub_dir.name):
            continue
        status_file = sub_dir / persistence.STATUS_FILE
        status = status_file.read_text(encoding="utf-8").strip() if status_file.is_file() else "unknown"
        checkpoint = sub_dir / persistence.CHECKPOINT_FILE
        msg_count = 0
        objective = ""
        if checkpoint.is_file():
            try:
                for line in checkpoint.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if "_meta" in obj:
                        objective = str((obj["_meta"] or {}).get("objective", ""))
                        continue
                    msg_count += 1
                    if not objective and obj.get("role") == "user":
                        objective = str(obj.get("content", ""))[:200]
            except Exception:
                pass
        marker = checkpoint if checkpoint.is_file() else status_file
        created = (
            datetime.fromtimestamp(marker.stat().st_mtime).isoformat(timespec="seconds")
            if marker.is_file()
            else ""
        )
        items.append(
            {
                "id": sub_dir.name,
                "status": status,
                "created": created,
                "message_count": msg_count,
                "objective": objective,
                "audit_path": f".agent/subagents/{sub_dir.name}/checkpoint.jsonl",
            }
        )
    return items


def get_subagent_detail(sub_id: str) -> dict[str, Any] | None:
    """Return full status and DAG nodes for a SubAgent sandbox."""
    persistence._validate_identifier(sub_id, "subagent id")
    root = subagents_root()
    sub_dir = root / sub_id
    if not sub_dir.is_dir():
        return None
    with mock.patch.object(persistence, "TASKS_ROOT", root):
        tree = persistence.get_task_tree(sub_id)
    return tree


def abort_subagent(sub_id: str | None = None, source: str = "operator", reason: str = "SubAgent interrupted") -> dict[str, Any] | None:
    """Set abort.flag on a specific or currently active SubAgent."""
    target_id = sub_id or get_active_subagent()
    if not target_id:
        return None
    return interrupt.set_abort_flag(
        target_id,
        source=source,
        reason=reason,
        mode="reject_execution",
        halt_loop=True,
        base_root=subagents_root(),
    )


def run_subagent(args: dict[str, Any], runtime: dict[str, Any] | None = None) -> str:
    """Execute an isolated SubAgent while enforcing the Single-SubAgent file lock."""
    from core.agent import Agent

    objective = str((args or {}).get("objective") or "").strip()
    if not objective:
        return "error: subagent requires a non-empty string 'objective'"

    context_hints = str((args or {}).get("context_hints") or "").strip()
    raw_max_rounds = (args or {}).get("max_rounds")
    max_rounds = min(max(1, int(raw_max_rounds if raw_max_rounds is not None else 15)), 40)
    allowed_skills_arg = (args or {}).get("allowed_skills")
    allowed_set = (
        {str(s).strip() for s in allowed_skills_arg if str(s).strip()}
        if isinstance(allowed_skills_arg, list) and allowed_skills_arg
        else None
    )

    root = subagents_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_file = active_lock_path()

    active_id = get_active_subagent()
    if active_id is not None:
        return (
            f"error: SUBAGENT_BUSY: SubAgent '{active_id}' is currently active. "
            "Maintain single-SubAgent execution; wait until it reaches completed, failed, or cancelled."
        )

    try:
        sub_config = config_mod.load_config(role="subagent")
        sub_config.max_rounds = max_rounds
        preferred_slot = sub_config.chat_extra.get("id_slot")
        injected_client = bool(runtime and runtime.get("subagent_client") is not None)
        available_slot = (
            config_mod.select_available_slot(sub_config.base_url, preferred=int(preferred_slot))
            if preferred_slot is not None and not injected_client
            else None
        )
    except Exception as exc:
        return f"error: SUBAGENT_CONFIG: unable to resolve the subagent endpoint: {exc}"

    if available_slot is None and preferred_slot is not None and not injected_client:
        return (
            "error: SUBAGENT_SLOTS_BUSY: every non-main inference slot is occupied by an "
            "external task. Continue without delegation or retry after the remote job finishes."
        )
    if available_slot is not None and available_slot != preferred_slot:
        sub_config.chat_extra["id_slot"] = available_slot

    with _subagent_mutex:
        active_id = get_active_subagent()
        if active_id is not None:
            return (
                f"error: SUBAGENT_BUSY: SubAgent '{active_id}' is currently active. "
                "Maintain single-SubAgent execution; wait until it reaches completed, failed, or cancelled."
            )
        sub_id = generate_subagent_id()
        sub_dir = root / sub_id
        sub_dir.mkdir(parents=True, exist_ok=True)
        persistence.atomic_write_text(lock_file, f"{sub_id}\n")
        persistence.atomic_write_text(sub_dir / persistence.STATUS_FILE, "running\n")

    parent_task_id = (runtime or {}).get("parent_task_id")
    main_tasks_root = persistence.TASKS_ROOT

    try:
        sub_agent = Agent(
            config=sub_config,
            goal_mode=False,
            role="subagent",
            tasks_root_override=root,
            parent_task_id=parent_task_id,
            parent_tasks_root=main_tasks_root,
            stream_to_stdout=False,
        )

        filtered_tools = []
        filtered_dispatchers = {}
        for tool in sub_agent.tools:
            tname = tool["function"]["name"]
            if tname in EXCLUDED_META_SKILLS:
                continue
            if allowed_set is not None and tname not in allowed_set and tname not in ("task_done", "task_blocked"):
                continue
            filtered_tools.append(tool)
            if tname in sub_agent.dispatchers:
                filtered_dispatchers[tname] = sub_agent.dispatchers[tname]

        sub_agent.tools = filtered_tools
        sub_agent.dispatchers = filtered_dispatchers

        if runtime and runtime.get("subagent_client") is not None:
            sub_agent.client = runtime["subagent_client"]
        elif runtime and runtime.get("client") is not None and sub_config.profile_name == "default":
            sub_agent.client = runtime["client"]

        prompt_parts = [f"[SubAgent Objective]\n{objective}"]
        if context_hints:
            prompt_parts.append(f"[Context Hints]\n{context_hints}")
        prompt_parts.append(
            "Complete the objective using available tools and finish with TASK_DONE: <concise distilled summary> "
            "or TASK_BLOCKED: <reason>."
        )
        full_prompt = "\n\n".join(prompt_parts)

        final_output = sub_agent.run_task(full_prompt, task_id=sub_id)
        was_aborted = bool(
            interrupt.is_step_rejected(sub_id, base_root=root)
            or (parent_task_id and interrupt.is_step_rejected(parent_task_id, base_root=main_tasks_root))
            or sub_agent.last_aborted
        )
        terminal_state = _map_subagent_terminal_status(final_output, was_aborted=was_aborted)
        persistence.atomic_write_text(sub_dir / persistence.STATUS_FILE, f"{terminal_state}\n")

    except Exception as exc:
        final_output = f"TASK_BLOCKED: SubAgent error: {exc}"
        terminal_state = "failed"
        persistence.atomic_write_text(sub_dir / persistence.STATUS_FILE, "failed\n")
    finally:
        with _subagent_mutex:
            if lock_file.is_file():
                try:
                    if lock_file.read_text(encoding="utf-8").strip() == sub_id:
                        lock_file.unlink(missing_ok=True)
                except OSError:
                    pass

    distilled = (final_output or "").strip()
    if len(distilled) > MAX_REPORT_CHARS:
        distilled = distilled[:MAX_REPORT_CHARS] + "...[truncated]"

    audit_rel = f".agent/subagents/{sub_id}/checkpoint.jsonl"
    result_payload = {
        "subagent_id": sub_id,
        "status": terminal_state,
        "audit_path": audit_rel,
        "report": distilled,
    }
    return json.dumps(result_payload, ensure_ascii=False)
