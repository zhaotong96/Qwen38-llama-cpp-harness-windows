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


class ContextLeakTests(IsolatedCase):
    """Sequential tasks on one agent must never leak history into each other."""

    def test_second_task_checkpoint_starts_clean(self):
        agent, llm = build_agent(
            [
                {"content": "first answer"},
                {"content": "TASK_DONE: second done"},
            ]
        )
        agent.run_task("first task")
        first_id = agent.current_task_id
        agent.run_task("second task")
        second_id = agent.current_task_id
        self.assertNotEqual(first_id, second_id)

        second_messages, _ = persistence.load_checkpoint(second_id)
        contents = [str(m.get("content", "")) for m in second_messages]
        self.assertFalse(any("first task" in c for c in contents))
        self.assertFalse(any("first answer" in c for c in contents))
        self.assertEqual(len(second_messages), 3)
        self.assertEqual(second_messages[0]["role"], "system")
        self.assertEqual(second_messages[1], {"role": "user", "content": "second task"})

        second_request_messages = llm.requests[1]["messages"]
        self.assertEqual(len(second_request_messages), 2)


class JsonArgumentErrorTests(IsolatedCase):
    """Malformed tool arguments produce a precise syntax receipt, never a blind default call."""

    def test_malformed_json_is_reported_not_executed(self):
        executed: list[str] = []
        replies = [
            {
                "tool_calls": [
                    {"id": "call_bad", "name": "mouse", "arguments": '{"x": 10,,}'}
                ]
            },
            {"content": "TASK_DONE: corrected"},
        ]
        agent, llm = build_agent(replies)
        agent.dispatchers["mouse"] = lambda args: executed.append("mouse") or "clicked"

        final = agent.run_task("click something")

        self.assertIn("TASK_DONE", final)
        self.assertEqual(executed, [])
        followup_messages = llm.requests[1]["messages"]
        tool_messages = [m for m in followup_messages if m.get("role") == "tool"]
        self.assertTrue(tool_messages)
        receipt = tool_messages[-1]
        self.assertEqual(receipt["tool_call_id"], "call_bad")
        self.assertTrue(receipt["content"].startswith("error:"))
        self.assertIn("JSONDecodeError", receipt["content"])
        self.assertIn("char 9", receipt["content"])


class ChatHyperparameterTests(IsolatedCase):
    """Prefix stability: tools stay attached; sampling is low-temperature with MTP extras."""

    def test_tools_stay_attached_when_forcing_text(self):
        agent, llm = build_agent([{"content": "text only"}])
        agent.chat(tool_choice="none")
        kwargs = llm.requests[-1]
        self.assertIn("tools", kwargs)
        self.assertTrue(kwargs["tools"])
        self.assertEqual(kwargs["tool_choice"], "none")
        self.assertEqual(kwargs["temperature"], 0.2)
        self.assertEqual(kwargs["top_p"], 0.95)

    def test_read_timeout_scales_with_context_size(self):
        agent, llm = build_agent([{"content": "text only"}])
        agent.chat()
        baseline = llm.requests[-1]["timeout"]
        self.assertGreaterEqual(baseline, agent.READ_TIMEOUT_BASE)

        agent.context.append({"role": "user", "content": "x" * 40000})
        agent.chat()
        self.assertGreater(llm.requests[-1]["timeout"], baseline)

    def test_chat_extra_carries_mtp_friendly_defaults(self):
        agent, llm = build_agent([{"content": "text only"}])
        agent.chat()
        extra = llm.requests[-1]["extra_body"]
        self.assertEqual(extra["top_k"], 20)
        self.assertEqual(extra["min_p"], 0.05)
        self.assertEqual(extra["repeat_penalty"], 1.05)
        self.assertTrue(extra["cache_prompt"])
        self.assertTrue(extra["chat_template_kwargs"]["enable_thinking"])
        self.assertTrue(extra["chat_template_kwargs"]["preserve_thinking"])


class FactLedgerSummaryTests(IsolatedCase):
    def test_summarize_request_is_a_thinking_free_five_section_ledger(self):
        agent, llm = build_agent([{"content": "1. 核心目标与进度\n已完成"}])
        summary = agent._summarize([{"role": "user", "content": "did things"}])

        self.assertIn("核心目标与进度", summary)
        request = llm.requests[-1]
        self.assertEqual(request["max_tokens"], 2048)
        self.assertEqual(request["temperature"], 0.2)
        self.assertFalse(request["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        prompt = request["messages"][0]["content"]
        for section in ("1. 核心目标与进度", "2. 已验证事实与环境基线", "3. 已修改文件清单",
                        "4. 被否决方案与操作员红线（永久保留）", "5. 待完成子目标与下一步行动"):
            self.assertIn(section, prompt)


class RedlineFidelityTests(IsolatedCase):
    REDLINE = "[Operator correction] NEVER touch the production slot; keep BENCH_SECRET_PORT=49152"

    @staticmethod
    def _ledger_without_redlines(note: str = "done") -> str:
        return (
            "1. 核心目标与进度\n" + note + "\n\n"
            "2. 已验证事实与环境基线\nok\n\n"
            "3. 已修改文件清单\nnone\n\n"
            "4. 被否决方案与操作员红线（永久保留）\n无\n\n"
            "5. 待完成子目标与下一步行动\ncontinue"
        )

    def test_old_section_six_is_stripped_before_summarization(self):
        agent, llm = build_agent([{"content": "summary"}])
        legacy = (
            "[Context Summary]\n1. 核心目标与进度\nx\n\n"
            "6. 已归档原始 DAG 节点索引表（可用 read_checkpoint_node 按 node_id 回溯原文）\n"
            "- n_000001 (user): secret [10 chars]"
        )
        agent._summarize([{"role": "system", "content": legacy}])
        prompt = llm.requests[-1]["messages"][0]["content"]
        self.assertIn("核心目标与进度", prompt)
        self.assertNotIn("已归档原始 DAG 节点索引表", prompt)

    def test_redlines_survive_three_cascaded_compactions_verbatim(self):
        agent, llm = build_agent([{"content": self._ledger_without_redlines()} for _ in range(3)])
        history = [{"role": "user", "content": self.REDLINE}]
        for round_i in range(1, 4):
            summary = agent._summarize(history)
            self.assertIn(self.REDLINE, summary, f"redline lost at round {round_i}")
            self.assertIn("4. 被否决方案与操作员红线（永久保留）", summary)
            history = [
                {"role": "system", "content": f"[Context Summary]\n{summary}"},
                {"role": "user", "content": f"round {round_i} work"},
            ]

        final = history[0]["content"]
        self.assertIn("BENCH_SECRET_PORT=49152", final)
        self.assertIn("NEVER touch the production slot", final)
        self.assertEqual(llm.requests[-1]["messages"][0]["role"], "user")

    def test_branch_rewind_lesson_survives_cascaded_compactions(self):
        lesson = (
            "[Branch Rewind Lesson] (rewound 'main'@n_1 -> 'alt'@n_2): "
            "Avoid failed approach: stale parser cache."
        )
        agent, _ = build_agent(
            [{"content": self._ledger_without_redlines()} for _ in range(3)]
        )
        history = [{"role": "user", "content": lesson}]
        for round_i in range(1, 4):
            summary = agent._summarize(history)
            self.assertIn(lesson, summary, f"rewind lesson lost at round {round_i}")
            self.assertIn("4. 被否决方案与操作员红线（永久保留）", summary)
            history = [
                {"role": "system", "content": f"[Context Summary]\n{summary}"},
                {"role": "user", "content": f"round {round_i} work"},
            ]

    def test_redline_block_is_injected_into_instruction(self):
        agent, llm = build_agent([{"content": self._ledger_without_redlines()}])
        agent._summarize([{"role": "user", "content": self.REDLINE}])
        prompt = llm.requests[-1]["messages"][0]["content"]
        self.assertIn("<immutable_redlines>", prompt)
        self.assertIn("BENCH_SECRET_PORT=49152", prompt)

    def test_redlines_inserted_inside_section_four_when_heading_precedes_section_five(self):
        malformed = (
            "1. 核心目标与进度\nx\n\n"
            "5. 待完成子目标与下一步行动\ncontinue\n\n"
            "4. 被否决方案与操作员红线（永久保留）\n无\n"
        )
        agent, _ = build_agent([{"content": malformed}])
        summary = agent._summarize([{"role": "user", "content": self.REDLINE}])

        self.assertIn(self.REDLINE, summary)
        self.assertIn("5. 待完成子目标与下一步行动", summary)
        self.assertLess(summary.index("4. 被否决方案"), summary.index(self.REDLINE))

    def test_prefix_sharing_reuses_live_slot_zero_prefix(self):
        agent, llm = build_agent([{"content": self._ledger_without_redlines()}])
        agent.context.append({"role": "user", "content": "first turn"})
        agent.context.append({"role": "assistant", "content": "first reply"})

        agent._summarize([{"role": "user", "content": "first turn"}])

        request = llm.requests[-1]
        self.assertEqual(request["extra_body"]["id_slot"], 0)
        self.assertEqual(len(request["messages"]), 3)
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertEqual(request["messages"][1]["content"], "first turn")
        self.assertEqual(request["messages"][2]["role"], "user")
        self.assertIn("1. 核心目标与进度", request["messages"][2]["content"])
        self.assertNotIn("对话历史（JSON）", request["messages"][2]["content"])

    def test_prefix_sharing_disabled_for_asymmetric_compactor(self):
        agent, llm = build_agent([{"content": self._ledger_without_redlines()}])
        agent.context.append({"role": "user", "content": "first turn"})
        agent.compactor_config = type(agent.config)(
            base_url="http://other.example/v1",
            model="other-model",
        )
        agent.compactor_client = llm

        agent._summarize([{"role": "user", "content": "first turn"}])

        request = llm.requests[-1]
        self.assertNotIn("id_slot", request["extra_body"])
        self.assertEqual(len(request["messages"]), 1)
        self.assertIn("对话历史（JSON）", request["messages"][0]["content"])


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