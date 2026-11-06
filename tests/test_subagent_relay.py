"""Phase 12C: Scout-Builder-Verifier relay pipeline with tamper-proof tickets.

Covers the sixteen mandated scenarios from TASK_DEV_PHASE12C_SUBAGENT_RELAY
§四: scout write bans, grid-aligned context anchors, builder prefix
inheritance and worktree isolation, verifier hazard detection (fake-green
skips, removed assertions, redline files), ticket issuance and tamper
proofing, parent --ff-only merge / revert verdicts, skill AST purity, the
end-to-end mock flow, crash cleanup, and relay concurrency guarding.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import agent as agent_mod
from core import persistence
from core import skill_sandbox
from core import subagent_gate
from core import subagent_runner
from core.subagent_runner import run_subagent
from skills import subagent_relay
from tests.support import IsolatedCase, ScriptedLLM
from tests.test_subagent_gate import _git, init_repo

BIG_REPORT = (
    "Investigation report: " + ("topology node " * 500)
    + "\nTASK_DONE: scout investigation complete"
)


class RelayStageCase(IsolatedCase):
    """Shared fixture: throwaway git repo + isolated subagent state."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="relay_test_")
        self.base = Path(self._tmp.name)
        self.repo = init_repo(self.base)
        self._orig_worktree_root = os.environ.get("SUBAGENT_WORKTREE_ROOT")
        os.environ["SUBAGENT_WORKTREE_ROOT"] = str(self.repo)
        self.addCleanup(self._restore_env)
        self.addCleanup(self._tmp.cleanup)

    def _restore_env(self) -> None:
        if self._orig_worktree_root is None:
            os.environ.pop("SUBAGENT_WORKTREE_ROOT", None)
        else:
            os.environ["SUBAGENT_WORKTREE_ROOT"] = self._orig_worktree_root


class ScoutRoleTests(RelayStageCase):
    def test_scout_role_strictly_bans_write_file(self):
        sub_llm = ScriptedLLM(
            [
                {"tool_calls": [
                    {"name": "write_file",
                     "arguments": json.dumps({"path": "evil.txt", "content": "boom"})},
                ]},
                {"content": "TASK_DONE: scout finished read-only"},
            ]
        )
        result = run_subagent(
            "parent_task_01", "investigate the modules", role_mode="scout",
            runtime={"subagent_client": sub_llm},
        )
        self.assertEqual(result["role_mode"], "scout")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["security_violations"])
        self.assertEqual(result["security_violations"][0]["tool"], "write_file")
        # The write never touched any filesystem.
        self.assertFalse((self.repo / "evil.txt").exists())
        self.assertEqual(self.repo.joinpath("README.md").read_text(encoding="utf-8"), "base\n")

    def test_scout_role_strictly_bans_patch_skill(self):
        sub_llm = ScriptedLLM(
            [
                {"tool_calls": [
                    {"name": "modify_skill",
                     "arguments": json.dumps({"skill_name": "mouse", "code": "DESCRIPTION='x'"})},
                ]},
                {"content": "TASK_DONE: scout finished"},
            ]
        )
        result = run_subagent(
            "parent_task_01", "investigate", role_mode="scout",
            runtime={"subagent_client": sub_llm},
        )
        self.assertEqual(result["security_violations"][0]["tool"], "modify_skill")
        self.assertIn("strictly read-only", result["security_violations"][0].get("message", ""))

    def test_scout_creates_grid_aligned_context_anchor(self):
        sub_llm = ScriptedLLM([{"content": BIG_REPORT}])
        result = run_subagent(
            "parent_task_01", "map the topology", role_mode="scout",
            runtime={"subagent_client": sub_llm},
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["anchor_ckpt_aligned"])
        self.assertTrue(result["anchor_node_id"])
        brief_path = subagent_runner.subagents_root() / result["subagent_id"] / "scout_brief.json"
        self.assertTrue(brief_path.is_file())
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        self.assertEqual(brief["anchor_node_id"], result["anchor_node_id"])
        self.assertTrue(brief["anchor_ckpt_aligned"])
        self.assertIn("topology", brief["report"])


class BuilderRoleTests(RelayStageCase):
    def test_builder_inherits_scout_anchor_prefix(self):
        sub_llm = ScriptedLLM(
            [
                {"content": BIG_REPORT},  # scout
                {"content": "TASK_DONE: builder plan accepted"},  # builder
            ]
        )
        scout = run_subagent(
            "parent_task_01", "map the topology", role_mode="scout",
            runtime={"subagent_client": sub_llm},
        )
        self.assertTrue(scout["anchor_ckpt_aligned"])
        builder = run_subagent(
            "parent_task_01", "implement the change", role_mode="builder",
            inherited_context={
                "anchor_node_id": scout["anchor_node_id"],
                "anchor_ckpt_aligned": scout["anchor_ckpt_aligned"],
                "report": scout["report"],
            },
            runtime={"subagent_client": sub_llm},
        )
        self.assertEqual(builder["status"], "completed")
        # The builder prompt forks from the scout anchor: prefix cache reuse.
        builder_prompt = sub_llm.requests[1]["messages"][-1]["content"]
        self.assertIn("[Inherited Scout Anchor]", builder_prompt)
        self.assertIn(scout["anchor_node_id"], builder_prompt)
        self.assertIn("topology", builder_prompt)

    def test_builder_modifications_stay_isolated_in_worktree(self):
        sub_llm = ScriptedLLM(
            [
                {"tool_calls": [
                    {"name": "write_file",
                     "arguments": json.dumps({"path": "core/app.py", "content": "VALUE = 42\n"})},
                ]},
                {"content": "TASK_DONE: change built"},
            ]
        )
        result = run_subagent(
            "parent_task_01", "bump VALUE", role_mode="builder",
            runtime={"subagent_client": sub_llm},
        )
        self.assertEqual(result["status"], "completed")
        wt_rel = result["worktree_path"]
        self.assertTrue(wt_rel.startswith(".agent/worktrees/"))
        wt_path = self.repo / wt_rel
        self.assertEqual((wt_path / "core" / "app.py").read_text(encoding="utf-8"), "VALUE = 42\n")
        # The main workspace is physically transparent to the builder.
        self.assertEqual((self.repo / "core" / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n")
        rc, status = _git(self.repo, "status", "--porcelain")
        self.assertEqual(rc, 0)
        self.assertEqual(status.strip(), "", "main workspace must stay pristine")


class VerifierTicketTests(IsolatedCase):
    def test_verifier_detects_builder_fake_green_skip(self):
        diff = (
            "diff --git a/tests/test_app.py b/tests/test_app.py\n"
            "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
            "@@ -1,3 +1,4 @@\n import unittest\n+@unittest.skip('flaky')\n def test_ok(self):\n"
        )
        hazards = subagent_gate.detect_diff_hazards(
            [{"path": "tests/test_app.py", "status": "M", "sha256": ""}], diff, []
        )
        self.assertIn(subagent_gate.HAZARD_TEST_SKIP_INJECTED, hazards)
        ticket = subagent_gate.generate_verification_ticket(
            "sub_relay_skip1", diff, {"exit_code": 0, "tests_run": 1}, hazards
        )
        self.assertEqual(ticket["verdict"], subagent_gate.TICKET_REJECTED)

    def test_verifier_detects_builder_removed_assertions(self):
        diff = (
            "diff --git a/tests/test_app.py b/tests/test_app.py\n"
            "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
            "@@ -1,3 +1,2 @@\n import unittest\n-self.assertEqual(1, 1)\n def test_ok(self):\n"
        )
        hazards = subagent_gate.detect_diff_hazards(
            [{"path": "tests/test_app.py", "status": "M", "sha256": ""}], diff, []
        )
        self.assertIn(subagent_gate.HAZARD_TEST_ASSERTION_REMOVED, hazards)
        ticket = subagent_gate.generate_verification_ticket(
            "sub_relay_del1", diff, {"exit_code": 0, "tests_run": 1}, hazards
        )
        self.assertEqual(ticket["verdict"], subagent_gate.TICKET_REJECTED)

    def test_verifier_detects_builder_redline_violation(self):
        diff = (
            "diff --git a/requirements.txt b/requirements.txt\n"
            "--- a/requirements.txt\n+++ b/requirements.txt\n"
            "+requests==2.0.0\n"
        )
        hazards = subagent_gate.detect_diff_hazards(
            [{"path": "requirements.txt", "status": "M", "sha256": ""}], diff, []
        )
        self.assertIn(subagent_gate.HAZARD_REDLINE_FILE_MODIFIED, hazards)
        ticket = subagent_gate.generate_verification_ticket(
            "sub_relay_red1", diff, {"exit_code": 0, "tests_run": 3}, hazards
        )
        self.assertEqual(ticket["verdict"], subagent_gate.TICKET_REJECTED)

    def test_verifier_issues_valid_ticket_on_clean_diff(self):
        diff = (
            "diff --git a/core/app.py b/core/app.py\n"
            "--- a/core/app.py\n+++ b/core/app.py\n"
            "-VALUE = 1\n+VALUE = 2\n"
        )
        tests = [{"step": 2, "command": "python -m unittest discover -s tests", "exit_code": 0}]
        hazards = subagent_gate.detect_diff_hazards(
            [{"path": "core/app.py", "status": "M", "sha256": "x"}], diff, tests, last_write_step=1
        )
        self.assertEqual(hazards, [])
        ticket = subagent_gate.generate_verification_ticket(
            "sub_relay_ok1", diff,
            {"command": "python -m unittest discover -s tests", "exit_code": 0, "tests_run": 8},
            hazards,
            builder_worktree=".agent/worktrees/sub_relay_ok1",
            scout_anchor_node="node_7a8b9c",
        )
        self.assertEqual(ticket["verdict"], subagent_gate.TICKET_VERIFIED)
        self.assertEqual(ticket["hazards_detected"], [])
        self.assertTrue(ticket["test_execution"]["passed"])
        self.assertEqual(ticket["test_execution"]["tests_run"], 8)
        self.assertEqual(ticket["builder_worktree"], ".agent/worktrees/sub_relay_ok1")
        self.assertEqual(ticket["scout_anchor_node"], "node_7a8b9c")
        # Persisted on disk under the subagent sandbox.
        persisted = subagent_gate.load_verification_ticket("sub_relay_ok1")
        self.assertIsNotNone(persisted)
        self.assertTrue(subagent_gate.verify_ticket_integrity(persisted))

    def test_verification_ticket_sha256_tamper_proofing(self):
        subagent_gate.generate_verification_ticket(
            "sub_relay_tamper1",
            "diff --git a/core/app.py b/core/app.py\n-VALUE = 1\n+VALUE = 2\n",
            {"exit_code": 0, "tests_run": 2},
            [],
        )
        path = subagent_gate.verification_ticket_path("sub_relay_tamper1")
        ticket = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(subagent_gate.verify_ticket_integrity(ticket))

        # Tamper with the attribution after the fact: integrity must fail.
        ticket["subagent_id"] = "sub_forged_identity"
        self.assertFalse(subagent_gate.verify_ticket_integrity(ticket))
        # A missing digest is equally untrustworthy.
        ticket2 = json.loads(path.read_text(encoding="utf-8"))
        ticket2.pop("payload_sha256")
        self.assertFalse(subagent_gate.verify_ticket_integrity(ticket2))
        # Forging a green exit code on a failed run is detected too.
        subagent_gate.generate_verification_ticket(
            "sub_relay_tamper2", "diff", {"exit_code": 1}, ["HAZARD_X"]
        )
        ticket3 = json.loads(
            subagent_gate.verification_ticket_path("sub_relay_tamper2").read_text(encoding="utf-8")
        )
        ticket3["test_execution"]["exit_code"] = 0
        self.assertFalse(subagent_gate.verify_ticket_integrity(ticket3))


class ParentVerdictTests(RelayStageCase):
    def test_parent_merges_clean_relay_delivery(self):
        sub_id = "sub_relay_merge1"
        wt = subagent_gate.create_subagent_worktree(sub_id, repo_root=self.repo)
        (wt / "core" / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
        rc, diff = _git(wt, "diff", "HEAD")
        self.assertEqual(rc, 0)
        ticket = subagent_gate.generate_verification_ticket(
            sub_id, diff, {"exit_code": 0, "tests_run": 1}, [],
            builder_worktree=f".agent/worktrees/{sub_id}",
        )
        outcome = subagent_gate.finalize_subagent_relay(
            sub_id, ticket, repo_root=self.repo, reason="clean delivery"
        )
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["verdict"], subagent_gate.TICKET_VERIFIED)
        self.assertEqual((self.repo / "core" / "app.py").read_text(encoding="utf-8"), "VALUE = 42\n")
        self.assertFalse(wt.exists())
        rc, branches = _git(self.repo, "branch", "--list", f"subtask_{sub_id}")
        self.assertEqual(branches.strip(), "")

    def test_parent_reverts_corrupt_relay_delivery(self):
        sub_id = "sub_relay_revert1"
        wt = subagent_gate.create_subagent_worktree(sub_id, repo_root=self.repo)
        (wt / "core" / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
        rc, diff = _git(wt, "diff", "HEAD")
        ticket = subagent_gate.generate_verification_ticket(
            sub_id, diff, {"exit_code": 3, "tests_run": 5},
            [subagent_gate.HAZARD_UNVERIFIED_CODE_EDIT],
            builder_worktree=f".agent/worktrees/{sub_id}",
        )
        outcome = subagent_gate.finalize_subagent_relay(
            sub_id, ticket, repo_root=self.repo, reason="tests never passed"
        )
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["verdict"], subagent_gate.TICKET_REJECTED)
        self.assertIn("Lesson", outcome.get("lesson", ""))
        self.assertFalse(wt.exists())
        self.assertEqual((self.repo / "core" / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_finalize_rejects_tampered_ticket(self):
        sub_id = "sub_relay_tamper3"
        ticket = subagent_gate.generate_verification_ticket(
            sub_id, "diff", {"exit_code": 1}, ["HAZARD_X"]
        )
        ticket["verdict"] = subagent_gate.TICKET_VERIFIED
        outcome = subagent_gate.finalize_subagent_relay(
            sub_id, ticket, repo_root=self.repo, reason="forged verdict"
        )
        self.assertFalse(outcome["ok"])
        self.assertIn("integrity", outcome["error"])


class RelayPipelineTests(RelayStageCase):
    def test_subagent_relay_skill_schema_and_ast_purity(self):
        self.assertTrue(subagent_relay.DESCRIPTION.strip())
        self.assertEqual(subagent_relay.PARAMETERS.get("type"), "object")
        errors = skill_sandbox.validate_openai_parameters_schema(subagent_relay.PARAMETERS)
        self.assertEqual(errors, [])
        source = Path(subagent_relay.__file__).read_text(encoding="utf-8")
        self.assertEqual(skill_sandbox.check_skill_ast(source), [])
        # It is protected as frozen core infrastructure.
        self.assertIn("subagent_relay", skill_sandbox.FROZEN_CORE_SKILLS)
        self.assertIn("subagent_relay", subagent_runner.EXCLUDED_META_SKILLS)

    def test_relay_pipeline_end_to_end_mock_flow(self):
        recorded: dict = {}

        def fake_run_subagent(task_id, prompt, role_mode="general", **kwargs):
            recorded.setdefault("stages", []).append((role_mode, prompt, kwargs))
            if role_mode == "scout":
                return {
                    "ok": True, "status": "completed", "subagent_id": "sub_scout1",
                    "anchor_node_id": "node_9aff", "anchor_ckpt_aligned": True,
                    "report": "topology findings",
                }
            if role_mode == "builder":
                return {
                    "ok": True, "status": "completed", "subagent_id": "sub_build1",
                    "worktree_path": ".agent/worktrees/sub_build1",
                }
            return {
                "ok": True, "status": "completed", "subagent_id": "sub_verify1",
                "builder_subagent_id": "sub_build1",
                "verification_ticket": {
                    "subagent_id": "sub_build1",
                    "verdict": subagent_gate.TICKET_VERIFIED,
                    "hazards_detected": [],
                },
                "hazard_flags": [],
                "test_execution": {"command": "python -m unittest", "exit_code": 0, "tests_run": 8},
            }

        with mock.patch.object(subagent_relay, "run_subagent", fake_run_subagent):
            raw = subagent_relay.run(
                {"goal": "fix the export bug", "target_modules": ["core/export.py"],
                 "test_target": "python -m unittest discover -p test_export.py"},
                runtime={"task_id": "main_session_77"},
            )
        summary = json.loads(raw)
        self.assertTrue(summary["ok"])
        stages = [s[0] for s in recorded["stages"]]
        self.assertEqual(stages, ["scout", "builder", "verifier"])
        # The scout anchor flowed into the builder context (prefix reuse).
        scout_prompt, builder_prompt, verifier_prompt = (
            recorded["stages"][0][1], recorded["stages"][1][1], recorded["stages"][2][1]
        )
        self.assertIn("fix the export bug", scout_prompt)
        self.assertIn("node_9aff", recorded["stages"][1][2]["inherited_context"]["anchor_node_id"])
        self.assertEqual(
            recorded["stages"][2][2]["builder_worktree"], ".agent/worktrees/sub_build1"
        )
        self.assertEqual(summary["builder"]["subagent_id"], "sub_build1")
        self.assertEqual(
            summary["verification_ticket"]["verdict"], subagent_gate.TICKET_VERIFIED
        )
        self.assertIn("finalize", summary["next_action"])

    def test_relay_finalize_action_applies_ticket_verdict(self):
        ticket = subagent_gate.generate_verification_ticket(
            "sub_relay_fin1", "diff", {"exit_code": 1}, ["HAZARD_X"]
        )
        applied: dict = {}

        def fake_finalize(sub_id, tk, repo_root=None, reason=""):
            applied["id"] = sub_id
            applied["ticket"] = tk
            applied["reason"] = reason
            return {"ok": True, "decision": "revert", "lesson": "[Branch Rewind Lesson] learned"}

        with mock.patch.object(subagent_gate, "finalize_subagent_relay", fake_finalize):
            raw = subagent_relay.run(
                {"action": "finalize", "subagent_id": "sub_relay_fin1", "reason": "bad delivery"}
            )
        outcome = json.loads(raw)
        self.assertTrue(outcome["ok"])
        self.assertEqual(applied["id"], "sub_relay_fin1")
        self.assertEqual(applied["ticket"]["payload_sha256"], ticket["payload_sha256"])
        self.assertEqual(applied["reason"], "bad delivery")

    def test_worktree_cleanup_on_relay_exception(self):
        sub_llm = ScriptedLLM([])
        with mock.patch.object(
            agent_mod.Agent, "run_task", side_effect=RuntimeError("builder exploded")
        ):
            result = run_subagent(
                "parent_task_01", "explode", role_mode="builder",
                runtime={"subagent_client": sub_llm},
            )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result.get("worktree_cleaned"))
        wt_path = self.repo / result["worktree_path"]
        self.assertFalse(wt_path.exists(), "crashed builder sandbox must be destroyed")
        rc, branches = _git(self.repo, "branch", "--list")
        self.assertNotIn("subtask_", branches)

    def test_active_lock_guards_relay_concurrency(self):
        root = subagent_runner.subagents_root()
        root.mkdir(parents=True, exist_ok=True)
        ghost_dir = root / "sub_ghost_1"
        ghost_dir.mkdir(parents=True, exist_ok=True)
        persistence.atomic_write_text(ghost_dir / persistence.STATUS_FILE, "running\n")
        persistence.atomic_write_text(subagent_runner.active_lock_path(), "sub_ghost_1\n")

        sub_llm = ScriptedLLM([{"content": "TASK_DONE: must never run"}])
        result = run_subagent(
            "parent_task_01", "sneak in", role_mode="scout",
            runtime={"subagent_client": sub_llm},
        )
        self.assertFalse(result.get("ok"))
        self.assertIn("SUBAGENT_BUSY", result["error"])
        self.assertEqual(sub_llm.requests, [], "no second SubAgent may start while one is active")


if __name__ == "__main__":
    unittest.main()
