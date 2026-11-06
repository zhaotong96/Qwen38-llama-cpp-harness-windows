"""Phase 12A: event-driven file bus, LRU token cache, and the resident daemon.

Covers the twelve mandated scenarios from TASK_DEV_PHASE12A_EVENT_BUS §四:
envelope serialization, strict priority dequeue, FIFO within a priority,
debounce merge/expiry, processed-archive population, stale purge, LRU
eviction and recency refresh, idle daemon polling, Level-0 emergency abort
injection, and single-flight mutual exclusion.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unittest
from unittest import mock

from core import event_bus
from core import persistence
from core import token_grid
from tests.support import IsolatedCase, build_agent

EVENT_ID_RE = re.compile(r"^[0-9a-f]{8}$")
FILENAME_RE = re.compile(r"^\d{8}_\d{6}_\d{6}_[0-9]_[0-9a-f]{8}\.json$")


def _cache_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class EventBusEnvelopeTests(IsolatedCase):
    def test_event_envelope_serialization_and_disk_structure(self):
        event_id = event_bus.emit_event(
            "OPERATOR_INTERRUPT",
            {"directive": "暂停当前工作", "abort_current": True},
            priority=0,
            source="web_console",
        )
        self.assertRegex(event_id, EVENT_ID_RE)
        inbox = event_bus.event_inbox_dir()
        files = list(inbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, FILENAME_RE)
        # The filename must carry the priority digit and the event id.
        self.assertIn(f"_0_{event_id}.json", files[0].name)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["event_id"], event_id)
        self.assertEqual(data["priority"], 0)
        self.assertEqual(data["event_type"], "OPERATOR_INTERRUPT")
        self.assertEqual(data["source"], "web_console")
        self.assertEqual(data["payload"]["directive"], "暂停当前工作")
        self.assertIsNone(data["debounce_key"])
        self.assertRegex(data["created_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_priority_ordering_strict_dequeue(self):
        for prio, etype in (
            (5, "WIN32_WINDOW_FOCUS"),
            (1, "USER_INBOX_PROMPT"),
            (0, "OPERATOR_INTERRUPT"),
            (3, "SCHEDULED_CRON"),
        ):
            event_bus.emit_event(etype, {"n": prio}, priority=prio)
        order = []
        while True:
            event = event_bus.pop_highest_priority_event()
            if event is None:
                break
            order.append(event["event_type"])
        self.assertEqual(
            order,
            [
                "OPERATOR_INTERRUPT",
                "USER_INBOX_PROMPT",
                "SCHEDULED_CRON",
                "WIN32_WINDOW_FOCUS",
            ],
        )

    def test_fifo_order_for_identical_priority(self):
        for index in range(4):
            event_bus.emit_event("SCHEDULED_CRON", {"seq": index}, priority=3)
            time.sleep(0.002)  # ensure distinct microsecond timestamps
        seqs = []
        while True:
            event = event_bus.pop_highest_priority_event()
            if event is None:
                break
            seqs.append(event["payload"]["seq"])
        self.assertEqual(seqs, [0, 1, 2, 3])

    def test_debounce_deduplication_window(self):
        first = event_bus.emit_event(
            "FS_WATCHDOG_CHANGE",
            {"path": "report.xlsx"},
            priority=4,
            debounce_key="fs_save",
            debounce_window_seconds=60.0,
        )
        second = event_bus.emit_event(
            "FS_WATCHDOG_CHANGE",
            {"path": "report_final.xlsx"},
            priority=4,
            debounce_key="fs_save",
            debounce_window_seconds=60.0,
        )
        self.assertEqual(first, second)
        inbox_files = list(event_bus.event_inbox_dir().glob("*.json"))
        self.assertEqual(len(inbox_files), 1)
        data = json.loads(inbox_files[0].read_text(encoding="utf-8"))
        # The repeat emission merged into the original envelope.
        self.assertEqual(data["payload"]["path"], "report_final.xlsx")
        self.assertEqual(data["merge_count"], 1)

    def test_debounce_expired_produces_new_event(self):
        first = event_bus.emit_event(
            "FS_WATCHDOG_CHANGE",
            {"path": "a.txt"},
            priority=4,
            debounce_key="fs_save",
            debounce_window_seconds=0.0,
        )
        time.sleep(0.01)
        second = event_bus.emit_event(
            "FS_WATCHDOG_CHANGE",
            {"path": "b.txt"},
            priority=4,
            debounce_key="fs_save",
            debounce_window_seconds=0.0,
        )
        self.assertNotEqual(first, second)
        files = list(event_bus.event_inbox_dir().glob("*.json"))
        self.assertEqual(len(files), 2)
        # FIFO: the oldest (first) event leaves the queue first.
        event = event_bus.pop_highest_priority_event()
        self.assertIsNotNone(event)
        self.assertEqual(event["event_id"], first)


class EventBusArchiveTests(IsolatedCase):
    def test_pop_moves_to_processed_archive(self):
        event_id = event_bus.emit_event(
            "USER_INBOX_PROMPT", {"directive": "检查导出目录"}, priority=1
        )
        inbox_dir = event_bus.event_inbox_dir()
        processed_dir = event_bus.event_processed_dir()
        self.assertEqual(len(list(inbox_dir.glob("*.json"))), 1)

        event = event_bus.pop_highest_priority_event()
        self.assertIsNotNone(event)
        self.assertEqual(event["event_id"], event_id)
        self.assertEqual(list(inbox_dir.glob("*.json")), [])
        archived = list(processed_dir.glob("*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(
            json.loads(archived[0].read_text(encoding="utf-8"))["event_id"], event_id
        )
        # The queue is now empty.
        self.assertIsNone(event_bus.pop_highest_priority_event())

    def test_purge_stale_archived_events(self):
        event_bus.emit_event("IDLE_MAINTENANCE", {"n": 1}, priority=9)
        event_bus.pop_highest_priority_event()
        processed_dir = event_bus.event_processed_dir()
        fresh = list(processed_dir.glob("*.json"))
        self.assertEqual(len(fresh), 1)

        # A fresh archive entry survives the default purge window.
        self.assertEqual(event_bus.purge_stale_events(max_age_seconds=86400), 0)
        self.assertEqual(len(list(processed_dir.glob("*.json"))), 1)

        # Aging the file beyond the window makes it eligible for collection.
        stale_cutoff = time.time() - 100.0
        os.utime(fresh[0], (stale_cutoff, stale_cutoff))
        self.assertEqual(event_bus.purge_stale_events(max_age_seconds=50.0), 1)
        self.assertEqual(list(processed_dir.glob("*.json")), [])

    def test_peek_does_not_consume(self):
        event_bus.emit_event("HEALTH_ALERT", {"n": 1}, priority=2)
        seen = event_bus.peek_highest_priority_event()
        self.assertIsNotNone(seen)
        self.assertEqual(seen["event_type"], "HEALTH_ALERT")
        self.assertEqual(len(list(event_bus.event_inbox_dir().glob("*.json"))), 1)
        again = event_bus.peek_highest_priority_event()
        self.assertEqual(again["event_id"], seen["event_id"])


class TokenCacheLruTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        token_grid.clear_tokenize_cache()

    def tearDown(self):
        token_grid.clear_tokenize_cache()
        super().tearDown()

    def test_token_cache_lru_eviction_at_capacity(self):
        total = token_grid.MAX_TOKEN_CACHE_SIZE + 50
        texts = [f"probe-{index:05d}" for index in range(total)]
        for text in texts:
            token_grid.exact_or_estimated_tokens(text)
        self.assertEqual(len(token_grid._TOKEN_CACHE), token_grid.MAX_TOKEN_CACHE_SIZE)
        # Strict FIFO eviction of the oldest keys: the first 50 are gone.
        for evicted in texts[:50]:
            self.assertNotIn(_cache_key(evicted), token_grid._TOKEN_CACHE)
        survivor = texts[-1]
        self.assertIn(_cache_key(survivor), token_grid._TOKEN_CACHE)
        self.assertEqual(
            token_grid._TOKEN_CACHE[_cache_key(survivor)],
            token_grid.estimate_text_tokens(survivor),
        )

    def test_token_cache_move_to_end_on_hit(self):
        # A tight ceiling makes the recency policy directly observable.
        with mock.patch.object(token_grid, "MAX_TOKEN_CACHE_SIZE", 3):
            a, b, c = "alpha-cache-key", "beta-cache-key", "gamma-cache-key"
            for text in (a, b, c):
                token_grid.exact_or_estimated_tokens(text)
            self.assertEqual(
                list(token_grid._TOKEN_CACHE.keys()),
                [_cache_key(a), _cache_key(b), _cache_key(c)],
            )
            # Touch the oldest entry: its recency must be refreshed to the end.
            token_grid.exact_or_estimated_tokens(a)
            self.assertEqual(
                list(token_grid._TOKEN_CACHE.keys()),
                [_cache_key(b), _cache_key(c), _cache_key(a)],
            )
            # Crossing the ceiling evicts the least-recently-used (b), not a.
            token_grid.exact_or_estimated_tokens("delta-cache-key")
            self.assertEqual(
                list(token_grid._TOKEN_CACHE.keys()),
                [_cache_key(c), _cache_key(a), _cache_key("delta-cache-key")],
            )
            self.assertNotIn(_cache_key(b), token_grid._TOKEN_CACHE)


class DaemonLoopTests(IsolatedCase):
    def test_daemon_idle_poll_and_event_ingestion(self):
        agent, llm = build_agent([{"content": "TASK_DONE: event handled"}])
        directive = "打开导出目录并核对报表"
        event_bus.emit_event("USER_INBOX_PROMPT", {"directive": directive}, priority=1)

        agent.run_daemon(poll_interval=0.01, max_events=1)

        self.assertEqual(len(llm.requests), 1)
        user_msg = llm.requests[0]["messages"][-1]
        self.assertIn(directive, user_msg["content"])
        self.assertIn("[EventBus Dispatch]", user_msg["content"])
        # The consumed event is archived for audit, and the task finished clean.
        processed = list(event_bus.event_processed_dir().glob("*.json"))
        self.assertEqual(len(processed), 1)
        task_dirs = list(persistence.TASKS_ROOT.iterdir())
        self.assertEqual(len(task_dirs), 1)
        self.assertEqual(
            (task_dirs[0] / "status").read_text(encoding="utf-8").strip(), "done"
        )
        self.assertFalse(event_bus.flight_slot_held())

    def test_daemon_idle_poll_exits_on_idle_budget(self):
        agent, llm = build_agent([])
        started = time.time()
        agent.run_daemon(poll_interval=0.01, max_idle_seconds=0.05)
        self.assertLess(time.time() - started, 5.0)
        self.assertEqual(llm.requests, [])

    def test_level0_emergency_abort_injection(self):
        agent, _llm = build_agent(
            [
                {"tool_calls": [{"name": "list_dir", "arguments": "{}"}]},
                {"content": "TASK_DONE: should not be reached"},
            ]
        )
        # The emergency envelope lands in the bus between ReAct rounds.
        original_consume = agent._consume_inbox
        consume_calls = {"n": 0}

        def consume_and_inject():
            notes = original_consume()
            consume_calls["n"] += 1
            if consume_calls["n"] >= 2:
                event_bus.emit_event(
                    "OPERATOR_INTERRUPT",
                    {"directive": "暂停当前工作，优先检查导出目录", "abort_current": True},
                    priority=0,
                    source="web_console",
                )
            return notes

        agent._consume_inbox = consume_and_inject
        final = agent.run_task("explore the workspace")
        self.assertTrue(final.startswith("TASK_ABORTED"), final)
        self.assertTrue(agent.last_aborted)
        # At least one ReAct round completed before the interrupt was honored.
        self.assertGreaterEqual(consume_calls["n"], 2)
        # The emergency envelope was consumed into the processed archive.
        processed = list(event_bus.event_processed_dir().glob("*.json"))
        self.assertEqual(len(processed), 1)
        payload = json.loads(processed[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["event_type"], "OPERATOR_INTERRUPT")
        self.assertEqual(payload["priority"], 0)
        # The abort.flag file was consumed by the existing abort-gate machinery
        # (step_state legitimately retains the rejected audit marker).
        task_dir = persistence.TASKS_ROOT / agent.current_task_id
        self.assertFalse((task_dir / "abort.flag").is_file())

    def test_single_flight_mutual_exclusion(self):
        agent, llm = build_agent([{"content": "TASK_DONE: owner task"}])
        # A foreign dispatcher already owns the single-flight slot.
        self.assertTrue(event_bus.acquire_flight_slot("foreign-daemon:9999"))
        try:
            agent.run_daemon(poll_interval=0.01, max_idle_seconds=0.06)
            # No second task may start while the slot is held by another owner.
            self.assertEqual(llm.requests, [])
            self.assertIsNone(event_bus.pop_highest_priority_event())
        finally:
            self.assertTrue(event_bus.release_flight_slot("foreign-daemon:9999"))

        # Once released, the queued event is dispatched exactly once.
        event_bus.emit_event("USER_INBOX_PROMPT", {"directive": "合并后的任务"}, priority=1)
        agent.run_daemon(poll_interval=0.01, max_events=1)
        self.assertEqual(len(llm.requests), 1)
        self.assertFalse(event_bus.flight_slot_held())


if __name__ == "__main__":
    unittest.main()
