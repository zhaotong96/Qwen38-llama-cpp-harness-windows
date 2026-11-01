"""Phase 11A: tail-anchored ephemeral error GC (transient trial folding).

Covers the seven mandated scenarios from TASK_DEV_PHASE11A_CONTEXT_GC §四.3:
idempotent single-tool fold, multi-turn trial chain fold, TDD gate
interlocking, protected-marker immunity, deep-history window protection,
OpenAI/Jinja topology invariants, and disk checkpoint immutability.

Failure receipts are sized like real tracebacks (>= min_error_tokens estimated
tokens) because the Pareto gate must NOT fold trivially small failures.
"""
from __future__ import annotations

import unittest

from core.context import ContextManager
from tests.support import IsolatedCase


def _noise(char: str, length: int = 1400) -> str:
    """Deterministic filler simulating a long traceback body."""
    return (char * 79 + "\n") * (length // 80)


def assistant_turn(call_id: str, tool: str, args_json: str = "{}") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool, "arguments": args_json},
            }
        ],
    }


def multi_assistant_turn(call_ids: list[str], tools: list[str]) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool, "arguments": "{}"},
            }
            for call_id, tool in zip(call_ids, tools)
        ],
    }


def tool_receipt(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def build_context(*turns: list[dict], system: str = "system prompt") -> ContextManager:
    ctx = ContextManager(system)
    for turn in turns:
        for message in turn:
            ctx.append(message)
    return ctx


def topology_violations(messages: list[dict]) -> list[str]:
    """Validate the Chat Completions role topology (no orphan tool receipts)."""
    problems: list[str] = []
    pending_ids: set[str] = set()
    for index, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            if pending_ids:
                problems.append(f"assistant at {index} while tool receipts pending: {pending_ids}")
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                if tc_id:
                    pending_ids.add(tc_id)
        elif role == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id not in pending_ids:
                problems.append(f"orphan tool receipt at {index}: {tc_id!r}")
            else:
                pending_ids.discard(tc_id)
        elif role in ("user", "system"):
            if pending_ids:
                problems.append(f"{role} at {index} while tool receipts pending: {pending_ids}")
    if pending_ids:
        problems.append(f"unanswered tool_calls at end: {pending_ids}")
    return problems


class SingleToolIdempotentFoldTests(unittest.TestCase):
    def test_single_tool_idempotent_fold(self):
        ctx = build_context(
            [assistant_turn("c1", "grounding"), tool_receipt("c1", "error: target not found\n" + _noise("a"))],
            [assistant_turn("c2", "grounding"), tool_receipt("c2", "[GROUNDING_HIT] control_id=7 center=(10, 20)")],
        )
        baseline = len(ctx.messages)
        result = ctx.prune_resolved_trial_tail()

        self.assertTrue(result["pruned"], result)
        self.assertEqual(result["pruned_turns"], 1)
        # The failed grounding turn span (assistant + tool) is removed whole.
        self.assertEqual(len(ctx.messages), baseline - 2)
        self.assertFalse(any(m.get("tool_call_id") == "c1" for m in ctx.messages))
        self.assertFalse(any(c.startswith("error:") for c in map(str, [m.get("content") for m in ctx.messages])))
        winning_receipt = next(
            m["content"] for m in ctx.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "c2"
        )
        self.assertTrue(winning_receipt.startswith("[Transient Attempt Pruned: 1 prior failed"))
        self.assertIn("resolved via grounding", winning_receipt)


class MultiTurnTrialChainFoldTests(unittest.TestCase):
    def test_multi_turn_trial_chain_fold(self):
        ctx = build_context(
            [assistant_turn("c1", "patch"), tool_receipt("c1", "error: patch failed: SyntaxError at line 3\n" + _noise("x"))],
            [assistant_turn("c2", "patch"), tool_receipt("c2", "error: patch failed: line offset mismatch\n" + _noise("y"))],
            [
                multi_assistant_turn(["c3a", "c3b"], ["patch", "shell"]),
                tool_receipt("c3a", "patched core/calc.py (hunk applied)"),
                tool_receipt("c3b", "exit=1\nRan 3 tests\nFAILED (failures=2)\n" + _noise("z")),
            ],
            [
                multi_assistant_turn(["c4a", "c4b"], ["patch", "shell"]),
                tool_receipt("c4a", "patched core/calc.py (hunk applied)"),
                tool_receipt("c4b", "exit=0\nRan 3 tests in 0.002s\nOK"),
            ],
        )
        result = ctx.prune_resolved_trial_tail()

        self.assertTrue(result["pruned"], result)
        # Rounds 1, 2 (pure failures) and 3 (write-ok but failing test) fold;
        # round 4 is the resolved TDD closure and stays as the winning span.
        self.assertEqual(result["pruned_turns"], 3)
        self.assertFalse(any(m.get("tool_call_id") in ("c1", "c2", "c3a", "c3b") for m in ctx.messages))
        self.assertFalse(any(str(m.get("content", "")).startswith("error:") for m in ctx.messages))
        contents = [str(m.get("content", "")) for m in ctx.messages]
        final_receipt = next(
            c for m, c in zip(ctx.messages, contents)
            if m.get("role") == "tool" and m.get("tool_call_id") == "c4a"
        )
        self.assertIn("[Transient Attempt Pruned: 3 prior failed attempt(s)", final_receipt)
        self.assertIn("exit=1", final_receipt)
        # The badge collapses the whole chain into exactly one marker line.
        self.assertEqual(
            sum("[Transient Attempt Pruned:" in c for c in contents),
            1,
            contents,
        )
        self.assertEqual(topology_violations(ctx.messages), [])


class TddGateInterlockingTests(unittest.TestCase):
    def test_tdd_gate_interlocking(self):
        ctx = build_context(
            [assistant_turn("c1", "patch"), tool_receipt("c1", "error: hunk failed\n" + _noise("h"))],
            [
                assistant_turn("c2", "write_file", '{"path": "core/calc.py"}'),
                tool_receipt("c2", "wrote core/calc.py"),
            ],
            [
                assistant_turn("c3", "shell"),
                tool_receipt("c3", "exit=0\nRan 2 tests in 0.001s\nOK"),
            ],
        )
        blocked = ctx.prune_resolved_trial_tail(tdd_gate_open=True)
        self.assertFalse(blocked["pruned"])
        self.assertEqual(blocked.get("reason"), "tdd_open_or_short")
        self.assertTrue(any(str(m.get("content", "")).startswith("error: hunk failed") for m in ctx.messages))

        allowed = ctx.prune_resolved_trial_tail(tdd_gate_open=False)
        self.assertTrue(allowed["pruned"], allowed)
        self.assertFalse(any(m.get("tool_call_id") == "c1" for m in ctx.messages))
        self.assertFalse(any(str(m.get("content", "")).startswith("error:") for m in ctx.messages))


class ProtectedMarkerImmunityTests(unittest.TestCase):
    def test_protected_markers_absolute_immunity(self):
        for marker in ("[Operator override]", "[Branch Rewind Lesson] target locked"):
            ctx = build_context(
                [assistant_turn("c1", "shell"), tool_receipt("c1", f"exit=1\n{marker}\n" + _noise("z"))],
                [
                    assistant_turn("c2", "shell"),
                    tool_receipt("c2", "exit=0\nrecovered"),
                ],
            )
            result = ctx.prune_resolved_trial_tail()
            self.assertFalse(result["pruned"], (marker, result))
            self.assertTrue(
                any(marker in m.get("content", "") for m in ctx.messages),
                marker,
            )

    def test_mixed_chain_survives_when_protected_turn_in_window(self):
        ctx = build_context(
            [assistant_turn("c1", "shell"), tool_receipt("c1", "exit=1\n[Operator override]\n" + _noise("q"))],
            [assistant_turn("c2", "shell"), tool_receipt("c2", "exit=1\nplain failure\n" + _noise("w"))],
            [assistant_turn("c3", "shell"), tool_receipt("c3", "exit=0\nfixed")],
        )
        result = ctx.prune_resolved_trial_tail()
        # Gate 4: the operator-marked turn is 100% exempt; the adjacent plain
        # transient failure still folds.
        self.assertTrue(result["pruned"], result)
        self.assertEqual(result["pruned_turns"], 1)
        self.assertTrue(any("[Operator override]" in m.get("content", "") for m in ctx.messages))
        self.assertFalse(any(m.get("tool_call_id") == "c2" for m in ctx.messages))


class DeepHistoryWindowProtectionTests(unittest.TestCase):
    def test_deep_history_window_protection(self):
        ancient_error = "error: patch failed: line offset mismatch\n" + _noise("a", 1600)
        healthy_middle = "exit=0\nlong-running healthy middle log\n" + _noise("b", 9000)
        ctx = build_context(
            [assistant_turn("c1", "patch"), tool_receipt("c1", ancient_error)],
            [assistant_turn("c2", "shell"), tool_receipt("c2", healthy_middle)],
            [assistant_turn("c3", "shell"), tool_receipt("c3", "exit=1\nfresh transient failure\n" + _noise("c", 1600))],
            [assistant_turn("c4", "shell"), tool_receipt("c4", "exit=0\nfixed at the tail")],
        )
        result = ctx.prune_resolved_trial_tail(max_tail_tokens=2048)

        self.assertTrue(result["pruned"], result)
        # The ancient failure sits beyond max_tail_tokens (the healthy middle
        # alone fills the window): the DeltaNet-stable prefix stays untouched
        # and only the fresh tail failure folds.
        self.assertTrue(any("line offset mismatch" in m.get("content", "") for m in ctx.messages))
        self.assertFalse(any("fresh transient failure" in m.get("content", "") for m in ctx.messages))
        self.assertEqual(result["pruned_turns"], 1)


class TopologyInvariantTests(unittest.TestCase):
    def test_openai_jinja_topology_invariants(self):
        ctx = build_context(
            [assistant_turn("c1", "shell"), tool_receipt("c1", "exit=1\nboom\n" + _noise("d"))],
            [
                assistant_turn("c2", "shell"),
                tool_receipt("c2", "exit=0\nok"),
            ],
        )
        # A second failing call inside the winning turn exercises k>1 receipts.
        ctx.append(multi_assistant_turn(["c3a", "c3b"], ["shell", "shell"]))
        ctx.append(tool_receipt("c3a", "exit=0\npart a"))
        ctx.append(tool_receipt("c3b", "exit=0\npart b"))

        self.assertEqual(topology_violations(ctx.messages), [])
        result = ctx.prune_resolved_trial_tail()
        self.assertTrue(result["pruned"], result)
        self.assertEqual(topology_violations(ctx.messages), [])
        self.assertEqual(ctx.messages[0].get("role"), "system")


class DiskCheckpointImmutabilityTests(IsolatedCase):
    def test_disk_checkpoint_immutability(self):
        from core import persistence

        task_id = "20260101_000000_000001"
        ctx = ContextManager("system prompt", task_id=task_id)
        ctx.bind_task(task_id)
        ctx.append({"role": "user", "content": "fix the calc module"})
        ctx.append(assistant_turn("c1", "patch"))
        ctx.append(tool_receipt("c1", "error: hunk failed\n" + _noise("e")))
        ctx.append(assistant_turn("c2", "shell"))
        ctx.append(tool_receipt("c2", "exit=0\nfixed"))

        checkpoint = self.tasks_root / task_id / "checkpoint.jsonl"
        before = checkpoint.read_text(encoding="utf-8")
        self.assertIn("hunk failed", before)

        result = ctx.prune_resolved_trial_tail()
        self.assertTrue(result["pruned"], result)
        self.assertEqual(
            checkpoint.read_text(encoding="utf-8"),
            before,
            "in-memory pruning must never mutate the immutable DAG store",
        )
        # The disk projection still replays the full un-pruned history.
        replayed = persistence.build_branch_context(task_id)
        self.assertTrue(any("hunk failed" in m.get("content", "") for m in replayed))


class NoopGuardsTests(unittest.TestCase):
    def test_short_context_and_unresolved_tail_are_noops(self):
        ctx = ContextManager("system prompt")
        self.assertEqual(
            ctx.prune_resolved_trial_tail(),
            {"pruned": False, "reason": "tdd_open_or_short"},
        )

        ctx = build_context(
            [assistant_turn("c1", "shell"), tool_receipt("c1", "exit=1\nboom\n" + _noise("f"))],
            [assistant_turn("c2", "shell"), tool_receipt("c2", "exit=1\nstill failing\n" + _noise("g"))],
        )
        self.assertEqual(
            ctx.prune_resolved_trial_tail(),
            {"pruned": False, "reason": "latest_turn_not_clean_success"},
        )

    def test_below_pareto_threshold_skips_tiny_single_failure(self):
        ctx = build_context(
            [assistant_turn("c1", "shell"), tool_receipt("c1", "exit=1\nnope")],
            [assistant_turn("c2", "shell"), tool_receipt("c2", "exit=0\nok")],
            [
                assistant_turn("c3", "read_file"),
                tool_receipt("c3", "ok: file contents"),
            ],
        )
        result = ctx.prune_resolved_trial_tail()
        self.assertFalse(result["pruned"])
        self.assertEqual(result.get("reason"), "below_pareto_threshold")

    def test_failure_signature_extraction(self):
        self.assertEqual(
            ContextManager._extract_failure_signature("boom: ValueError('x') then OSError"),
            "ValueError/OSError",
        )
        self.assertEqual(
            ContextManager._extract_failure_signature("weird failure"),
            "weird failure"[:48],
        )
        self.assertFalse(ContextManager._is_failed_tool_receipt(["not", "a", "string"]))
        self.assertTrue(ContextManager._is_failed_tool_receipt("[TDD_VERIFICATION_GATE] run tests"))


if __name__ == "__main__":
    unittest.main()
