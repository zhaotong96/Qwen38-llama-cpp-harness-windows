"""Tests for 3-gate decoding/execution interrupts and crash self-healing (GATE-FUNC-03 & GATE-ARCH-02)."""
from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from core import api as api_mod
from core import interrupt
from core import persistence
from core import queue_manager
from core import subagent_runner
from core.config import AgentConfig
from tests.support import IsolatedCase, ScriptedLLM, build_agent
from tests.test_api import ApiCase, call


class InterruptGatesTests(IsolatedCase):
    def test_gate1_and_gate2_refuse_tool_execution_when_aborted_during_decoding(self):
        """Even when LLM finishes decoding tool_calls JSON, abort.flag set during decoding blocks execution 100%."""
        task_id = "interrupt_decode_task"
        executed_tools: list[str] = []

        agent, llm = build_agent([
            {"content": "TASK_DONE: recovered after rejection"},
        ])
        agent.dispatchers["list_dir"] = lambda args: executed_tools.append("list_dir") or "listed"

        first_call = True
        orig_create = llm.create

        def streaming_with_mid_decode_abort(**kwargs):
            nonlocal first_call
            if first_call:
                first_call = False

                def gen():
                    tc_delta = SimpleNamespace(
                        index=0,
                        id="call_dangerous_1",
                        function=SimpleNamespace(name="list_dir", arguments='{"path": "."}'),
                    )
                    yield SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content="I am going to run list_dir",
                                    reasoning_content="thinking...",
                                    tool_calls=[tc_delta],
                                ),
                                finish_reason=None,
                            )
                        ]
                    )
                    interrupt.set_abort_flag(
                        task_id,
                        source="web",
                        reason="Do not execute this tool call",
                        mode="reject_execution",
                        halt_loop=False,
                    )
                    yield SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=None),
                                finish_reason="tool_calls",
                            )
                        ]
                    )

                return gen()
            return orig_create(**kwargs)

        agent.client.chat.completions.create = streaming_with_mid_decode_abort
        final = agent.run_task("test decoding abort", task_id=task_id)

        self.assertIn("TASK_DONE", final)
        self.assertEqual(executed_tools, [])

        messages, _ = persistence.load_checkpoint(task_id)
        tool_msgs = [m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "call_dangerous_1"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("[EXECUTION_REJECTED]", tool_msgs[0]["content"])
        self.assertIn("REJECTED_BY_WEB", tool_msgs[0]["content"])

    def test_gate3_mid_execution_interrupt_discards_tool_output_and_consumes_queue(self):
        """If abort.flag is set while a tool is running, its output is ignored and queued correction is consumed."""
        task_id = "interrupt_exec_task"
        replies = [
            {"tool_calls": [{"id": "call_slow_1", "name": "list_dir", "arguments": '{"path": "."}'}]},
            {"content": "TASK_DONE: followed new queued direction"},
        ]
        agent, llm = build_agent(replies)

        def tool_that_gets_interrupted(args):
            interrupt.set_abort_flag(
                task_id,
                source="api",
                reason="Stop mid-execution",
                mode="reject_execution",
                halt_loop=False,
            )
            queue_manager.enqueue("Use alternative approach and finish", priority=10, source="api")
            return ("SENSITIVE_POLLUTED_OUTPUT_THAT_MUST_BE_IGNORED", "YWJj")

        agent.dispatchers["list_dir"] = tool_that_gets_interrupted
        final = agent.run_task("start slow tool", task_id=task_id)

        self.assertIn("TASK_DONE", final)
        messages, _ = persistence.load_checkpoint(task_id)
        tool_msgs = [m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "call_slow_1"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("[EXECUTION_INTERRUPTED_AND_IGNORED]", tool_msgs[0]["content"])
        self.assertNotIn("SENSITIVE_POLLUTED_OUTPUT_THAT_MUST_BE_IGNORED", tool_msgs[0]["content"])

        all_text = "\n".join(str(m.get("content", "")) for m in messages)
        self.assertIn("Use alternative approach and finish", all_text)


class TestCrashRecovery(IsolatedCase):
    """Chaos & power-loss crash recovery suite required by GATE-ARCH-02."""

    def test_orphan_subagent_lock_and_processing_queue_self_heal_on_startup(self):
        sub_root = subagent_runner.subagents_root()
        orphan_id = "sub_20261015_000000_000001"
        orphan_dir = sub_root / orphan_id
        orphan_dir.mkdir(parents=True, exist_ok=True)
        (orphan_dir / "status").write_text("running\n", encoding="utf-8")
        subagent_runner.active_lock_path().write_text(f"{orphan_id}\n", encoding="utf-8")

        item = queue_manager.enqueue("recover me after crash", priority=20)
        q_file = next(queue_manager.queue_dir().glob("*.json"))
        q_data = json.loads(q_file.read_text(encoding="utf-8"))
        q_data["status"] = "processing"
        q_file.write_text(json.dumps(q_data), encoding="utf-8")

        from core import agent as agent_mod
        with unittest.mock.patch.object(api_mod, "TASKS_ROOT", self.tasks_root), \
                unittest.mock.patch.object(api_mod, "load_config", return_value=AgentConfig()), \
                unittest.mock.patch.object(agent_mod, "OpenAI", lambda *a, **kw: ScriptedLLM()):
            harness = api_mod.Harness()

        self.assertIn(orphan_id, harness.healed_subagents)
        self.assertFalse(subagent_runner.active_lock_path().exists())
        self.assertEqual((orphan_dir / "status").read_text(encoding="utf-8").strip(), "cancelled")

        self.assertEqual(harness.recovered_queue_items, 1)
        recovered_list = queue_manager.list_queue()
        self.assertEqual(len(recovered_list), 1)
        self.assertEqual(recovered_list[0]["id"], item["id"])
        self.assertEqual(recovered_list[0]["status"], "queued")

    def test_dangling_tool_calls_healed_automatically_on_resume(self):
        task_id = "crashed_mid_tool"
        persistence.append_dag_node(task_id, {"role": "system", "content": "sys"})
        persistence.append_dag_node(task_id, {"role": "user", "content": "do work"})
        persistence.append_dag_node(
            task_id,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_orphan_99", "type": "function", "function": {"name": "shell", "arguments": '{"command": "dir"}'}}
                ],
            },
        )
        persistence.save_status(task_id, "running")

        agent, llm = build_agent([{"content": "TASK_DONE: resumed cleanly after crash"}])
        final = agent.resume_task(task_id)

        self.assertIn("TASK_DONE", final)
        self.assertEqual(persistence.load_status(task_id), "done")

        messages, _ = persistence.load_checkpoint(task_id)
        tool_receipts = [
            m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "call_orphan_99"
        ]
        self.assertEqual(len(tool_receipts), 1)
        self.assertIn("[CRASH_RECOVERY]", tool_receipts[0]["content"])


class InterruptApiTests(ApiCase):
    def test_post_interrupt_endpoint_writes_abort_flag_without_blocking(self):
        persistence.save_checkpoint("running_task_1", [{"role": "user", "content": "hi"}])
        interrupt.write_step_state("running_task_1", "step_0001", 1, phase="decoding")

        code, res = call(
            self.base,
            "POST",
            "/tasks/running_task_1/interrupt",
            {"source": "web", "reason": "Wrong direction", "mode": "reject_execution"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(res["ok"])
        self.assertEqual(res["abort"]["target_step_id"], "step_0001")
        self.assertTrue(res["abort"]["rejected"])

        code, state_res = call(self.base, "GET", "/tasks/running_task_1/step_state")
        self.assertEqual(code, 200)
        self.assertTrue(state_res["step_state"]["rejected"])
        self.assertEqual(state_res["abort_flag"]["reason"], "Wrong direction")


if __name__ == "__main__":
    unittest.main()
