"""Conversation context lifecycle helpers with Git-style branch DAG backing."""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

from core import persistence


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF      # CJK Unified Ideographs Extension A
        or 0x4E00 <= code <= 0x9FFF   # CJK Unified Ideographs
        or 0xF900 <= code <= 0xFAFF   # CJK Compatibility Ideographs
        or 0x3000 <= code <= 0x303F   # CJK punctuation
        or 0xFF00 <= code <= 0xFFEF   # Full-width forms
    )


def estimate_text_tokens(text: str) -> int:
    """Weighted character estimate: CJK 0.75 token/char, other text 0.25 token/char."""
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    return int(cjk * 0.75 + (len(text) - cjk) * 0.25)


class ContextManager:
    """Manage conversation messages, token estimates, retained screenshots, and branch DAG sync."""

    def __init__(
        self,
        system_prompt: str,
        max_screenshots: int = 3,
        retain_screenshots: int | None = None,
        task_id: str | None = None,
        branch: str | None = None,
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
        return self.messages

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
                self.branch = persistence.get_current_branch(self.task_id)
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
        return True
