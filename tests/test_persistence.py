"""Tests for the task state files: checkpoint.jsonl and status markers."""
from __future__ import annotations

import unittest

from core import persistence
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


if __name__ == "__main__":
    unittest.main()