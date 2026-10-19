"""Phase 8B/9A integration: skill health, reload wiring, and the TDD gate."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from core import agent as agent_mod
from core import skill_sandbox
from core import subagent_runner
from core import tools as tools_mod
from tests.support import IsolatedCase, build_agent


class GovernanceRegistrationTests(unittest.TestCase):
    def test_risky_tools_include_branch_rewind(self):
        self.assertIn("branch_rewind", agent_mod.Agent.RISKY_TOOLS)

    def test_subagents_cannot_call_branch_rewind(self):
        self.assertIn("branch_rewind", subagent_runner.EXCLUDED_META_SKILLS)

    def test_redline_markers_include_rewind_lesson(self):
        self.assertIn("[Branch Rewind Lesson]", agent_mod.Agent._REDLINE_MARKERS)


class SkillHealthIntegrationTests(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.health_file = self.state / "skill_health.json"
        self._stack.enter_context(
            mock.patch.object(skill_sandbox, "HEALTH_FILE", self.health_file)
        )

    def test_dynamic_skill_success_is_persisted(self):
        replies = [
            {
                "tool_calls": [
                    {
                        "id": "call_dynamic",
                        "name": "dynamic_probe",
                        "arguments": "{}",
                    }
                ]
            },
            {"content": "TASK_DONE: healthy"},
        ]
        agent, _ = build_agent(replies)
        agent.dispatchers["dynamic_probe"] = lambda args: "probe ok"

        final = agent.run_task("exercise a dynamic skill")

        self.assertIn("TASK_DONE", final)
        self.assertTrue(self.health_file.is_file())
        entry = json.loads(self.health_file.read_text(encoding="utf-8"))["dynamic_probe"]
        self.assertEqual(entry["status"], "healthy")

    def test_handler_exception_is_recorded_as_failure(self):
        replies = [
            {
                "tool_calls": [
                    {
                        "id": "call_dynamic",
                        "name": "dynamic_probe",
                        "arguments": "{}",
                    }
                ]
            },
            {"content": "TASK_DONE: recovered"},
        ]
        agent, _ = build_agent(replies)

        def explosion(args):
            raise RuntimeError("handler exploded")

        agent.dispatchers["dynamic_probe"] = explosion

        final = agent.run_task("exercise a failing dynamic skill")

        self.assertIn("TASK_DONE", final)
        entry = json.loads(self.health_file.read_text(encoding="utf-8"))["dynamic_probe"]
        self.assertEqual(entry["consecutive_failures"], 1)
        self.assertEqual(entry["status"], "degraded")


class SkillReloadAndQuarantineIntegrationTests(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.skills_dir = self.state / "skills"
        self.skills_dir.mkdir()
        self.disabled_dir = self.state / "disabled"
        self.health_file = self.state / "skill_health.json"
        self._stack.enter_context(
            mock.patch.object(tools_mod, "SKILL_ROOTS", (self.skills_dir,))
        )
        self._stack.enter_context(
            mock.patch.object(skill_sandbox, "SKILLS_DIR", self.skills_dir)
        )
        self._stack.enter_context(
            mock.patch.object(skill_sandbox, "DISABLED_DIR", self.disabled_dir)
        )
        self._stack.enter_context(
            mock.patch.object(skill_sandbox, "HEALTH_FILE", self.health_file)
        )

    def test_add_skill_success_reloads_registry(self):
        new_skill = self.skills_dir / "dynamic_new.py"
        new_skill.write_text(
            'DESCRIPTION = "new"\n'
            'PARAMETERS = {"type": "object", "properties": {}}\n'
            'def HANDLER(**kwargs):\n'
            '    return "new skill"\n',
            encoding="utf-8",
        )
        replies = [
            {
                "tool_calls": [
                    {
                        "id": "call_add",
                        "name": "add_skill",
                        "arguments": "{}",
                    }
                ]
            },
            {"content": "TASK_DONE: added"},
        ]
        agent, _ = build_agent(replies)
        agent.dispatchers["add_skill"] = lambda args: "created skill dynamic_new.py"

        final = agent.run_task("add a dynamic skill")

        self.assertIn("TASK_DONE", final)
        self.assertIn("dynamic_new", agent.dispatchers)

    def test_third_failure_quarantines_and_loop_continues(self):
        skill_path = self.skills_dir / "dynamic_target.py"
        skill_path.write_text(
            'DESCRIPTION = "fails"\n'
            'PARAMETERS = {"type": "object", "properties": {"attempt": {"type": "integer"}}}\n'
            'def HANDLER(**kwargs):\n'
            '    return "error: deliberate failure"\n',
            encoding="utf-8",
        )
        replies = [
            {
                "tool_calls": [
                    {
                        "id": f"call_{attempt}",
                        "name": "dynamic_target",
                        "arguments": json.dumps({"attempt": attempt}),
                    }
                ]
            }
            for attempt in range(3)
        ] + [{"content": "TASK_DONE: recovered"}]
        agent, llm = build_agent(replies)
        self.assertIn("dynamic_target", agent.dispatchers)

        final = agent.run_task("exercise a broken dynamic skill")

        self.assertIn("TASK_DONE", final)
        self.assertFalse(skill_path.exists())
        self.assertTrue((self.disabled_dir / "dynamic_target.py").is_file())
        self.assertNotIn("dynamic_target", agent.dispatchers)
        self.assertEqual(
            json.loads(self.health_file.read_text(encoding="utf-8"))["dynamic_target"]["status"],
            "quarantined",
        )
        receipts = [
            str(message.get("content", ""))
            for request in llm.requests
            for message in request["messages"]
            if message.get("role") == "tool"
        ]
        self.assertTrue(any("[SKILL_QUARANTINED]" in receipt for receipt in receipts))


class TddVerificationGateTests(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.executed: list[str] = []

    def test_task_done_blocks_until_a_passing_test_run(self):
        replies = [
            {
                "tool_calls": [
                    {
                        "id": "call_write",
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": "core/generated.py", "content": "x = 1\n"}
                        ),
                    }
                ]
            },
            {"content": "TASK_DONE: implementation finished"},
            {
                "tool_calls": [
                    {
                        "id": "call_test",
                        "name": "shell",
                        "arguments": json.dumps(
                            {"command": "python -m unittest discover -s tests -t ."}
                        ),
                    }
                ]
            },
            {"content": "TASK_DONE: implementation finished"},
        ]
        agent, llm = build_agent(replies)
        agent.dispatchers["write_file"] = (
            lambda args: self.executed.append("write") or "wrote core/generated.py"
        )
        agent.dispatchers["shell"] = (
            lambda args: self.executed.append("test") or "exit=0\nOK"
        )

        final = agent.run_task("write code and prove it works")

        self.assertIn("TASK_DONE", final)
        self.assertEqual(self.executed, ["write", "test"])
        self.assertEqual(len(llm.requests), 4)
        gate_requests = [
            request
            for request in llm.requests
            if any(
                "[TDD_VERIFICATION_GATE]" in str(message.get("content", ""))
                for message in request["messages"]
            )
        ]
        self.assertTrue(gate_requests)

    def test_task_done_tool_call_is_intercepted_before_execution(self):
        replies = [
            {
                "tool_calls": [
                    {
                        "id": "call_write",
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": "core/generated.py", "content": "x = 1\n"}
                        ),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call_done",
                        "name": "task_done",
                        "arguments": json.dumps({"summary": "finished"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call_test",
                        "name": "shell",
                        "arguments": json.dumps({"command": "python -m unittest"}),
                    }
                ]
            },
            {"content": "TASK_DONE: finished"},
        ]
        agent, llm = build_agent(replies)
        agent.dispatchers["write_file"] = (
            lambda args: self.executed.append("write") or "wrote core/generated.py"
        )
        agent.dispatchers["shell"] = lambda args: "exit=0\nOK"
        agent.dispatchers["task_done"] = (
            lambda args: self.executed.append("task_done") or "TASK_DONE: finished"
        )

        final = agent.run_task("must not finish without tests")

        self.assertIn("TASK_DONE", final)
        self.assertEqual(self.executed, ["write"])
        receipts = [
            str(message.get("content", ""))
            for request in llm.requests
            for message in request["messages"]
            if message.get("role") == "tool"
        ]
        self.assertTrue(any("[TDD_VERIFICATION_GATE]" in receipt for receipt in receipts))

    def test_python_write_clears_after_successful_test(self):
        replies = [
            {
                "tool_calls": [
                    {
                        "id": "call_write",
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": "core/generated.py", "content": "x = 1\n"}
                        ),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "call_test",
                        "name": "shell",
                        "arguments": json.dumps({"command": "pytest tests/test_x.py"}),
                    }
                ]
            },
            {"content": "TASK_DONE: done"},
        ]
        agent, _ = build_agent(replies)
        agent.dispatchers["write_file"] = lambda args: "wrote core/generated.py"
        agent.dispatchers["shell"] = lambda args: "exit=0\npassed"

        final = agent.run_task("write and test in one flow")

        self.assertIn("TASK_DONE", final)

    def test_failed_python_write_does_not_open_the_gate(self):
        replies = [
            {
                "tool_calls": [
                    {
                        "id": "call_write",
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": "core/generated.py", "content": "x = 1\n"}
                        ),
                    }
                ]
            },
            {"content": "TASK_DONE: nothing was actually written"},
        ]
        agent, llm = build_agent(replies)
        agent.dispatchers["write_file"] = (
            lambda args: "error: permission denied for core/generated.py"
        )

        final = agent.run_task("attempt a write")

        self.assertIn("TASK_DONE", final)
        self.assertEqual(len(llm.requests), 2)
        gate_requests = [
            request
            for request in llm.requests
            if any(
                "[TDD_VERIFICATION_GATE]" in str(message.get("content", ""))
                for message in request["messages"]
            )
        ]
        self.assertFalse(gate_requests)


if __name__ == "__main__":
    unittest.main()
