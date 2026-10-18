"""Tests for conversation context: token estimates, screenshot retention, compaction."""
from __future__ import annotations

import unittest

from core.context import ContextManager


def image_message(path: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"Image observation attached. Continue. (file at {path})"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ],
    }


class TokenEstimateTests(unittest.TestCase):
    def test_text_and_image_cost(self):
        ctx = ContextManager("sys")
        base = ctx.token_estimate()
        ctx.append({"role": "user", "content": "x" * 400})
        after_text = ctx.token_estimate()
        self.assertGreaterEqual(after_text - base, 100)
        ctx.append(image_message(".agent/screen_000.jpg"))
        self.assertGreaterEqual(ctx.token_estimate() - after_text, 800)


class ScreenshotRetentionTests(unittest.TestCase):
    def test_old_screenshots_become_text_placeholders(self):
        ctx = ContextManager("sys", max_screenshots=2)
        for i in range(3):
            ctx.append(image_message(f".agent/screen_{i:03d}.jpg"))

        self.assertEqual(len(ctx.messages), 4)  # system + 3 observations
        first = ctx.messages[1]
        self.assertIsInstance(first["content"], str)
        self.assertIn("screenshot removed", first["content"])
        self.assertIn("screen_000.jpg", first["content"])
        self.assertIsInstance(ctx.messages[3]["content"], list)  # newest kept as image


class CompactionTests(unittest.TestCase):
    def test_compact_folds_old_messages(self):
        ctx = ContextManager("sys")
        for i in range(10):
            ctx.append({"role": "user", "content": f"message {i} " + "x" * 200})

        compacted = ctx.compact(
            lambda messages: f"summary of {len(messages)} messages",
            token_limit=50,
            keep_recent=2,
        )
        self.assertTrue(compacted)
        self.assertEqual(ctx.messages[0]["role"], "system")
        self.assertIn("summary of 8 messages", ctx.messages[1]["content"])
        self.assertEqual(len(ctx.messages), 4)  # system + summary + 2 recent

    def test_compact_skipped_below_limit(self):
        ctx = ContextManager("sys")
        ctx.append({"role": "user", "content": "small"})
        self.assertFalse(ctx.compact(lambda messages: "s", token_limit=10_000))

    def test_clear_task_resets_to_system_prompt(self):
        ctx = ContextManager("sys")
        ctx.append({"role": "user", "content": "hello"})
        ctx.clear_task()
        self.assertEqual(len(ctx.messages), 1)
        self.assertEqual(ctx.messages[0]["role"], "system")


if __name__ == "__main__":
    unittest.main()