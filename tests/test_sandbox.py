"""Acceptance tests for the Phase 6A execution sandbox.

Covers three operator-facing guarantees:
1. Large child output (including a single huge line) never deadlocks the tool.
2. Aborting a running step reaps the whole process tree, grandchildren included.
3. File writes are confined to the workspace and the system temp directory.
"""
from __future__ import annotations

import ctypes
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from pathlib import Path

from core import interrupt
from core import tools as tools_mod
from tests.support import IsolatedCase


def _drain_result(out: str) -> None:
    if "exit=" not in out:
        raise unittest.SkipTest("pwsh not available")


class PipeDrainTests(unittest.TestCase):
    def test_large_stdout_does_not_deadlock(self):
        command = (
            "1..2000 | ForEach-Object { $_ ; 'x' * 100 }"
        )
        started = time.monotonic()
        out = tools_mod.tool_shell(command)
        elapsed = time.monotonic() - started
        _drain_result(out)
        self.assertIn("exit=0", out)
        self.assertIn("more chars", out)
        self.assertLess(elapsed, 10.0)

    def test_single_huge_line_does_not_deadlock(self):
        command = "'A' * 300000"
        started = time.monotonic()
        out = tools_mod.tool_shell(command)
        elapsed = time.monotonic() - started
        _drain_result(out)
        self.assertIn("exit=0", out)
        self.assertLess(elapsed, 10.0)

    def test_child_stderr_also_drained(self):
        command = "1..500 | ForEach-Object { [Console]::Error.WriteLine('e' * 100) }"
        out = tools_mod.tool_shell(command)
        _drain_result(out)
        self.assertIn("exit=0", out)
        self.assertIn("more chars", out)


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32" or pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        kernel32.CloseHandle(handle)


class AbortReapingTests(IsolatedCase):
    def _build_tree_command(self, pid_file: Path) -> str:
        return (
            "$p = Start-Process pwsh "
            "-ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 120' "
            "-PassThru -WindowStyle Hidden; "
            f"Set-Content -LiteralPath '{pid_file}' -Value \"$PID $($p.Id)\"; "
            "Start-Sleep -Seconds 120"
        )

    def test_abort_reaps_grandchildren(self):
        if sys.platform != "win32":
            self.skipTest("process-tree reaping assertions target Windows")
        task_id = "sandbox_abort_task"
        tools_mod.set_active_execution_context(task_id, base_root=self.tasks_root)
        self.addCleanup(tools_mod.set_active_execution_context, None)

        pid_file = self.state / "child_pids.txt"
        seen: list[str] = []

        def abort_once_child_is_up():
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if pid_file.is_file():
                    seen.append(pid_file.read_text(encoding="utf-8").strip())
                    interrupt.set_abort_flag(task_id, base_root=self.tasks_root)
                    return
                time.sleep(0.1)

        helper = threading.Thread(target=abort_once_child_is_up, daemon=True)
        helper.start()
        started = time.monotonic()
        result = tools_mod.tool_shell(self._build_tree_command(pid_file))
        elapsed = time.monotonic() - started
        helper.join(timeout=5)

        if not seen:
            self.skipTest("pwsh not available or child tree never started")

        self.assertIn("EXECUTION_INTERRUPTED_AND_IGNORED", result)
        self.assertLess(elapsed, 30.0)
        pid_values = [int(value) for value in seen[0].split()]
        self.assertEqual(len(pid_values), 2)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in pid_values):
            time.sleep(0.2)
        self.assertFalse(
            any(_pid_alive(pid) for pid in pid_values),
            f"processes still alive after abort: {pid_values}",
        )


class WriteBoundaryTests(unittest.TestCase):
    def test_workspace_path_allowed(self):
        target = tools_mod.ROOT / ".agent" / "sandbox_probe_tmp.txt"
        try:
            self.assertIn("wrote", tools_mod.tool_write(str(target), "probe"))
        finally:
            target.unlink(missing_ok=True)

    def test_temp_dir_path_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "probe.txt"
            self.assertIn("wrote", tools_mod.tool_write(str(target), "probe"))

    def test_outside_workspace_rejected(self):
        escape = Path(tempfile.gettempdir()).resolve().parent / "sandbox_escape_probe.txt"
        try:
            out = tools_mod.tool_write(str(escape), "nope")
            self.assertTrue(out.startswith("error:"), out)
            self.assertIn("write denied", out)
            self.assertFalse(escape.exists())
        finally:
            escape.unlink(missing_ok=True)

    if sys.platform == "win32":
        def test_system_directory_rejected(self):
            target = "C:\\Windows\\System32\\drivers\\etc\\sandbox_probe.txt"
            out = tools_mod.tool_write(target, "nope")
            self.assertTrue(out.startswith("error:"), out)
            self.assertIn("write denied", out)


if __name__ == "__main__":
    unittest.main()
