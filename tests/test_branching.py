"""Tests for Git-style file-backed DAG context branching, checkout, and non-destructive compaction (GATE-FUNC-01 & GATE-ARCH-01)."""
from __future__ import annotations

import json
import unittest

from core import persistence
from core.context import ContextManager
from tests.support import IsolatedCase
from tests.test_api import ApiCase, call


class GitBranchingUnitTests(IsolatedCase):
    def test_legacy_linear_checkpoint_auto_upgrades_to_dag(self):
        task_id = "legacy_task_01"
        task_dir = self.tasks_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        raw_lines = [
            json.dumps({"role": "system", "content": "sys"}),
            json.dumps({"role": "user", "content": "hello"}),
            json.dumps({"role": "assistant", "content": "world"}),
        ]
        (task_dir / "checkpoint.jsonl").write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

        nodes, _ = persistence.load_dag_nodes(task_id)
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0]["id"], "legacy_000001")
        self.assertIsNone(nodes[0]["parent_id"])
        self.assertEqual(nodes[1]["id"], "legacy_000002")
        self.assertEqual(nodes[1]["parent_id"], "legacy_000001")
        self.assertEqual(nodes[2]["id"], "legacy_000003")
        self.assertEqual(nodes[2]["parent_id"], "legacy_000002")
        self.assertEqual(persistence.get_current_branch(task_id), "main")
        self.assertEqual(persistence.get_branch_head(task_id, "main"), "legacy_000003")

    def test_fork_branch_from_any_commit_and_checkout_isolation(self):
        task_id = "branch_task_01"
        ctx = ContextManager("sys prompt")
        ctx.bind_task(task_id)
        ctx.append({"role": "user", "content": "step 1"})
        n_asst1 = ctx.append({"role": "assistant", "content": "reply 1"})
        ctx.append({"role": "user", "content": "step 2 on main"})
        ctx.append({"role": "assistant", "content": "reply 2 on main"})

        fork_res = persistence.create_branch(
            task_id,
            branch_name="exp_alt",
            from_node_id=n_asst1["id"],
            checkout=True,
        )
        self.assertEqual(fork_res["branch"], "exp_alt")
        self.assertEqual(fork_res["head_node_id"], n_asst1["id"])
        self.assertEqual(persistence.get_current_branch(task_id), "exp_alt")

        ctx.reload_from_branch("exp_alt")
        self.assertEqual(len(ctx.messages), 3)
        ctx.append({"role": "user", "content": "step 2 on exp_alt"})
        ctx.append({"role": "assistant", "content": "reply 2 on exp_alt"})

        exp_msgs = persistence.build_branch_context(task_id, branch="exp_alt")
        self.assertEqual([m["content"] for m in exp_msgs], [
            "sys prompt",
            "step 1",
            "reply 1",
            "step 2 on exp_alt",
            "reply 2 on exp_alt",
        ])

        main_msgs = persistence.build_branch_context(task_id, branch="main")
        self.assertEqual([m["content"] for m in main_msgs], [
            "sys prompt",
            "step 1",
            "reply 1",
            "step 2 on main",
            "reply 2 on main",
        ])

        persistence.checkout_branch(task_id, "main")
        ctx.reload_from_branch("main")
        self.assertEqual([m["content"] for m in ctx.messages], [
            "sys prompt",
            "step 1",
            "reply 1",
            "step 2 on main",
            "reply 2 on main",
        ])

    def test_tool_call_pairing_integrity_on_fork(self):
        task_id = "pairing_task"
        ctx = ContextManager("sys")
        ctx.bind_task(task_id)
        ctx.append({"role": "user", "content": "run tool"})
        n_tc = ctx.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_abc", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
            ],
        })
        n_tool = ctx.append({"role": "tool", "tool_call_id": "call_abc", "content": "file1.txt"})

        res_auto = persistence.create_branch(task_id, "safe_fork", from_node_id=n_tc["id"], checkout=False)
        self.assertEqual(res_auto["head_node_id"], n_tool["id"])

        res_forced = persistence.create_branch(
            task_id,
            "forced_fork",
            from_node_id=n_tc["id"],
            checkout=True,
            force_at_assistant=True,
        )
        forced_msgs = persistence.build_branch_context(task_id, branch="forced_fork")
        self.assertEqual(forced_msgs[-1]["role"], "tool")
        self.assertEqual(forced_msgs[-1]["tool_call_id"], "call_abc")
        self.assertIn("BRANCH_FORKED_BEFORE_TOOL_EXECUTION", forced_msgs[-1]["content"])
        self.assertNotEqual(res_forced["head_node_id"], n_tool["id"])

    def test_non_destructive_branch_compaction_preserves_other_branches(self):
        task_id = "compact_task"
        ctx = ContextManager("sys")
        ctx.bind_task(task_id)
        for i in range(6):
            ctx.append({"role": "user", "content": f"turn {i} " + ("x" * 120)})

        persistence.create_branch(task_id, "uncompacted_branch", checkout=False)

        compacted = ctx.compact(
            lambda msgs: f"folded {len(msgs)} turns",
            token_limit=50,
            keep_recent=2,
        )
        self.assertTrue(compacted)

        main_msgs = persistence.build_branch_context(task_id, branch="main")
        self.assertEqual(len(main_msgs), 4)
        self.assertIn("[Context Summary]", main_msgs[1]["content"])
        self.assertIn("folded 4 turns", main_msgs[1]["content"])

        other_msgs = persistence.build_branch_context(task_id, branch="uncompacted_branch")
        self.assertEqual(len(other_msgs), 7)
        self.assertEqual(other_msgs[1]["content"], "turn 0 " + ("x" * 120))


class BranchingApiTests(ApiCase):
    def test_tree_and_branch_endpoints_match_disk_files(self):
        code, submitted = call(self.base, "POST", "/tasks", {"task": "initial task"})
        self.assertEqual(code, 202)
        task_id = submitted["task_id"]
        self.wait_idle()

        code, tree = call(self.base, "GET", f"/tasks/{task_id}/tree")
        self.assertEqual(code, 200)
        self.assertEqual(tree["current_branch"], "main")
        self.assertGreaterEqual(len(tree["nodes"]), 3)

        disk_nodes, _ = persistence.load_dag_nodes(task_id)
        self.assertEqual([n["id"] for n in tree["nodes"]], [n["id"] for n in disk_nodes])

        first_user_node = next(n for n in tree["nodes"] if n["role"] == "user")
        code, forked = call(
            self.base,
            "POST",
            f"/tasks/{task_id}/branches",
            {"name": "alt_branch", "from_node_id": first_user_node["id"], "checkout": True},
        )
        self.assertEqual(code, 201)
        self.assertEqual(forked["current_branch"], "alt_branch")
        self.assertEqual(persistence.get_current_branch(task_id), "alt_branch")

        code, tagged = call(
            self.base,
            "POST",
            f"/tasks/{task_id}/tags",
            {"name": "v1_milestone", "node_id": first_user_node["id"]},
        )
        self.assertEqual(code, 201)
        self.assertEqual(persistence.list_tags(task_id)["v1_milestone"], first_user_node["id"])


if __name__ == "__main__":
    unittest.main()
