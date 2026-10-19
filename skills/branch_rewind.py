"""
Goal-driven autonomous DAG rewind with a four-dimensional anti-oscillation guard.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from core import persistence

DESCRIPTION = (
    "Autonomously rewind the conversation DAG to an earlier historical node_id on a new branch "
    "when the current technical path is proven unviable, injecting a compact failure lesson into Section 4."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "target_node_id": {
            "type": "string",
            "description": "Historical DAG node_id to rewind back to (e.g. n_000015_ab12cd)",
        },
        "new_branch_name": {
            "type": "string",
            "description": "Alphanumeric/underscore name for the new exploration branch (e.g. fix_alt_path)",
        },
        "failed_lesson": {
            "type": "string",
            "description": "Concise root cause of why the current branch failed (pinned to Ledger Section 4)",
        },
        "new_plan": {
            "type": "string",
            "description": "Distinct alternative strategy to execute on the new branch",
        },
    },
    "required": ["target_node_id", "new_branch_name", "failed_lesson", "new_plan"],
}

MAX_TOTAL_REWINDS_PER_TASK = 4
MAX_FORKS_PER_TARGET_NODE = 2
MIN_STEPS_BEFORE_REWIND = 3
SIMILARITY_THRESHOLD = 0.70
REWIND_LEDGER_FILE = "rewind_ledger.jsonl"


def _tokenize_plan(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_\u4e00-\u9fff]+", (text or "").lower()))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


def _load_rewind_ledger(task_dir: Path) -> list[dict[str, Any]]:
    path = task_dir / REWIND_LEDGER_FILE
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    entries.append(value)
    except Exception:
        pass
    return entries


def _scan_abandoned_segment(
    chain: list[dict[str, Any]], target_node_id: str
) -> tuple[int, list[str]]:
    """Count assistant steps after the target and collect touched workspace files."""
    index = next(
        (position for position, node in enumerate(chain) if node.get("id") == target_node_id),
        -1,
    )
    if index == -1:
        return -1, []
    tail = chain[index + 1:]
    assistant_steps = sum(1 for node in tail if node.get("role") == "assistant")
    touched_files: list[str] = []
    for node in tail:
        for tool_call in node.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            tool_name = function.get("name")
            if tool_name not in ("write_file", "patch", "add_skill", "modify_skill"):
                continue
            try:
                tool_args = json.loads(function.get("arguments") or "{}")
                file_path = str(
                    tool_args.get("path") or tool_args.get("skill_name") or ""
                ).strip()
                if file_path and file_path not in touched_files:
                    touched_files.append(file_path)
            except Exception:
                pass
    return assistant_steps, touched_files


def run(args: dict, runtime: dict | None = None) -> str:
    runtime = runtime or {}
    task_id = str(
        runtime.get("task_id") or persistence.get_current_session() or ""
    ).strip()
    if not task_id:
        return "error: no active task_id available for branch_rewind"

    target_node_id = str((args or {}).get("target_node_id") or "").strip()
    new_branch = str((args or {}).get("new_branch_name") or "").strip()
    failed_lesson = " ".join(str((args or {}).get("failed_lesson") or "").strip().split())
    new_plan = " ".join(str((args or {}).get("new_plan") or "").strip().split())

    if not target_node_id or not new_branch or not failed_lesson or not new_plan:
        return (
            "error: target_node_id, new_branch_name, failed_lesson, and new_plan "
            "are all required"
        )

    try:
        persistence._validate_identifier(new_branch, "branch name")
    except ValueError as exc:
        return f"error: {exc}"

    old_branch = persistence.get_current_branch(task_id)
    existing_branches = persistence.list_branches(task_id)
    if new_branch in existing_branches:
        return f"error: branch '{new_branch}' already exists; choose a unique branch name"

    chain = persistence.build_branch_nodes(task_id, old_branch)
    if not chain:
        return f"error: active branch '{old_branch}' has no DAG nodes"

    assistant_steps, touched_files = _scan_abandoned_segment(chain, target_node_id)
    if assistant_steps < 0:
        return (
            f"error: target_node_id '{target_node_id}' is not an ancestor on current "
            f"branch '{old_branch}'. Use a valid node_id from the current branch lineage."
        )

    if assistant_steps < MIN_STEPS_BEFORE_REWIND:
        return (
            f"error: [ANTI_OSCILLATION_COOLDOWN] Only {assistant_steps} assistant step(s) "
            f"elapsed since '{target_node_id}' (minimum required: {MIN_STEPS_BEFORE_REWIND}). "
            "Execute concrete diagnostic steps before rewinding again."
        )

    task_dir = persistence.TASKS_ROOT / task_id
    history = _load_rewind_ledger(task_dir)

    if len(history) >= MAX_TOTAL_REWINDS_PER_TASK:
        return (
            f"error: [ANTI_OSCILLATION_QUOTA_EXCEEDED] Task '{task_id}' has already used all "
            f"{MAX_TOTAL_REWINDS_PER_TASK} autonomous rewinds. Continue on the current branch "
            "or call task_blocked."
        )

    same_node_rewinds = [
        entry for entry in history if entry.get("target_node_id") == target_node_id
    ]
    if len(same_node_rewinds) >= MAX_FORKS_PER_TARGET_NODE:
        return (
            f"error: [ANTI_OSCILLATION_NODE_CAP] Node '{target_node_id}' has already been "
            f"forked {len(same_node_rewinds)} times (max {MAX_FORKS_PER_TARGET_NODE}). "
            "Select an earlier ancestor node or declare task_blocked."
        )

    new_tokens = _tokenize_plan(new_plan)
    for prior in history:
        prior_tokens = _tokenize_plan(str(prior.get("new_plan", "")))
        similarity = _jaccard(new_tokens, prior_tokens)
        if similarity >= SIMILARITY_THRESHOLD:
            return (
                f"error: [ANTI_OSCILLATION_DUPLICATE_PLAN] Proposed new_plan is too "
                f"similar (Jaccard={similarity:.2f}) to prior rewind branch "
                f"'{prior.get('new_branch')}'. Formulate a genuinely different approach."
            )

    old_head = chain[-1]["id"]

    fork_result = persistence.create_branch(
        task_id,
        branch_name=new_branch,
        from_node_id=target_node_id,
        checkout=True,
        force_at_assistant=False,
    )
    anchor_id = fork_result["head_node_id"]

    files_note = (
        f" Dirty workspace files from abandoned branch: {touched_files}."
        if touched_files
        else ""
    )
    lesson_line = (
        f"[Branch Rewind Lesson] (rewound '{old_branch}'@{old_head} -> "
        f"'{new_branch}'@{anchor_id}): "
        f"Avoid failed approach: {failed_lesson[:240]}. "
        f"Execute new plan: {new_plan[:240]}.{files_note}"
    )
    lesson_node = persistence.append_dag_node(
        task_id,
        {"role": "user", "content": lesson_line},
        branch=new_branch,
        parent_id=anchor_id,
        node_type="message",
        meta={
            "rewind_lesson": True,
            "abandoned_branch": old_branch,
            "abandoned_head": old_head,
            "rewind_target": target_node_id,
        },
    )

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "from_branch": old_branch,
        "from_head": old_head,
        "target_node_id": target_node_id,
        "anchor_node_id": anchor_id,
        "new_branch": new_branch,
        "lesson_node_id": lesson_node["id"],
        "failed_lesson": failed_lesson,
        "new_plan": new_plan,
        "touched_files": touched_files,
        "plan_hash": hashlib.sha1(
            " ".join(sorted(new_tokens)).encode("utf-8")
        ).hexdigest()[:10],
    }
    task_dir.mkdir(parents=True, exist_ok=True)
    with (task_dir / REWIND_LEDGER_FILE).open(
        "a", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return json.dumps(
        {
            "ok": True,
            "switched_to_branch": new_branch,
            "rewound_to_node": anchor_id,
            "lesson_node_id": lesson_node["id"],
            "rewinds_remaining": MAX_TOTAL_REWINDS_PER_TASK - (len(history) + 1),
            "dirty_workspace_files": touched_files,
        },
        ensure_ascii=False,
    )
