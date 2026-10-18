"""Tests for the Agent: system prompt, skill registry, task lifecycle markers."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from core import agent as agent_mod
from core import persistence
from core.config import AgentConfig
from tests.support import IsolatedCase, build_agent


class BuildSystemTests(IsolatedCase):
    def test_reads_agents_and_goal_from_state_dir(self):
        agents = self.state / "AGENTS.md"
        agents.write_text("# Rules\nbe nice", encoding="utf-8")
        goal = self.state / "GOAL.md"
        goal.write_text("finish the thing", encoding="utf-8")

        with mock.patch.object(agent_mod, "AGENTS_PATH", agents), \
                mock.patch.object(agent_mod, "GOAL_PATH", goal):
            agent, _ = build_agent()

        system = agent.context.messages[0]["content"]
        self.assertIn("be nice", system)
        self.assertIn("finish the thing", system)
        self.assertIn("Available skills", system)

    def test_missing_agents_md_fails_without_fallback(self):
        missing = self.state / "AGENTS.md"
        with mock.patch.object(agent_mod, "AGENTS_PATH", missing):
            with self.assertRaises(RuntimeError):
                build_agent()

    def test_goal_is_optional(self):
        agents = self.state / "AGENTS.md"
        agents.write_text("rules only", encoding="utf-8")
        missing_goal = self.state / "GOAL.md"

        with mock.patch.object(agent_mod, "AGENTS_PATH", agents), \
                mock.patch.object(agent_mod, "GOAL_PATH", missing_goal):
            agent, _ = build_agent()

        self.assertIn("rules only", agent.context.messages[0]["content"])


class TerminalStatusTests(unittest.TestCase):
    def test_marker_mapping(self):
        self.assertEqual(agent_mod._terminal_status("done! TASK_DONE: ok"), "done")
        self.assertEqual(agent_mod._terminal_status("TASK_BLOCKED: stuck"), "blocked")
        self.assertEqual(agent_mod._terminal_status("plain answer"), "finished")


class RunTaskTests(IsolatedCase):
    def test_status_and_checkpoint_written(self):
        agent, llm = build_agent([{"content": "TASK_DONE: all good"}])
        final = agent.run_task("demo task")

        self.assertEqual(final, "TASK_DONE: all good")
        task_id = agent.current_task_id
        self.assertEqual(persistence.load_status(task_id), "done")

        messages, _ = persistence.load_checkpoint(task_id)
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == "demo task" for m in messages))
        self.assertTrue(any(m.get("role") == "assistant" for m in messages))
        self.assertEqual(llm.requests[0]["model"], agent.config.model)

    def test_failure_marks_blocked(self):
        agent, _ = build_agent([{"raise": "boom"}])
        final = agent.run_task("will fail")

        self.assertIn("TASK_BLOCKED", final)
        self.assertEqual(persistence.load_status(agent.current_task_id), "blocked")

    def test_plain_answer_marks_finished(self):
        agent, _ = build_agent([{"content": "just an answer"}])
        agent.run_task("question")
        self.assertEqual(persistence.load_status(agent.current_task_id), "finished")

    def test_tool_call_round_trip(self):
        replies = [
            {"tool_calls": [{"id": "call_1", "name": "list_dir", "arguments": json.dumps({"path": "."})}]},
            {"content": "TASK_DONE: listed"},
        ]
        agent, llm = build_agent(replies)
        final = agent.run_task("list files")

        self.assertIn("TASK_DONE", final)
        self.assertGreaterEqual(len(llm.requests), 2)
        messages, _ = persistence.load_checkpoint(agent.current_task_id)
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_1")
        self.assertIn("core", tool_messages[0]["content"])

    def test_pinned_task_id_is_used(self):
        agent, _ = build_agent()
        agent.run_task("pin me", task_id="20260101_000000_000000")
        self.assertEqual(agent.current_task_id, "20260101_000000_000000")
        self.assertEqual(persistence.load_status("20260101_000000_000000"), "done")


class ResumeTaskTests(IsolatedCase):
    def test_missing_checkpoint_creates_nothing(self):
        agent, _ = build_agent()
        out = agent.resume_task("no_such_task")
        self.assertIn("TASK_BLOCKED", out)
        self.assertFalse((self.tasks_root / "no_such_task").exists())

    def test_resume_continues_and_marks_done(self):
        persistence.save_checkpoint(
            "resume_me",
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "old question"},
            ],
        )
        agent, _ = build_agent([{"content": "TASK_DONE: continued"}])
        out = agent.resume_task("resume_me")

        self.assertIn("TASK_DONE", out)
        self.assertEqual(persistence.load_status("resume_me"), "done")
        messages, _ = persistence.load_checkpoint("resume_me")
        self.assertGreaterEqual(len(messages), 3)


class SkillRegistryTests(IsolatedCase):
    def test_skill_without_description_rejected(self):
        self.write_skill("bad", "PARAMETERS = {'type': 'object'}\nHANDLER = lambda **kw: 'x'\n")
        with self.patch_skill_roots():
            with self.assertRaises(ValueError) as ctx:
                build_agent()
        self.assertIn("DESCRIPTION", str(ctx.exception))

    def test_skill_without_handler_or_run_rejected(self):
        self.write_skill("bad", "DESCRIPTION = 'd'\nPARAMETERS = {'type': 'object'}\n")
        with self.patch_skill_roots():
            with self.assertRaises(ValueError) as ctx:
                build_agent()
        self.assertIn("HANDLER", str(ctx.exception))

    def test_valid_skill_registered_as_tool(self):
        self.write_skill(
            "good",
            "DESCRIPTION = 'demo skill'\n"
            "PARAMETERS = {'type': 'object', 'properties': {}}\n"
            "HANDLER = lambda **kwargs: 'ok'\n",
        )
        with self.patch_skill_roots():
            agent, _ = build_agent()
        names = [tool["function"]["name"] for tool in agent.tools]
        self.assertEqual(names, ["good"])
        self.assertEqual(agent.dispatchers["good"]({}), "ok")
        self.assertIn("demo skill", agent.context.messages[0]["content"])

    def test_hot_reload_picks_up_changed_skill(self):
        body = (
            "DESCRIPTION = 'demo skill'\n"
            "PARAMETERS = {'type': 'object', 'properties': {}}\n"
            "HANDLER = lambda **kwargs: 'v1'\n"
        )
        self.write_skill("good", body)
        with self.patch_skill_roots():
            agent, _ = build_agent()
            self.assertEqual(agent.dispatchers["good"]({}), "v1")
            self.write_skill("good", body.replace("'v1'", "'v2'"))
            agent.reload_skills()
            self.assertEqual(agent.dispatchers["good"]({}), "v2")


class CollectStreamTests(IsolatedCase):
    """collect_stream() reassembles text, reasoning, and tool-call deltas."""

    @staticmethod
    def _chunk(content="", reasoning="", tool_calls=None, finish_reason=None):
        delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])

    def test_parses_text_reasoning_and_tool_calls(self):
        agent, _ = build_agent()
        call_delta = SimpleNamespace(
            index=0,
            id="call_9999",
            function=SimpleNamespace(name="list_dir", arguments='{"path": "."}'),
        )
        stream = [
            self._chunk(content="I will check", reasoning="thinking"),
            self._chunk(content="", reasoning="", tool_calls=[call_delta], finish_reason="tool_calls"),
        ]
        resp = agent.collect_stream(stream)

        message = resp.choices[0].message
        self.assertEqual(message.content, "I will check")
        self.assertEqual(message.reasoning_content, "thinking")
        self.assertEqual(len(message.tool_calls), 1)
        self.assertEqual(message.tool_calls[0].id, "call_9999")
        self.assertEqual(message.tool_calls[0].function.name, "list_dir")
        self.assertEqual(message.tool_calls[0].function.arguments, '{"path": "."}')
        self.assertEqual(resp.choices[0].finish_reason, "tool_calls")

    def test_abrupt_stream_error_propagates(self):
        agent, _ = build_agent()

        def broken_stream():
            yield self._chunk(content="Partial content", reasoning="")
            raise ConnectionResetError("stream disconnected abruptly")

        with self.assertRaises(ConnectionResetError):
            agent.collect_stream(broken_stream())


class StepLoopTests(IsolatedCase):
    def test_goal_mode_continues_until_task_done(self):
        agent, llm = build_agent(
            [
                {"content": "Working on step 1"},
                {"content": "Finished! TASK_DONE: all goals met"},
            ],
            goal_mode=True,
        )
        result = agent.step_loop()

        self.assertIn("TASK_DONE", result)
        self.assertEqual(len(llm.requests), 2)

    def test_max_rounds_terminates_without_goal_mode(self):
        replies = [{"tool_calls": [{"id": "c1", "name": "list_dir", "arguments": "{}"}]}] * 5
        agent, llm = build_agent(replies, config=AgentConfig(max_rounds=2))
        result = agent.step_loop()

        self.assertIn("TASK_BLOCKED", result)
        self.assertEqual(len(llm.requests), 2)


if __name__ == "__main__":
    unittest.main()