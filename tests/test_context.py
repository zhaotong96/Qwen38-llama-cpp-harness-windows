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


class TokenEstimateDensityTests(unittest.TestCase):
    def test_chinese_is_weighted_higher_than_ascii(self):
        ctx = ContextManager("sys")
        base = ctx.token_estimate()
        ctx.append({"role": "user", "content": "中" * 100})
        self.assertEqual(ctx.token_estimate() - base, 75)

    def test_reasoning_and_tool_calls_are_counted(self):
        ctx = ContextManager("sys")
        base = ctx.token_estimate()
        ctx.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "x" * 400,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "mouse", "arguments": "{\"x\": 1}"},
                    }
                ],
            }
        )
        self.assertGreater(ctx.token_estimate() - base, 100)

    def test_image_observation_costs_1120(self):
        ctx = ContextManager("sys")
        base = ctx.token_estimate()
        ctx.append(image_message(".agent/screen_000.jpg"))
        text_part = "Image observation attached. Continue. (file at .agent/screen_000.jpg)"
        ascii_cost = int(len(text_part) * 0.25)
        self.assertEqual(ctx.token_estimate() - base, 1120 + ascii_cost)


class HysteresisWindowTests(unittest.TestCase):
    def test_batch_eviction_only_after_high_watermark(self):
        ctx = ContextManager("sys", max_screenshots=6, retain_screenshots=2)
        for i in range(6):
            ctx.append(image_message(f".agent/screen_{i:03d}.jpg"))
        kept = [m for m in ctx.messages if isinstance(m["content"], list)]
        self.assertEqual(len(kept), 6)

        ctx.append(image_message(".agent/screen_006.jpg"))
        kept = [m for m in ctx.messages if isinstance(m["content"], list)]
        self.assertEqual(len(kept), 2)
        placeholders = [
            m
            for m in ctx.messages
            if isinstance(m["content"], str) and "screenshot removed" in m["content"]
        ]
        self.assertEqual(len(placeholders), 5)
        self.assertIn("screen_006.jpg", str(ctx.messages[-1]))

    def test_default_retain_is_half_of_high_watermark(self):
        ctx = ContextManager("sys", max_screenshots=4)
        self.assertEqual(ctx.retain_screenshots, 2)


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