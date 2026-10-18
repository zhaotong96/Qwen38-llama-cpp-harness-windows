"""Tests for the built-in tool implementations shared by skills."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import tools as tools_mod


class LoadTextTests(unittest.TestCase):
    def test_absolute_path_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.txt"
            target.write_text("hello\n", encoding="utf-8")
            self.assertEqual(tools_mod.load_text(target), "hello")
            self.assertEqual(tools_mod.load_text(Path(tmp) / "missing.txt", "fallback"), "fallback")

    def test_relative_path_resolves_under_root(self):
        self.assertTrue(tools_mod.load_text(".agent/AGENTS.md"))


class PathTests(unittest.TestCase):
    def test_safe_path_relative_and_absolute(self):
        relative = tools_mod.safe_path("core/agent.py")
        self.assertEqual(relative, (tools_mod.ROOT / "core" / "agent.py").resolve())
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tools_mod.writable_path(tmp), Path(tmp).resolve())


class TruncTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(tools_mod.trunc("abc"), "abc")

    def test_long_text_marked(self):
        out = tools_mod.trunc("x" * 20, 10)
        self.assertTrue(out.startswith("x" * 10))
        self.assertIn("10 more chars", out)


class FileToolTests(unittest.TestCase):
    def test_write_read_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "notes.txt"
            self.assertIn("wrote", tools_mod.tool_write(str(target), "hello world"))
            self.assertEqual(tools_mod.tool_read(str(target)), "hello world")
            self.assertIn("notes.txt", tools_mod.tool_ls(tmp))

    def test_read_missing_file(self):
        self.assertIn("error: not a file", tools_mod.tool_read("no_such_file_xyz.txt"))

    def test_read_directory_rejected(self):
        self.assertIn("error: not a directory", tools_mod.tool_ls(str(tools_mod.ROOT / "core" / "agent.py")))

    def test_large_file_read_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.txt"
            big.write_text("A" * (2 * 1024 * 1024), encoding="utf-8")
            out = tools_mod.tool_read(str(big))
        self.assertLess(len(out), 20_000)
        self.assertIn("more chars", out)


class ToolChainTests(unittest.TestCase):
    """write_file -> shell -> read_file: the three-step tool chain skills rely on."""

    def test_write_run_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "calc.py"
            result = Path(tmp) / "result.txt"
            code = (
                "total = sum(range(1, 101))\n"
                f"open(r'{result}', 'w', encoding='utf-8').write(str(total))\n"
            )
            self.assertIn("wrote", tools_mod.tool_write(str(script), code))

            out = tools_mod.tool_shell(f'& "{sys.executable}" "{script}"')
            if "exit=" not in out:
                self.skipTest("pwsh not available")
            self.assertIn("exit=0", out)
            self.assertEqual(tools_mod.tool_read(str(result)).strip(), "5050")


class SearchToolTests(unittest.TestCase):
    def test_finds_pattern_in_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.txt").write_text("needle-123 marker\n", encoding="utf-8")
            out = tools_mod.tool_search("needle-123", path=tmp, glob="*.txt")
        if "error" in out and "needle-123" not in out:
            self.skipTest("no search backend available (rg and pwsh missing)")
        self.assertIn("needle-123", out)


class ReadSkillTests(unittest.TestCase):
    def test_known_skill_source(self):
        self.assertIn("DESCRIPTION", tools_mod.tool_read_skill("shell"))

    def test_unknown_skill(self):
        self.assertIn("unknown skill", tools_mod.tool_read_skill("definitely_not_a_skill"))

    def test_path_traversal_rejected(self):
        self.assertEqual(tools_mod.tool_read_skill("../skills/shell"), "error: invalid skill name")


class ShellToolTests(unittest.TestCase):
    def test_dangerous_command_variants_blocked(self):
        commands = (
            "rm -rf /",
            "rm -rf C:\\Windows",
            "format-volume -DriveLetter C",
            "Remove-Item -Path C:\\Windows -Recurse -Force",
            "shutdown /s /t 0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            safety_log = Path(tmp) / "safety.log"
            with mock.patch.object(tools_mod, "SAFETY_LOG", safety_log):
                for command in commands:
                    with self.subTest(command=command):
                        self.assertIn("SECURITY_BLOCK", tools_mod.tool_shell(command))
            self.assertIn("BLOCK shell", safety_log.read_text(encoding="utf-8"))

    def test_benign_command_executes(self):
        out = tools_mod.tool_shell("Get-Date -Format o")
        if "exit=" not in out:
            self.skipTest("pwsh not available")
        self.assertIn("exit=0", out)


if __name__ == "__main__":
    unittest.main()