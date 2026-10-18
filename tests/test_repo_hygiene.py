"""Privacy baseline: the working tree must stay free of private infrastructure data.

This is a regression guard for the publishable state of the repository. It
scans the working tree only (not git history): rerun ``git log``-level checks
manually before publishing rewritten history.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Patterns are assembled so this file never matches its own source.
FORBIDDEN = {
    "private network address": "192" + r"\.168\.",
    "douyin share id": "MS4w" + "LjABAAAA",
    "douyin domain": "douyin" + r"\.com",
    "home directory path": "/home/" + "[a-z]{1,12}" + "/bin/",
    "internal network drive": "[Zz]" + r":\\[wW]ork",
    "internal nas host": "zt" + "-nas",
}

SKIPPED_DIRS = {".git", "__pycache__", ".pytest_cache", "logs"}
SKIPPED_SUFFIXES = {".pyc"}


def _is_runtime_state(rel: Path) -> bool:
    """Runtime state under .agent/ (tasks, inbox, screenshots) is not publishable content."""
    return rel.parts[0] == ".agent" and rel.name != "AGENTS.md"


class WorkingTreePrivacyTests(unittest.TestCase):
    def test_no_private_infrastructure_data(self):
        hits: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix in SKIPPED_SUFFIXES:
                continue
            rel = path.relative_to(ROOT)
            if any(part in SKIPPED_DIRS for part in rel.parts):
                continue
            if _is_runtime_state(rel):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for label, pattern in FORBIDDEN.items():
                if re.search(pattern, text):
                    hits.append(f"{rel}: {label}")

        self.assertEqual(hits, [], f"private data found in working tree: {hits}")


if __name__ == "__main__":
    unittest.main()