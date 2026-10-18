"""Conversation context lifecycle helpers with Git-style branch DAG backing."""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from core import persistence


class ContextManager:
    """Manage conversation messages, token estimates, retained screenshots, and branch DAG sync."""

    def __init__(
        self,
        system_prompt: str,
        max_screenshots: int = 3,
        task_id: str | None = None,
        branch: str | None = None,
    ) -> None:
        self.max_screenshots = max(0, int(max_screenshots))
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

        while len(self._screenshot_indices) > self.max_screenshots:
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
            return len(content) // 4
        if isinstance(content, list):
            estimate = 0
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    estimate += 800
                elif isinstance(part, dict) and part.get("type") == "text":
                    estimate += len(str(part.get("text", ""))) // 4
                elif part is not None:
                    estimate += len(str(part)) // 4
            return estimate
        if content is None:
            return 0
        return len(str(content)) // 4

    def token_estimate(self) -> int:
        """Return a rough token count using text length and fixed image cost."""
        return sum(
            self._estimate_content(message.get("content"))
            for message in self.messages
            if isinstance(message, dict)
        )

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
        if keep_recent:
            old_messages = conversation[:-keep_recent]
            recent_messages = conversation[-keep_recent:]
        else:
            old_messages = conversation
            recent_messages = []

        if not old_messages:
            return False

        summary = str(summarizer(old_messages))
        summary_message = {
            "role": "system",
            "content": f"[Context Summary]\n{summary}",
        }

        if self.task_id:
            try:
                persistence.compact_branch_dag(
                    self.task_id,
                    summary_text=summary,
                    keep_recent=keep_recent,
                    branch=self.branch,
                )
            except Exception:
                pass

        self.messages = [self.messages[0], summary_message, *recent_messages]
        self._screenshot_indices.clear()
        self._screenshot_paths.clear()
        self._refresh_screenshot_indices()
        return True
