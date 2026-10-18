"""Shared test scaffolding.

Two jobs:

1. Deterministic fakes: ``ScriptedLLM`` replaces the OpenAI client.
2. Isolation: ``IsolatedCase`` redirects all task/inbox file state into a temp
   directory so tests never write into the repository's .agent/.

The ``openai`` import shim lives in ``tests/__init__.py`` so it is always
installed before the harness core gets imported.
"""
from __future__ import annotations

import contextlib
import copy
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from core import agent as agent_mod
from core import persistence
from core import tools as tools_mod


# --- streamed reply fakes -------------------------------------------------
def _chunk(content: str = "", tool_calls: list | None = None) -> types.SimpleNamespace:
    delta = types.SimpleNamespace(content=content or None, reasoning_content=None, tool_calls=tool_calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta, finish_reason=None)])


def _final_chunk() -> types.SimpleNamespace:
    delta = types.SimpleNamespace(content=None, reasoning_content=None, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta, finish_reason="stop")])


def _chunks_for(content: str, tool_calls: list[dict[str, Any]]) -> list[types.SimpleNamespace]:
    chunks: list[types.SimpleNamespace] = []
    for index, call in enumerate(tool_calls):
        function = types.SimpleNamespace(name=call["name"], arguments=call.get("arguments", "{}"))
        delta = types.SimpleNamespace(index=index, id=call.get("id", f"call_{index}"), function=function)
        chunks.append(_chunk(tool_calls=[delta]))
    if content:
        chunks.append(_chunk(content=content))
    chunks.append(_final_chunk())
    return chunks


class ScriptedLLM:
    """Deterministic stand-in for the OpenAI client (scripted replies, no network)."""

    def __init__(self, replies: list[dict[str, Any]] | None = None) -> None:
        self.replies = list(replies or [])
        self.requests: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any):
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = copy.deepcopy(snapshot["messages"])
        self.requests.append(snapshot)
        reply = self.replies.pop(0) if self.replies else {"content": "TASK_DONE: stub"}
        if reply.get("raise"):
            raise RuntimeError(reply["raise"])
        if kwargs.get("stream"):
            content = reply.get("content", "")
            tool_calls = reply.get("tool_calls") or []
            delay = float(reply.get("delay") or 0.0)

            def gen():
                if delay:
                    time.sleep(delay)
                yield from _chunks_for(content, tool_calls)

            return gen()
        message = types.SimpleNamespace(content=reply.get("content", ""), reasoning_content=None, tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason="stop")])


def build_agent(replies: list[dict[str, Any]] | None = None, **agent_kwargs: Any):
    """Construct an Agent whose LLM client is a ScriptedLLM (no network use)."""
    llm = ScriptedLLM(replies)
    with mock.patch.object(agent_mod, "OpenAI", lambda *args, **kwargs: llm):
        agent = agent_mod.Agent(**agent_kwargs)
    return agent, llm


# --- filesystem isolation -------------------------------------------------
class IsolatedCase(unittest.TestCase):
    """Redirect task/inbox state into a temp directory for the duration of a test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gemma_test_")
        self.state = Path(self._tmp.name)
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(mock.patch.object(persistence, "TASKS_ROOT", self.tasks_root))
        self._stack.enter_context(mock.patch.object(agent_mod, "INBOX", self.state / "inbox"))
        self._stack.enter_context(
            mock.patch.object(agent_mod, "INBOX_PROCESSED", self.state / "inbox_processed")
        )
        self.addCleanup(self._cleanup)

    @property
    def tasks_root(self) -> Path:
        return self.state / "tasks"

    def _cleanup(self) -> None:
        self._stack.close()
        self._tmp.cleanup()

    # convenience for tests that exercise skill discovery
    def write_skill(self, name: str, body: str) -> Path:
        path = self.state / "skills" / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def patch_skill_roots(self):
        return mock.patch.object(tools_mod, "SKILL_ROOTS", (self.state / "skills",))