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
from unittest import mock

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


class _ChunkedStream:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read(self, size: int) -> str:
        if not self._chunks:
            return ""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


class RingBufferDrainTests(unittest.TestCase):
    def test_million_char_single_line_is_bounded_and_marked(self):
        stream = _ChunkedStream(["A" * 1000] * 1000)
        sink: list[str] = []
        tools_mod._drain_stream(stream, sink)
        text = "".join(sink)

        self.assertTrue(stream.closed)
        self.assertIn("more chars", text)
        self.assertIn("output truncated", text)
        self.assertLessEqual(
            len(text),
            tools_mod.PIPE_HEAD_BYTES + tools_mod.PIPE_TAIL_BYTES + 120,
        )

    def test_short_output_is_returned_verbatim(self):
        sink: list[str] = []
        tools_mod._drain_stream(_ChunkedStream(["hello ", "world"]), sink)
        self.assertEqual("".join(sink), "hello world")

    def test_head_and_tail_are_preserved(self):
        body = "H" * 5000 + "M" * 20000 + "T" * 5000
        sink: list[str] = []
        tools_mod._drain_stream(
            _ChunkedStream([body[i:i + 4096] for i in range(0, len(body), 4096)]),
            sink,
            head_limit=4096,
            tail_limit=4096,
        )
        text = "".join(sink)
        self.assertTrue(text.startswith("H" * 4096))
        self.assertTrue(text.endswith("T" * 4096))
        self.assertIn("21808 more chars omitted", text)


class NonInteractiveTests(unittest.TestCase):
    def test_run_process_injects_stdin_devnull_and_env(self):
        captured = {}

        class _FakeProc:
            pid = 1234
            returncode = 0
            stdout = None
            stderr = None

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProc()

        with mock.patch.object(tools_mod.subprocess, "Popen", fake_popen):
            with mock.patch.object(tools_mod, "_create_kill_on_close_job", lambda: None):
                tools_mod._run_process(["pwsh", "-NoProfile", "-Command", "echo hi"], 1)

        self.assertEqual(captured["kwargs"]["stdin"], tools_mod.subprocess.DEVNULL)
        env = captured["kwargs"]["env"]
        for key, value in tools_mod.NON_INTERACTIVE_ENV.items():
            self.assertEqual(env[key], value)
        self.assertEqual(tools_mod.NON_INTERACTIVE_ENV["GIT_TERMINAL_PROMPT"], "0")

    def test_shell_launches_pwsh_non_interactive(self):
        captured = {}

        def fake_run_process(argv, timeout):
            captured["argv"] = argv
            return "ok", "done", "", 0

        with mock.patch.object(tools_mod, "_run_process", fake_run_process):
            out = tools_mod.tool_shell("Write-Output hi")

        self.assertIn("-NonInteractive", captured["argv"])
        self.assertIn("exit=0", out)

    def test_read_host_returns_immediately(self):
        started = time.monotonic()
        out = tools_mod.tool_shell("Read-Host 'name'")
        elapsed = time.monotonic() - started
        _drain_result(out)
        self.assertLess(elapsed, 10.0)
        self.assertNotIn("exit=0", out)


class GitSandboxTests(unittest.TestCase):
    def test_git_status_runs_through_sandbox(self):
        module = tools_mod._load_skill_module(tools_mod.ROOT / "skills" / "git.py")
        fake = mock.Mock(return_value=("ok", "clean", "", 0))
        with mock.patch.object(module, "_run_process", fake):
            out = module.HANDLER(action="status")
        self.assertTrue(fake.called)
        self.assertIn("exit=0", out)
        self.assertIn("clean", out)

    def test_git_interrupt_marker_is_reported(self):
        module = tools_mod._load_skill_module(tools_mod.ROOT / "skills" / "git.py")
        with mock.patch.object(
            module, "_run_process", lambda argv, timeout: ("interrupted", "", "", None)
        ):
            out = module.HANDLER(action="status")
        self.assertIn("EXECUTION_INTERRUPTED_AND_IGNORED", out)


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
