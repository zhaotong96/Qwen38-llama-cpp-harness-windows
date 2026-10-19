"""Phase 7 P2: pre-dispatch operator breakpoint (step_risky / step_all)."""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from core import interrupt
from core import persistence
from tests.support import IsolatedCase, build_agent


class StepModeFileTests(IsolatedCase):
    def test_default_mode_is_auto(self):
        self.assertEqual(interrupt.get_step_mode("no_task"), "auto")

    def test_set_and_read_mode_roundtrip(self):
        interrupt.set_step_mode("mode_task", "step_risky", base_root=self.tasks_root)
        self.assertEqual(interrupt.get_step_mode("mode_task", base_root=self.tasks_root), "step_risky")
        raw = (self.tasks_root / "mode_task" / "step_mode").read_text(encoding="utf-8")
        self.assertEqual(raw.strip(), "step_risky")
        self.assertEqual(raw.count("\n"), 1)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            interrupt.set_step_mode("mode_task", "yolo", base_root=self.tasks_root)

    def test_step_continue_flag_is_one_shot(self):
        interrupt.write_step_continue("flag_task", base_root=self.tasks_root)
        self.assertTrue(interrupt.consume_step_continue("flag_task", base_root=self.tasks_root))
        self.assertFalse(interrupt.consume_step_continue("flag_task", base_root=self.tasks_root))

    def test_wait_for_step_continue_returns_abort(self):
        def abort_soon():
            time.sleep(0.15)
            interrupt.set_abort_flag("wait_task", reason="operator stop", base_root=self.tasks_root)

        helper = threading.Thread(target=abort_soon, daemon=True)
        helper.start()
        started = time.monotonic()
        info = interrupt.wait_for_step_continue("wait_task", poll_interval=0.02, base_root=self.tasks_root)
        elapsed = time.monotonic() - started
        helper.join(timeout=2)
        self.assertIsNotNone(info)
        self.assertEqual(info["reason"], "operator stop")
        self.assertLess(elapsed, 2.0)

    def test_wait_for_step_continue_returns_none_on_release(self):
        def release_soon():
            time.sleep(0.1)
            interrupt.write_step_continue("release_task", base_root=self.tasks_root)

        helper = threading.Thread(target=release_soon, daemon=True)
        helper.start()
        info = interrupt.wait_for_step_continue("release_task", poll_interval=0.02, base_root=self.tasks_root)
        helper.join(timeout=2)
        self.assertIsNone(info)


class StepGateLoopTests(IsolatedCase):
    def _tool_reply(self, name: str, arguments: str = "{}") -> dict:
        return {"tool_calls": [{"id": f"call_{name}", "name": name, "arguments": arguments}]}

    def _prepare(self, mode: str, tool_name: str):
        arguments = '{"command": "Write-Output hi"}' if tool_name == "shell" else '{"path": "."}'
        agent, llm = build_agent(
            [self._tool_reply(tool_name, arguments), {"content": "TASK_DONE: released"}]
        )
        task_id = "gate_task"
        persistence.save_checkpoint(task_id, [{"role": "user", "content": "go"}])
        agent.current_task_id = task_id
        agent.context.bind_task(task_id)
        agent.context.append({"role": "user", "content": "go"})
        interrupt.set_step_mode(task_id, mode, base_root=self.tasks_root)
        return agent, llm, task_id

    def test_risky_tool_pauses_then_releases_on_flag(self):
        agent, _, task_id = self._prepare("step_risky", "shell")

        def release_when_paused():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = interrupt.read_step_state(task_id, base_root=self.tasks_root)
                if state and state.get("phase") == "paused_before_tool":
                    interrupt.write_step_continue(task_id, base_root=self.tasks_root)
                    return
                time.sleep(0.02)

        helper = threading.Thread(target=release_when_paused, daemon=True)
        helper.start()
        with mock.patch.object(agent, "_pause_for_operator", wraps=agent._pause_for_operator) as pause_spy:
            result = agent.step_loop()
        helper.join(timeout=5)

        self.assertIn("TASK_DONE", result)
        self.assertTrue(pause_spy.called)
        self.assertEqual(pause_spy.call_args[0][2], "shell")

    def test_readonly_tool_is_not_paused_in_step_risky(self):
        agent, _, _ = self._prepare("step_risky", "list_dir")
        with mock.patch.object(agent, "_pause_for_operator", wraps=agent._pause_for_operator) as pause_spy:
            result = agent.step_loop()
        self.assertIn("TASK_DONE", result)
        self.assertFalse(pause_spy.called)

    def test_step_all_pauses_even_for_readonly_tools(self):
        agent, _, task_id = self._prepare("step_all", "list_dir")

        def release_when_paused():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = interrupt.read_step_state(task_id, base_root=self.tasks_root)
                if state and state.get("phase") == "paused_before_tool":
                    interrupt.write_step_continue(task_id, base_root=self.tasks_root)
                    return
                time.sleep(0.02)

        helper = threading.Thread(target=release_when_paused, daemon=True)
        helper.start()
        with mock.patch.object(agent, "_pause_for_operator", wraps=agent._pause_for_operator) as pause_spy:
            result = agent.step_loop()
        helper.join(timeout=5)
        self.assertIn("TASK_DONE", result)
        self.assertTrue(pause_spy.called)

    def test_abort_at_breakpoint_rejects_tool(self):
        agent, _, task_id = self._prepare("step_risky", "shell")

        def abort_when_paused():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = interrupt.read_step_state(task_id, base_root=self.tasks_root)
                if state and state.get("phase") == "paused_before_tool":
                    interrupt.set_abort_flag(
                        task_id,
                        source="operator",
                        reason="no shell today",
                        halt_loop=True,
                        base_root=self.tasks_root,
                    )
                    return
                time.sleep(0.02)

        helper = threading.Thread(target=abort_when_paused, daemon=True)
        helper.start()
        with mock.patch.object(agent, "step_loop", wraps=agent.step_loop):
            result = agent.step_loop()
        helper.join(timeout=5)

        self.assertIn("TASK_ABORTED", result)
        messages = persistence.build_branch_context(task_id)
        rejection = next(
            (m for m in messages if m.get("role") == "tool" and "EXECUTION_REJECTED" in str(m.get("content"))),
            None,
        )
        self.assertIsNotNone(rejection)
        self.assertIn("no shell today", rejection["content"])

    def test_tree_exposes_step_mode(self):
        task_id = "tree_mode_task"
        persistence.save_checkpoint(task_id, [{"role": "user", "content": "go"}])
        interrupt.set_step_mode(task_id, "step_risky", base_root=self.tasks_root)
        tree = persistence.get_task_tree(task_id)
        self.assertEqual(tree["step_mode"], "step_risky")


if __name__ == "__main__":
    unittest.main()
