"""Tests for isolated SubAgent sandbox and Single-SubAgent lock (GATE-FUNC-04 & GATE-ARCH-03)."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from core import agent as agent_mod
from core import interrupt
from core import persistence
from core import subagent_runner
from skills import subagent as subagent_skill
from tests.support import IsolatedCase, ScriptedLLM, build_agent


class SubAgentIsolationTests(IsolatedCase):
    def test_subagent_isolates_intermediate_turns_and_returns_distilled_summary(self):
        sub_llm = ScriptedLLM([
            {"tool_calls": [{"id": "sub_call_1", "name": "list_dir", "arguments": '{"path": "."}'}]},
            {"content": "TASK_DONE: Found core modules and verified structure."},
        ])

        main_agent, _ = build_agent([{"content": "TASK_DONE: main completed"}])
        main_agent.current_task_id = "main_session_01"
        main_agent.context.bind_task("main_session_01")
        main_agent.context.append({"role": "user", "content": "delegate exploration"})

        with mock.patch.object(agent_mod, "OpenAI", lambda *args, **kwargs: sub_llm):
            raw_result = subagent_skill.run(
                {
                    "objective": "Explore workspace directory structure",
                    "context_hints": "Check root directory",
                    "max_rounds": 5,
                    "allowed_skills": ["list_dir"],
                },
                runtime={"parent_task_id": "main_session_01", "subagent_client": sub_llm},
            )

        parsed = json.loads(raw_result)
        self.assertEqual(parsed["status"], "completed")
        self.assertIn("TASK_DONE: Found core modules", parsed["report"])
        sub_id = parsed["subagent_id"]

        sub_checkpoint = subagent_runner.subagents_root() / sub_id / "checkpoint.jsonl"
        self.assertTrue(sub_checkpoint.is_file())
        sub_status = (subagent_runner.subagents_root() / sub_id / "status").read_text(encoding="utf-8").strip()
        self.assertEqual(sub_status, "completed")

        self.assertFalse(subagent_runner.active_lock_path().exists())

        main_msgs = main_agent.context.as_list()
        self.assertFalse(any(m.get("role") == "tool" for m in main_msgs))

    def test_single_subagent_lock_rejects_concurrent_subagent(self):
        sub_root = subagent_runner.subagents_root()
        active_id = "sub_20261016_010101_000001"
        active_dir = sub_root / active_id
        active_dir.mkdir(parents=True, exist_ok=True)
        (active_dir / "status").write_text("running\n", encoding="utf-8")
        subagent_runner.active_lock_path().write_text(f"{active_id}\n", encoding="utf-8")

        out = subagent_skill.run({"objective": "try second subagent"})
        self.assertTrue(out.startswith("error: SUBAGENT_BUSY:"))
        self.assertIn(active_id, out)

    def test_cascading_abort_from_main_task_cancels_subagent(self):
        parent_id = "main_parent_task"
        persistence.save_checkpoint(parent_id, [{"role": "user", "content": "parent"}])

        sub_llm = ScriptedLLM([
            {"tool_calls": [{"id": "sc1", "name": "list_dir", "arguments": '{"path": "."}'}]},
            {"content": "TASK_DONE: should not reach here"},
        ])

        interrupt.set_abort_flag(parent_id, source="web", reason="Parent task aborted", halt_loop=True)

        with mock.patch.object(agent_mod, "OpenAI", lambda *args, **kwargs: sub_llm):
            raw_res = subagent_skill.run(
                {"objective": "will be cancelled by parent"},
                runtime={"parent_task_id": parent_id, "subagent_client": sub_llm},
            )

        parsed = json.loads(raw_res)
        self.assertEqual(parsed["status"], "cancelled")
        sub_id = parsed["subagent_id"]
        sub_status = (subagent_runner.subagents_root() / sub_id / "status").read_text(encoding="utf-8").strip()
        self.assertEqual(sub_status, "cancelled")
        self.assertFalse(subagent_runner.active_lock_path().exists())


if __name__ == "__main__":
    unittest.main()
