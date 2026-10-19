"""
Core Agent Class for LLM interaction, ReAct execution, Git-style DAG context,
3-gate interrupt handling, and Skill management.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock

from openai import OpenAI

from core import config as config_mod
from core.config import AgentConfig, load_config, record_usage
from core.context import ContextManager
from core.interrupt import (
    consume_abort_flag,
    consume_step_continue,
    get_step_mode,
    is_step_rejected,
    wait_for_step_continue,
    write_step_state,
)
from core.logger import setup_logger
from core.models import (
    AGENTS_PATH,
    GEN_OVERHEAD_MARGIN,
    GOAL_PATH,
    INBOX,
    INBOX_PROCESSED,
    SCREEN_PATH,
)
from core import persistence
from core.persistence import (
    append_stream_event,
    generate_task_id,
    load_checkpoint,
    save_checkpoint,
    save_status,
    set_current_session,
)
from core import queue_manager
from core.tools import (
    discover_skills,
    load_text,
    log_safety,
    set_active_execution_context,
)
from core.validator import ToolValidator


def _terminal_status(final: str, was_aborted: bool = False) -> str:
    """Map the final answer's markers to the explicit status file vocabulary."""
    if "TASK_DONE" in final:
        return "done"
    if was_aborted and "TASK_ABORTED" in final:
        return "interrupted"
    if "TASK_BLOCKED" in final or "TASK_ABORTED" in final:
        return "blocked"
    return "finished"


def _strip_images(messages: list) -> int:
    """Replace multimodal user turns with text-only (server without mmproj)."""
    n = 0
    for m in messages:
        c = m.get("content")
        if m.get("role") == "user" and isinstance(c, list):
            texts = [
                p.get("text", "")
                for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            m["content"] = (
                " ".join(t for t in texts if t)
                + f" (image not sent: no mmproj; file at {SCREEN_PATH})"
            )
            n += 1
    return n


class FileAndSseObserver:
    """Pluggable stream observer writing to live_stream.jsonl and notifying SSE listeners."""

    def __init__(self, stream_to_stdout: bool = True) -> None:
        self.stream_to_stdout = stream_to_stdout
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def emit(
        self,
        task_id: str | None,
        event_type: str,
        delta: Any,
        step_id: str = "",
        tasks_root: Path | None = None,
    ) -> dict[str, Any] | None:
        event: dict[str, Any] | None = None
        if task_id:
            try:
                if tasks_root is not None:
                    with mock.patch.object(persistence, "TASKS_ROOT", tasks_root):
                        event = append_stream_event(task_id, event_type, delta, step_id=step_id)
                else:
                    event = append_stream_event(task_id, event_type, delta, step_id=step_id)
            except Exception:
                event = None

        if event is None:
            event = {
                "seq": 0,
                "timestamp": "",
                "task_id": task_id,
                "step_id": step_id,
                "event_type": event_type,
                "delta": delta,
            }
        else:
            event["task_id"] = task_id

        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                pass
        return event


class Agent:
    """Core Agent class encapsulating conversation loop, skill dispatch, and execution state."""

    RISKY_TOOLS = frozenset(
        {"shell", "write_file", "patch", "git", "mouse", "keyboard", "subagent", "add_skill", "modify_skill"}
    )
    READ_TIMEOUT_BASE = 300.0
    READ_TIMEOUT_TOKENS_PER_SECOND = 400

    def __init__(
        self,
        config: AgentConfig | None = None,
        system_prompt: str | None = None,
        validator: ToolValidator | None = None,
        goal_mode: bool = False,
        observer: FileAndSseObserver | None = None,
        role: str = "main",
        tasks_root_override: Path | None = None,
        parent_task_id: str | None = None,
        parent_tasks_root: Path | None = None,
        stream_to_stdout: bool = True,
    ) -> None:
        self.role = role
        self.config = config or AgentConfig()
        self.logger = setup_logger(log_level=self.config.log_level)
        self.client = OpenAI(base_url=self.config.base_url, api_key=getattr(self.config, "api_key", "local"))
        self.compactor_config: AgentConfig | None = None
        self.compactor_client: Any = None
        self._init_role_clients()

        self.validator = validator or ToolValidator()
        self.goal_mode = goal_mode
        self.vision_ok = [True]
        self.observer = observer or FileAndSseObserver(stream_to_stdout=stream_to_stdout)
        self._tasks_root = tasks_root_override
        self.parent_task_id = parent_task_id
        self.parent_tasks_root = parent_tasks_root
        self.last_aborted = False

        self.tools: list[dict[str, Any]] = []
        self.dispatchers: dict[str, Any] = {}
        self.load_skills()

        base_system = system_prompt or self.build_system()
        if self.goal_mode:
            base_system += "\n\nGOAL MODE ACTIVE: You must continue working until the goal is fully achieved. Put TASK_DONE only when complete."

        self.context = ContextManager(
            base_system,
            max_screenshots=6,
            retain_screenshots=2,
        )
        self.current_task_id: str | None = None
        self._active_step_id: str = ""

    def _init_role_clients(self) -> None:
        """Initialize optional tiered compactor client when configured."""
        try:
            providers_data = config_mod.load_providers_file()
            has_compactor_env = bool(
                os.environ.get("COMPACT_BASE_URL")
                or os.environ.get("COMPACT_MODEL")
                or os.environ.get("COMPACT_API_KEY")
            )
            if providers_data is not None or has_compactor_env:
                self.compactor_config = load_config(role="compactor")
                self.compactor_client = OpenAI(
                    base_url=self.compactor_config.base_url,
                    api_key=self.compactor_config.api_key,
                )
            else:
                self.compactor_config = self.config
                self.compactor_client = self.client
        except Exception:
            self.compactor_config = self.config
            self.compactor_client = self.client

    def reload_providers(self) -> dict[str, AgentConfig]:
        """Reload role-tiered provider configs from .agent/providers.json."""
        configs = config_mod.load_all_role_configs()
        main_cfg = configs.get(self.role) or configs["main"]
        self.config = main_cfg
        self.client = OpenAI(base_url=main_cfg.base_url, api_key=main_cfg.api_key)
        comp_cfg = configs.get("compactor") or main_cfg
        self.compactor_config = comp_cfg
        self.compactor_client = OpenAI(base_url=comp_cfg.base_url, api_key=comp_cfg.api_key)
        self.load_skills()
        return configs

    @contextlib.contextmanager
    def _scoped_persistence(self):
        if self._tasks_root is not None:
            with mock.patch.object(persistence, "TASKS_ROOT", self._tasks_root):
                yield
        else:
            yield

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Backward-compatible read access to the live context messages."""
        return self.context.as_list()

    def build_system(self) -> str:
        """Assemble the system prompt from .agent/AGENTS.md (required) and .agent/GOAL.md."""
        agents = load_text(AGENTS_PATH)
        if not agents:
            raise RuntimeError(f"missing required runtime rules: {AGENTS_PATH}")
        parts = [agents]
        goal = load_text(GOAL_PATH)
        if goal:
            parts.append(f"Long-term goal (GOAL.md):\n{goal}")
        skills = discover_skills()
        if skills:
            lines = ["Available skills (call read_skill before using a relevant skill):"]
            for skill in skills:
                lines.append(
                    f"- {skill['name']}: {skill['description']} (path: {skill['path']})"
                )
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def load_skills(self) -> None:
        """Load skills from skills/ directory into schemas and dispatchers."""
        self.tools.clear()
        self.dispatchers.clear()

        for skill in discover_skills():
            module = skill["module"]

            if not hasattr(module, "DESCRIPTION") or not getattr(module, "DESCRIPTION"):
                raise ValueError(f"Skill '{skill['name']}' missing required attribute 'DESCRIPTION'")

            if not hasattr(module, "PARAMETERS"):
                raise ValueError(f"Skill '{skill['name']}' missing required attribute 'PARAMETERS'")

            name = skill["name"]
            self.tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": getattr(module, "DESCRIPTION"),
                        "parameters": getattr(module, "PARAMETERS"),
                    },
                }
            )

            if hasattr(module, "HANDLER"):
                handler = getattr(module, "HANDLER")
                self.dispatchers[name] = lambda args, h=handler: h(**args) if isinstance(args, dict) else h(args)
            elif callable(getattr(module, "run", None)):
                def _invoke_run(args, m=module):
                    runtime = {
                        "CHROME_CDP_PORT": self.config.chrome_cdp_port,
                        "client": self.client,
                        "MODEL": self.config.model,
                        "CHAT_EXTRA": self.config.chat_extra,
                        "MAX_TOKENS": self.config.max_gen_tokens,
                        "task_id": self.current_task_id,
                        "parent_task_id": self.parent_task_id or self.current_task_id,
                        "tasks_root": self._tasks_root,
                        "parent_tasks_root": self.parent_tasks_root,
                    }
                    return m.run(args, runtime)

                self.dispatchers[name] = _invoke_run
            else:
                raise ValueError(f"Skill '{name}' must define either 'HANDLER' or 'run(args, runtime)'")

    def reload_skills(self) -> None:
        """Reload all skills from disk dynamically."""
        self.logger.info("Reloading skills from disk...")
        self.load_skills()

    def _is_step_rejected(self) -> dict[str, Any] | None:
        """Check abort.flag and step_state.rejected on current task and optional parent task."""
        if self.current_task_id:
            info = is_step_rejected(self.current_task_id, base_root=self._tasks_root)
            if info is not None:
                return info
        if self.parent_task_id:
            parent_info = is_step_rejected(self.parent_task_id, base_root=self.parent_tasks_root)
            if parent_info is not None:
                cascaded = dict(parent_info)
                cascaded["halt_loop"] = True
                cascaded["reason"] = f"Cascaded abort from parent task {self.parent_task_id}: {parent_info.get('reason', '')}"
                return cascaded
        return None

    def _should_pause_before_tool(self, tool_name: str) -> bool:
        """Decide whether the pre-dispatch risk gate must stop before this tool."""
        if not self.current_task_id:
            return False
        mode = get_step_mode(self.current_task_id, base_root=self._tasks_root)
        if mode == "step_all":
            return True
        if mode == "step_risky":
            return tool_name in self.RISKY_TOOLS
        return False

    def _pause_for_operator(self, step_id: str, round_i: int, tool_name: str) -> dict[str, Any] | None:
        """Persist paused_before_tool and block until release or abort.

        Returns abort info when the operator rejected, otherwise None.
        """
        if not self.current_task_id:
            return None
        write_step_state(
            self.current_task_id,
            step_id,
            round_i,
            phase="paused_before_tool",
            rejected=False,
            active_tool=tool_name,
            base_root=self._tasks_root,
        )
        self.observer.emit(
            self.current_task_id,
            "step_phase",
            {"phase": "paused_before_tool", "round": round_i, "tool": tool_name},
            step_id=step_id,
            tasks_root=self._tasks_root,
        )
        self.logger.info(f"[step-gate] paused before tool '{tool_name}'; awaiting step_continue.flag")
        abort_info = wait_for_step_continue(self.current_task_id, base_root=self._tasks_root)
        if abort_info is None:
            self.observer.emit(
                self.current_task_id,
                "step_phase",
                {"phase": "step_released", "round": round_i, "tool": tool_name},
                step_id=step_id,
                tasks_root=self._tasks_root,
            )
        return abort_info

    def _consume_inbox(self) -> list[str]:
        """Consume operator messages from both legacy .agent/inbox/ and unified .agent/queue/."""
        notes: list[str] = []

        # 1. Legacy .agent/inbox/*.json
        INBOX.mkdir(parents=True, exist_ok=True)
        INBOX_PROCESSED.mkdir(parents=True, exist_ok=True)
        for p in sorted(INBOX.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                text = str(data.get("message", "")).strip()
                if text:
                    notes.append(text)
                dest = INBOX_PROCESSED / p.name
                if dest.exists():
                    dest.unlink()
                p.replace(dest)
            except Exception as e:
                log_safety(f"INBOX_ERROR file={p} error={e}")

        # 2. Unified .agent/queue/*.json (only 'queued' status; 'held' items are skipped)
        if self.role == "main":
            try:
                with self._scoped_persistence():
                    target_branch = (
                        persistence.get_current_branch(self.current_task_id)
                        if self.current_task_id
                        else None
                    )
                    queued_items = queue_manager.consume_ready(target_branch=target_branch, by="agent")
                for item in queued_items:
                    content = str(item.get("content", "")).strip()
                    if content:
                        notes.append(content)
                        self.observer.emit(
                            self.current_task_id,
                            "queue_consumed",
                            {"id": item.get("id"), "content": content, "source": item.get("source")},
                            step_id=self._active_step_id,
                            tasks_root=self._tasks_root,
                        )
            except Exception as e:
                log_safety(f"QUEUE_CONSUME_ERROR error={e}")

        return notes

    def _adaptive_read_timeout(self) -> float:
        """Scale the HTTP read timeout with the live context size.

        A large-context prefill on the remote host can legitimately exceed a
        fixed client timeout; scaling with the token estimate keeps long
        interactions from being cut off as false failures.
        """
        return self.READ_TIMEOUT_BASE + self.context.token_estimate() / self.READ_TIMEOUT_TOKENS_PER_SECOND

    def chat(self, tool_choice: str = "auto"):
        # Dynamic generation cap: never exceed the remaining window space.
        gen_cap = min(
            self.config.max_gen_tokens,
            self.config.n_ctx - self.context.token_estimate() - GEN_OVERHEAD_MARGIN,
        )
        gen_cap = max(gen_cap, 1024)  # floor so tiny payloads keep a usable budget
        kwargs = dict(
            model=self.config.model,
            messages=self.context.as_list(),
            tools=self.tools,
            tool_choice=tool_choice,
            temperature=0.2,
            top_p=0.95,
            max_tokens=gen_cap,
            stream=True,
            extra_body=self.config.chat_extra,
            timeout=self._adaptive_read_timeout(),
        )
        last_err: Exception | None = None
        for attempt in range(1, 9):
            try:
                return self.client.chat.completions.create(**kwargs)
            except Exception as e:
                last_err = e
                err = str(e).lower()
                if "mmproj" in err or "image input is not supported" in err:
                    self.vision_ok[0] = False
                    n = _strip_images(self.context.as_list())
                    self.context._refresh_screenshot_indices()
                    self.logger.warning(f"vision unsupported on server — stripped {n} image(s), retry")
                    return self.client.chat.completions.create(**kwargs)
                if any(x in err for x in ("503", "loading model", "unavailable", "connection", "timeout", "502", "504")):
                    wait = min(60, 5 * attempt)
                    self.logger.warning(f"LLM busy/503 attempt {attempt}/8 — sleep {wait}s: {e}")
                    time.sleep(wait)
                    continue
                raise
        raise last_err if last_err else RuntimeError("chat failed")

    def collect_stream(self, stream):
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason = None
        printed = False
        reasoning_printed = False
        abort_detected: dict[str, Any] | None = None
        output_queue: queue.SimpleQueue = queue.SimpleQueue()
        stream_to_stdout = bool(getattr(self.observer, "stream_to_stdout", True))

        def write_output() -> None:
            phase = ""
            while True:
                item = output_queue.get()
                if item is None:
                    if phase and stream_to_stdout:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                    return
                kind, text = item
                if not stream_to_stdout:
                    continue
                if kind == "reasoning" and not phase:
                    sys.stdout.write("[thinking] ")
                    phase = "reasoning"
                elif kind == "content" and phase != "content":
                    if phase == "reasoning":
                        sys.stdout.write("\n")
                    sys.stdout.write("assistant: ")
                    phase = "content"
                sys.stdout.write(text)
                sys.stdout.flush()

        output_thread = threading.Thread(target=write_output, daemon=True)
        output_thread.start()

        try:
            for chunk in stream:
                # Gate 1: In-Stream Decoding Abort Check
                current_abort = self._is_step_rejected()
                if current_abort is not None:
                    abort_detected = current_abort
                    if current_abort.get("mode") == "abort_stream":
                        if hasattr(stream, "close"):
                            try:
                                stream.close()
                            except Exception:
                                pass
                        break

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta
                reasoning = getattr(delta, "reasoning_content", None) or ""
                if reasoning:
                    reasoning_printed = True
                    reasoning_parts.append(reasoning)
                    output_queue.put(("reasoning", reasoning))
                    self.observer.emit(
                        self.current_task_id,
                        "reasoning_delta",
                        reasoning,
                        step_id=self._active_step_id,
                        tasks_root=self._tasks_root,
                    )
                content = getattr(delta, "content", None) or ""
                if content:
                    printed = True
                    content_parts.append(content)
                    output_queue.put(("content", content))
                    self.observer.emit(
                        self.current_task_id,
                        "content_delta",
                        content,
                        step_id=self._active_step_id,
                        tasks_root=self._tasks_root,
                    )
                for tc in getattr(delta, "tool_calls", None) or []:
                    part = tool_parts.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        part["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn:
                        fn_name = getattr(fn, "name", None) or ""
                        fn_args = getattr(fn, "arguments", None) or ""
                        part["name"] += fn_name
                        part["arguments"] += fn_args
                        self.observer.emit(
                            self.current_task_id,
                            "tool_call_delta",
                            {"index": tc.index, "id": part["id"], "name": fn_name, "arguments": fn_args},
                            step_id=self._active_step_id,
                            tasks_root=self._tasks_root,
                        )
        finally:
            output_queue.put(None)
            output_thread.join()

        if abort_detected is None:
            abort_detected = self._is_step_rejected()

        tool_calls = [
            SimpleNamespace(
                id=part["id"],
                function=SimpleNamespace(
                    name=part["name"], arguments=part["arguments"]
                ),
            )
            for _, part in sorted(tool_parts.items())
        ]
        message = SimpleNamespace(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts),
            tool_calls=tool_calls or None,
            streamed=printed,
            reasoning_streamed=reasoning_printed,
            rejected=bool(abort_detected),
            abort_info=abort_detected,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
        )

    _LEDGER_INSTRUCTION = (
        "你是长程任务的事实账本压缩器（Fact Ledger Compactor）。把下面的对话历史压缩为一份"
        "结构化中文 Markdown 事实账本。必须输出且只输出以下五个编号小节；"
        "信息密度优先，保留具体文件路径、命令、报错原文与数字，不要寒暄：\n"
        "1. 核心目标与进度\n"
        "2. 已验证事实与环境基线\n"
        "3. 已修改文件清单\n"
        "4. 被否决方案与操作员红线（永久保留）\n"
        "5. 待完成子目标与下一步行动\n"
    )

    _REDLINE_MARKERS = (
        "[Operator correction]",
        "[EXECUTION_REJECTED]",
        "[EXECUTION_INTERRUPTED_AND_IGNORED]",
    )
    _SECTION4_HEADING = "4. 被否决方案与操作员红线（永久保留）"
    _REDLINE_PLACEHOLDERS = frozenset({"无", "none", "n/a", "(none)", "-"})

    @staticmethod
    def _strip_archived_index(content: str) -> str:
        """Drop the legacy section 6 index table before sending history to the LLM."""
        marker = "\n\n6. 已归档原始 DAG 节点索引表"
        if marker in content:
            return content.split(marker, 1)[0]
        return content

    @staticmethod
    def _ledger_section(content: str, number: int) -> str:
        """Return the body of numbered ledger section ``number`` (empty when absent)."""
        match = re.search(rf"(?m)^\s*{number}\.\s.*$", content)
        if not match:
            return ""
        start = match.end()
        next_match = re.search(rf"(?m)^\s*{number + 1}\.\s", content[start:])
        end = start + next_match.start() if next_match else len(content)
        return content[start:end]

    @classmethod
    def _extract_pinned_redlines(cls, messages: list[dict[str, Any]]) -> list[str]:
        """Collect every operator redline verbatim from prior ledgers and this window."""
        pins: list[str] = []

        def add(line: str) -> None:
            value = line.strip()
            while value.startswith(("- ", "* ")):
                value = value[2:].strip()
            if value and value not in pins:
                pins.append(value)

        for message in messages:
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if content.startswith("[Context Summary]"):
                for line in cls._ledger_section(content, 4).splitlines():
                    value = line.strip().lstrip("-* ").strip()
                    if value and value.lower() not in cls._REDLINE_PLACEHOLDERS:
                        add(value)
                continue
            for marker in cls._REDLINE_MARKERS:
                if marker not in content:
                    continue
                if marker == "[Operator correction]":
                    for block in content.split(marker)[1:]:
                        tail = " ".join(block.strip().split())
                        while tail.startswith(marker):
                            tail = tail[len(marker):].strip()
                        if tail:
                            add(f"{marker} {tail}")
                    continue
                for line in content.splitlines():
                    if marker in line:
                        add(line)
        return pins

    def _can_prefix_share(self, messages: list[dict[str, Any]]) -> bool:
        """True when the compactor reuses the main slot and the prefix is still live."""
        comp_cfg = self.compactor_config or self.config
        if (comp_cfg.base_url, comp_cfg.model) != (self.config.base_url, self.config.model):
            return False
        prefix_len = len(messages) + 1
        if len(self.messages) < prefix_len:
            return False
        prefix = self.messages[:prefix_len]
        if prefix[0].get("role") != "system":
            return False
        for live, folded in zip(prefix[1:], messages):
            live_content = live.get("content")
            folded_content = folded.get("content")
            if isinstance(live_content, str) and live_content.startswith("[Context Summary]"):
                live_content = self._strip_archived_index(live_content)
            if isinstance(folded_content, str) and folded_content.startswith("[Context Summary]"):
                folded_content = self._strip_archived_index(folded_content)
            if live_content != folded_content or live.get("role") != folded.get("role"):
                return False
        return True

    @classmethod
    def _enforce_redlines(cls, summary: str, pinned: list[str]) -> str:
        """Guarantee every pinned redline survives verbatim in ledger section 4."""
        if not pinned:
            return summary
        heading = cls._SECTION4_HEADING
        if heading not in summary:
            summary = summary.rstrip() + f"\n\n{heading}\n"
        section4 = cls._ledger_section(summary, 4)
        missing = [pin for pin in pinned if pin not in section4]
        if not missing:
            return summary
        additions = "\n".join(f"- {pin}" for pin in missing)
        section4_start = summary.find(heading) + len(heading)
        next_heading = re.search(r"(?m)^\s*5\.\s", summary[section4_start:])
        if next_heading:
            insert_at = section4_start + next_heading.start()
            head = summary[:insert_at].rstrip()
            tail = summary[insert_at:]
            return f"{head}\n{additions}\n\n{tail}"
        return summary.rstrip() + "\n" + additions + "\n"

    def _summarize(self, messages: list[dict[str, Any]]) -> str:
        """Ask the compactor for a five-section ledger with deterministic redline retention."""
        pinned_redlines = self._extract_pinned_redlines(messages)
        redline_block = ""
        if pinned_redlines:
            redline_block = (
                "\n<immutable_redlines>\n"
                + "\n".join(pinned_redlines)
                + "\n</immutable_redlines>\n"
                "第 4 节必须逐字原样保留上述每一条红线，禁止改写、合并或删除。\n"
            )
        instruction = self._LEDGER_INSTRUCTION + redline_block

        comp_cfg = self.compactor_config or self.config
        comp_client = self.compactor_client if (self.compactor_client is not None and comp_cfg is not self.config) else self.client
        extra_body = dict(comp_cfg.chat_extra or {})
        template_kwargs = dict(extra_body.get("chat_template_kwargs") or {})
        template_kwargs["enable_thinking"] = False
        extra_body["chat_template_kwargs"] = template_kwargs

        if self._can_prefix_share(messages):
            prefix = [dict(m) for m in self.messages[: len(messages) + 1]]
            request_messages = [*prefix, {"role": "user", "content": instruction}]
            extra_body["id_slot"] = 0
        else:
            if "id_slot" in extra_body:
                preferred_slot = int(extra_body["id_slot"])
                available_slot = config_mod.select_available_slot(
                    comp_cfg.base_url, preferred=preferred_slot
                )
                if available_slot is not None:
                    extra_body["id_slot"] = available_slot
            cleaned: list[dict[str, Any]] = []
            for message in messages:
                item = dict(message)
                content = item.get("content")
                if isinstance(content, str) and content.startswith("[Context Summary]"):
                    item["content"] = self._strip_archived_index(content)
                cleaned.append(item)
            history = json.dumps(cleaned, ensure_ascii=False, default=str)
            prompt = f"{instruction}\n对话历史（JSON）：\n{history}"
            request_messages = [{"role": "user", "content": prompt}]

        response = comp_client.chat.completions.create(
            model=comp_cfg.model,
            messages=request_messages,
            temperature=0.2,
            max_tokens=2048,
            stream=False,
            extra_body=extra_body,
            timeout=self._adaptive_read_timeout(),
        )
        message = response.choices[0].message
        summary = (getattr(message, "content", None) or "").strip()
        if not summary:
            summary = (getattr(message, "reasoning_content", None) or "").strip()
        summary = self._enforce_redlines(summary, pinned_redlines)
        try:
            record_usage(
                role="compactor",
                model=comp_cfg.model,
                base_url=comp_cfg.base_url,
                input_tokens=sum(len(str(m.get("content", ""))) for m in request_messages) // 4,
                output_tokens=len(summary) // 4,
                price_per_1m_input=comp_cfg.price_per_1m_input,
                price_per_1m_output=comp_cfg.price_per_1m_output,
                task_id=self.current_task_id,
            )
        except Exception:
            pass
        return summary or "（无可用历史摘要）"

    def step_loop(self) -> str:
        last = ""
        force_text = False
        round_i = 0

        while True:
            round_i += 1
            if not self.goal_mode and round_i > self.config.max_rounds:
                break

            step_id = f"step_{round_i:04d}"
            self._active_step_id = step_id

            # Sync active branch if changed externally via API/Web checkout
            if self.current_task_id:
                with self._scoped_persistence():
                    disk_branch = persistence.get_current_branch(self.current_task_id)
                    if disk_branch != self.context.branch:
                        self.context.reload_from_branch(disk_branch)

            if self.context.token_estimate() > self.config.compact_trigger:
                try:
                    with self._scoped_persistence():
                        compacted = self.context.compact(
                            self._summarize,
                            token_limit=self.config.compact_target,
                        )
                    if compacted:
                        self.logger.info(
                            f"[context] compacted; token estimate={self.context.token_estimate()}"
                        )
                except Exception as e:
                    self.logger.warning(f"[context] compaction failed; continuing without it: {e}")

            for note in self._consume_inbox():
                self.logger.info(f"[operator] {note}")
                with self._scoped_persistence():
                    self.context.append({"role": "user", "content": f"[Operator correction]\n{note}"})

            # Check if an abort flag was already placed before decoding starts
            pre_round_abort = self._is_step_rejected()
            if pre_round_abort and pre_round_abort.get("halt_loop"):
                self.last_aborted = True
                consume_abort_flag(self.current_task_id, base_root=self._tasks_root)
                if self.current_task_id:
                    write_step_state(
                        self.current_task_id,
                        step_id,
                        round_i,
                        phase="rejected",
                        rejected=True,
                        base_root=self._tasks_root,
                    )
                return f"TASK_ABORTED: {pre_round_abort.get('reason', 'User interrupted')}"

            choice = "none" if force_text else "auto"
            if force_text:
                self.logger.info("[guard] tool_choice=none (break tool loop)")
                force_text = False

            if self.current_task_id:
                write_step_state(
                    self.current_task_id,
                    step_id,
                    round_i,
                    phase="decoding",
                    rejected=False,
                    active_tool=None,
                    base_root=self._tasks_root,
                )
            self.observer.emit(
                self.current_task_id,
                "step_phase",
                {"phase": "decoding", "round": round_i},
                step_id=step_id,
                tasks_root=self._tasks_root,
            )

            input_est = self.context.token_estimate()
            raw_resp = self.chat(tool_choice=choice)
            resp = raw_resp if hasattr(raw_resp, "choices") else self.collect_stream(raw_resp)
            msg = resp.choices[0].message

            reasoning = (getattr(msg, "reasoning_content", None) or "").strip()
            if reasoning and not getattr(msg, "reasoning_streamed", False):
                self.logger.info(f"[thinking] {reasoning[:400]}{'...' if len(reasoning) > 400 else ''}")

            output_est = (len(msg.content or "") + len(reasoning)) // 4
            try:
                record_usage(
                    role=self.role,
                    model=self.config.model,
                    base_url=self.config.base_url,
                    input_tokens=input_est,
                    output_tokens=output_est,
                    price_per_1m_input=self.config.price_per_1m_input,
                    price_per_1m_output=self.config.price_per_1m_output,
                    task_id=self.current_task_id,
                )
            except Exception:
                pass

            abort_info = self._is_step_rejected() or getattr(msg, "abort_info", None)
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if reasoning:
                entry["reasoning_content"] = reasoning
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in msg.tool_calls
                ]
            node_meta: dict[str, Any] = {
                "model": self.config.model,
                "tokens": output_est,
                "step_id": step_id,
            }
            if abort_info:
                node_meta["rejected_by_operator"] = True
                node_meta["abort_reason"] = abort_info.get("reason", "")

            with self._scoped_persistence():
                self.context.append(entry, meta=node_meta)

            if msg.tool_calls:
                round_sigs = []
                step_was_aborted = bool(abort_info)

                for tc in msg.tool_calls:
                    name = tc.function.name
                    raw_arguments = tc.function.arguments or "{}"
                    try:
                        args = json.loads(raw_arguments)
                    except json.JSONDecodeError as json_err:
                        diag_msg = (
                            f"error: JSONDecodeError in tool '{name}' arguments at char {json_err.pos} "
                            f"(line {json_err.lineno}, column {json_err.colno}): {json_err.msg}. "
                            f"Raw arguments received: {raw_arguments[:400]!r}. "
                            "The tool was NOT executed. Fix the JSON syntax and call the tool again."
                        )
                        self.logger.warning(f"<- [json-error] {diag_msg}")
                        with self._scoped_persistence():
                            self.context.append(
                                {"role": "tool", "tool_call_id": tc.id, "content": diag_msg},
                                meta={"step_id": step_id, "json_error": True},
                            )
                        self.observer.emit(
                            self.current_task_id,
                            "tool_rejected",
                            {
                                "tool": name,
                                "tool_call_id": tc.id,
                                "reason": f"JSONDecodeError at char {json_err.pos}",
                            },
                            step_id=step_id,
                            tasks_root=self._tasks_root,
                        )
                        continue

                    # Gate 2: Pre-Dispatch Hard Rejection Gate
                    current_abort = self._is_step_rejected() or abort_info
                    if current_abort is not None:
                        step_was_aborted = True
                        abort_info = current_abort
                        src = str(current_abort.get("source", "operator")).upper()
                        reason = str(current_abort.get("reason", "User interrupted"))
                        reject_msg = (
                            f"[EXECUTION_REJECTED] [REJECTED_BY_{src}] "
                            f"Tool '{name}' execution was refused via abort.flag marker before execution: {reason}"
                        )
                        self.logger.warning(f"<- [abort-gate-2] {reject_msg}")
                        with self._scoped_persistence():
                            self.context.append(
                                {"role": "tool", "tool_call_id": tc.id, "content": reject_msg},
                                meta={"step_id": step_id, "rejected": True},
                            )
                        self.observer.emit(
                            self.current_task_id,
                            "tool_rejected",
                            {"tool": name, "tool_call_id": tc.id, "reason": reason, "source": src},
                            step_id=step_id,
                            tasks_root=self._tasks_root,
                        )
                        continue

                    if self.current_task_id:
                        write_step_state(
                            self.current_task_id,
                            step_id,
                            round_i,
                            phase="validating",
                            rejected=False,
                            active_tool=name,
                            base_root=self._tasks_root,
                        )

                    val_res = self.validator.validate(name, args)
                    if not val_res.is_valid:
                        err_msg = f"error: {val_res.error_message}"
                        self.logger.warning(f"<- [validator] {err_msg}")
                        with self._scoped_persistence():
                            self.context.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        if val_res.should_halt:
                            force_text = True
                            with self._scoped_persistence():
                                self.context.append(
                                    {
                                        "role": "user",
                                        "content": (
                                            f"Tool execution halted by validator: {val_res.error_message}. "
                                            "Please try a different approach or specify TASK_BLOCKED."
                                        ),
                                    }
                                )
                        continue

                    sig = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
                    round_sigs.append(sig)
                    self.logger.info(f"-> {name}({json.dumps(args, ensure_ascii=False)[:200]})")

                    # Pre-Dispatch Operator Breakpoint (step_risky / step_all)
                    if self._should_pause_before_tool(name):
                        pause_abort = self._pause_for_operator(step_id, round_i, name)
                        if pause_abort is not None:
                            step_was_aborted = True
                            abort_info = pause_abort
                            src = str(pause_abort.get("source", "operator")).upper()
                            reason = str(pause_abort.get("reason", "User interrupted"))
                            reject_msg = (
                                f"[EXECUTION_REJECTED] [REJECTED_BY_{src}] "
                                f"Tool '{name}' execution was refused at the step breakpoint: {reason}"
                            )
                            self.logger.warning(f"<- [step-gate-abort] {reject_msg}")
                            with self._scoped_persistence():
                                self.context.append(
                                    {"role": "tool", "tool_call_id": tc.id, "content": reject_msg},
                                    meta={"step_id": step_id, "rejected": True},
                                )
                            self.observer.emit(
                                self.current_task_id,
                                "tool_rejected",
                                {"tool": name, "tool_call_id": tc.id, "reason": reason, "source": src},
                                step_id=step_id,
                                tasks_root=self._tasks_root,
                            )
                            continue

                    if self.current_task_id:
                        write_step_state(
                            self.current_task_id,
                            step_id,
                            round_i,
                            phase="executing_tool",
                            rejected=False,
                            active_tool=name,
                            base_root=self._tasks_root,
                        )
                    self.observer.emit(
                        self.current_task_id,
                        "tool_start",
                        {"tool": name, "args": args, "tool_call_id": tc.id},
                        step_id=step_id,
                        tasks_root=self._tasks_root,
                    )

                    set_active_execution_context(
                        self.current_task_id,
                        base_root=self._tasks_root,
                        parent_task_id=self.parent_task_id,
                    )
                    fn = self.dispatchers.get(name)
                    try:
                        if fn:
                            try:
                                res = fn(args)
                            except Exception as e:
                                res = f"error executing {name}: {e}"
                        else:
                            res = f"error: unknown tool {name}"
                    finally:
                        set_active_execution_context(None)

                    # Gate 3: Mid/Post-Execution Interrupt & Output Ignore Gate
                    post_abort = self._is_step_rejected()
                    if post_abort is not None or (
                        isinstance(res, str) and res.startswith("[EXECUTION_INTERRUPTED_AND_IGNORED]")
                    ):
                        step_was_aborted = True
                        abort_info = post_abort or abort_info or {"source": "operator", "reason": "Interrupted during execution"}
                        src = str(abort_info.get("source", "operator"))
                        reason = str(abort_info.get("reason", "User interrupted"))
                        ignored_msg = (
                            f"[EXECUTION_INTERRUPTED_AND_IGNORED] Tool '{name}' was interrupted by {src}; "
                            f"its output and side-effects have been ignored. ({reason})"
                        )
                        self.logger.warning(f"<- [abort-gate-3] {ignored_msg}")
                        with self._scoped_persistence():
                            self.context.append(
                                {"role": "tool", "tool_call_id": tc.id, "content": ignored_msg},
                                meta={"step_id": step_id, "interrupted": True},
                            )
                        self.observer.emit(
                            self.current_task_id,
                            "tool_interrupted",
                            {"tool": name, "tool_call_id": tc.id, "reason": reason},
                            step_id=step_id,
                            tasks_root=self._tasks_root,
                        )
                        continue

                    b64 = None
                    if isinstance(res, tuple) and len(res) == 2:
                        text, b64 = res
                    else:
                        text = res

                    self.logger.info(f"<- {str(text).replace(chr(10), ' ')[:240]}")
                    with self._scoped_persistence():
                        self.context.append(
                            {"role": "tool", "tool_call_id": tc.id, "content": str(text)},
                            meta={"step_id": step_id},
                        )

                        if b64 and self.vision_ok[0]:
                            self.context.append(
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Image observation attached. Continue.",
                                        },
                                        {
                                            "type": "image_url",
                                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                                        },
                                    ],
                                },
                                meta={"step_id": step_id},
                            )
                        elif b64:
                            self.context.append(
                                {
                                    "role": "user",
                                    "content": "Image observation captured (no mmproj). Continue.",
                                },
                                meta={"step_id": step_id},
                            )

                if step_was_aborted:
                    self.last_aborted = True
                    consumed_info = consume_abort_flag(self.current_task_id, base_root=self._tasks_root) or abort_info or {}
                    if self.current_task_id:
                        write_step_state(
                            self.current_task_id,
                            step_id,
                            round_i,
                            phase="rejected",
                            rejected=True,
                            active_tool=None,
                            base_root=self._tasks_root,
                        )
                    self.observer.emit(
                        self.current_task_id,
                        "step_phase",
                        {"phase": "rejected", "round": round_i, "abort": consumed_info},
                        step_id=step_id,
                        tasks_root=self._tasks_root,
                    )
                    if consumed_info.get("halt_loop"):
                        return f"TASK_ABORTED: {consumed_info.get('reason', 'User interrupted')}"
                    continue

                if self.current_task_id:
                    write_step_state(
                        self.current_task_id,
                        step_id,
                        round_i,
                        phase="completed",
                        rejected=False,
                        active_tool=None,
                        base_root=self._tasks_root,
                    )

                # Unified Batch Repetition Check in Validator
                batch_res = self.validator.check_batch_repetition(round_sigs)
                if not batch_res.is_valid:
                    self.logger.warning(f"[guard] {batch_res.error_message}")
                    with self._scoped_persistence():
                        self.context.append({"role": "user", "content": batch_res.error_message})

                continue

            if abort_info:
                self.last_aborted = True
                consumed_info = consume_abort_flag(self.current_task_id, base_root=self._tasks_root) or abort_info
                if self.current_task_id:
                    write_step_state(
                        self.current_task_id,
                        step_id,
                        round_i,
                        phase="rejected",
                        rejected=True,
                        active_tool=None,
                        base_root=self._tasks_root,
                    )
                if consumed_info.get("halt_loop"):
                    return f"TASK_ABORTED: {consumed_info.get('reason', 'User interrupted')}"

            if self.current_task_id:
                write_step_state(
                    self.current_task_id,
                    step_id,
                    round_i,
                    phase="completed",
                    rejected=False,
                    active_tool=None,
                    base_root=self._tasks_root,
                )

            last = (msg.content or "").strip()
            if not last and reasoning:
                last = reasoning
                self.logger.warning("empty content; using thinking text as reply")

            fr = getattr(resp.choices[0], "finish_reason", None)
            if fr == "length":
                self.logger.warning("finish_reason=length — asking for short final answer")
                with self._scoped_persistence():
                    self.context.append(
                        {
                            "role": "user",
                            "content": "Your previous reply was cut off. Give a short final answer. If done: TASK_DONE.",
                        }
                    )
                continue

            if self.goal_mode and "TASK_DONE" not in last and "TASK_BLOCKED" not in last:
                self.logger.info("[goal_mode] Result missing TASK_DONE, asking LLM to continue goal...")
                with self._scoped_persistence():
                    self.context.append(
                        {
                            "role": "user",
                            "content": "Goal mode active: Goal not finished. Continue working until complete or TASK_BLOCKED.",
                        }
                    )
                continue

            return last

        return last or "TASK_BLOCKED: max rounds reached"

    def ensure_goal_doc(self, task_input: str = "") -> None:
        goal_path = GOAL_PATH
        if goal_path.exists():
            return

        self.logger.info(f"[goal_mode] {GOAL_PATH} missing, generating via LLM summary request...")
        agents_text = load_text(AGENTS_PATH)

        prompt = (
            "Based on the following task and instructions, summarize a clear, structured long-term goal document in Markdown format.\n\n"
            f"Task:\n{task_input}\n\n"
            f"Instructions ({AGENTS_PATH}):\n{agents_text}\n"
        )

        try:
            comp_cfg = self.compactor_config or self.config
            comp_client = self.compactor_client if (self.compactor_client is not None and comp_cfg is not self.config) else self.client
            response = comp_client.chat.completions.create(
                model=comp_cfg.model,
                messages=[
                    {"role": "system", "content": "You are a task goal summarizer. Output the goal document in Markdown."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=1024,
            )
            content = response.choices[0].message.content or ""
            if not content.strip():
                raise RuntimeError("LLM returned empty response for GOAL generation")

            goal_path.parent.mkdir(parents=True, exist_ok=True)
            goal_path.write_text(content.strip(), encoding="utf-8")
            self.logger.info(f"[goal_mode] Successfully generated and saved {goal_path}")

            new_system = self.build_system()
            if self.goal_mode:
                new_system += "\n\nGOAL MODE ACTIVE: You must continue working until the goal is fully achieved. Put TASK_DONE only when complete."
            self.context.messages[0]["content"] = new_system
        except Exception as e:
            self.logger.error(f"[goal_mode] Failed to generate {GOAL_PATH}: {e}")
            raise

    def run_task(self, user_text: str, task_id: str | None = None) -> str:
        """Run one task; the caller may pin the task id (used by the HTTP API).

        Every fresh (non-resume) task starts from a clean context: the message
        cache is reset before binding so no previous task's history can leak
        into this task's checkpoint. GOAL.md is materialized before binding so
        the root system node is complete from the very first disk write.
        """
        self.validator.reset()
        self.last_aborted = False
        with self._scoped_persistence():
            self.current_task_id = task_id or generate_task_id()
            if self.role == "main":
                try:
                    set_current_session(self.current_task_id)
                except Exception:
                    pass
            self.context.clear_task()
            save_status(self.current_task_id, "running")

        try:
            if self.goal_mode and not GOAL_PATH.exists():
                self.ensure_goal_doc(user_text)
            with self._scoped_persistence():
                self.context.bind_task(self.current_task_id, sync_initial=True)
                self.context.append({"role": "user", "content": user_text})
            final = self.step_loop()
        except Exception as e:
            self.logger.error(f"Execution error: {e}")
            if self.context.as_list() and self.context.as_list()[-1].get("role") == "user":
                self.context.as_list().pop()
            final = f"TASK_BLOCKED: {e}"
        finally:
            with self._scoped_persistence():
                try:
                    save_checkpoint(self.current_task_id, self.context.as_list())
                except Exception as e:
                    self.logger.error(f"Checkpoint save failed: {e}")
                try:
                    save_status(self.current_task_id, _terminal_status(final, was_aborted=self.last_aborted))
                except Exception as e:
                    self.logger.error(f"Status save failed: {e}")

        if "TASK_DONE" in final:
            self.logger.info("[done]")
        elif "TASK_BLOCKED" in final:
            self.logger.info("[blocked]")

        return final

    def resume_task(self, task_id: str, branch: str | None = None) -> str:
        """Restore a checkpoint (healing any crash-interrupted tool_calls) and continue its ReAct loop."""
        with self._scoped_persistence():
            try:
                loaded = load_checkpoint(task_id)
            except Exception as e:
                return f"TASK_BLOCKED: unable to load checkpoint: {e}"
            if loaded is None:
                return f"TASK_BLOCKED: checkpoint not found: {task_id}"

            messages, _ = loaded
            if not messages:
                return f"TASK_BLOCKED: checkpoint is empty: {task_id}"

            if branch:
                persistence.set_current_branch(task_id, branch)

            self.current_task_id = task_id
            if self.role == "main":
                try:
                    set_current_session(task_id)
                except Exception:
                    pass
            self.context.task_id = task_id
            self.context.reload_from_branch(
                branch=branch or persistence.get_current_branch(task_id),
                heal_dangling=True,
            )

        self.validator.reset()
        self.last_aborted = False
        with self._scoped_persistence():
            save_status(task_id, "running")
        try:
            final = self.step_loop()
        except Exception as e:
            self.logger.error(f"Resume execution error: {e}")
            final = f"TASK_BLOCKED: {e}"
        finally:
            with self._scoped_persistence():
                try:
                    save_checkpoint(task_id, self.context.as_list())
                except Exception as e:
                    self.logger.error(f"Checkpoint save failed: {e}")
                try:
                    save_status(task_id, _terminal_status(final, was_aborted=self.last_aborted))
                except Exception as e:
                    self.logger.error(f"Status save failed: {e}")

        if "TASK_DONE" in final:
            self.logger.info("[done]")
        elif "TASK_BLOCKED" in final:
            self.logger.info("[blocked]")
        return final

    def run(self, user_text: str) -> str:
        return self.run_task(user_text)
