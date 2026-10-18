"""Tests for the dual-control unified file-backed message queue (GATE-FUNC-02)."""
from __future__ import annotations

import json
import unittest

from core import queue_manager
from tests.support import IsolatedCase, build_agent
from tests.test_api import ApiCase, call


class QueueManagerUnitTests(IsolatedCase):
    def test_enqueue_hold_modify_reprioritize_and_cancel(self):
        q1 = queue_manager.enqueue("step normal", priority=50, source="web", author="alice")
        q2 = queue_manager.enqueue("step urgent", priority=10, source="api", author="codex")

        listed = queue_manager.list_queue()
        self.assertEqual([item["id"] for item in listed], [q2["id"], q1["id"]])

        held = queue_manager.modify_item(q2["id"], status="held", content="step urgent edited", by="web")
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["content"], "step urgent edited")

        consumed = queue_manager.consume_ready()
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0]["id"], q1["id"])

        remaining = queue_manager.list_queue()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], q2["id"])
        self.assertEqual(remaining[0]["status"], "held")

        queue_manager.modify_item(q2["id"], status="queued", by="api")
        consumed2 = queue_manager.consume_ready()
        self.assertEqual(len(consumed2), 1)
        self.assertEqual(consumed2[0]["id"], q2["id"])
        self.assertEqual(consumed2[0]["content"], "step urgent edited")
        self.assertEqual(queue_manager.list_queue(), [])

    def test_agent_loop_skips_held_and_consumes_queued(self):
        q_held = queue_manager.enqueue("DO NOT RUN HELD", priority=10, status="held")
        q_ready = queue_manager.enqueue("RUN THIS QUEUED NOTE", priority=50, status="queued")

        agent, llm = build_agent([{"content": "TASK_DONE: handled queue"}])
        agent.run_task("initial instruction")

        sent_messages = llm.requests[0]["messages"]
        combined_text = "\n".join(str(m.get("content", "")) for m in sent_messages)
        self.assertIn("RUN THIS QUEUED NOTE", combined_text)
        self.assertNotIn("DO NOT RUN HELD", combined_text)

        still_queued = queue_manager.list_queue()
        self.assertEqual(len(still_queued), 1)
        self.assertEqual(still_queued[0]["id"], q_held["id"])
        self.assertNotEqual(still_queued[0]["id"], q_ready["id"])


class QueueApiDualControlTests(ApiCase):
    def test_web_and_api_dual_control_mutual_correction(self):
        code, created = call(
            self.base,
            "POST",
            "/queue",
            {"content": "click wrong button", "priority": 50, "source": "api", "author": "external-agent"},
        )
        self.assertEqual(code, 201)
        item_id = created["item"]["id"]

        code, q_list = call(self.base, "GET", "/queue")
        self.assertEqual(code, 200)
        self.assertEqual(len(q_list["queue"]), 1)
        self.assertEqual(q_list["queue"][0]["id"], item_id)

        code, modified = call(
            self.base,
            "PATCH",
            f"/queue/{item_id}",
            {"status": "held", "content": "click submit button instead", "priority": 10, "by": "web"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(modified["item"]["status"], "held")
        self.assertEqual(modified["item"]["content"], "click submit button instead")
        self.assertEqual(modified["item"]["priority"], 10)

        disk_files = list((self.state / "queue").glob("*.json"))
        self.assertEqual(len(disk_files), 1)
        disk_payload = json.loads(disk_files[0].read_text(encoding="utf-8"))
        self.assertEqual(disk_payload["content"], "click submit button instead")
        self.assertEqual(disk_payload["status"], "held")

        code, deleted = call(self.base, "DELETE", f"/queue/{item_id}")
        self.assertEqual(code, 200)
        self.assertEqual(deleted["item"]["status"], "cancelled")

        code, after_delete = call(self.base, "GET", "/queue")
        self.assertEqual(after_delete["queue"], [])


if __name__ == "__main__":
    unittest.main()
