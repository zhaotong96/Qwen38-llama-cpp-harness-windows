"""Conversation context lifecycle helpers with Git-style branch DAG backing.

Phase 11A adds tail-anchored ephemeral error GC: once a transient trial-and-
error chain resolves at the very end of the conversation, the failed turn
spans are folded out of the *in-memory* projection (the disk checkpoint stays
immutable) and crystallized into a single compact causal badge.

Phase 11B aligns the long-lived skeleton prefix (system prompt + fact-ledger
summary) onto the server's 1024-token DeltaNet checkpoint grid.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

from core import persistence
from core.token_grid import (
    align_skeleton_block_to_grid,
    estimate_text_tokens,
    exact_or_estimated_tokens,
)

__all__ = [
    "ContextManager",
    "estimate_text_tokens",
    "exact_or_estimated_tokens",
]

# Messages carrying any of these markers are 100% exempt from pruning: they
# encode operator interventions, gate rejections, rewind lessons, or the
# immutable architecture redlines and must never be treated as transient.
_PROTECTED_MARKERS = (
    "[Operator",
    "[EXECUTION_REJECTED]",
    "[EXECUTION_INTERRUPTED",
    "[Branch Rewind Lesson]",
    "<immutable_redlines>",
)

_EXCEPTION_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning|Failure)|exit=[1-9]\d*|JSONDecodeError)\b"
)


class ContextManager:
    """Manage conversation messages, token estimates, retained screenshots, and branch DAG sync."""

    def __init__(
        self,
        system_prompt: str,
        max_screenshots: int = 3,
        retain_screenshots: int | None = None,
        task_id: str | None = None,
        branch: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.max_screenshots = max(0, int(max_screenshots))
        if retain_screenshots is None:
            self.retain_screenshots = max(1, self.max_screenshots // 2) if self.max_screenshots else 0
        else:
            self.retain_screenshots = max(0, min(int(retain_screenshots), self.max_screenshots))
        self._system_message: dict[str, Any] = {
            "role": "system",
            "content": system_prompt,
        }
        self.messages: list[dict[str, Any]] = [self._system_message]
        self._screenshot_indices: list[int] = []
        self._screenshot_paths: dict[int, str] = {}
        self._screenshot_sequence = 0
        self.task_id: str | None = task_id
        self.branch: str = branch or persistence.DEFAULT_BRANCH
        self._grid_base_url = base_url or None
        self._skeleton_alignment: dict[str, Any] = {"aligned": False, "reason": "not_probed"}
        self._align_skeleton()

    def bind_task(self, task_id: str, branch: str | None = None, sync_initial: bool = True) -> None:
        """Bind this ContextManager to a persistent task_id and branch on disk."""
        self.task_id = task_id
        self.branch = branch or persistence.get_current_branch(task_id)
        if sync_initial:
            loaded = persistence.load_dag_nodes(task_id)
            if loaded is None or not loaded[0]:
                persistence.save_checkpoint(task_id, self.messages, branch=self.branch)
            else:
                self.reload_from_branch(self.branch)

    def reload_from_branch(self, branch: str | None = None, heal_dangling: bool = False) -> list[dict[str, Any]]:
        """Rebuild the active linear message projection from disk for the given branch."""
        if not self.task_id:
            return self.messages
        target_branch = branch or persistence.get_current_branch(self.task_id)
        self.branch = target_branch
        projected = persistence.build_branch_context(
            self.task_id,
            branch=target_branch,
            max_screenshots=self.max_screenshots,
            retain_screenshots=self.retain_screenshots,
            heal_dangling_tool_calls=heal_dangling,
        )
        if projected:
            self.messages = projected
            if self.messages and self.messages[0].get("role") == "system":
                self._system_message = self.messages[0]
        else:
            self.messages = [self._system_message]
        self._refresh_screenshot_indices()
        self._align_skeleton()
        return self.messages

    # --- Phase 11B: 1024-token checkpoint grid alignment --------------------

    def _align_skeleton(self) -> None:
        """Snap the static skeleton prefix onto the DeltaNet checkpoint grid.

        The skeleton is messages[0] (system prompt) plus, when present, the
        leading ``[Context Summary]`` fact-ledger message. Alignment is a
        deterministic pure function of the text, so re-projecting the same
        content from disk reproduces the identical prefix byte-for-byte.
        """
        applied: dict[str, Any] = {"aligned": False, "reason": "no_static_block"}
        prior = 0
        for index in range(min(2, len(self.messages))):
            message = self.messages[index]
            content = message.get("content")
            if not isinstance(content, str) or not content:
                break
            if index == 1 and not (
                message.get("role") == "system"
                and content.startswith("[Context Summary]")
            ):
                break
            try:
                padded, info = align_skeleton_block_to_grid(
                    content,
                    prior_tokens=prior,
                    base_url=self._grid_base_url,
                )
            except Exception as exc:  # grid alignment must never block the loop
                applied = {"aligned": False, "reason": f"alignment_error: {exc}"}
                break
            applied = dict(info)
            if padded != content:
                message["content"] = padded
            prior += exact_or_estimated_tokens(
                padded, base_url=self._grid_base_url
            )
        self._skeleton_alignment = applied

    def get_prefix_token_count(self) -> int:
        """Token budget of the immutable skeleton prefix, for telemetry audits."""
        prefix = 0
        for message in self.messages[:2]:
            content = message.get("content")
            if not isinstance(content, str):
                break
            if message is not self.messages[0] and not (
                message.get("role") == "system"
                and content.startswith("[Context Summary]")
            ):
                break
            prefix += exact_or_estimated_tokens(content, base_url=self._grid_base_url)
        return prefix

    @staticmethod
    def _is_screenshot(message: dict[str, Any]) -> bool:
        content = message.get("content")
        return isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in content
        )

    @staticmethod
    def _path_in_message(message: dict[str, Any]) -> str | None:
        """Extract a saved screenshot path from the text accompanying an image."""
        content = message.get("content")
        if not isinstance(content, list):
            return None

        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = str(part.get("text", ""))
            match = re.search(
                r"(?:file(?:\s+also)?\s+at|saved\s+to)\s+(.+?)(?:[;,)]|\.\s|$)",
                text,
                flags=re.IGNORECASE,
            )
            if match:
                candidate = match.group(1).strip().rstrip(".")
                if candidate:
                    return candidate

            match = re.search(r"(?:[A-Za-z]:[\\/])?[^\s,;)]+screen_\d+\.jpg", text)
            if match:
                return match.group(0)
        return None

    def _refresh_screenshot_indices(self) -> None:
        """Rebuild tracking after callers mutate the exposed message list."""
        self._screenshot_indices = [
            index
            for index, message in enumerate(self.messages)
            if isinstance(message, dict) and self._is_screenshot(message)
        ]
        self._screenshot_paths = {
            index: path
            for index, path in self._screenshot_paths.items()
            if index in self._screenshot_indices
        }

    def append(self, message: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Append a message to live context (and DAG disk store if bound) and enforce screenshot retention."""
        raw_copy = copy.deepcopy(message)
        node: dict[str, Any] | None = None
        if self.task_id:
            try:
                loaded = persistence.load_dag_nodes(self.task_id)
                if loaded is None or not loaded[0]:
                    persistence.save_checkpoint(self.task_id, self.messages, branch=self.branch)
                node = persistence.append_dag_node(
                    self.task_id,
                    raw_copy,
                    branch=self.branch,
                    meta=meta,
                )
            except Exception:
                node = None

        self._refresh_screenshot_indices()
        self.messages.append(message)

        if not self._is_screenshot(message):
            return node

        index = len(self.messages) - 1
        self._screenshot_sequence += 1
        self._screenshot_indices.append(index)
        self._screenshot_paths[index] = self._path_in_message(message) or (
            f".agent/screen_{self._screenshot_sequence:03d}.jpg"
        )

        if len(self._screenshot_indices) > self.max_screenshots:
            while len(self._screenshot_indices) > self.retain_screenshots:
                old_index = self._screenshot_indices.pop(0)
                old_message = self.messages[old_index]
                path = self._path_in_message(old_message) or self._screenshot_paths.get(
                    old_index,
                    ".agent/screen.jpg",
                )
                self.messages[old_index] = {
                    "role": old_message.get("role", "user"),
                    "content": f"[screenshot removed, file at {path}]",
                }
                self._screenshot_paths.pop(old_index, None)
        return node

    @staticmethod
    def _estimate_content(content: Any) -> int:
        if isinstance(content, str):
            return estimate_text_tokens(content)
        if isinstance(content, list):
            estimate = 0
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    estimate += 1120
                elif isinstance(part, dict) and part.get("type") == "text":
                    estimate += estimate_text_tokens(str(part.get("text", "")))
                elif part is not None:
                    estimate += estimate_text_tokens(str(part))
            return estimate
        if content is None:
            return 0
        return estimate_text_tokens(str(content))

    def token_estimate(self) -> int:
        """Weighted token estimate covering text, Chinese density, images, reasoning and tool payloads."""
        total = 0
        for message in self.messages:
            if not isinstance(message, dict):
                continue
            total += self._estimate_content(message.get("content"))
            reasoning = message.get("reasoning_content")
            if reasoning:
                total += estimate_text_tokens(str(reasoning))
            tool_calls = message.get("tool_calls")
            if tool_calls:
                total += estimate_text_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
        return total

    def as_list(self) -> list[dict[str, Any]]:
        """Return the live message list used by the chat client."""
        return self.messages

    def clear_task(self) -> None:
        """Reset the context to the original system prompt."""
        self.messages = [self._system_message]
        self._screenshot_indices.clear()
        self._screenshot_paths.clear()
        self._screenshot_sequence = 0
        self.task_id = None
        self.branch = persistence.DEFAULT_BRANCH

    def compact(
        self,
        summarizer: Callable[[list[dict[str, Any]]], str],
        token_limit: int = 8000,
        keep_recent: int = 6,
    ) -> bool:
        """Summarize older messages when the estimated context exceeds a limit.

        When bound to a task_id on disk, writes a non-destructive compaction node
        and re-parented recent messages on the current branch without mutating
        historical nodes needed by other branches.
        """
        if self.token_estimate() <= token_limit:
            return False

        keep_recent = max(0, int(keep_recent))
        conversation = self.messages[1:]
        if not conversation:
            return False

        split = persistence.split_for_compaction(conversation, keep_recent)
        old_messages = conversation[:split]
        recent_messages = conversation[split:]

        if not old_messages:
            return False

        summary = str(summarizer(old_messages))

        if self.task_id:
            try:
                result = persistence.compact_branch_dag(
                    self.task_id,
                    summary_text=summary,
                    keep_recent=keep_recent,
                    branch=self.branch,
                )
                if isinstance(result, dict) and result.get("summary_content"):
                    summary = str(result["summary_content"])
            except Exception:
                pass

        summary_message = {
            "role": "system",
            "content": f"[Context Summary]\n{summary}",
        }
        self.messages = [self.messages[0], summary_message, *recent_messages]
        self._screenshot_indices.clear()
        self._screenshot_paths.clear()
        self._refresh_screenshot_indices()
        # Phase 11B: force the fresh ledger base to land just past a 1024*K
        # grid line so llama-server freezes a durable DeltaNet checkpoint.
        self._align_skeleton()
        return True

    # --- Phase 11A: Tail-Anchored Ephemeral Error GC -----------------------

    @staticmethod
    def _is_failed_tool_receipt(content: Any) -> bool:
        """Determine if a tool execution receipt indicates transient failure."""
        if not isinstance(content, str):
            return False
        stripped = content.strip()
        if stripped.startswith("error:") or "[TDD_VERIFICATION_GATE]" in stripped:
            return True
        if re.search(r"\bexit=[1-9]\d*\b", stripped) or "Traceback (most recent call last):" in stripped:
            return True
        return False

    @classmethod
    def _extract_failure_signature(cls, content: str) -> str:
        """Extract up to two primary exception/exit signatures for compact badge crystallization."""
        matches = _EXCEPTION_RE.findall(content or "")
        if matches:
            unique = list(dict.fromkeys(matches))[:2]
            return "/".join(unique)
        first_line = (content or "").strip().splitlines()[0][:48] if content else "error"
        return first_line

    def prune_resolved_trial_tail(
        self,
        max_tail_tokens: int = 2048,
        min_error_tokens: int = 280,
        tdd_gate_open: bool = False,
    ) -> dict[str, Any]:
        """Fold resolved transient trial-and-error turn spans near the active tail.

        Guarantees:
        1. Disk checkpoint.jsonl is NEVER mutated (100% white-box auditability).
        2. Only complete (assistant + matching tools) turn spans are pruned.
        3. Never prunes while tdd_gate_open is True (waits for exit=0 test verification).
        4. Never prunes any turn containing operator redlines or rewind lessons.
        5. Only operates within max_tail_tokens of the end of self.messages to
           protect the server-side KV-cache / recurrent-state prefix.
        """
        if tdd_gate_open or len(self.messages) < 5:
            return {"pruned": False, "reason": "tdd_open_or_short"}

        # Step 1: Partition self.messages[1:] into atomic Turn Spans
        spans: list[dict[str, Any]] = []
        idx = 1
        n = len(self.messages)
        while idx < n:
            msg = self.messages[idx]
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                end_idx = idx + 1
                tool_msgs = []
                while end_idx < n and self.messages[end_idx].get("role") == "tool":
                    tool_msgs.append(self.messages[end_idx])
                    end_idx += 1
                all_succeeded = bool(tool_msgs) and all(
                    not self._is_failed_tool_receipt(tm.get("content")) for tm in tool_msgs
                )
                raw_text = json.dumps(self.messages[idx:end_idx], ensure_ascii=False, default=str)
                protected = any(marker in raw_text for marker in _PROTECTED_MARKERS)
                span_tokens = sum(
                    self._estimate_content(m.get("content"))
                    + estimate_text_tokens(str(m.get("reasoning_content") or ""))
                    + estimate_text_tokens(json.dumps(m.get("tool_calls") or [], ensure_ascii=False))
                    for m in self.messages[idx:end_idx]
                )
                spans.append({
                    "kind": "tool_turn",
                    "start": idx,
                    "end": end_idx,
                    # Any failed receipt inside the span marks it as a transient
                    # trial attempt (mixed patch-success + failing-test spans of
                    # the TDD loop fold together with the pure failures).
                    "prunable": bool(tool_msgs) and not all_succeeded and not protected,
                    "all_succeeded": all_succeeded,
                    "tokens": span_tokens,
                    "tool_names": [
                        (tc.get("function") or {}).get("name", "tool")
                        for tc in (msg.get("tool_calls") or [])
                        if isinstance(tc, dict)
                    ],
                    "errors": [
                        self._extract_failure_signature(str(tm.get("content", "")))
                        for tm in tool_msgs
                        if self._is_failed_tool_receipt(tm.get("content"))
                    ],
                    "node_ids": [
                        m.get("id") for m in self.messages[idx:end_idx] if m.get("id")
                    ],
                })
                idx = end_idx
            else:
                spans.append({
                    "kind": role or "other",
                    "start": idx,
                    "end": idx + 1,
                    "prunable": False,
                    "all_succeeded": False,
                    "tokens": self._estimate_content(msg.get("content")),
                    "tool_names": [],
                    "errors": [],
                    "node_ids": [],
                })
                idx += 1

        if len(spans) < 2 or not spans[-1]["all_succeeded"]:
            return {"pruned": False, "reason": "latest_turn_not_clean_success"}

        # Step 2: Walk backward from spans[-2] within max_tail_tokens
        tail_tokens = spans[-1]["tokens"]
        prunable_indices: list[int] = []
        pruned_tokens = 0
        error_sigs: list[str] = []
        pruned_node_ids: list[str] = []

        for s_idx in range(len(spans) - 2, -1, -1):
            sp = spans[s_idx]
            if tail_tokens + sp["tokens"] > max_tail_tokens:
                break
            tail_tokens += sp["tokens"]
            if sp["prunable"]:
                prunable_indices.append(s_idx)
                pruned_tokens += sp["tokens"]
                error_sigs.extend(sp.get("errors", []))
                pruned_node_ids.extend(sp.get("node_ids", []))
            elif sp["kind"] == "user":
                # Never prune across an interactive user boundary
                break

        if not prunable_indices or (pruned_tokens < min_error_tokens and len(prunable_indices) < 2):
            return {"pruned": False, "reason": "below_pareto_threshold"}

        # Step 3: Build causal crystallization badge
        prunable_set = set(prunable_indices)
        unique_sigs = ", ".join(list(dict.fromkeys(error_sigs))[:3]) or "transient error"
        node_ref = (
            f" (DAG nodes {pruned_node_ids[-1]}..{pruned_node_ids[0]})"
            if pruned_node_ids
            else ""
        )
        winning_tools = "/".join(dict.fromkeys(spans[-1].get("tool_names", ["tool"])))
        badge = (
            f"[Transient Attempt Pruned: {len(prunable_indices)} prior failed attempt(s) "
            f"({unique_sigs}) resolved via {winning_tools}{node_ref}]\n"
        )

        # Inject badge into first tool receipt of winning span
        win_start, win_end = spans[-1]["start"], spans[-1]["end"]
        for m_idx in range(win_start + 1, win_end):
            if self.messages[m_idx].get("role") == "tool" and isinstance(self.messages[m_idx].get("content"), str):
                if "[Transient Attempt Pruned:" not in self.messages[m_idx]["content"]:
                    self.messages[m_idx]["content"] = badge + self.messages[m_idx]["content"]
                break

        # Step 4: Rebuild self.messages excluding pruned spans
        new_messages = [self.messages[0]]
        for s_idx, sp in enumerate(spans):
            if s_idx in prunable_set:
                continue
            new_messages.extend(self.messages[sp["start"]:sp["end"]])

        self.messages = new_messages
        self._refresh_screenshot_indices()
        return {
            "pruned": True,
            "pruned_turns": len(prunable_indices),
            "tokens_saved": pruned_tokens - estimate_text_tokens(badge),
            "badge": badge.strip(),
        }
