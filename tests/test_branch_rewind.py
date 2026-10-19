"""Phase 9A: DAG rewind guards, ledger, and Section 4 lesson injection."""
from __future__ import annotations

import json
import unittest

from core import persistence
from core.context import ContextManager
from skills import branch_rewind
from tests.support import IsolatedCase


class BranchRewindCase(IsolatedCase):
    def build_history(self, task_id: str, assistants_after_target: int = 3):
        ctx = ContextManager("system")
        ctx.bind_task(task_id)
        ctx.append({"role": "user", "content": "start"})
        target = ctx.append({"role": "assistant", "content": "root plan"})
        for index in range(assistants_after_target):
            ctx.append({"role": "assistant", "content": f"failed step {index}"})
        return ctx, target["id"]

    def rewind(self, task_id: str, target_id: str, branch: str = "safe_alt", **overrides):
        payload = {
            "target_node_id": target_id,
            "new_branch_name": branch,
            "failed_lesson": "The parser approach cannot handle nested arrays.",
            "new_plan": "Replace the recursive descent parser with a token stream.",
            **overrides,
        }
        return branch_rewind.run(payload, {"task_id": task_id})

    def ledger(self, task_id: str) -> list[dict]:
        path = self.tasks_root / task_id / branch_rewind.REWIND_LEDGER_FILE
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class RewindGuardTests(BranchRewindCase):
    def test_rewind_succeeds_and_injects_lesson(self):
        task_id = "rewind_ok"
        _, target_id = self.build_history(task_id)

        result = json.loads(self.rewind(task_id, target_id))

        self.assertTrue(result["ok"])
        self.assertEqual(result["switched_to_branch"], "safe_alt")
        self.assertEqual(result["rewinds_remaining"], 3)
        self.assertEqual(persistence.get_current_branch(task_id), "safe_alt")
        chain = persistence.build_branch_nodes(task_id, "safe_alt")
        self.assertIn("[Branch Rewind Lesson]", chain[-1]["content"])
        self.assertIn("Replace the recursive descent parser", chain[-1]["content"])
        self.assertEqual(len(self.ledger(task_id)), 1)

    def test_cooldown_blocks_immediate_repeat(self):
        task_id = "rewind_cooldown"
        ctx, target_id = self.build_history(task_id, assistants_after_target=2)

        result = self.rewind(task_id, target_id)
        self.assertIn("ANTI_OSCILLATION_COOLDOWN", result)

        ctx.append({"role": "assistant", "content": "one more diagnostic step"})
        result = json.loads(self.rewind(task_id, target_id))
        self.assertTrue(result["ok"])

    def test_total_quota_blocks_fifth_rewind(self):
        task_id = "rewind_quota"
        _, target_id = self.build_history(task_id)
        task_dir = self.tasks_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        with (task_dir / branch_rewind.REWIND_LEDGER_FILE).open("w", encoding="utf-8") as handle:
            for index in range(4):
                handle.write(json.dumps({"new_plan": f"unique plan {index}"}) + "\n")

        result = self.rewind(task_id, target_id)
        self.assertIn("ANTI_OSCILLATION_QUOTA_EXCEEDED", result)

    def test_node_fork_cap_blocks_third_fork(self):
        task_id = "rewind_node_cap"
        _, target_id = self.build_history(task_id)
        task_dir = self.tasks_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        with (task_dir / branch_rewind.REWIND_LEDGER_FILE).open("w", encoding="utf-8") as handle:
            for index in range(2):
                handle.write(
                    json.dumps(
                        {"target_node_id": target_id, "new_plan": f"prior plan {index}"}
                    )
                    + "\n"
                )

        result = self.rewind(task_id, target_id)
        self.assertIn("ANTI_OSCILLATION_NODE_CAP", result)

    def test_similar_plan_is_rejected(self):
        task_id = "rewind_similar"
        _, target_id = self.build_history(task_id)
        task_dir = self.tasks_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        prior_plan = "Replace recursive descent parser with a token stream"
        with (task_dir / branch_rewind.REWIND_LEDGER_FILE).open("w", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"new_plan": prior_plan, "new_branch": "prior_alt"}) + "\n"
            )

        result = self.rewind(
            task_id,
            target_id,
            new_plan="Replace recursive descent parser with a token stream now",
        )
        self.assertIn("ANTI_OSCILLATION_DUPLICATE_PLAN", result)

    def test_unknown_target_is_rejected(self):
        task_id = "rewind_bad_target"
        self.build_history(task_id)
        result = self.rewind(task_id, "n_999999_deadbeef")
        self.assertIn("not an ancestor", result)

    def test_existing_branch_name_is_rejected(self):
        task_id = "rewind_existing"
        _, target_id = self.build_history(task_id)
        persistence.create_branch(task_id, "taken_branch", checkout=False)
        result = self.rewind(task_id, target_id, branch="taken_branch")
        self.assertIn("already exists", result)


class RewindDirtyWorkspaceTests(BranchRewindCase):
    def test_abandoned_write_paths_are_reported(self):
        task_id = "rewind_dirty"
        ctx = ContextManager("system")
        ctx.bind_task(task_id)
        ctx.append({"role": "user", "content": "start"})
        target = ctx.append({"role": "assistant", "content": "root plan"})
        ctx.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "core/parser.py", "content": "broken"}
                            ),
                        },
                    }
                ],
            }
        )
        ctx.append({"role": "assistant", "content": "step two"})
        ctx.append({"role": "assistant", "content": "step three"})

        result = json.loads(self.rewind(task_id, target["id"]))
        self.assertEqual(result["dirty_workspace_files"], ["core/parser.py"])
        chain = persistence.build_branch_nodes(task_id, "safe_alt")
        self.assertIn("core/parser.py", chain[-1]["content"])


if __name__ == "__main__":
    unittest.main()
