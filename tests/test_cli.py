"""Smoke test of the real CLI entry point (subprocess; no model traffic).

Skipped when the ``openai`` package is unavailable, because the real entry
point imports it at startup.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests import OPENAI_AVAILABLE

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(OPENAI_AVAILABLE, "openai package not installed")
class CliSmokeTests(unittest.TestCase):
    """main.py must boot and exit cleanly in one-shot and REPL modes."""

    def run_cli(self, args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        return subprocess.run(
            [sys.executable, str(ROOT / "main.py"), *args],
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            env=env,
            timeout=90,
        )

    def test_exit_argument(self):
        proc = self.run_cli(["exit"])
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])

    def test_interactive_shell_exits_cleanly(self):
        proc = self.run_cli([], input_text="exit\n")
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertIn("Operator>", proc.stdout)


if __name__ == "__main__":
    unittest.main()