"""Tests for the task state files: checkpoint.jsonl and status markers."""
from __future__ import annotations

import unittest

from core import persistence
from core.context import ContextManager
from core import tools as tools_mod
from tests.support import IsolatedCase


class TaskIdTests(unittest.TestCase):
    def test_format_and_uniqueness(self):
        first = persistence.generate_task_id()
        second = persistence.generate_task_id()
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^\d{8}_\d{6}_\d{6}$")


class CheckpointTests(IsolatedCase):
    def test_roundtrip_with_metadata(self):
        messages = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "ok"}]
        path = persistence.save_checkpoint("t1", messages, metadata={"note": "m"})
        self.assertTrue(path.is_file())
        loaded, meta = persistence.load_checkpoint("t1")
        self.assertEqual(loaded, messages)
        self.assertEqual(meta, {"note": "m"})

    def test_missing_checkpoint_returns_none(self):
        self.assertIsNone(persistence.load_checkpoint("ghost"))

    def test_invalid_task_id_rejected(self):
        for bad in ("..", "a/b", "a\\b", "", "x y"):
            with self.assertRaises(ValueError):
                persistence.load_checkpoint(bad)
            with self.assertRaises(ValueError):
                persistence.save_status(bad, "running")


class StatusTests(IsolatedCase):
    def test_status_roundtrip(self):
        persistence.save_status("t2", "running")
        self.assertEqual(persistence.load_status("t2"), "running")
        persistence.save_status("t2", "done")
        self.assertEqual(persistence.load_status("t2"), "done")

    def test_missing_status_returns_none(self):
        self.assertIsNone(persistence.load_status("ghost"))


class ListTasksTests(IsolatedCase):
    def test_status_legacy_and_sorting(self):
        persistence.save_checkpoint("20260101_000000_000000", [{"role": "user", "content": "a"}])
        persistence.save_status("20260101_000000_000000", "done")
        persistence.save_checkpoint("20260102_000000_000000", [{"role": "user", "content": "b"}])
        (self.tasks_root / "not_a_task").mkdir(parents=True)  # skipped: no checkpoint

        tasks = persistence.list_tasks()
        self.assertEqual([t["id"] for t in tasks], ["20260102_000000_000000", "20260101_000000_000000"])
        by_id = {t["id"]: t for t in tasks}
        self.assertEqual(by_id["20260101_000000_000000"]["status"], "done")
        self.assertEqual(by_id["20260102_000000_000000"]["status"], "unknown")
        self.assertEqual(by_id["20260101_000000_000000"]["message_count"], 1)

    def test_empty_root(self):
        self.assertEqual(persistence.list_tasks(), [])


class CompactionBoundaryTests(IsolatedCase):
    """Fold split re-anchors on a user node; archived nodes stay addressable."""

    def _seed_task(self, task_id: str) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {
                "role": "assistant",
                "content": "a1",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "mouse", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "t1"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u2"},
        ]
        persistence.save_checkpoint(task_id, messages)

    def test_recent_window_starts_on_user_and_indexes_archived_nodes(self):
        task_id = "compact_boundary"
        self._seed_task(task_id)
        nodes_before, _ = persistence.load_dag_nodes(task_id)
        id_of_a1 = next(n["id"] for n in nodes_before if n.get("content") == "a1")

        result = persistence.compact_branch_dag(task_id, "摘要文本", keep_recent=2)
        self.assertIsNotNone(result)
        self.assertIn("summary_content", result)
        self.assertIn(id_of_a1, result["summary_content"])

        rebuilt = persistence.build_branch_context(task_id)
        self.assertEqual(rebuilt[0]["role"], "system")
        self.assertIn("[Context Summary]", rebuilt[1]["content"])
        self.assertIn("6. 已归档原始 DAG 节点索引表", rebuilt[1]["content"])
        self.assertIn(id_of_a1, rebuilt[1]["content"])
        self.assertEqual(rebuilt[2]["role"], "user")
        self.assertEqual(rebuilt[2]["content"], "u2")

    def test_read_checkpoint_node_skill_recovers_folded_node(self):
        task_id = "compact_recall"
        self._seed_task(task_id)
        nodes_before, _ = persistence.load_dag_nodes(task_id)
        id_of_a1 = next(n["id"] for n in nodes_before if n.get("content") == "a1")

        persistence.compact_branch_dag(task_id, "折叠摘要", keep_recent=2)

        skill = next(
            s for s in tools_mod.discover_skills() if s["name"] == "read_checkpoint_node"
        )
        out = skill["module"].run({"node_id": id_of_a1}, {"parent_task_id": task_id})
        self.assertIn("a1", out)
        self.assertNotIn("parent_id", out)
        self.assertNotIn("branch", out)

    def test_read_checkpoint_node_skill_handles_missing_node(self):
        skill = next(
            s for s in tools_mod.discover_skills() if s["name"] == "read_checkpoint_node"
        )
        self._seed_task("compact_missing")
        out = skill["module"].run({"node_id": "n_404"}, {"parent_task_id": "compact_missing"})
        self.assertTrue(out.startswith("error:"))
        self.assertIn("not found", out)

    def test_index_table_is_bounded_and_collapses_prior_epochs(self):
        task_id = "compact_epochs"
        ctx = ContextManager("sys")
        ctx.bind_task(task_id)
        for i in range(80):
            ctx.append({"role": "user", "content": f"turn {i} " + ("x" * 80)})
            ctx.append({"role": "assistant", "content": f"reply {i}"})

        results = []
        for _ in range(3):
            result = persistence.compact_branch_dag(task_id, "摘要文本", keep_recent=2)
            self.assertIsNotNone(result)
            results.append(result)
            branch_msgs = persistence.build_branch_context(task_id)
            summary = branch_msgs[1]["content"]
            index_block = summary.split("6. 已归档原始 DAG 节点索引表", 1)[1]
            self.assertLessEqual(len(index_block.strip().splitlines()), 30)
            ctx.reload_from_branch("main")
            for i in range(80):
                ctx.append({"role": "user", "content": f"more {i} " + ("y" * 80)})
                ctx.append({"role": "assistant", "content": f"reply more {i}"})

        final_summary = persistence.build_branch_context(task_id)[1]["content"]
        self.assertIn("compaction-epoch", final_summary)
        self.assertIn(results[-2]["compaction_node_id"], final_summary)


if __name__ == "__main__":
    unittest.main()