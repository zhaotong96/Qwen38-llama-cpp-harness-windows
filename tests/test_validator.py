"""Tests for the ReAct tool validator: schema checks and dead-loop guards."""
from __future__ import annotations

import unittest

from core.validator import ToolValidator


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.validator = ToolValidator()

    def test_valid_arguments_accepted(self):
        cases = [
            ("shell", {"command": "Get-Date"}),
            ("read_file", {"path": "core/agent.py"}),
            ("write_file", {"path": "a.txt", "content": "x"}),
            ("mouse", {"action": "click", "x": 10, "y": 20}),
            ("keyboard", {"action": "type", "text": "hi"}),
            ("chrome_dom", {"action": "links"}),
            ("read_skill", {"skill_name": "shell"}),
            ("unknown_skill", {"anything": 1}),
        ]
        for name, args in cases:
            with self.subTest(name=name):
                self.assertTrue(self.validator.validate_schema(name, args).is_valid)

    def test_invalid_arguments_rejected(self):
        cases = [
            ("shell", {}),
            ("shell", {"command": "   "}),
            ("read_file", {}),
            ("write_file", {"path": "a.txt"}),
            ("mouse", {"action": "wiggle"}),
            ("mouse", {"action": "click", "x": -1, "y": 0}),
            ("keyboard", {"action": "type"}),
            ("keyboard", {"action": "hotkey"}),
            ("chrome_dom", {"action": "destroy"}),
            ("read_skill", {}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                result = self.validator.validate_schema(name, args)
                self.assertFalse(result.is_valid)
                self.assertTrue(result.error_message)

    def test_non_dict_arguments_rejected(self):
        self.assertFalse(self.validator.validate_schema("shell", ["not", "a", "dict"]).is_valid)


class RepetitionGuardTests(unittest.TestCase):
    def setUp(self):
        self.validator = ToolValidator(max_repeats=3)

    def test_identical_calls_halt_on_third(self):
        args = {"command": "Get-Date"}
        self.assertTrue(self.validator.validate("shell", args).is_valid)
        self.assertTrue(self.validator.validate("shell", args).is_valid)
        third = self.validator.validate("shell", args)
        self.assertFalse(third.is_valid)
        self.assertTrue(third.should_halt)
        self.assertIn("Dead loop", third.error_message)

    def test_varying_calls_stay_valid(self):
        for i in range(5):
            self.assertTrue(self.validator.validate("shell", {"command": f"echo {i}"}).is_valid)

    def test_reset_clears_history(self):
        args = {"command": "Get-Date"}
        self.validator.validate("shell", args)
        self.validator.validate("shell", args)
        self.validator.reset()
        self.assertTrue(self.validator.validate("shell", args).is_valid)

    def test_batch_repetition_detected(self):
        batch = ["shell:{'command': 'a'}", "read_file:{'path': 'b'}"]
        self.assertTrue(self.validator.check_batch_repetition(batch).is_valid)
        self.assertTrue(self.validator.check_batch_repetition(batch).is_valid)
        third = self.validator.check_batch_repetition(batch)
        self.assertFalse(third.is_valid)
        self.assertIn("Batch loop", third.error_message)

    def test_empty_batch_is_valid(self):
        self.assertTrue(self.validator.check_batch_repetition([]).is_valid)


if __name__ == "__main__":
    unittest.main()