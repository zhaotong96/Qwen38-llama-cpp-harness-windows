"""Phase 11D: macro crystallization — distillation, taint slots, purity, handover.

Covers the seven mandated scenarios from TASK_DEV_PHASE11D_MACRO_CRYSTALLIZATION
§四.3: taint seed extraction, causal spine deforestation, spatial invariance,
AST purity, sandbox dry-run promotion, step-indexed stale-UI hot handover, and
zero-token deterministic replay.
"""
from __future__ import annotations

import json
import sys
import time
import types
import unittest
from unittest import mock

from core import persistence
from core import skill_sandbox
from core.macro_compiler import (
    compile_and_deploy_macro,
    extract_goal_taint_seeds,
    extract_winning_spine,
    synthesize_macro_skill_code,
)
from tests.support import IsolatedCase


def add_turn(
    task_id: str,
    calls: list[tuple[str, str, dict]],
    receipts: list[tuple[str, str]],
) -> None:
    """Append one (assistant tool_calls + tool receipts) turn span to the DAG."""
    persistence.append_dag_node(
        task_id,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for call_id, name, args in calls
            ],
        },
    )
    for call_id, content in receipts:
        persistence.append_dag_node(
            task_id, {"role": "tool", "tool_call_id": call_id, "content": content}
        )


GROUNDING_HIT = (
    '[GROUNDING_HIT] mode=uia tier=2 matched=1 visual_tokens=0\n'
    '  target="导出报表" role=Button\n'
    '  control_id=7 Button "导出报表"\n'
    '  rect=[10, 20, 110, 60] center=(60, 40)  # global physical px\n'
    "  next: mouse click x=60 y=40 (or control_id=7)"
)


def sample_trajectory(task_id: str) -> None:
    """goal → screenshot probe → grounding+click → failed retry → taint paste → success."""
    persistence.append_dag_node(
        task_id,
        {"role": "user", "content": '请把 "季度财务报表" 导出到 report.xlsx，日期 2026-10-30'},
    )
    add_turn(task_id,
             [("c1", "screenshot", {})],
             [("c1", "saved to .agent/screen_001.jpg")])
    add_turn(task_id,
             [("c2", "grounding", {"target": "导出报表", "role": "Button", "auto_id": "btnExport"})],
             [("c2", GROUNDING_HIT)])
    add_turn(task_id,
             [("c3", "mouse", {"action": "click", "x": 60, "y": 40})],
             [("c3", "exit=0\nclicked control 7")])
    add_turn(task_id,
             [("c4", "shell", {"command": "python build_report.py"})],
             [("c4", "exit=1\nTraceback (most recent call last):\nValueError: bad input")])
    add_turn(task_id,
             [("c5", "clipboard", {"action": "set_text", "text": "2026-10-30"})],
             [("c5", '{"ok": true}')])
    add_turn(task_id,
             [("c6", "shell", {"command": "python make_report.py"})],
             [("c6", "exit=0\nreport generated")])
    persistence.append_dag_node(
        task_id, {"role": "user", "content": "TASK_DONE: report exported"}
    )


class TaintSeedExtractionTests(unittest.TestCase):
    def test_taint_seed_extraction(self):
        goal = (
            '请在 2026-10-30 把 "季度财务报表" 导出到 C:\\data\\report.xlsx，'
            "保留最近 30 天数据"
        )
        seeds = extract_goal_taint_seeds(goal)
        values = {entry["value"] for entry in seeds.values()}
        self.assertIn("2026-10-30", values)
        self.assertIn("季度财务报表", values)
        self.assertIn("30", values)
        kinds = {entry["kind"] for entry in seeds.values()}
        self.assertIn("date", kinds)
        self.assertIn("quoted", kinds)
        self.assertIn("number", kinds)
        # Slot names are deterministic and kind-prefixed.
        self.assertIn("target_date_1", seeds)
        self.assertEqual(seeds["target_date_1"]["value"], "2026-10-30")

    def test_windows_path_seed(self):
        seeds = extract_goal_taint_seeds('save to C:\\data\\report.xlsx and finish')
        paths = [e["value"] for e in seeds.values() if e["kind"] == "path"]
        self.assertTrue(any("report.xlsx" in p for p in paths), seeds)

    def test_empty_goal_has_no_seeds(self):
        self.assertEqual(extract_goal_taint_seeds(""), {})


class CausalSpineDeforestationTests(IsolatedCase):
    def test_causal_spine_deforestation(self):
        task_id = "20260101_000000_000011"
        sample_trajectory(task_id)
        spine = extract_winning_spine(task_id)

        ops = [step["op"] for step in spine]
        self.assertEqual(ops, ["uia_click", "clipboard", "shell"], spine)
        # The grounding+mouse pair folds into one semantic click.
        self.assertEqual(spine[0]["args"]["target"], "导出报表")
        self.assertEqual(spine[0]["args"]["auto_id"], "btnExport")
        # The failed retry turn is gone; the successful shell stays.
        self.assertEqual(spine[2]["args"]["command"], "python make_report.py")
        self.assertNotIn("screenshot", ops)

    def test_empty_trajectory_yields_empty_spine(self):
        self.assertEqual(extract_winning_spine("20260101_000000_099999"), [])


class SpatialInvarianceTests(IsolatedCase):
    def test_spatial_invariance_relative_coords(self):
        task_id = "20260101_000000_000012"
        add_turn(task_id,
                 [("m1", "mouse", {"action": "click", "x": 1920, "y": 1080})],
                 [("m1", "exit=0\nclicked raw pixels")])
        add_turn(task_id,
                 [("m2", "mouse", {"action": "click", "control_id": 9})],
                 [("m2", "exit=0\nclicked control 9")])
        spine = extract_winning_spine(task_id)

        ops = [step["op"] for step in spine]
        self.assertEqual(ops, ["mouse"], spine)
        args = spine[0]["args"]
        self.assertNotIn("x", args)
        self.assertNotIn("y", args)
        self.assertEqual(args["control_id"], 9)
        serialized = json.dumps(spine)
        self.assertNotIn('"x":', serialized)
        self.assertNotIn('"y":', serialized)


class AstPurityTests(unittest.TestCase):
    def test_ast_purity_of_synthesized_code(self):
        steps = [
            {
                "op": "uia_click",
                "args": {"target": "$slot:target_text_1", "role": "Button", "auto_id": ""},
                "intent": "click export",
            },
            {
                "op": "shell",
                "args": {"command": "python make_report.py"},
                "intent": "run report",
            },
        ]
        code = synthesize_macro_skill_code(
            "macro_export_report",
            "Export the quarterly report",
            {
                "type": "object",
                "properties": {
                    "target_text_1": {"type": "string", "description": "button label"}
                },
                "required": [],
            },
            {"target_text_1": "导出报表"},
            steps,
        )
        errors = skill_sandbox.check_skill_ast(code)
        self.assertEqual(errors, [], errors)
        # The module must also import cleanly (constants only at top level).
        namespace: dict = {}
        exec(compile(code, "macro_export_report.py", "exec"), namespace)
        self.assertEqual(namespace["MACRO_NAME"], "macro_export_report")
        self.assertEqual(len(namespace["MACRO_STEPS"]), 2)
        self.assertTrue(callable(namespace["run"]))


SANDBOX_SKILL = '''\
import json, re, time

DESCRIPTION = {description_r}
MACRO_NAME = {macro_name_r}
PARAMETERS = {schema_r}
DEFAULT_ARGS = {defaults_r}
RETRY_BACKOFF_SECONDS = (0.0, 0.0, 0.0)
MACRO_STEPS = {steps_r}
_CONTROL_ID_RE = r"control_id=(\\d+)"
_CENTER_RE = r"center=\\((-?\\d+),\\s*(-?\\d+)\\)"
_EXIT_FAIL_RE = r"(?m)^exit=[1-9]"
'''


def build_macro_module(steps, defaults=None, schema=None, name="macro_test_flow"):
    """Compile a macro module into a live namespace with instant backoff."""
    code = synthesize_macro_skill_code(
        name,
        "test macro",
        schema or {"type": "object", "properties": {}, "required": []},
        defaults or {},
        steps,
    )
    namespace: dict = {}
    exec(compile(code, f"{name}.py", "exec"), namespace)
    namespace["RETRY_BACKOFF_SECONDS"] = (0.0, 0.0, 0.0)
    return namespace


def stub_skill_modules(**modules):
    """Inject fake skills.<name> modules so macros never touch the desktop.

    Patches both ``sys.modules`` and the ``skills`` namespace package
    attributes: once a prior test imported the real module, the parent
    package attribute would otherwise shadow the sys.modules stub during
    ``from skills import <name>``.
    """
    import contextlib

    import skills as skills_pkg

    stack = contextlib.ExitStack()
    mapping: dict[str, types.ModuleType] = {}
    for name, spec in modules.items():
        stub = types.ModuleType(f"skills.{name}")
        for attr, value in spec.items():
            setattr(stub, attr, value)
        mapping[f"skills.{name}"] = stub
        previous = getattr(skills_pkg, name, None)
        setattr(skills_pkg, name, stub)
        if previous is not None:
            stack.callback(setattr, skills_pkg, name, previous)
        else:
            stack.callback(lambda pkg=skills_pkg, attr=name: pkg.__dict__.pop(attr, None))
    stack.enter_context(mock.patch.dict(sys.modules, mapping))
    return stack


class SandboxPromotionTests(IsolatedCase):
    def test_sandbox_dry_run_promotion(self):
        task_id = "20260101_000000_000013"
        sample_trajectory(task_id)

        skills_dir = self.state / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(skill_sandbox, "SKILLS_DIR", skills_dir), mock.patch.object(
            skill_sandbox, "STAGING_DIR", self.state / "staging"
        ), mock.patch.object(skill_sandbox, "HEALTH_FILE", self.state / "health.json"):
            result = compile_and_deploy_macro(
                task_id,
                "macro_export_report",
                "Export the quarterly report with one click",
            )

        self.assertTrue(result.startswith("created skill"), result)
        deployed = skills_dir / "macro_export_report.py"
        self.assertTrue(deployed.is_file())
        source = deployed.read_text(encoding="utf-8")
        self.assertIn("MACRO_STALE_HANDOVER", source)
        self.assertIn("导出报表", source)
        # The taint seed became a slot reference inside MACRO_STEPS.
        self.assertIn("$slot:target_date_1", source)
        self.assertEqual(
            skill_sandbox.check_skill_ast(source), [],
            "deployed macro must be AST-pure",
        )

    def test_rejects_non_macro_name(self):
        result = compile_and_deploy_macro("20260101_000000_000013", "not_macro", "x")
        self.assertTrue(result.startswith("error:"))

    def test_captures_window_title_guard_v01(self):
        task_id = "20260101_000000_000099"
        persistence.append_dag_node(task_id, {"role": "user", "content": "导出财务报表"})
        add_turn(
            task_id,
            [("w1", "window_manager", {"action": "activate_window", "title": "Excel - 报表"})],
            [("w1", "activated")],
        )
        add_turn(
            task_id,
            [("s1", "shell", {"command": "python export.py"})],
            [("s1", "exit=0\nok")],
        )
        skills_dir = self.state / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(skill_sandbox, "SKILLS_DIR", skills_dir), mock.patch.object(
            skill_sandbox, "STAGING_DIR", self.state / "staging"
        ), mock.patch.object(skill_sandbox, "HEALTH_FILE", self.state / "health.json"):
            result = compile_and_deploy_macro(task_id, "macro_excel_export", "Excel export")
        self.assertTrue(result.startswith("created skill"), result)
        deployed = skills_dir / "macro_excel_export.py"
        source = deployed.read_text(encoding="utf-8")
        self.assertIn("'window_title': 'Excel - 报表'", source)


class HotHandoverTests(unittest.TestCase):
    def test_step_indexed_hot_handover_on_stale_ui(self):
        steps = [
            {
                "op": "clipboard",
                "args": {"action": "set_text", "text": "$slot:target_date_1"},
                "intent": "fill the date",
            },
            {
                "op": "uia_click",
                "args": {"target": "导出报表", "role": "Button", "auto_id": ""},
                "intent": "导出报表",
            },
            {
                "op": "shell",
                "args": {"command": "python make_report.py"},
                "intent": "run report",
            },
        ]
        module = build_macro_module(
            steps,
            defaults={"target_date_1": "2026-10-30"},
            name="macro_stale_flow",
        )

        clipboard_stub = {
            "run": lambda args, runtime=None: json.dumps({"ok": True}),
        }
        grounding_stub = {
            "run": lambda args, runtime=None: "error: no semantic match for 导出报表",
        }

        with stub_skill_modules(clipboard=clipboard_stub, grounding=grounding_stub):
            raw = module["run"]({}, {})

        payload = json.loads(raw)
        self.assertEqual(payload["status"], "MACRO_STALE_AT_STEP")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["completed_steps_count"], 1)
        self.assertEqual(payload["failed_step_index"], 2)
        self.assertEqual(payload["failed_step_spec"]["op"], "uia_click")
        self.assertIn("[MACRO_STALE_HANDOVER]", payload["recovery_directive"])
        self.assertIn("Do NOT restart from Step 1", payload["recovery_directive"])
        self.assertIn("Resume interactively from Step 2", payload["recovery_directive"])
        self.assertIn("导出报表", payload["recovery_directive"])

    def test_invalid_decision_free_input_is_json_safe(self):
        steps = [{"op": "shell", "args": {"command": "boom"}, "intent": "x"}]
        module = build_macro_module(steps, name="macro_fail_flow")
        shell_stub = {"HANDLER": lambda **kwargs: "exit=1\nnope"}
        with stub_skill_modules(shell=shell_stub):
            raw = module["run"]({}, {})
        payload = json.loads(raw)
        self.assertEqual(payload["completed_steps_count"], 0)
        self.assertEqual(payload["failed_step_index"], 1)


class ZeroTokenReplayTests(unittest.TestCase):
    def test_macro_execution_zero_token_success(self):
        steps = [
            {
                "op": "clipboard",
                "args": {"action": "set_text", "text": "$slot:target_date_1"},
                "intent": "fill the date",
            },
            {
                "op": "uia_click",
                "args": {"target": "导出报表", "role": "Button", "auto_id": ""},
                "intent": "导出报表",
            },
            {
                "op": "shell",
                "args": {"command": "python make_report.py"},
                "intent": "run report",
            },
        ]
        module = build_macro_module(
            steps,
            defaults={"target_date_1": "2026-10-30"},
            name="macro_fast_flow",
        )

        seen: dict[str, list] = {"mouse": [], "clipboard": []}

        def clipboard_run(args, runtime=None):
            seen["clipboard"].append(dict(args))
            return json.dumps({"ok": True})

        def grounding_run(args, runtime=None):
            return GROUNDING_HIT

        def mouse_handler(**kwargs):
            seen["mouse"].append(dict(kwargs))
            return "exit=0\nclicked"

        def shell_handler(**kwargs):
            return "exit=0\nreport generated"

        with stub_skill_modules(
            clipboard={"run": clipboard_run},
            grounding={"run": grounding_run},
            mouse={"HANDLER": mouse_handler},
            shell={"HANDLER": shell_handler},
        ):
            started = time.perf_counter()
            raw = module["run"]({}, {})
            elapsed = time.perf_counter() - started

        self.assertTrue(raw.startswith("MACRO_COMPLETED_ZERO_TOKEN"), raw)
        self.assertIn("(0 model tokens consumed)", raw)
        self.assertLess(elapsed, 1.0, f"replay took {elapsed:.3f}s, must be millisecond-scale")
        # The taint slot resolved through DEFAULT_ARGS; the click used the
        # re-grounded control_id, never the recorded raw pixels.
        self.assertEqual(seen["clipboard"][0]["text"], "2026-10-30")
        self.assertEqual(seen["mouse"][0], {"action": "click", "control_id": 7})

    def test_retry_envelope_recovers_transient_failure(self):
        steps = [
            {"op": "shell", "args": {"command": "flaky.py"}, "intent": "flaky step"},
        ]
        module = build_macro_module(steps, name="macro_retry_flow")
        calls = {"n": 0}

        def flaky_handler(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return "exit=1\ntransient UI delay"
            return "exit=0\ndone"

        with stub_skill_modules(shell={"HANDLER": flaky_handler}):
            raw = module["run"]({}, {})
        self.assertEqual(calls["n"], 3)
        self.assertTrue(raw.startswith("MACRO_COMPLETED_ZERO_TOKEN"), raw)


if __name__ == "__main__":
    unittest.main()
