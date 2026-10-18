"""JSONL and Git-style DAG persistence for resumable agent tasks.

Every task lives in ``.agent/tasks/<task_id>/`` and is described by explicit
file markers:
- ``checkpoint.jsonl``: Append-only DAG commit node store (backward compatible
  with linear message lists).
- ``current_branch``: Single-line active branch name (default ``main``).
- ``branches/<branch_name>``: Single-line leaf node_id pointer.
- ``tags/<tag_name>``: Single-line milestone node_id pointer.
- ``status``: Lifecycle marker word (running / done / blocked / finished / interrupted).
- ``live_stream.jsonl``: Real-time structured token and lifecycle stream projection.
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.models import ROOT


TASKS_ROOT = ROOT / ".agent" / "tasks"
STATUS_FILE = "status"
CHECKPOINT_FILE = "checkpoint.jsonl"
CURRENT_BRANCH_FILE = "current_branch"
BRANCHES_DIR = "branches"
TAGS_DIR = "tags"
LIVE_STREAM_FILE = "live_stream.jsonl"
DEFAULT_BRANCH = "main"

_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_DAG_ENVELOPE_KEYS = frozenset({"id", "parent_id", "branch", "timestamp", "type", "meta", "_preserve_keys"})


def _agent_state_root() -> Path:
    """Resolve the .agent state directory dynamically from TASKS_ROOT.parent."""
    return TASKS_ROOT.parent


def current_session_file() -> Path:
    return _agent_state_root() / "current_session"


def _validate_identifier(value: str, label: str = "task id") -> None:
    if not isinstance(value, str) or not _TASK_ID_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")


def _task_directory(task_id: str) -> Path:
    _validate_identifier(task_id, "task id")
    return TASKS_ROOT / task_id


def atomic_write_text(target_path: Path, content: str, encoding: str = "utf-8") -> Path:
    """Atomically write text to target_path with NTFS lock backoff retries."""
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tid = threading.get_ident()
    pid = os.getpid()
    tmp_path = target_path.parent / f".{target_path.name}.{pid}.{tid}.{uuid.uuid4().hex[:6]}.tmp"

    with tmp_path.open("w", encoding=encoding, newline="\n") as handle:
        handle.write(content)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass

    last_err: OSError | None = None
    for attempt in range(10):
        try:
            os.replace(tmp_path, target_path)
            return target_path
        except PermissionError as err:
            last_err = err
            time.sleep(random.uniform(0.005, 0.025) * (attempt + 1))
        except OSError as err:
            last_err = err
            time.sleep(random.uniform(0.005, 0.015))

    try:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    except OSError:
        pass
    if last_err is not None:
        raise last_err
    return target_path


def _read_text_tolerant(path: Path) -> str | None:
    """Read a text file, returning None when it is missing or mid-replacement.

    State files are swapped in via ``atomic_write_text`` (os.replace) while the
    HTTP API and the agent loop read them concurrently; a reader may observe a
    file that has just been renamed away. Callers treat None as "absent".
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


# --- Singleton Session Pointer (.agent/current_session) -------------------

def get_current_session() -> str | None:
    """Read the active session task_id from .agent/current_session."""
    path = current_session_file()
    raw = _read_text_tolerant(path)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or not _TASK_ID_PATTERN.fullmatch(raw):
        return None
    return raw


def set_current_session(task_id: str) -> Path:
    """Atomically set .agent/current_session to task_id."""
    _validate_identifier(task_id, "task id")
    return atomic_write_text(current_session_file(), f"{task_id}\n")


def ensure_current_session(preferred_task_id: str | None = None) -> str:
    """Return the active session task_id, creating one if none exists."""
    current = get_current_session()
    if current and (_task_directory(current).is_dir() or preferred_task_id is None):
        task_dir = _task_directory(current)
        task_dir.mkdir(parents=True, exist_ok=True)
        return current
    task_id = preferred_task_id or generate_task_id()
    _task_directory(task_id).mkdir(parents=True, exist_ok=True)
    set_current_session(task_id)
    return task_id


# --- Live Stream Event Projection (live_stream.jsonl) ---------------------

_stream_seq_lock = threading.Lock()
_stream_seq_counters: dict[str, int] = {}


def append_stream_event(
    task_id: str,
    event_type: str,
    delta: Any,
    step_id: str = "",
) -> dict[str, Any]:
    """Append a structured real-time event line to .agent/tasks/<id>/live_stream.jsonl."""
    task_dir = _task_directory(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    stream_file = task_dir / LIVE_STREAM_FILE

    with _stream_seq_lock:
        seq = _stream_seq_counters.get(task_id, 0)
        if seq == 0 and stream_file.is_file():
            try:
                with stream_file.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            seq += 1
            except OSError:
                seq = 0
        seq += 1
        _stream_seq_counters[task_id] = seq

    event = {
        "seq": seq,
        "timestamp": datetime.now().isoformat(timespec="microseconds"),
        "step_id": step_id,
        "event_type": event_type,
        "delta": delta,
    }
    with stream_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        handle.flush()
    return event


def read_stream_events(task_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    """Read structured events with seq > since_seq from live_stream.jsonl."""
    stream_file = _task_directory(task_id) / LIVE_STREAM_FILE
    if not stream_file.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        with stream_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                    if int(evt.get("seq", 0)) > since_seq:
                        events.append(evt)
                except Exception:
                    continue
    except OSError:
        return []
    return events


# --- Git-Style Context DAG Helpers ----------------------------------------

def _generate_node_id(seq: int) -> str:
    return f"n_{seq:06d}_{uuid.uuid4().hex[:6]}"


def get_current_branch(task_id: str) -> str:
    branch_file = _task_directory(task_id) / CURRENT_BRANCH_FILE
    val = (_read_text_tolerant(branch_file) or "").strip()
    if val and _TASK_ID_PATTERN.fullmatch(val):
        return val
    return DEFAULT_BRANCH


def set_current_branch(task_id: str, branch: str) -> Path:
    _validate_identifier(branch, "branch name")
    return atomic_write_text(_task_directory(task_id) / CURRENT_BRANCH_FILE, f"{branch}\n")


def get_branch_head(task_id: str, branch: str | None = None) -> str | None:
    target_branch = branch or get_current_branch(task_id)
    _validate_identifier(target_branch, "branch name")
    ref_file = _task_directory(task_id) / BRANCHES_DIR / target_branch
    val = (_read_text_tolerant(ref_file) or "").strip()
    if val:
        return val
    return None


def set_branch_head(task_id: str, branch: str, node_id: str) -> Path:
    _validate_identifier(branch, "branch name")
    ref_file = _task_directory(task_id) / BRANCHES_DIR / branch
    return atomic_write_text(ref_file, f"{node_id}\n")


def list_branches(task_id: str) -> dict[str, str]:
    branches_dir = _task_directory(task_id) / BRANCHES_DIR
    if not branches_dir.is_dir():
        return {}
    result: dict[str, str] = {}
    for p in sorted(branches_dir.iterdir()):
        if _TASK_ID_PATTERN.fullmatch(p.name):
            head = (_read_text_tolerant(p) or "").strip()
            if head:
                result[p.name] = head
    return result


def list_tags(task_id: str) -> dict[str, str]:
    tags_dir = _task_directory(task_id) / TAGS_DIR
    if not tags_dir.is_dir():
        return {}
    result: dict[str, str] = {}
    for p in sorted(tags_dir.iterdir()):
        if _TASK_ID_PATTERN.fullmatch(p.name):
            node_id = (_read_text_tolerant(p) or "").strip()
            if node_id:
                result[p.name] = node_id
    return result


def create_tag(task_id: str, tag_name: str, node_id: str | None = None) -> dict[str, Any]:
    _validate_identifier(tag_name, "tag name")
    loaded = load_dag_nodes(task_id)
    if loaded is None:
        raise ValueError(f"no checkpoint for task: {task_id}")
    nodes, _ = loaded
    node_map = {n["id"]: n for n in nodes}
    target_id = node_id or get_branch_head(task_id)
    if not target_id or target_id not in node_map:
        raise ValueError(f"unknown target node_id: {target_id!r}")
    tag_file = _task_directory(task_id) / TAGS_DIR / tag_name
    atomic_write_text(tag_file, f"{target_id}\n")
    return {"tag": tag_name, "node_id": target_id}


def strip_dag_envelope(node: dict[str, Any]) -> dict[str, Any]:
    """Extract the clean OpenAI message payload from a DAG commit node."""
    preserved = set(node.get("_preserve_keys") or [])
    clean: dict[str, Any] = {}
    for k, v in node.items():
        if k in _DAG_ENVELOPE_KEYS and k not in preserved:
            continue
        clean[k] = copy.deepcopy(v)
    return clean


def _wrap_message_as_node(
    message: dict[str, Any],
    node_id: str,
    parent_id: str | None,
    branch: str,
    node_type: str = "message",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if "id" in message and "parent_id" in message and "role" in message:
        node = copy.deepcopy(message)
        node.setdefault("branch", branch)
        node.setdefault("timestamp", datetime.now().isoformat(timespec="microseconds"))
        node.setdefault("type", node_type)
        if meta:
            existing_meta = dict(node.get("meta") or {})
            existing_meta.update(meta)
            node["meta"] = existing_meta
        return node

    preserve = [k for k in message.keys() if k in _DAG_ENVELOPE_KEYS]
    node = {
        "id": node_id,
        "parent_id": parent_id,
        "branch": branch,
        "timestamp": datetime.now().isoformat(timespec="microseconds"),
        "type": node_type,
    }
    for k, v in message.items():
        if k in ("id", "parent_id", "branch", "timestamp", "type"):
            continue
        node[k] = copy.deepcopy(v)
    if meta is not None or "meta" in message:
        combined_meta = {}
        if isinstance(message.get("meta"), dict):
            combined_meta.update(message["meta"])
        if meta:
            combined_meta.update(meta)
        node["meta"] = combined_meta
    if preserve:
        node["_preserve_keys"] = preserve
    return node


def load_dag_nodes(task_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Load all DAG commit nodes and optional _meta from checkpoint.jsonl.

    Automatically assigns deterministic ``legacy_00000k`` IDs and initializes
    ``branches/main`` + ``current_branch`` when reading legacy linear files.
    """
    task_dir = _task_directory(task_id)
    checkpoint = task_dir / CHECKPOINT_FILE

    nodes: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    had_legacy_nodes = False
    msg_index = 0

    try:
        handle = checkpoint.open("r", encoding="utf-8")
    except OSError:
        return None

    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if line_number == 1 and isinstance(value, dict) and "_meta" in value:
                raw_metadata = value["_meta"]
                metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
                continue
            if not isinstance(value, dict):
                raise ValueError(f"checkpoint line {line_number} is not a message object")
            msg_index += 1
            if "id" not in value or "parent_id" not in value:
                had_legacy_nodes = True
                node_id = value.get("id") or f"legacy_{msg_index:06d}"
                parent_id = (
                    value.get("parent_id")
                    if "parent_id" in value
                    else (nodes[-1]["id"] if nodes else None)
                )
                preserve = [k for k in value.keys() if k in _DAG_ENVELOPE_KEYS]
                upgraded: dict[str, Any] = {
                    "id": node_id,
                    "parent_id": parent_id,
                    "branch": value.get("branch", DEFAULT_BRANCH),
                    "timestamp": value.get("timestamp", ""),
                    "type": value.get("type", "message"),
                }
                for k, v in value.items():
                    if k not in upgraded:
                        upgraded[k] = v
                if preserve:
                    upgraded["_preserve_keys"] = preserve
                nodes.append(upgraded)
            else:
                nodes.append(value)

    if nodes:
        branch_file = task_dir / CURRENT_BRANCH_FILE
        main_ref = task_dir / BRANCHES_DIR / DEFAULT_BRANCH
        if had_legacy_nodes or (not branch_file.is_file() and not main_ref.is_file()):
            if not main_ref.is_file():
                set_branch_head(task_id, DEFAULT_BRANCH, nodes[-1]["id"])
            if not branch_file.is_file():
                set_current_branch(task_id, DEFAULT_BRANCH)

    return nodes, metadata


def build_branch_nodes(task_id: str, branch: str | None = None) -> list[dict[str, Any]]:
    """Reconstruct the ordered root-to-leaf DAG node chain for a branch in O(D)."""
    loaded = load_dag_nodes(task_id)
    if loaded is None:
        return []
    nodes, _ = loaded
    if not nodes:
        return []

    node_map = {n["id"]: n for n in nodes}
    target_branch = branch or get_current_branch(task_id)
    leaf_id = get_branch_head(task_id, target_branch)

    if not leaf_id or leaf_id not in node_map:
        branch_matches = [n for n in nodes if n.get("branch") == target_branch]
        leaf_id = branch_matches[-1]["id"] if branch_matches else nodes[-1]["id"]

    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    curr_id: str | None = leaf_id
    while curr_id is not None:
        if curr_id in visited:
            break
        visited.add(curr_id)
        node = node_map.get(curr_id)
        if node is None:
            break
        chain.append(node)
        curr_id = node.get("parent_id")

    chain.reverse()
    return chain


def _is_screenshot_message(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in content
    )


def _extract_screenshot_path(message: dict[str, Any], seq: int) -> str:
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text", ""))
                match = re.search(
                    r"(?:file(?:\s+also)?\s+at|saved\s+to)\s+(.+?)(?:[;,)]|\.\s|$)",
                    text,
                    flags=re.IGNORECASE,
                )
                if match and match.group(1).strip().rstrip("."):
                    return match.group(1).strip().rstrip(".")
                match = re.search(r"(?:[A-Za-z]:[\\/])?[^\s,;)]+screen_\d+\.jpg", text)
                if match:
                    return match.group(0)
    return f".agent/screen_{seq:03d}.jpg"


def split_for_compaction(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """Return the fold split index with recent[0] pinned to a ``user`` boundary.

    Starting the recent window on a user turn keeps tool-call pairing intact
    (no dangling ``tool`` node without its parent ``assistant`` tool_calls).
    Falls back to the last user turn before the split, then to the raw split.
    """
    total = len(messages)
    keep = max(0, int(keep_recent))
    start = max(0, total - keep)
    if start >= total:
        return start
    for index in range(start, total):
        if messages[index].get("role") == "user":
            return index
    for index in range(start - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return start


def build_branch_context(
    task_id: str,
    branch: str | None = None,
    max_screenshots: int | None = None,
    retain_screenshots: int | None = None,
    heal_dangling_tool_calls: bool = False,
) -> list[dict[str, Any]]:
    """Build the linear OpenAI message list for the target branch.

    Supports non-destructive screenshot degradation and automatic crash recovery
    for dangling tool_calls when resuming after a hard kill.
    """
    chain = build_branch_nodes(task_id, branch)
    if not chain:
        return []

    target_branch = branch or get_current_branch(task_id)

    if heal_dangling_tool_calls:
        leaf = chain[-1]
        if leaf.get("role") == "assistant" and leaf.get("tool_calls"):
            for tc in leaf.get("tool_calls") or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if not tc_id:
                    continue
                recovery_msg = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": (
                        "[CRASH_RECOVERY] Process terminated unexpectedly while executing tool; "
                        "state recovered from disk."
                    ),
                }
                new_node = append_dag_node(
                    task_id,
                    recovery_msg,
                    branch=target_branch,
                    parent_id=chain[-1]["id"],
                    node_type="message",
                    meta={"crash_recovery": True},
                )
                chain.append(new_node)

    messages = [strip_dag_envelope(node) for node in chain]

    if max_screenshots is not None:
        max_keep = max(0, int(max_screenshots))
        screenshot_positions: list[tuple[int, str]] = []
        seq = 0
        for idx, msg in enumerate(messages):
            if _is_screenshot_message(msg):
                seq += 1
                screenshot_positions.append((idx, _extract_screenshot_path(msg, seq)))
        if len(screenshot_positions) > max_keep:
            target_keep = max_keep
            if retain_screenshots is not None:
                target_keep = min(max(0, int(retain_screenshots)), max_keep)
            while len(screenshot_positions) > target_keep:
                old_idx, path = screenshot_positions.pop(0)
                old_msg = messages[old_idx]
                messages[old_idx] = {
                    "role": old_msg.get("role", "user"),
                    "content": f"[screenshot removed, file at {path}]",
                }

    return messages


def append_dag_node(
    task_id: str,
    message: dict[str, Any],
    branch: str | None = None,
    parent_id: str | None = "__AUTO__",
    node_type: str = "message",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one immutable Commit Node to checkpoint.jsonl and advance branch ref."""
    task_dir = _task_directory(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = task_dir / CHECKPOINT_FILE

    loaded = load_dag_nodes(task_id)
    existing_nodes = loaded[0] if loaded else []
    target_branch = branch or get_current_branch(task_id)

    if parent_id == "__AUTO__":
        resolved_parent = get_branch_head(task_id, target_branch)
        if resolved_parent is None and existing_nodes:
            resolved_parent = existing_nodes[-1]["id"]
    else:
        resolved_parent = parent_id

    node_id = message.get("id") if ("id" in message and "parent_id" in message) else _generate_node_id(len(existing_nodes) + 1)
    node = _wrap_message_as_node(
        message,
        node_id=node_id,
        parent_id=resolved_parent,
        branch=target_branch,
        node_type=node_type,
        meta=meta,
    )

    with checkpoint.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(node, ensure_ascii=False) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass

    set_branch_head(task_id, target_branch, node["id"])
    if not (task_dir / CURRENT_BRANCH_FILE).is_file():
        set_current_branch(task_id, target_branch)
    return node


def save_checkpoint(
    task_id: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
    branch: str | None = None,
) -> Path:
    """Save or append a message snapshot as DAG JSONL while preserving all branches."""
    task_directory = _task_directory(task_id)
    task_directory.mkdir(parents=True, exist_ok=True)
    checkpoint = task_directory / CHECKPOINT_FILE
    target_branch = branch or get_current_branch(task_id)

    loaded = load_dag_nodes(task_id)
    if loaded is None or (not loaded[0] and metadata is not None):
        with checkpoint.open("w", encoding="utf-8", newline="\n") as handle:
            if metadata is not None:
                handle.write(json.dumps({"_meta": metadata}, ensure_ascii=False) + "\n")
            prev_id: str | None = None
            for idx, message in enumerate(messages, start=1):
                node_id = _generate_node_id(idx)
                node = _wrap_message_as_node(
                    message,
                    node_id=node_id,
                    parent_id=prev_id,
                    branch=target_branch,
                )
                handle.write(json.dumps(node, ensure_ascii=False) + "\n")
                prev_id = node["id"]
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        if prev_id is not None:
            set_branch_head(task_id, target_branch, prev_id)
        set_current_branch(task_id, target_branch)
        return checkpoint

    all_nodes, existing_meta = loaded
    chain = build_branch_nodes(task_id, target_branch)
    chain_msgs = [strip_dag_envelope(n) for n in chain]

    common_len = 0
    for existing_msg, new_msg in zip(chain_msgs, messages):
        if existing_msg == strip_dag_envelope(new_msg):
            common_len += 1
        else:
            break

    parent_id = chain[common_len - 1]["id"] if common_len > 0 else None
    seq_base = len(all_nodes)
    new_nodes: list[dict[str, Any]] = []
    for offset, message in enumerate(messages[common_len:], start=1):
        node_id = _generate_node_id(seq_base + offset)
        is_summary = (
            isinstance(message.get("content"), str)
            and message.get("role") == "system"
            and message["content"].startswith("[Context Summary]")
        )
        node_type = "compaction" if is_summary else "message"
        node = _wrap_message_as_node(
            message,
            node_id=node_id,
            parent_id=parent_id,
            branch=target_branch,
            node_type=node_type,
        )
        new_nodes.append(node)
        parent_id = node["id"]

    if new_nodes:
        with checkpoint.open("a", encoding="utf-8", newline="\n") as handle:
            for node in new_nodes:
                handle.write(json.dumps(node, ensure_ascii=False) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass

    if parent_id is not None:
        set_branch_head(task_id, target_branch, parent_id)
    set_current_branch(task_id, target_branch)
    return checkpoint


def load_checkpoint(task_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Load the active branch's linear checkpoint and its optional metadata."""
    loaded = load_dag_nodes(task_id)
    if loaded is None:
        return None
    nodes, metadata = loaded
    if not nodes:
        return [], metadata
    messages = build_branch_context(task_id)
    return messages, metadata


def create_branch(
    task_id: str,
    branch_name: str,
    from_node_id: str | None = None,
    checkout: bool = True,
    force_at_assistant: bool = False,
) -> dict[str, Any]:
    """Fork a new Git-style branch from any historical node_id with tool-call pairing integrity."""
    _validate_identifier(branch_name, "branch name")
    loaded = load_dag_nodes(task_id)
    if loaded is None or not loaded[0]:
        raise ValueError(f"cannot create branch on empty or missing task: {task_id}")
    nodes, _ = loaded
    node_map = {n["id"]: n for n in nodes}

    target_id = from_node_id or get_branch_head(task_id) or nodes[-1]["id"]
    if target_id not in node_map:
        raise ValueError(f"unknown from_node_id: {target_id!r}")

    target_node = node_map[target_id]
    anchor_id = target_id

    # Tool-Call Pairing Integrity Check (Section 3.2.3)
    if target_node.get("role") == "assistant" and target_node.get("tool_calls"):
        tool_calls = target_node.get("tool_calls") or []
        expected_tc_ids = [
            tc.get("id") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")
        ]
        children_by_parent: dict[str | None, list[dict[str, Any]]] = {}
        for n in nodes:
            children_by_parent.setdefault(n.get("parent_id"), []).append(n)

        answered_ids: set[str] = set()
        curr_anchor = target_id
        if not force_at_assistant:
            while True:
                next_tools = [
                    c for c in children_by_parent.get(curr_anchor, []) if c.get("role") == "tool"
                ]
                if not next_tools:
                    break
                chosen = next_tools[-1]
                tc_id = chosen.get("tool_call_id")
                if tc_id:
                    answered_ids.add(tc_id)
                curr_anchor = chosen["id"]
                if expected_tc_ids and all(eid in answered_ids for eid in expected_tc_ids):
                    break

        if not force_at_assistant and expected_tc_ids and all(eid in answered_ids for eid in expected_tc_ids):
            anchor_id = curr_anchor
        else:
            # Append synthetic tool receipts so OpenAI tool_calls pairing stays 100% valid
            curr_parent = target_id
            for tc in tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if not tc_id:
                    continue
                synthetic_tool = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": (
                        "[BRANCH_FORKED_BEFORE_TOOL_EXECUTION] "
                        f"Branch '{branch_name}' forked at assistant tool_calls before execution."
                    ),
                }
                created = append_dag_node(
                    task_id,
                    synthetic_tool,
                    branch=branch_name,
                    parent_id=curr_parent,
                    node_type="message",
                    meta={"synthetic_fork_receipt": True, "forked_from": target_id},
                )
                curr_parent = created["id"]
            anchor_id = curr_parent

    set_branch_head(task_id, branch_name, anchor_id)
    if checkout:
        set_current_branch(task_id, branch_name)

    return {
        "task_id": task_id,
        "branch": branch_name,
        "from_node_id": target_id,
        "head_node_id": anchor_id,
        "current_branch": get_current_branch(task_id),
    }


def checkout_branch(task_id: str, branch_name: str) -> dict[str, Any]:
    """Switch the active branch pointer for task_id and return its reconstructed context."""
    _validate_identifier(branch_name, "branch name")
    branches = list_branches(task_id)
    if branch_name not in branches:
        raise ValueError(f"branch {branch_name!r} does not exist in task {task_id}")
    set_current_branch(task_id, branch_name)
    return {
        "task_id": task_id,
        "branch": branch_name,
        "head_node_id": branches[branch_name],
        "current_branch": branch_name,
        "messages": build_branch_context(task_id, branch_name),
    }


def _node_preview(node: dict[str, Any], limit: int = 60) -> str:
    """Deterministic one-line preview of a DAG node for the archived-node index table."""
    content = node.get("content")
    if isinstance(content, str) and content.strip():
        text = " ".join(content.split())
        return text[:limit] + ("…" if len(text) > limit else "")
    if node.get("tool_calls"):
        names = [
            str(tc.get("function", {}).get("name", "?"))
            for tc in node.get("tool_calls") or []
            if isinstance(tc, dict)
        ]
        return "[tool_calls: " + ", ".join(names) + "]"
    if isinstance(content, list):
        return "[multimodal observation]"
    return "[" + str(node.get("role", "?")) + " message]"


def compact_branch_dag(
    task_id: str,
    summary_text: str,
    keep_recent: int = 6,
    branch: str | None = None,
) -> dict[str, Any] | None:
    """Non-destructively compact a branch in checkpoint.jsonl without altering other branches.

    The recent window is re-anchored onto a ``user`` node (no dangling ``tool``
    node) and a deterministic archived-node index table (section 6) is appended
    to the ledger so any folded node stays addressable via read_checkpoint_node.
    """
    target_branch = branch or get_current_branch(task_id)
    chain = build_branch_nodes(task_id, target_branch)
    if len(chain) <= 1:
        return None

    keep_recent = max(0, int(keep_recent))
    root_node = chain[0]
    conversation = chain[1:]
    split = split_for_compaction(conversation, keep_recent)
    old_nodes = conversation[:split]
    recent_nodes = conversation[split:]

    if not old_nodes:
        return None

    index_lines = ["6. 已归档原始 DAG 节点索引表（可用 read_checkpoint_node 按 node_id 回溯原文）"]
    for node in old_nodes:
        clean = strip_dag_envelope(node)
        char_count = len(json.dumps(clean, ensure_ascii=False, default=str))
        index_lines.append(
            f"- {node.get('id')} ({node.get('role', '?')}): {_node_preview(clean)} [{char_count} chars]"
        )
    summary_content = summary_text.rstrip() + "\n\n" + "\n".join(index_lines)

    summary_msg = {
        "role": "system",
        "content": f"[Context Summary]\n{summary_content}",
    }
    compaction_node = append_dag_node(
        task_id,
        summary_msg,
        branch=target_branch,
        parent_id=root_node["id"],
        node_type="compaction",
        meta={
            "summarized_up_to": old_nodes[-1]["id"],
            "compacted_count": len(old_nodes),
        },
    )

    curr_parent = compaction_node["id"]
    for recent in recent_nodes:
        clean_msg = strip_dag_envelope(recent)
        cloned = append_dag_node(
            task_id,
            clean_msg,
            branch=target_branch,
            parent_id=curr_parent,
            node_type=recent.get("type", "message"),
            meta={"reparented_from": recent["id"]},
        )
        curr_parent = cloned["id"]

    set_branch_head(task_id, target_branch, curr_parent)
    return {
        "compaction_node_id": compaction_node["id"],
        "head_node_id": curr_parent,
        "branch": target_branch,
        "summarized_up_to": old_nodes[-1]["id"],
        "summary_content": summary_content,
    }


def get_task_tree(task_id: str) -> dict[str, Any] | None:
    """Return the full disk-backed DAG tree, branch pointers, tags, and status for task_id."""
    loaded = load_dag_nodes(task_id)
    status = load_status(task_id)
    if loaded is None and status is None:
        return None
    nodes, metadata = loaded if loaded else ([], {})
    step_state_path = _task_directory(task_id) / "step_state"
    step_state = None
    if step_state_path.is_file():
        try:
            step_state = json.loads(step_state_path.read_text(encoding="utf-8"))
        except Exception:
            step_state = None
    return {
        "task_id": task_id,
        "status": status or "unknown",
        "current_branch": get_current_branch(task_id),
        "branches": list_branches(task_id),
        "tags": list_tags(task_id),
        "nodes": nodes,
        "metadata": metadata,
        "step_state": step_state,
    }


def save_status(task_id: str, status: str) -> Path:
    """Write the explicit lifecycle marker file for a task and return its path."""
    task_directory = _task_directory(task_id)
    task_directory.mkdir(parents=True, exist_ok=True)
    status_file = task_directory / STATUS_FILE
    return atomic_write_text(status_file, f"{status}\n")


def load_status(task_id: str) -> str | None:
    """Read the lifecycle marker word, or return ``None`` when absent."""
    status_file = _task_directory(task_id) / STATUS_FILE
    return (_read_text_tolerant(status_file) or "").strip() or None


def generate_task_id() -> str:
    """Generate a unique task ID in local time (microsecond precision)."""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def list_tasks() -> list[dict[str, Any]]:
    """List task checkpoints with creation time, message counts, branches, and status."""
    if not TASKS_ROOT.is_dir():
        return []

    tasks: list[dict[str, Any]] = []
    for task_directory in TASKS_ROOT.iterdir():
        if not task_directory.is_dir() or not _TASK_ID_PATTERN.fullmatch(task_directory.name):
            continue
        checkpoint = task_directory / CHECKPOINT_FILE
        if not checkpoint.is_file():
            continue
        loaded = load_checkpoint(task_directory.name)
        if loaded is None:
            continue
        messages, _ = loaded
        try:
            created = datetime.fromtimestamp(checkpoint.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            continue
        tasks.append(
            {
                "id": task_directory.name,
                "created": created,
                "status": load_status(task_directory.name) or "unknown",
                "message_count": len(messages),
                "current_branch": get_current_branch(task_directory.name),
                "branches": list(list_branches(task_directory.name).keys()) or [DEFAULT_BRANCH],
            }
        )

    return sorted(tasks, key=lambda task: task["id"], reverse=True)
