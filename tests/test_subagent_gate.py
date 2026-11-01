"""Phase 11C: SubAgent Git-worktree isolation, artifact cards, and hazard gates.

Covers the seven mandated scenarios from TASK_DEV_PHASE11C_SUBAGENT_WORKTREE
§三.4 plus host-authoritative card generation and the runner integration.
All Git fixtures live in throwaway temp repositories.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import agent as agent_mod
from core import subagent_gate
from core import subagent_runner
from core import tools as tools_mod
from skills import subagent_review
from tests.support import IsolatedCase, ScriptedLLM


def _git(repo: Path, *args: str) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def init_repo(base: Path) -> Path:
    """Build a throwaway git repo mirroring the production layout."""
    repo = base / "repo"
    (repo / "core").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / ".gitignore").write_text(".agent/\n__pycache__/\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "core" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertEqual(1, 1)\n",
        encoding="utf-8",
    )
    assert _git(repo, "init", "-b", "main")[0] == 0
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "Gate Tester")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-m", "init")[0] == 0
    return repo


class WorktreeGateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="subgate_test_")
        self.base = Path(self._tmp.name)
        self.repo = init_repo(self.base)
        self.addCleanup(self._tmp.cleanup)

    def worktree(self, sub_id: str = "sub_20261031_120000_000001") -> Path:
        return subagent_gate.create_subagent_worktree(sub_id, repo_root=self.repo)

    def main_status(self) -> str:
        rc, out = _git(self.repo, "status", "--porcelain")
        assert rc == 0, out
        return out


class WorktreeCreationAndIsolationTests(WorktreeGateCase):
    def test_worktree_creation_and_physical_isolation(self):
        wt = self.worktree()
        self.assertTrue(wt.is_dir())
        branch_out = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()
        self.assertEqual(branch_out, "main")

        # The SubAgent writes only inside the worktree.
        (wt / "core" / "exploration.md").write_text("deep dive notes\n", encoding="utf-8")
        (wt / "core" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

        self.assertEqual(self.main_status(), "", "main workspace must stay pristine")
        self.assertFalse((self.repo / "core" / "exploration.md").exists())
        self.assertEqual((self.repo / "core" / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n")
        self.assertEqual((wt / "core" / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n")

    def test_stale_worktree_is_recreated_cleanly(self):
        sub_id = "sub_20261031_120000_000009"
        first = subagent_gate.create_subagent_worktree(sub_id, repo_root=self.repo)
        (first / "junk.txt").write_text("stale\n", encoding="utf-8")
        second = subagent_gate.create_subagent_worktree(sub_id, repo_root=self.repo)
        self.assertEqual(first, second)
        self.assertFalse((second / "junk.txt").exists())

    def test_invalid_subagent_id_rejected(self):
        with self.assertRaises(ValueError):
            subagent_gate.create_subagent_worktree("../escape", repo_root=self.repo)


class HazardDetectionTests(unittest.TestCase):
    def test_hazard_test_assertion_removed(self):
        diff = "\n".join([
            "diff --git a/tests/test_app.py b/tests/test_app.py",
            "--- a/tests/test_app.py",
            "+++ b/tests/test_app.py",
            "@@ -1,5 +1,3 @@",
            " import unittest",
            "-    def test_old_case(self):",
            "-        self.assertEqual(1, 1)",
            "     def test_kept(self):",
        ])
        flags = subagent_gate.detect_diff_hazards(
            [{"path": "tests/test_app.py", "status": "M"}], diff, []
        )
        self.assertIn(subagent_gate.HAZARD_TEST_ASSERTION_REMOVED, flags)

    def test_hazard_test_function_deleted_without_assert_keyword(self):
        diff = "\n".join([
            "diff --git a/tests/test_logic.py b/tests/test_logic.py",
            "--- a/tests/test_logic.py",
            "+++ b/tests/test_logic.py",
            "@@ -1,4 +1,3 @@",
            "-    def test_complex_feature(self):",
            "     def test_other(self):",
        ])
        flags = subagent_gate.detect_diff_hazards(
            [{"path": "tests/test_logic.py", "status": "M"}], diff, []
        )
        self.assertIn(subagent_gate.HAZARD_TEST_ASSERTION_REMOVED, flags)

    def test_hazard_test_skip_injected(self):
        skip_decorator = "\n".join([
            "diff --git a/tests/test_app.py b/tests/test_app.py",
            "--- a/tests/test_app.py",
            "+++ b/tests/test_app.py",
            "@@ -1,4 +1,6 @@",
            " import unittest",
            "+@unittest.skip('flaky on this host')",
            " def test_ok(self):",
            "     self.assertEqual(1, 1)",
        ])
        self.assertIn(
            subagent_gate.HAZARD_TEST_SKIP_INJECTED,
            subagent_gate.detect_diff_hazards([], skip_decorator, []),
        )
        pass_stub = "\n".join([
            "diff --git a/tests/test_more.py b/tests/test_more.py",
            "--- /dev/null",
            "+++ b/tests/test_more.py",
            "@@ -0,0 +1,3 @@",
            "+def test_stubbed(self):",
            "+    pass",
        ])
        self.assertIn(
            subagent_gate.HAZARD_TEST_SKIP_INJECTED,
            subagent_gate.detect_diff_hazards([], pass_stub, []),
        )

    def test_hazard_unverified_code_edit(self):
        files = [{"path": "core/app.py", "status": "M"}]
        flags = subagent_gate.detect_diff_hazards(files, "diff --git a/core/app.py b/core/app.py", [])
        self.assertIn(subagent_gate.HAZARD_UNVERIFIED_CODE_EDIT, flags)
        # A passing test executed AFTER the write step clears the flag.
        flags_ok = subagent_gate.detect_diff_hazards(
            files, "diff", [{"step": 3, "command": "python -m unittest", "exit_code": 0}],
            last_write_step=2,
        )
        self.assertNotIn(subagent_gate.HAZARD_UNVERIFIED_CODE_EDIT, flags_ok)
        # A passing test BEFORE the write step does not clear it.
        flags_stale = subagent_gate.detect_diff_hazards(
            files, "diff", [{"step": 1, "command": "python -m unittest", "exit_code": 0}],
            last_write_step=2,
        )
        self.assertIn(subagent_gate.HAZARD_UNVERIFIED_CODE_EDIT, flags_stale)

    def test_hazard_redline_file_modified(self):
        for path in ("requirements.txt", "docs/SPEC.md", "package.json", "skills/shell.py"):
            flags = subagent_gate.detect_diff_hazards([{"path": path, "status": "M"}], "diff", [])
            self.assertIn(subagent_gate.HAZARD_REDLINE_FILE_MODIFIED, flags, path)
        # A brand-new dynamic skill is not a redline violation.
        flags_new = subagent_gate.detect_diff_hazards(
            [{"path": "skills/macro_new_thing.py", "status": "A"}], "diff", []
        )
        self.assertNotIn(subagent_gate.HAZARD_REDLINE_FILE_MODIFIED, flags_new)

    def test_hazard_empty_or_noop_diff(self):
        flags = subagent_gate.detect_diff_hazards([], "", [])
        self.assertIn(subagent_gate.HAZARD_EMPTY_OR_NOOP_DIFF, flags)
        self.assertEqual(
            subagent_gate.detect_diff_hazards([{"path": "x.txt", "status": "A"}], "diff", []),
            [],
        )


class ArtifactCardTests(WorktreeGateCase):
    def test_artifact_card_is_host_authoritative(self):
        wt = self.worktree()
        (wt / "core" / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
        (wt / "NOTES.md").write_text("new file\n", encoding="utf-8")

        with mock.patch.object(
            subagent_gate.persistence, "TASKS_ROOT", self.base / "state" / "tasks"
        ):
            card = subagent_gate.generate_artifact_card(
                "sub_20261031_120000_000002",
                wt,
                tests_executed=[{"step": 5, "command": "python -m unittest tests", "exit_code": 0}],
                summary="bumped VALUE",
                last_write_step=4,
            )
        paths = {entry["path"]: entry for entry in card["files_modified"]}
        self.assertIn("core/app.py", paths)
        self.assertIn("NOTES.md", paths)
        self.assertNotIn(subagent_gate.HAZARD_UNVERIFIED_CODE_EDIT, card["hazard_flags"])
        self.assertNotIn(subagent_gate.HAZARD_EMPTY_OR_NOOP_DIFF, card["hazard_flags"])
        import hashlib
        # Windows text-mode writes translate LF to CRLF; hash the real bytes.
        self.assertEqual(
            paths["core/app.py"]["sha256"],
            hashlib.sha256((wt / "core" / "app.py").read_bytes()).hexdigest(),
        )
        self.assertIn("VALUE = 42", card["git_diff_patch"])

        card_file = self.base / "state" / "subagents" / card["subagent_id"] / "artifact_card.json"
        self.assertTrue(card_file.is_file())
        self.assertEqual(json.loads(card_file.read_text(encoding="utf-8"))["branch"], card["branch"])

    def test_review_prompt_orders_telemetry_above_self_report(self):
        with mock.patch.object(
            subagent_gate.persistence, "TASKS_ROOT", self.base / "state" / "tasks"
        ):
            card = subagent_gate.generate_artifact_card(
                "sub_20261031_120000_000003",
                self.worktree(),
                tests_executed=[],
                summary="everything is fine, trust me",
            )
        prompt = subagent_gate.format_adversarial_review_prompt(card)
        self.assertLess(
            prompt.index("HAZARD_FLAGS"),
            prompt.index("UNTRUSTED"),
            "objective telemetry must precede the self-report",
        )
        self.assertIn("Q1 Authenticity", prompt)
        self.assertIn("Q2 Verification", prompt)
        self.assertIn("subagent_review(subagent_id=...", prompt)


class DecisionMergeTests(WorktreeGateCase):
    def test_decision_merge_fast_forward(self):
        sub_id = "sub_20261031_120000_000004"
        wt = self.worktree(sub_id)
        (wt / "core" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")

        result = subagent_gate.finalize_subagent_worktree(
            sub_id, "merge", "diff verified, tests exit=0", repo_root=self.repo
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(
            (self.repo / "core" / "app.py").read_text(encoding="utf-8"),
            "VALUE = 99\n",
            "fast-forward merge must land the SubAgent delivery in the main workspace",
        )
        self.assertFalse(wt.exists(), "worktree must be cleaned up after merge")
        self.assertNotIn("subtask_sub_20261031_120000_000004", _git(self.repo, "branch")[1])
        self.assertEqual(self.main_status(), "")

    def test_merge_requires_reason(self):
        result = subagent_gate.finalize_subagent_worktree(
            "sub_x", "merge", "", repo_root=self.repo
        )
        self.assertFalse(result.get("ok"))


class DecisionRevertTests(WorktreeGateCase):
    def test_decision_revert_and_ledger_injection(self):
        sub_id = "sub_20261031_120000_000005"
        wt = self.worktree(sub_id)
        (wt / "core" / "app.py").write_text("VALUE = -1\n", encoding="utf-8")

        result = subagent_gate.finalize_subagent_worktree(
            sub_id,
            "revert",
            "no test evidence for the core edit",
            repo_root=self.repo,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertIn("[Branch Rewind Lesson]", result.get("lesson", ""))
        self.assertIn("no test evidence", result["lesson"])
        # The main workspace never saw the poisoned edit.
        self.assertEqual((self.repo / "core" / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n")
        self.assertFalse(wt.exists())
        self.assertNotIn("subtask_sub_20261031_120000_000005", _git(self.repo, "branch")[1])

    def test_revert_skill_receipt_pins_lesson_for_ledger(self):
        raw = subagent_review.run(
            {
                "subagent_id": "sub_20261031_120000_000006",
                "decision": "revert",
                "reason": "hazard flags fired",
            }
        )
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertIn("[Branch Rewind Lesson]", str(payload["receipt"]))
        # The lesson is auto-pinned into ledger section 4 by the redline pipeline.
        from core.agent import Agent

        pinned = Agent._extract_pinned_redlines(
            [{"role": "tool", "content": str(payload["receipt"])}]
        )
        self.assertTrue(
            any("SubAgent 'sub_20261031_120000_000006' reverted" in line for line in pinned),
            pinned,
        )

    def test_review_skill_validates_input(self):
        self.assertTrue(
            subagent_review.run({"subagent_id": "x", "decision": "nuke", "reason": "y"}).startswith(
                "error:"
            )
        )
        self.assertTrue(
            subagent_review.run({"subagent_id": "x", "decision": "merge", "reason": ""}).startswith(
                "error:"
            )
        )


class RunnerIntegrationTests(WorktreeGateCase):
    def test_run_subagent_delivers_worktree_artifact_card(self):
        sub_llm = ScriptedLLM([
            {
                "tool_calls": [
                    {
                        "id": "w1",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "notes.txt", "content": "hello from sandbox\n"}),
                    }
                ]
            },
            {"content": "TASK_DONE: wrote the notes file"},
        ])

        with mock.patch.dict(
            os.environ,
            {
                "SUBAGENT_WORKTREE": "1",
                "SUBAGENT_WORKTREE_ROOT": str(self.repo),
            },
        ), mock.patch.object(
            agent_mod, "OpenAI", lambda *args, **kwargs: sub_llm
        ), mock.patch.object(
            tools_mod, "ROOT", self.repo
        ), mock.patch.object(
            subagent_runner.persistence, "TASKS_ROOT", self.base / "agent_state" / "tasks"
        ):
            raw = subagent_runner.run_subagent(
                {"objective": "write the notes file", "allowed_skills": ["write_file"]},
                runtime={"subagent_client": sub_llm},
            )

        parsed = json.loads(raw)
        self.assertEqual(parsed["status"], "completed")
        self.assertIn("worktree_path", parsed)
        sub_id = parsed["subagent_id"]
        wt = self.repo / ".agent" / "worktrees" / sub_id
        self.assertTrue(wt.is_dir(), "worktree survives until the parent review")
        self.assertTrue((wt / "notes.txt").is_file())
        # The main workspace is physically untouched by the SubAgent write.
        self.assertEqual(self.main_status(), "")
        # The card and the adversarial prompt travel with the report.
        self.assertEqual(
            parsed["hazard_flags"], [],
            "a fresh non-Python text file must not raise any hazard flag",
        )
        self.assertIn("UNTRUSTED", parsed["review_prompt"])
        card_file = (
            self.base / "agent_state" / "subagents" / sub_id / "artifact_card.json"
        )
        self.assertTrue(card_file.is_file())
        # Cleanup: destroy the sandbox to leave the fixture repo pristine.
        subagent_gate.finalize_subagent_worktree(
            sub_id, "revert", "integration test cleanup", repo_root=self.repo
        )

    def test_excluded_meta_skills_block_subagent_review(self):
        self.assertIn("subagent_review", subagent_runner.EXCLUDED_META_SKILLS)
        self.assertIn("crystallize_macro", subagent_runner.EXCLUDED_META_SKILLS)


if __name__ == "__main__":
    unittest.main()
