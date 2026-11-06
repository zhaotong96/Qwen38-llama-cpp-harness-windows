"""Isolated SubAgent Sandbox Runner (.agent/subagents/).

Phase 11C adds Git-worktree physical isolation: the SubAgent's file tools and
shell CWD are redirected into ``.agent/worktrees/<id>`` while the objective
delivery telemetry (diff, hashes, test receipts, hazard flags) is captured
host-side into an artifact card for the parent's adversarial review.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from unittest import mock

from core import config as config_mod
from core import interrupt
from core import persistence
from core import tools as tools_mod


ACTIVE_LOCK_FILE = "active.lock"
EXCLUDED_META_SKILLS = frozenset(
    {"subagent", "add_skill", "modify_skill", "branch_rewind", "subagent_review", "crystallize_macro", "subagent_relay"}
)
MAX_REPORT_CHARS = 2000
WORKTREE_WRITE_TOOLS = ("write_file", "patch", "modify_skill", "add_skill")
_SHELL_EXIT_RE = re.compile(r"(?m)^exit=(\d+)\s*$")
_subagent_mutex = threading.Lock()

# --- Phase 12C: three-role relay pipeline (Scout → Builder → Verifier) ---
ROLE_MODES = ("general", "scout", "builder", "verifier")
SCOUT_TOOL_WHITELIST = frozenset(
    {"read_file", "list_dir", "search", "screenshot", "ui_tree", "read_skill", "grounding", "task_done", "task_blocked"}
)
VERIFIER_TOOL_WHITELIST = frozenset({"shell", "read_file", "task_done", "task_blocked"})
SCOUT_VIOLATION_MESSAGE = "error: [SECURITY_VIOLATION] Scout role is strictly read-only"
VERIFIER_VIOLATION_MESSAGE = (
    "error: [SECURITY_VIOLATION] Verifier shell is limited to read-only unittest/pytest runs"
)
_VERIFIER_TEST_CMD_RE = re.compile(r"(?i)\bunittest\b|\bpytest\b")
_UNITTEST_COUNT_RE = re.compile(r"(?m)^Ran (\d+) tests?")


def worktree_isolation_enabled() -> bool:
    """Env kill-switch for Git worktree isolation (default: enabled)."""
    return os.environ.get("SUBAGENT_WORKTREE", "1").strip().lower() not in (
        "0", "false", "off", "disabled",
    )


def worktree_repo_root() -> Path:
    """Repo root the isolation worktree forks from (overridable for tests)."""
    override = os.environ.get("SUBAGENT_WORKTREE_ROOT", "").strip()
    return Path(override).resolve() if override else tools_mod.ROOT


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


def _install_telemetry_interception(sub_agent: Any, telemetry: dict[str, Any]) -> None:
    """Record the objective write/test timeline by wrapping raw dispatchers.

    The SubAgent LLM cannot falsify this telemetry: the host counts every
    write-family tool call and every shell receipt's exit code itself.
    """
    def _wrapped(name: str, original: Any):
        def _invoke(args: dict[str, Any]):
            telemetry["step_counter"] = int(telemetry.get("step_counter") or 0) + 1
            step = telemetry["step_counter"]
            result = original(args)
            if name == "shell":
                match = _SHELL_EXIT_RE.search(str(result))
                telemetry.setdefault("tests", []).append(
                    {
                        "step": step,
                        "command": str((args or {}).get("command", ""))[:300],
                        "exit_code": int(match.group(1)) if match else None,
                    }
                )
            else:
                telemetry["last_write_step"] = step
            return result
        return _invoke

    for name in ("shell", *WORKTREE_WRITE_TOOLS):
        if name in sub_agent.dispatchers:
            sub_agent.dispatchers[name] = _wrapped(name, sub_agent.dispatchers[name])


def run_subagent(args_or_task_id: Any, prompt_or_runtime: Any = None, role_mode: str = "general", timeout: float = 300.0, **kwargs: Any):
    """Dual-contract SubAgent entry point (Phase 12C).

    - ``run_subagent(args_dict, runtime_dict) -> str``: the original skill
      form (single general-purpose SubAgent).
    - ``run_subagent(task_id, prompt, role_mode="general", timeout=300.0) -> dict``:
      the Phase 12C relay form with role-scoped sandboxing; ``role_mode`` is
      one of ``general`` (compatibility), ``scout``, ``builder`` or
      ``verifier``.
    """
    if isinstance(args_or_task_id, dict):
        return _run_subagent_legacy(args_or_task_id, prompt_or_runtime)
    return run_relay_subagent(
        str(args_or_task_id),
        str(prompt_or_runtime or ""),
        role_mode=role_mode,
        timeout=timeout,
        **kwargs,
    )


def _run_subagent_legacy(args: dict[str, Any], runtime: dict[str, Any] | None = None) -> str:
    """Execute an isolated SubAgent while enforcing the Single-SubAgent file lock."""
    from core.agent import Agent
    from core import subagent_gate

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

    # Phase 11C: fork a dedicated Git worktree so the main workspace stays
    # physically pristine regardless of what the SubAgent does to files.
    wt_path: Path | None = None
    isolation_note = ""
    if worktree_isolation_enabled():
        try:
            wt_path = subagent_gate.create_subagent_worktree(
                sub_id, repo_root=worktree_repo_root()
            )
        except Exception as exc:
            wt_path = None
            isolation_note = (
                "worktree isolation unavailable; running in the shared workspace "
                f"(main workspace may be mutated): {exc}"
            )

    telemetry: dict[str, Any] = {"step_counter": 0, "last_write_step": None, "tests": []}

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

        if wt_path is not None:
            _install_telemetry_interception(sub_agent, telemetry)

        if runtime and runtime.get("subagent_client") is not None:
            sub_agent.client = runtime["subagent_client"]
        elif runtime and runtime.get("client") is not None and sub_config.profile_name == "default":
            sub_agent.client = runtime["client"]

        prompt_parts = [f"[SubAgent Objective]\n{objective}"]
        if context_hints:
            prompt_parts.append(f"[Context Hints]\n{context_hints}")
        if wt_path is not None:
            prompt_parts.append(
                "[Isolation] Your workspace is the isolated Git worktree at "
                f"{wt_path}. All file edits stay there until the parent reviews "
                "your artifact delivery card."
            )
        if isolation_note:
            prompt_parts.append(f"[Isolation Notice] {isolation_note}")
        prompt_parts.append(
            "Complete the objective using available tools and finish with TASK_DONE: <concise distilled summary> "
            "or TASK_BLOCKED: <reason>."
        )
        full_prompt = "\n\n".join(prompt_parts)

        root_patcher = (
            mock.patch.object(tools_mod, "ROOT", wt_path) if wt_path is not None else None
        )
        if root_patcher is not None:
            root_patcher.start()
        try:
            final_output = sub_agent.run_task(full_prompt, task_id=sub_id)
        finally:
            if root_patcher is not None:
                root_patcher.stop()

        was_aborted = bool(
            interrupt.is_step_rejected(sub_id, base_root=root)
            or (parent_task_id and interrupt.is_step_rejected(parent_task_id, base_root=main_tasks_root))
            or sub_agent.last_aborted
        )
        terminal_state = _map_subagent_terminal_status(final_output, was_aborted=was_aborted)
        persistence.atomic_write_text(sub_dir / persistence.STATUS_FILE, f"{terminal_state}\n")

        artifact_card: dict[str, Any] | None = None
        review_prompt = ""
        if wt_path is not None:
            try:
                artifact_card = subagent_gate.generate_artifact_card(
                    sub_id,
                    wt_path,
                    tests_executed=list(telemetry.get("tests") or []),
                    summary=final_output,
                    last_write_step=telemetry.get("last_write_step"),
                    terminal_state=terminal_state,
                    risk_assessment=str((args or {}).get("risk_assessment") or ""),
                )
                review_prompt = subagent_gate.format_adversarial_review_prompt(artifact_card)
            except Exception as card_err:
                artifact_card = {
                    "subagent_id": sub_id,
                    "error": f"artifact card generation failed: {card_err}",
                }

    except Exception as exc:
        final_output = f"TASK_BLOCKED: SubAgent error: {exc}"
        terminal_state = "failed"
        artifact_card = None
        review_prompt = ""
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
    result_payload: dict[str, Any] = {
        "subagent_id": sub_id,
        "status": terminal_state,
        "audit_path": audit_rel,
        "report": distilled,
    }
    if isolation_note:
        result_payload["isolation_note"] = isolation_note
    if artifact_card is not None:
        result_payload["artifact_card_path"] = artifact_card.get("artifact_card_path", "")
        result_payload["hazard_flags"] = artifact_card.get("hazard_flags", [])
        result_payload["worktree_path"] = artifact_card.get("worktree_path", "")
        if review_prompt:
            result_payload["review_prompt"] = review_prompt
    return json.dumps(result_payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Phase 12C: three-role relay pipeline (Scout → Builder → Verifier)
# ---------------------------------------------------------------------------

def _denying_dispatcher(tool_name: str, message: str, violations: list) -> Any:
    """Host-side interception stub for role-forbidden tools (tamper-proof)."""
    def _denied(args):
        violations.append({"tool": tool_name, "args": args, "message": message})
        return message
    return _denied


def _install_role_guards(
    sub_agent: Any,
    role_mode: str,
    violations: list,
    test_execution: dict[str, Any],
) -> None:
    """Enforce the role permission matrix at the dispatcher boundary.

    Scout: every write-capable tool is replaced by a security-violation stub.
    Verifier: shell is narrowed to read-only unittest/pytest runs whose exit
    codes are captured host-side; everything else is denied.
    """
    if role_mode == "scout":
        whitelist = SCOUT_TOOL_WHITELIST
    elif role_mode == "verifier":
        whitelist = VERIFIER_TOOL_WHITELIST
    else:
        return
    message = SCOUT_VIOLATION_MESSAGE if role_mode == "scout" else VERIFIER_VIOLATION_MESSAGE
    # Cover both the surviving dispatchers and the meta-skills that the
    # general filter already dropped: any write attempt must be intercepted
    # with an explicit security violation, never with "unknown tool".
    for name in set(sub_agent.dispatchers.keys()) | EXCLUDED_META_SKILLS:
        if name in whitelist:
            continue
        sub_agent.dispatchers[name] = _denying_dispatcher(name, message, violations)

    if role_mode == "verifier" and "shell" in sub_agent.dispatchers:
        original_shell = sub_agent.dispatchers["shell"]

        def _verifier_shell(args):
            command = str((args or {}).get("command") or "")
            if not _VERIFIER_TEST_CMD_RE.search(command):
                violations.append({"tool": "shell", "args": args, "reason": "non-test command"})
                return VERIFIER_VIOLATION_MESSAGE
            result = original_shell(args)
            exit_match = _SHELL_EXIT_RE.search(str(result))
            count_match = _UNITTEST_COUNT_RE.search(str(result))
            test_execution.update(
                {
                    "command": command[:300],
                    "exit_code": int(exit_match.group(1)) if exit_match else 1,
                    "tests_run": int(count_match.group(1)) if count_match else None,
                }
            )
            return result

        sub_agent.dispatchers["shell"] = _verifier_shell


def _relay_branch_nodes(sub_id: str, root: Path) -> list[dict[str, Any]]:
    with mock.patch.object(persistence, "TASKS_ROOT", root):
        return persistence.build_branch_nodes(sub_id, "main")


def _relay_anchor(sub_id: str, root: Path) -> tuple[str, bool]:
    """Snap the relay anchor to the last 1024-aligned node, else the head."""
    chain = _relay_branch_nodes(sub_id, root)
    if not chain:
        return "", False
    for node in reversed(chain):
        if (node.get("meta") or {}).get("ckpt_aligned"):
            return str(node["id"]), True
    return str(chain[-1]["id"]), False


_SCOUT_PROBE_TOOLS = frozenset({"read_file", "list_dir", "search", "ui_tree", "grounding"})


def _extract_scout_affected_files(sub_id: str, root: Path) -> list[str]:
    """Deterministically collect the paths the scout probed, host-side."""
    affected: list[str] = []
    for node in _relay_branch_nodes(sub_id, root):
        for tc in node.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            function = tc.get("function") or {}
            if str(function.get("name") or "") not in _SCOUT_PROBE_TOOLS:
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except Exception:
                continue
            for key in ("path", "target", "query", "pattern"):
                value = str((args or {}).get(key) or "").strip()
                if value and value not in affected:
                    affected.append(value)
            if len(affected) >= 20:
                return affected
    return affected


def _write_scout_brief(
    sub_id: str,
    root: Path,
    objective: str,
    report: str,
    affected_files: list[str],
    anchor_node_id: str,
    anchor_aligned: bool,
) -> dict[str, Any]:
    brief = {
        "subagent_id": sub_id,
        "objective": str(objective)[:2000],
        "report": str(report)[:6000],
        "affected_files": affected_files,
        "anchor_node_id": anchor_node_id,
        "anchor_ckpt_aligned": anchor_aligned,
        "generated_at": datetime.now().isoformat(timespec="microseconds"),
    }
    persistence.atomic_write_text(
        root / sub_id / "scout_brief.json",
        json.dumps(brief, ensure_ascii=False, indent=2) + "\n",
    )
    return brief


def _relay_worktree_path(worktree: Path) -> str:
    try:
        return f".agent/worktrees/{Path(worktree).name}"
    except Exception:
        return str(worktree)


def run_relay_subagent(
    task_id: str,
    prompt: str,
    role_mode: str = "general",
    timeout: float = 300.0,
    builder_worktree: str | None = None,
    inherited_context: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one relay stage under its role sandbox (single-flight enforced).

    - ``scout``: strictly read-only in the main workspace; emits
      ``scout_brief.json`` with a 1024-grid-aligned anchor node.
    - ``builder``: mandatory ``.agent/worktrees/<id>`` isolation; ROOT and
      file tools are locked to the sandbox.
    - ``verifier``: mounts the builder's worktree read-only, runs the targeted
      tests, and the host deterministically issues the verification ticket.
    - ``general``: behaves like the legacy single SubAgent (returns dict).
    """
    from core.agent import Agent
    from core import subagent_gate

    role_mode = str(role_mode or "general").strip().lower()
    if role_mode not in ROLE_MODES:
        return {
            "ok": False,
            "error": f"invalid role_mode {role_mode!r} (expected one of {', '.join(ROLE_MODES)})",
        }
    prompt = str(prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "relay subagent requires a non-empty prompt"}

    root = subagents_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_file = active_lock_path()
    sub_id = ""

    active_id = get_active_subagent()
    if active_id is not None:
        return {
            "ok": False,
            "error": (
                f"SUBAGENT_BUSY: SubAgent '{active_id}' is currently active. "
                "Maintain single-SubAgent execution; wait until it reaches completed, failed, or cancelled."
            ),
        }

    try:
        sub_config = config_mod.load_config(role="subagent")
    except Exception as exc:
        return {"ok": False, "error": f"SUBAGENT_CONFIG: unable to resolve the subagent endpoint: {exc}"}

    with _subagent_mutex:
        active_id = get_active_subagent()
        if active_id is not None:
            return {
                "ok": False,
                "error": (
                    f"SUBAGENT_BUSY: SubAgent '{active_id}' is currently active. "
                    "Maintain single-SubAgent execution; wait until it reaches completed, failed, or cancelled."
                ),
            }
        sub_id = f"sub_relay_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        sub_dir = root / sub_id
        sub_dir.mkdir(parents=True, exist_ok=True)
        persistence.atomic_write_text(lock_file, f"{sub_id}\n")
        persistence.atomic_write_text(sub_dir / persistence.STATUS_FILE, "running\n")

    parent_task_id = str(task_id or "").strip() or None
    main_tasks_root = persistence.TASKS_ROOT

    wt_path: Path | None = None
    violations: list = []
    test_execution: dict[str, Any] = {}
    result: dict[str, Any] = {"ok": True, "subagent_id": sub_id, "role_mode": role_mode}
    try:
        # Role worktree policy. Builder isolation is mandatory (no fallback).
        if role_mode == "builder":
            wt_path = subagent_gate.create_subagent_worktree(sub_id, repo_root=worktree_repo_root())
        elif role_mode == "verifier":
            if not builder_worktree:
                raise RuntimeError("verifier role requires the builder_worktree to mount")
            candidate = Path(builder_worktree)
            if not candidate.is_absolute():
                for base in (worktree_repo_root(), Path.cwd()):
                    resolved = base / candidate
                    if resolved.is_dir():
                        candidate = resolved
                        break
            if not candidate.is_dir():
                raise RuntimeError(f"builder worktree does not exist: {builder_worktree}")
            wt_path = candidate
            result["builder_subagent_id"] = candidate.name

        client_stub = (runtime or {}).get("subagent_client")
        if client_stub is not None:
            # Injected clients must also cover Agent.__init__, which builds
            # its own OpenAI client before the runtime swap below.
            from core import agent as agent_module

            with mock.patch.object(agent_module, "OpenAI", lambda *a, **k: client_stub):
                sub_agent = Agent(
                    config=sub_config,
                    goal_mode=False,
                    role="subagent",
                    tasks_root_override=root,
                    parent_task_id=parent_task_id,
                    parent_tasks_root=main_tasks_root,
                    stream_to_stdout=False,
                )
            sub_agent.client = client_stub
        else:
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
            filtered_tools.append(tool)
            if tname in sub_agent.dispatchers:
                filtered_dispatchers[tname] = sub_agent.dispatchers[tname]
        sub_agent.tools = filtered_tools
        sub_agent.dispatchers = filtered_dispatchers

        _install_role_guards(sub_agent, role_mode, violations, test_execution)

        prompt_parts = [f"[Relay Role: {role_mode.upper()}]\n{prompt}"]
        if role_mode == "scout":
            prompt_parts.append(
                "[Role Constraints] You are a strictly read-only scout. Analyze the code "
                "topology and affected files; never modify anything. Finish with a "
                "structured investigation report and TASK_DONE."
            )
        elif role_mode == "builder":
            prompt_parts.append(
                f"[Isolation] Your workspace is the isolated Git worktree at {wt_path}. "
                "All file edits stay there until the verifier signs the delivery. "
                "Finish with TASK_DONE once the change is complete and self-tested."
            )
        elif role_mode == "verifier":
            prompt_parts.append(
                f"[Verification Target] Mount the builder delivery read-only at {wt_path}. "
                "Run the targeted unittest/pytest suite and static scans; never modify "
                "anything. Finish with TASK_DONE and the exact test outcome."
            )
        if isinstance(inherited_context, dict) and inherited_context.get("anchor_node_id"):
            prompt_parts.append(
                "[Inherited Scout Anchor] Continue from the scout's aligned context "
                f"anchor node '{inherited_context.get('anchor_node_id')}' (ckpt_aligned="
                f"{bool(inherited_context.get('anchor_ckpt_aligned'))}) so the slot-0 "
                "prefix cache is reused without a cold re-prefill."
            )
            if inherited_context.get("report"):
                prompt_parts.append(f"[Scout Findings]\n{inherited_context['report']}")
        full_prompt = "\n\n".join(prompt_parts)

        root_patcher = (
            mock.patch.object(tools_mod, "ROOT", wt_path) if wt_path is not None else None
        )
        if root_patcher is not None:
            root_patcher.start()
        run_error: Exception | None = None
        try:
            final_output = sub_agent.run_task(full_prompt, task_id=sub_id)
        except Exception as exc:
            run_error = exc
            final_output = f"TASK_BLOCKED: relay role error: {exc}"
        finally:
            if root_patcher is not None:
                root_patcher.stop()

        was_aborted = bool(
            interrupt.is_step_rejected(sub_id, base_root=root)
            or (parent_task_id and interrupt.is_step_rejected(parent_task_id, base_root=main_tasks_root))
            or sub_agent.last_aborted
        )
        terminal_state = (
            "failed" if run_error is not None else _map_subagent_terminal_status(final_output, was_aborted=was_aborted)
        )
        persistence.atomic_write_text(sub_dir / persistence.STATUS_FILE, f"{terminal_state}\n")

        distilled = (final_output or "").strip()
        if len(distilled) > MAX_REPORT_CHARS:
            distilled = distilled[:MAX_REPORT_CHARS] + "...[truncated]"
        result.update(
            {
                "status": terminal_state,
                "report": distilled,
                "audit_path": f".agent/subagents/{sub_id}/checkpoint.jsonl",
            }
        )
        if wt_path is not None:
            result["worktree_path"] = _relay_worktree_path(wt_path)
        if violations:
            result["security_violations"] = violations

        if role_mode == "scout":
            anchor_node_id, anchor_aligned = _relay_anchor(sub_id, root)
            affected = _extract_scout_affected_files(sub_id, root)
            brief = _write_scout_brief(
                sub_id, root, prompt, final_output, affected, anchor_node_id, anchor_aligned
            )
            result["scout_brief_path"] = f".agent/subagents/{sub_id}/scout_brief.json"
            result["anchor_node_id"] = anchor_node_id
            result["anchor_ckpt_aligned"] = anchor_aligned
            result["scout_brief"] = brief

        if role_mode == "verifier":
            owner_id = wt_path.name
            tests_executed = [dict(test_execution)] if test_execution else []
            card = subagent_gate.generate_artifact_card(
                owner_id,
                wt_path,
                tests_executed=tests_executed,
                summary=final_output,
                terminal_state=terminal_state,
            )
            anchor = ""
            if isinstance(inherited_context, dict):
                anchor = str(inherited_context.get("anchor_node_id") or "")
            ticket = subagent_gate.generate_verification_ticket(
                owner_id,
                card.get("git_diff_patch") or "",
                dict(test_execution),
                card.get("hazard_flags") or [],
                builder_worktree=_relay_worktree_path(wt_path),
                scout_anchor_node=anchor,
            )
            result["builder_subagent_id"] = owner_id
            result["verification_ticket"] = ticket
            result["hazard_flags"] = card.get("hazard_flags") or []
            result["test_execution"] = dict(test_execution)
            result["artifact_card_path"] = card.get("artifact_card_path", "")

        if run_error is not None and role_mode in ("builder", "verifier") and wt_path is not None:
            # Crash cleanup: destroy the partial sandbox, never leak residue.
            try:
                subagent_gate.finalize_subagent_worktree(
                    wt_path.name,
                    "revert",
                    f"relay stage crashed: {run_error}",
                    repo_root=worktree_repo_root(),
                )
                result["worktree_cleaned"] = True
            except Exception as cleanup_err:
                result["worktree_cleaned"] = False
                result["cleanup_error"] = str(cleanup_err)
        return result

    except Exception as exc:
        try:
            persistence.atomic_write_text(sub_dir / persistence.STATUS_FILE, "failed\n")
        except Exception:
            pass
        if wt_path is not None and role_mode == "builder":
            try:
                subagent_gate.finalize_subagent_worktree(
                    wt_path.name, "revert", f"relay stage crashed: {exc}", repo_root=worktree_repo_root()
                )
            except Exception:
                pass
        return {"ok": False, "subagent_id": sub_id, "role_mode": role_mode, "status": "failed", "error": str(exc)}
    finally:
        with _subagent_mutex:
            if sub_id and lock_file.is_file():
                try:
                    if lock_file.read_text(encoding="utf-8").strip() == sub_id:
                        lock_file.unlink(missing_ok=True)
                except OSError:
                    pass
