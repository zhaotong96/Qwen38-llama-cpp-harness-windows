"""Phase 12D: macro health ledger and autonomous hot re-crystallization.

Covers the fourteen mandated scenarios from TASK_DEV_PHASE12D_MACRO_EVOLUTION
§四: enriched stale-handover capture, the three-strike health ledger, agent
recognition and autonomous re-crystallization on TASK_DONE, safe overwrite
deployment, ledger reset, updated-selector replay, no-op guards, AST purity,
ledger concurrency, GC immutability of the handover receipt, and the full
mock lifecycle.
"""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from unittest import mock

from core import agent as agent_mod
from core import macro_compiler
from core import persistence
from core import skill_sandbox
from core.macro_compiler import compile_and_deploy_macro, synthesize_macro_skill_code
from tests.support import IsolatedCase, build_agent
from tests.test_macro_compiler import (
    GROUNDING_HIT,
    add_turn,
    build_macro_module,
    stub_skill_modules,
)

MACRO_NAME = "macro_export_report"
OLD_TARGET = "导出报表"
NEW_TARGET = "批量导出"
WINDOW_TITLE = "财务报表管理系统 v2.0"

STALE_RECEIPT = json.dumps(
    {
        "ok": False,
        "status": "MACRO_STALE_AT_STEP",
        "macro_name": MACRO_NAME,
        "completed_steps_count": 2,
        "failed_step_index": 3,
        "failed_step_op": "uia_click",
        "failed_target": OLD_TARGET,
        "window_title": WINDOW_TITLE,
        "error_snapshot": "grounding miss for target '导出报表'",
        "directive": (
            "[MACRO_STALE_HANDOVER] Steps 0..1 already executed on screen. "
            f"Step 3 ('{OLD_TARGET}') failed due to UI change. Please take over from Step 3."
        ),
    },
    ensure_ascii=False,
)


def v1_trajectory(task_id: str) -> None:
    """goal → grounding(导出报表)+click → shell: the original winning spine."""
    persistence.append_dag_node(
        task_id,
        {"role": "user", "content": f'请把 "季度财务报表" 导出到 report.xlsx，日期 2026-10-30'},
    )
    add_turn(
        task_id,
        [("g1", "grounding", {"target": OLD_TARGET, "role": "Button", "auto_id": ""})],
        [("g1", GROUNDING_HIT)],
    )
    add_turn(
        task_id,
        [("m1", "mouse", {"action": "click", "control_id": 7})],
        [("m1", "exit=0\nclicked control 7")],
    )
    add_turn(
        task_id,
        [("s1", "shell", {"command": "python build_report.py"})],
        [("s1", "exit=0\nreport generated")],
    )


def healed_trajectory(task_id: str) -> None:
    """goal → stale macro handover → interactive takeover with the new selector."""
    persistence.append_dag_node(
        task_id,
        {"role": "user", "content": f'请把 "季度财务报表" 导出到 report.xlsx，日期 2026-10-30'},
    )
    # The stale macro replay: steps 1..2 ran on screen, step 3 missed.
    add_turn(
        task_id,
        [("mc1", MACRO_NAME, {})],
        [("mc1", STALE_RECEIPT)],
    )
    # The visual-semantic takeover with the updated UI selector.
    add_turn(
        task_id,
        [("g2", "grounding", {"target": NEW_TARGET, "role": "Button", "auto_id": ""})],
        [("g2", GROUNDING_HIT.replace(OLD_TARGET, NEW_TARGET))],
    )
    add_turn(
        task_id,
        [("m2", "mouse", {"action": "click", "control_id": 7})],
        [("m2", "exit=0\nclicked control 7")],
    )
    add_turn(
        task_id,
        [("s2", "shell", {"command": "python build_report.py"})],
        [("s2", "exit=0\nreport generated")],
    )


class MacroEvolutionCase(IsolatedCase):
    """Shared fixture: isolated skills dir + health file + window-carrying v1."""

    def setUp(self) -> None:
        super().setUp()
        self.skills_dir = self.state / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.health_file = self.state / "skill_health.json"
        self._health_patches = mock.patch.multiple(
            skill_sandbox,
            SKILLS_DIR=self.skills_dir,
            HEALTH_FILE=self.health_file,
        )
        self._health_patches.start()
        self.addCleanup(self._health_patches.stop)

    def deploy_v1(self, task_id: str) -> str:
        result = compile_and_deploy_macro(
            task_id, MACRO_NAME, "Export the quarterly financial report", overwrite=False
        )
        self.assertTrue(result.startswith("created skill"), result)
        return result


class MacroHealthLedgerTests(MacroEvolutionCase):
    def test_macro_execution_success_resets_failure_counter(self):
        skill_sandbox.record_macro_execution_result(MACRO_NAME, success=False, failure_step=3)
        skill_sandbox.record_macro_execution_result(MACRO_NAME, success=False, failure_step=3)
        entry = skill_sandbox.record_macro_execution_result(MACRO_NAME, success=True)
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertEqual(entry["status"], skill_sandbox.MACRO_STATUS_HEALTHY)
        self.assertEqual(entry["total_executions"], 3)
        persisted = skill_sandbox.get_macro_health(MACRO_NAME)
        self.assertEqual(persisted["consecutive_failures"], 0)
        self.assertEqual(persisted["status"], skill_sandbox.MACRO_STATUS_HEALTHY)

    def test_macro_consecutive_failures_triggers_degraded_status(self):
        for index in range(2):
            entry = skill_sandbox.record_macro_execution_result(
                MACRO_NAME, success=False, failure_step=3
            )
            self.assertEqual(entry["status"], skill_sandbox.MACRO_STATUS_HEALTHY)
        entry = skill_sandbox.record_macro_execution_result(MACRO_NAME, success=False, failure_step=3)
        self.assertEqual(entry["consecutive_failures"], 3)
        self.assertEqual(entry["status"], skill_sandbox.MACRO_STATUS_DEGRADED)
        self.assertEqual(len(entry["failure_history"]), 3)
        self.assertEqual(entry["failure_history"][-1]["step"], 3)

    def test_concurrent_macro_execution_health_ledger_safety(self):
        errors: list[Exception] = []

        def worker(macro: str, fail: bool) -> None:
            try:
                for _ in range(25):
                    skill_sandbox.record_macro_execution_result(macro, success=not fail)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"macro_thread_{i % 2}", i % 2 == 1))
            for i in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        # The ledger file must stay valid JSON under concurrent writers.
        data = json.loads(self.health_file.read_text(encoding="utf-8"))
        entries = data["skills"]
        for name in ("macro_thread_0", "macro_thread_1"):
            self.assertIn(name, entries)
            self.assertEqual(entries[name]["total_executions"], 75)
        # One macro only ever succeeded, the other only ever failed.
        self.assertEqual(entries["macro_thread_0"]["consecutive_failures"], 0)
        self.assertGreaterEqual(entries["macro_thread_1"]["consecutive_failures"], 3)


class StaleHandoverCaptureTests(MacroEvolutionCase):
    def test_macro_stale_handover_captures_window_and_target_context(self):
        steps = [
            {
                "op": "uia_click",
                "args": {"target": OLD_TARGET, "role": "Button", "auto_id": ""},
                "intent": OLD_TARGET,
            },
        ]
        module = build_macro_module(
            steps, defaults={"window_title": WINDOW_TITLE, "target_text_1": OLD_TARGET}
        )

        def grounding_miss(args, runtime=None):
            return "[GROUNDING_MISS] no control matched"

        with stub_skill_modules(grounding={"run": grounding_miss}):
            raw = module["run"]({}, runtime={})

        receipt = json.loads(raw)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["status"], "MACRO_STALE_AT_STEP")
        self.assertEqual(receipt["macro_name"], "macro_test_flow")
        self.assertEqual(receipt["completed_steps_count"], 0)
        self.assertEqual(receipt["failed_step_index"], 1)
        self.assertEqual(receipt["failed_step_op"], "uia_click")
        self.assertEqual(receipt["failed_target"], OLD_TARGET)
        self.assertEqual(receipt["window_title"], WINDOW_TITLE)
        self.assertTrue(receipt["error_snapshot"])
        self.assertIn("[MACRO_STALE_HANDOVER]", receipt["directive"])
        self.assertIn("Step 1 ('导出报表') failed due to UI change", receipt["directive"])
        self.assertIn("take over from Step 1", receipt["directive"])
        # The legacy recovery directive is preserved for backward compatibility.
        self.assertIn("Resume interactively from Step 1", receipt["recovery_directive"])

    def test_stale_handover_retains_immutable_context_facts(self):
        agent, _llm = build_agent([])
        task_id = "20261101_150000_000001"
        agent.current_task_id = task_id
        persistence.append_dag_node(task_id, {"role": "user", "content": "导出报表"})
        # One assistant turn: macro handover + a transient screenshot failure.
        # Without protection the GC would fold the whole span — including the
        # handover receipt that the healing hook depends on.
        persistence.append_dag_node(
            task_id,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": MACRO_NAME, "arguments": "{}"}},
                    {"id": "c2", "type": "function",
                     "function": {"name": "screenshot", "arguments": "{}"}},
                ],
            },
        )
        persistence.append_dag_node(
            task_id, {"role": "tool", "tool_call_id": "c1", "content": STALE_RECEIPT}
        )
        persistence.append_dag_node(
            task_id,
            {"role": "tool", "tool_call_id": "c2", "content": "error: screenshot device busy"},
        )
        # A clean closing turn so the GC tail condition holds.
        add_turn(
            task_id,
            [("ok1", "list_dir", {"path": "."})],
            [("ok1", ".agent/ core/ docs/ skills/ tests/")],
        )
        agent.context.bind_task(task_id)
        result = agent.context.prune_resolved_trial_tail()
        # The stale handover receipt is a physical fact: never folded away.
        contents = [str(m.get("content") or "") for m in agent.context.as_list()]
        self.assertTrue(
            any("[MACRO_STALE_HANDOVER]" in c for c in contents),
            f"handover receipt was pruned: {result}",
        )
        self.assertIn("MACRO_STALE_AT_STEP", " ".join(contents))


class RecrystallizationTests(MacroEvolutionCase):
    def setUp(self) -> None:
        super().setUp()
        self.base_task = "20261101_140000_000001"
        v1_trajectory(self.base_task)
        self.deploy_v1(self.base_task)

    def _build_healed_task(self) -> str:
        task_id = "20261101_150000_000002"
        healed_trajectory(task_id)
        return task_id

    def test_agent_identifies_stale_macro_in_trajectory(self):
        task_id = self._build_healed_task()
        agent, _llm = build_agent([])
        agent.current_task_id = task_id
        self.assertEqual(agent._find_stale_macro_name(), MACRO_NAME)

    def test_agent_auto_recrystallizes_healed_macro_on_task_done(self):
        task_id = self._build_healed_task()
        agent, llm = build_agent([{"content": "TASK_DONE: takeover completed and verified"}])

        final = agent.run_task("finish the healed flow", task_id=task_id)

        self.assertIn("TASK_DONE", final)
        self.assertIn("[AUTONOMOUS_HEALING]", final)
        self.assertIn(MACRO_NAME, final)
        # The regenerated macro carries the healed selector, not the stale one.
        healed_code = (self.skills_dir / f"{MACRO_NAME}.py").read_text(encoding="utf-8")
        self.assertIn(NEW_TARGET, healed_code)
        self.assertNotIn(f"'{OLD_TARGET}'", healed_code)
        # One model round produced the final answer; the healing was host-side.
        self.assertEqual(len(llm.requests), 1)

    def test_auto_recrystallize_overwrites_existing_skill_safely(self):
        task_id = self._build_healed_task()
        old_code = (self.skills_dir / f"{MACRO_NAME}.py").read_text(encoding="utf-8")
        self.assertNotIn(NEW_TARGET, old_code)

        agent, _llm = build_agent([{"content": "TASK_DONE: healed"}])
        agent.run_task("finish", task_id=task_id)

        healed_code = (self.skills_dir / f"{MACRO_NAME}.py").read_text(encoding="utf-8")
        self.assertIn(NEW_TARGET, healed_code)
        self.assertEqual(skill_sandbox.check_skill_ast(healed_code), [])

    def test_auto_recrystallize_resets_health_ledger(self):
        task_id = self._build_healed_task()
        for _ in range(3):
            skill_sandbox.record_macro_execution_result(MACRO_NAME, success=False, failure_step=3)
        self.assertEqual(
            skill_sandbox.get_macro_health(MACRO_NAME)["status"],
            skill_sandbox.MACRO_STATUS_DEGRADED,
        )

        agent, _llm = build_agent([{"content": "TASK_DONE: healed"}])
        agent.run_task("finish", task_id=task_id)

        entry = skill_sandbox.get_macro_health(MACRO_NAME)
        self.assertEqual(entry["status"], skill_sandbox.MACRO_STATUS_HEALTHY)
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertEqual(entry["failure_history"], [])
        self.assertTrue(entry.get("last_healed_at"))

    def test_recrystallized_macro_replays_with_updated_selector(self):
        task_id = self._build_healed_task()
        agent, _llm = build_agent([{"content": "TASK_DONE: healed"}])
        agent.run_task("finish", task_id=task_id)

        code = (self.skills_dir / f"{MACRO_NAME}.py").read_text(encoding="utf-8")
        namespace: dict = {}
        exec(compile(code, f"{MACRO_NAME}.py", "exec"), namespace)
        namespace["RETRY_BACKOFF_SECONDS"] = (0.0, 0.0, 0.0)

        clicked: list = []

        def grounding_run(args, runtime=None):
            self.assertEqual(args.get("target"), NEW_TARGET)
            return GROUNDING_HIT.replace(OLD_TARGET, NEW_TARGET)

        def mouse_handler(**kwargs):
            clicked.append(dict(kwargs))
            return "exit=0\nclicked control 7"

        def shell_handler(**kwargs):
            return "exit=0\nreport generated"

        with stub_skill_modules(
            grounding={"run": grounding_run},
            mouse={"HANDLER": mouse_handler},
            shell={"HANDLER": shell_handler},
        ):
            raw = namespace["run"]({}, runtime={})

        self.assertTrue(raw.startswith("MACRO_COMPLETED_ZERO_TOKEN"), raw)
        self.assertEqual(clicked, [{"action": "click", "control_id": 7}])

    def test_no_recrystallize_triggered_when_macro_fully_succeeded(self):
        task_id = "20261101_160000_000003"
        persistence.append_dag_node(
            task_id, {"role": "user", "content": "导出报表"}
        )
        add_turn(
            task_id,
            [("okm", MACRO_NAME, {})],
            [("okm", "MACRO_COMPLETED_ZERO_TOKEN: 3 step(s) replayed deterministically (0 model tokens consumed).")],
        )
        agent, _llm = build_agent([{"content": "TASK_DONE: all good"}])
        with mock.patch.object(
            macro_compiler, "compile_and_deploy_macro", side_effect=AssertionError("must not recompile")
        ) as deploy:
            final = agent.run_task("wrap up", task_id=task_id)
        self.assertIn("TASK_DONE", final)
        self.assertNotIn("[AUTONOMOUS_HEALING]", final)
        self.assertEqual(deploy.call_count, 0)

    def test_no_recrystallize_triggered_when_task_fails_or_blocked(self):
        task_id = self._build_healed_task()
        agent, _llm = build_agent([{"content": "TASK_BLOCKED: takeover impossible"}])
        with mock.patch.object(
            macro_compiler, "compile_and_deploy_macro", side_effect=AssertionError("must not recompile")
        ) as deploy:
            final = agent.run_task("try to finish", task_id=task_id)
        self.assertIn("TASK_BLOCKED", final)
        self.assertEqual(deploy.call_count, 0)

    def test_ast_purity_guarantee_for_evolved_macro(self):
        task_id = self._build_healed_task()
        spine = macro_compiler.extract_winning_spine(task_id, "main")
        # The stale macro invocation itself must never join the healed spine.
        self.assertFalse(any(step["op"] == MACRO_NAME for step in spine))
        self.assertTrue(any(step["op"] == "uia_click" for step in spine))

        goal = '请把 "季度财务报表" 导出到 report.xlsx，日期 2026-10-30'
        seeds = macro_compiler.extract_goal_taint_seeds(goal)
        steps, defaults, schema = macro_compiler._parameterize_steps(spine, seeds)
        code = synthesize_macro_skill_code(MACRO_NAME, "healed export flow", schema, defaults, steps)
        self.assertEqual(skill_sandbox.check_skill_ast(code), [], code)

    def test_macro_evolution_end_to_end_mock_lifecycle(self):
        # 1. The macro runs fine initially.
        entry = skill_sandbox.record_macro_execution_result(MACRO_NAME, success=True)
        self.assertEqual(entry["status"], skill_sandbox.MACRO_STATUS_HEALTHY)
        # 2. A UI change makes step 3 miss, three times: degraded.
        for _ in range(3):
            skill_sandbox.record_macro_execution_result(MACRO_NAME, success=False, failure_step=3)
        self.assertEqual(
            skill_sandbox.get_macro_health(MACRO_NAME)["status"],
            skill_sandbox.MACRO_STATUS_DEGRADED,
        )
        # 3. The model takes over interactively and reaches TASK_DONE.
        task_id = self._build_healed_task()
        agent, _llm = build_agent([{"content": "TASK_DONE: takeover finished"}])
        final = agent.run_task("complete the export", task_id=task_id)
        # 4. The finishing hook re-crystallized the healed spine automatically.
        self.assertIn("[AUTONOMOUS_HEALING]", final)
        healed_code = (self.skills_dir / f"{MACRO_NAME}.py").read_text(encoding="utf-8")
        self.assertIn(NEW_TARGET, healed_code)
        self.assertEqual(skill_sandbox.check_skill_ast(healed_code), [])
        # 5. The health ledger is reset: one block, healed in place, immune.
        entry = skill_sandbox.get_macro_health(MACRO_NAME)
        self.assertEqual(entry["status"], skill_sandbox.MACRO_STATUS_HEALTHY)
        self.assertEqual(entry["consecutive_failures"], 0)


if __name__ == "__main__":
    unittest.main()
