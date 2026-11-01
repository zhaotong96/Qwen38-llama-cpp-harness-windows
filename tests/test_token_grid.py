"""Phase 11B: DeltaNet 1024-token grid alignment and hidden-state telemetry.

Covers the six mandated scenarios from TASK_DEV_PHASE11B_TOKEN_GRID §五.5:
tokenize endpoint + SHA-1 cache, structural divider padding, the pad ceiling,
compaction grid alignment, telemetry audit states, and ckpt_aligned DAG
metadata — plus offline degradation and re-seed detection helpers.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from core import persistence
from core import token_grid
from core.context import ContextManager
from tests.support import IsolatedCase


def setUpModule() -> None:
    token_grid.clear_tokenize_cache()


class TokenizeEndpointAndCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        token_grid.clear_tokenize_cache()

    def tearDown(self) -> None:
        token_grid.clear_tokenize_cache()

    def test_tokenize_endpoint_and_cache(self):
        calls: list[dict] = []

        def fake_urlopen(request, timeout=None):
            calls.append({"payload": json.loads(request.data), "timeout": timeout})
            body = json.dumps({"tokens": [1, 2, 3]}).encode("utf-8")
            return mock.mock_open(read_data=body).return_value

        text = "grid alignment probe"
        with mock.patch.object(token_grid.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(token_grid.exact_or_estimated_tokens(text, base_url="http://srv:1/v1"), 3)
            self.assertEqual(token_grid.exact_or_estimated_tokens(text, base_url="http://srv:1/v1"), 3)

        self.assertEqual(len(calls), 1, "second call must hit the SHA-1 memoization cache")
        self.assertEqual(calls[0]["payload"], {"content": text})
        self.assertEqual(calls[0]["timeout"], token_grid.TOKENIZE_TIMEOUT)

    def test_offline_smooth_degradation_to_estimator(self):
        with mock.patch.object(
            token_grid.urllib.request,
            "urlopen",
            side_effect=OSError("connection refused"),
        ):
            count = token_grid.exact_or_estimated_tokens("abcdefgh", base_url="http://dead:1/v1")
        self.assertEqual(count, token_grid.estimate_text_tokens("abcdefgh"))
        self.assertEqual(count, int(8 * 0.25))

    def test_unusable_payload_falls_back(self):
        body = json.dumps({"unexpected": "shape"}).encode("utf-8")
        with mock.patch.object(
            token_grid.urllib.request,
            "urlopen",
            return_value=mock.mock_open(read_data=body).return_value,
        ):
            count = token_grid.exact_or_estimated_tokens("stable fallback", base_url="http://srv:1/v1")
        self.assertEqual(count, token_grid.estimate_text_tokens("stable fallback"))

    def test_empty_text_is_zero_without_network(self):
        self.assertEqual(token_grid.exact_or_estimated_tokens(""), 0)


class StructuralDividerPaddingTests(unittest.TestCase):
    def test_structural_divider_padding(self):
        block = "fact ledger line\n" * 400  # ~1700 estimated tokens
        prior = token_grid.estimate_text_tokens(block)
        prior = 1024 - prior - 20  # leave a 20-token gap to the next grid line
        padded, info = token_grid.align_skeleton_block_to_grid(block, prior_tokens=prior)

        self.assertNotEqual(padded, block)
        self.assertIn("\n---", padded)
        self.assertNotIn("<!-- pad", padded)
        self.assertNotIn("<!--", padded)
        self.assertTrue(padded.startswith(block))
        self.assertTrue(info["aligned"], info)
        self.assertGreaterEqual(info["pad_lines"], 1)
        self.assertLessEqual(info["pad_tokens"], token_grid.MAX_PAD_TOKENS)
        self.assertGreater(info["after_tokens"] // 1024, info["before_tokens"] // 1024)

    def test_alignment_is_idempotent(self):
        block = "stable skeleton\n" * 400
        prior = 1024 - token_grid.estimate_text_tokens(block) - 20
        padded, info = token_grid.align_skeleton_block_to_grid(block, prior_tokens=prior)
        repadded, info2 = token_grid.align_skeleton_block_to_grid(
            padded, prior_tokens=prior
        )
        self.assertEqual(repadded, padded)
        self.assertEqual(info2["pad_lines"], 0)
        self.assertTrue(info2["aligned"])

    def test_already_on_boundary_appends_nothing(self):
        block = "x" * 4096  # 1024 estimated tokens exactly
        padded, info = token_grid.align_skeleton_block_to_grid(block, prior_tokens=0)
        self.assertEqual(padded, block)
        self.assertTrue(info["aligned"])
        self.assertEqual(info["gap_to_next"], 0)


class PaddingBoundaryCeilingTests(unittest.TestCase):
    def test_padding_boundary_ceiling(self):
        block = "skeleton body content\n" * 300  # mid-grid, far from a line
        before = token_grid.estimate_text_tokens(block)
        prior = 1024 - (before % 1024) + 200  # force a >64-token gap
        padded, info = token_grid.align_skeleton_block_to_grid(block, prior_tokens=prior)

        self.assertEqual(padded, block, "must not brute-force padding beyond the ceiling")
        self.assertFalse(info["aligned"])
        self.assertEqual(info["pad_lines"], 0)
        self.assertEqual(info.get("reason"), "gap_exceeds_ceiling")
        self.assertNotIn("<!-- pad", padded)


class TelemetryAuditStateTests(unittest.TestCase):
    def test_telemetry_audit_states(self):
        grid = 1024
        resonant = token_grid.verify_deltanet_checkpoint_telemetry(
            {"cache_n": 2048, "prompt_n": 2100}, expected_prefix_tokens=3000
        )
        self.assertEqual(resonant["status"], "RESONANT")

        cold = token_grid.verify_deltanet_checkpoint_telemetry(
            {"cache_n": 0, "prompt_n": 3000}, expected_prefix_tokens=3000
        )
        self.assertEqual(cold["status"], "COLD_RESET_DETECTED")

        drift = token_grid.verify_deltanet_checkpoint_telemetry(
            {"cache_n": 512, "prompt_n": 2600}, expected_prefix_tokens=3000
        )
        self.assertEqual(drift["status"], "CHECKPOINT_GAP_DRIFT")
        self.assertEqual(drift["min_expected_cache"], grid)

        unknown = token_grid.verify_deltanet_checkpoint_telemetry(None, 3000)
        self.assertEqual(unknown["status"], "UNKNOWN")

        short = token_grid.verify_deltanet_checkpoint_telemetry(
            {"cache_n": 0, "prompt_n": 500}, expected_prefix_tokens=500
        )
        self.assertEqual(short["status"], "RESONANT", "below one grid line there is no drift")


class FourgramReseedTests(unittest.TestCase):
    def test_repetition_loop_detection(self):
        loop = "click the button click the button click the button click the button"
        self.assertTrue(token_grid.should_reseed_generation(loop))

        normal = (
            "Open the workbook, select the export ribbon, choose the quarterly "
            "report template, then review the generated summary sheet."
        )
        self.assertFalse(token_grid.should_reseed_generation(normal))
        self.assertFalse(token_grid.should_reseed_generation(""))


class CompactionGridAlignmentTests(unittest.TestCase):
    def test_compaction_grid_alignment(self):
        # Unbound context: the summarizer output becomes the ledger verbatim,
        # so the boundary arithmetic is exact. System = 7 estimated tokens.
        ledger = "L" * 3988  # 997 estimated tokens → skeleton lands 20 before the line
        ctx = ContextManager("system prompt for grid tests")
        ctx.append({"role": "user", "content": "seed " + "x" * 200})
        ctx.append({"role": "assistant", "content": "reply " + "y" * 200})
        for i in range(6):
            ctx.append({"role": "user", "content": f"turn {i} " + "z" * 300})
            ctx.append({"role": "assistant", "content": f"answer {i} " + "w" * 300})

        compacted = ctx.compact(lambda messages: ledger, token_limit=800, keep_recent=2)
        self.assertTrue(compacted)

        summary_message = ctx.messages[1]
        self.assertEqual(summary_message["role"], "system")
        self.assertTrue(summary_message["content"].startswith("[Context Summary]"))
        # The skeleton base (system + ledger) crossed a 1024*K checkpoint line.
        prefix_tokens = ctx.get_prefix_token_count()
        self.assertEqual(prefix_tokens // 1024, 1, prefix_tokens)
        self.assertTrue(ctx._skeleton_alignment.get("aligned"), ctx._skeleton_alignment)
        self.assertNotIn("<!-- pad", summary_message["content"])
        self.assertIn("\n---", summary_message["content"])

    def test_prefix_token_count_without_summary(self):
        ctx = ContextManager("tiny")
        self.assertEqual(ctx.get_prefix_token_count(), token_grid.estimate_text_tokens("tiny"))

    def test_reload_preserves_aligned_prefix(self):
        task_id = "20260101_000000_000002"
        ctx = ContextManager("system prompt", task_id=task_id)
        ctx.bind_task(task_id)
        ctx.append({"role": "user", "content": "start " + "p" * 300})
        ctx.append({"role": "assistant", "content": "ack " + "q" * 200})
        ctx.append({"role": "user", "content": "again " + "r" * 300})
        compacted = ctx.compact(
            lambda messages: "ledger body\n" + "line\n" * 400,
            token_limit=100,
            keep_recent=1,
        )
        self.assertTrue(compacted)

        aligned_prefix = ctx.get_prefix_token_count()
        padded_summary = ctx.messages[1]["content"]

        reloaded = ContextManager("system prompt", task_id=task_id)
        reloaded.bind_task(task_id)
        self.assertEqual(reloaded.messages[1]["content"], padded_summary)
        self.assertEqual(reloaded.get_prefix_token_count(), aligned_prefix)


class DagNodeAlignedMetaTests(IsolatedCase):
    def test_dag_node_aligned_meta(self):
        task_id = "20260101_000000_000003"
        persistence.save_status(task_id, "running")

        filler = "x" * 900  # ~227 estimated tokens per turn: 4 turns stay below 1024
        for i in range(4):
            persistence.append_dag_node(
                task_id,
                {"role": "user", "content": f"chunk {i} {filler}"},
            )
        # cum_tokens now ~908; the next big turn must cross the 1024 line.
        crossing = persistence.append_dag_node(
            task_id,
            {"role": "tool", "tool_call_id": "t1", "content": "exit=0\n" + "y" * 800},
        )
        self.assertIn("cum_tokens", crossing["meta"])
        self.assertGreater(crossing["meta"]["cum_tokens"], 1024)
        self.assertTrue(crossing["meta"].get("ckpt_aligned"), crossing["meta"])

        plain = persistence.append_dag_node(
            task_id,
            {"role": "user", "content": "small turn"},
        )
        self.assertIn("cum_tokens", plain["meta"])
        self.assertNotIn("ckpt_aligned", plain["meta"])

    def test_assistant_tool_call_node_is_not_turn_closing(self):
        task_id = "20260101_000000_000004"
        persistence.save_status(task_id, "running")
        for i in range(4):
            persistence.append_dag_node(
                task_id,
                {"role": "user", "content": "pad " + "x" * 1100},
            )
        # cum_tokens crosses the 1024 line inside this turn, but an assistant
        # node with unanswered tool_calls sits mid-span: it must never be
        # flagged as an anchorable boundary.
        mid_span = persistence.append_dag_node(
            task_id,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "t9", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
                ],
            },
        )
        self.assertGreater(mid_span["meta"]["cum_tokens"], 1024)
        self.assertNotIn("ckpt_aligned", mid_span["meta"])


if __name__ == "__main__":
    unittest.main()
