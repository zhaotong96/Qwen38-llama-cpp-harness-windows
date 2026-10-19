"""mtime-backed task summary cache and Phase 7 P2 dashboard projections."""
from __future__ import annotations

import time
import unittest
from unittest import mock

from core import persistence
from tests.support import IsolatedCase


class TaskSummaryCacheTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        persistence.clear_task_summary_cache()
        self.addCleanup(persistence.clear_task_summary_cache)

    def test_repeated_listing_reuses_cache(self):
        persistence.save_checkpoint("cache_task", [{"role": "user", "content": "a"}])
        with mock.patch.object(persistence, "load_checkpoint", wraps=persistence.load_checkpoint) as spy:
            first = persistence.list_tasks()
            second = persistence.list_tasks()
        self.assertEqual([t["id"] for t in first], [t["id"] for t in second])
        self.assertEqual(spy.call_count, 1)

    def test_checkpoint_change_invalidates_cache(self):
        persistence.save_checkpoint("cache_task", [{"role": "user", "content": "a"}])
        first = persistence.list_tasks()
        self.assertEqual(first[0]["message_count"], 1)

        time.sleep(0.01)
        persistence.append_dag_node("cache_task", {"role": "assistant", "content": "b"})
        second = persistence.list_tasks()
        self.assertEqual(second[0]["message_count"], 2)

    def test_status_change_invalidates_cache(self):
        persistence.save_checkpoint("cache_task", [{"role": "user", "content": "a"}])
        self.assertEqual(persistence.list_tasks()[0]["status"], "unknown")
        time.sleep(0.01)
        persistence.save_status("cache_task", "done")
        self.assertEqual(persistence.list_tasks()[0]["status"], "done")

    def test_removed_task_drops_from_cache(self):
        persistence.save_checkpoint("cache_task", [{"role": "user", "content": "a"}])
        self.assertEqual(len(persistence.list_tasks()), 1)
        import shutil

        shutil.rmtree(self.tasks_root / "cache_task")
        self.assertEqual(persistence.list_tasks(), [])


class LedgerProjectionContractTests(IsolatedCase):
    """The web ledger reads section headings emitted by compact_branch_dag."""

    def test_compaction_summary_keeps_five_sections_and_bounded_sixth(self):
        task_id = "ledger_contract"
        persistence.save_checkpoint(
            task_id,
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "goal: ship phase 7"},
                {"role": "assistant", "content": "working"},
                {"role": "user", "content": "more"},
                {"role": "assistant", "content": "done"},
            ],
        )
        ledger = (
            "1. 核心目标与进度\nship phase 7\n\n"
            "2. 已验证事实与环境基线\nn_ctx=32768\n\n"
            "3. 已修改文件清单\ncore/tools.py\n\n"
            "4. 被否决方案与操作员红线（永久保留）\n- [Operator correction] keep cache-reuse=0\n\n"
            "5. 待完成子目标与下一步行动\nrun tests"
        )
        result = persistence.compact_branch_dag(task_id, ledger, keep_recent=2)
        content = result["summary_content"]
        for index in range(1, 6):
            self.assertIn(f"{index}. ", content)
        self.assertIn("6. 已归档原始 DAG 节点索引表", content)
        sixth = content.split("6. 已归档原始 DAG 节点索引表", 1)[1]
        self.assertLessEqual(len(sixth.strip().splitlines()), 30)
        self.assertIn("[Operator correction] keep cache-reuse=0", content)


if __name__ == "__main__":
    unittest.main()
