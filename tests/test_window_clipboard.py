"""Phase 10A: Win32 window_manager & clipboard atomic desktop skills.

Covers SPEC §4 contract validation, the skill-sandbox AST top-level purity
theorem, mocked Win32 state-machine scenarios (hung-window fuse, UIPI
avoidance, iconic guard, watchdog fallback, spinlock contention, 64-bit
pointer typing, kernel ownership transfer), and real-machine clipboard
round trips.
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from core.skill_sandbox import check_skill_ast, validate_openai_parameters_schema
from skills import clipboard as clipboard_mod
from skills import window_manager as window_manager_mod

ROOT = Path(__file__).resolve().parent.parent

TARGET_HWND = 42
FG_HWND = 999
CUR_PID = 111
TARGET_PID = 222
FG_PID = 333
CUR_TID = 777
TARGET_TID = 555
FG_TID = 666
SELF_RID = 0x2000
HIGH_RID = 0x3000

WIN32_ONLY = unittest.skipUnless(sys.platform == "win32", "Win32 state machine only")


def _read_skill_source(name: str) -> str:
    return (ROOT / "skills" / f"{name}.py").read_text(encoding="utf-8")


class SkillContractTests(unittest.TestCase):
    """SPEC §4 + skill-sandbox gates over both new skill modules."""

    def test_ast_top_level_purity_zero_errors(self):
        for name in ("window_manager", "clipboard"):
            with self.subTest(skill=name):
                errors = check_skill_ast(_read_skill_source(name))
                self.assertEqual(errors, [])

    def test_parameters_schema_is_valid_openai_json_schema(self):
        modules = {
            "window_manager": (window_manager_mod, ["list_windows", "activate_window"]),
            "clipboard": (clipboard_mod, ["get_text", "set_text"]),
        }
        for name, (module, actions) in modules.items():
            with self.subTest(skill=name):
                schema = module.PARAMETERS
                self.assertEqual(validate_openai_parameters_schema(schema), [])
                self.assertEqual(schema["properties"]["action"]["enum"], actions)
                self.assertEqual(schema["required"], ["action"])

    def test_skill_contract_exports(self):
        for module in (window_manager_mod, clipboard_mod):
            with self.subTest(skill=module.__name__):
                self.assertTrue(str(module.DESCRIPTION).strip())
                self.assertIsInstance(module.PARAMETERS, dict)
                self.assertTrue(callable(module.run))


def _make_window_apis(hung=False, iconic_value=0, chrome_child=0, rid_map=None):
    """Fake (user32, kernel32, advapi32) triple driving the activation state machine."""
    fg_state = [FG_HWND]

    user32 = mock.Mock()
    user32.IsWindow.return_value = 1
    user32.IsWindowVisible.return_value = 1
    user32.IsIconic.return_value = iconic_value
    user32.SendMessageTimeoutW.return_value = 0 if hung else 1
    user32.ShowWindowAsync.return_value = 1
    user32.LockSetForegroundWindow.return_value = 1
    user32.AllowSetForegroundWindow.return_value = 1
    user32.SendInput.return_value = 1
    user32.SystemParametersInfoW.return_value = 1
    user32.BringWindowToTop.return_value = 1
    user32.SetWindowPos.return_value = 1
    user32.SetActiveWindow.return_value = TARGET_HWND
    user32.FindWindowExW.return_value = chrome_child
    user32.SetFocus.return_value = TARGET_HWND
    user32.AttachThreadInput.return_value = 1

    pid_by_hwnd = {TARGET_HWND: TARGET_PID, FG_HWND: FG_PID}
    tid_by_hwnd = {TARGET_HWND: TARGET_TID, FG_HWND: FG_TID}

    def get_window_thread_process_id(hwnd, pid_ref):
        dword = ctypes.cast(pid_ref, ctypes.POINTER(wintypes.DWORD)).contents
        dword.value = pid_by_hwnd.get(hwnd, 0)
        return tid_by_hwnd.get(hwnd, 0)

    user32.GetWindowThreadProcessId.side_effect = get_window_thread_process_id
    user32.GetForegroundWindow.side_effect = lambda: fg_state[0]

    def set_foreground_window(hwnd):
        fg_state[0] = hwnd
        return 1

    user32.SetForegroundWindow.side_effect = set_foreground_window

    kernel32 = mock.Mock()
    kernel32.GetCurrentProcessId.return_value = CUR_PID
    kernel32.GetCurrentThreadId.return_value = CUR_TID

    advapi32 = mock.Mock()
    rid_by_pid = rid_map if rid_map is not None else {
        CUR_PID: SELF_RID, TARGET_PID: SELF_RID, FG_PID: SELF_RID,
    }

    def integrity_rid(_kernel32, _advapi32, pid):
        return rid_by_pid.get(int(pid), SELF_RID)

    return user32, kernel32, advapi32, fg_state, integrity_rid


@WIN32_ONLY
class ActivateWindowStateTests(unittest.TestCase):
    def _patch_apis(self, **kwargs):
        user32, kernel32, advapi32, fg_state, integrity_rid = _make_window_apis(**kwargs)
        patcher_apis = mock.patch.object(
            window_manager_mod, "_get_win32_apis",
            lambda: (user32, kernel32, mock.Mock(), advapi32),
        )
        patcher_rid = mock.patch.object(
            window_manager_mod, "_get_process_integrity_rid", side_effect=integrity_rid
        )
        patcher_apis.start()
        patcher_rid.start()
        self.addCleanup(patcher_apis.stop)
        self.addCleanup(patcher_rid.stop)
        return user32, fg_state

    def test_hung_window_fuses_fast_before_any_state_change(self):
        user32, _ = self._patch_apis(hung=True)
        started = time.monotonic()
        result = window_manager_mod.activate_window_safely(TARGET_HWND)
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "WINDOW_HUNG_OR_INVALID")
        self.assertLess(elapsed, 0.5)
        user32.ShowWindowAsync.assert_not_called()
        user32.AttachThreadInput.assert_not_called()

    def test_already_foreground_short_circuits(self):
        user32, fg_state = self._patch_apis()
        fg_state[0] = TARGET_HWND
        result = window_manager_mod.activate_window_safely(TARGET_HWND)
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "already_foreground")
        user32.ShowWindowAsync.assert_not_called()

    def test_uipi_elevated_target_skips_thread_input_attach(self):
        user32, _ = self._patch_apis(
            rid_map={CUR_PID: SELF_RID, TARGET_PID: HIGH_RID, FG_PID: SELF_RID}
        )
        result = window_manager_mod.activate_window_safely(TARGET_HWND)
        self.assertTrue(result["ok"])
        self.assertTrue(result["uipi_elevated"])
        attach_calls = [
            call for call in user32.AttachThreadInput.call_args_list
            if call.args[1] == TARGET_TID
        ]
        self.assertEqual(attach_calls, [])

    def test_same_integrity_target_attaches_and_detaches_in_pair(self):
        user32, _ = self._patch_apis()
        result = window_manager_mod.activate_window_safely(TARGET_HWND)
        self.assertTrue(result["ok"])
        self.assertFalse(result["uipi_elevated"])
        sequence = [tuple(c.args) for c in user32.AttachThreadInput.call_args_list]
        self.assertIn((CUR_TID, TARGET_TID, True), sequence)
        self.assertIn((CUR_TID, TARGET_TID, False), sequence)
        self.assertLess(
            sequence.index((CUR_TID, TARGET_TID, True)),
            sequence.index((CUR_TID, TARGET_TID, False)),
        )

    def test_iconic_window_is_restored_not_shown(self):
        user32, _ = self._patch_apis(iconic_value=1)
        window_manager_mod.activate_window_safely(TARGET_HWND)
        user32.ShowWindowAsync.assert_called_once_with(TARGET_HWND, 9)  # SW_RESTORE

    def test_non_iconic_window_is_shown_never_restored(self):
        user32, _ = self._patch_apis(iconic_value=0)
        window_manager_mod.activate_window_safely(TARGET_HWND)
        user32.ShowWindowAsync.assert_called_once_with(TARGET_HWND, 5)  # SW_SHOW

    def test_watchdog_topmost_toggle_fallback_breaks_the_lock(self):
        user32, fg_state = self._patch_apis()

        def switch_to_this_window(hwnd, _alive):
            fg_state[0] = hwnd

        user32.SwitchToThisWindow.side_effect = switch_to_this_window
        # SetForegroundWindow cannot break the lock by itself: disable the
        # default side effect so only the watchdog fallback can win focus.
        user32.SetForegroundWindow.side_effect = None
        user32.SetForegroundWindow.return_value = 1

        result = window_manager_mod.activate_window_safely(TARGET_HWND, watchdog_ms=150)
        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback_used"])
        insert_afters = [c.args[1] for c in user32.SetWindowPos.call_args_list]
        self.assertIn(-1, insert_afters)  # HWND_TOPMOST
        self.assertIn(-2, insert_afters)  # HWND_NOTOPMOST
        user32.SwitchToThisWindow.assert_called_once()

    def test_foreground_lock_rejected_is_reported_after_watchdog(self):
        user32, _ = self._patch_apis()
        user32.SetForegroundWindow.side_effect = None
        user32.SetForegroundWindow.return_value = 1  # never wins the foreground
        started = time.monotonic()
        result = window_manager_mod.activate_window_safely(TARGET_HWND, watchdog_ms=150)
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "FOREGROUND_LOCK_REJECTED")
        self.assertTrue(result["fallback_used"])  # fallback fired but could not win focus
        user32.SwitchToThisWindow.assert_called_once()
        self.assertGreaterEqual(elapsed, 0.14)
        self.assertEqual(result["actual_foreground_hwnd"], FG_HWND)

    def test_chromium_render_child_receives_keyboard_focus(self):
        user32, _ = self._patch_apis(chrome_child=4242)
        window_manager_mod.activate_window_safely(TARGET_HWND)
        user32.FindWindowExW.assert_called_once_with(
            TARGET_HWND, None, "Chrome_RenderWidgetHostHWND", None
        )
        user32.SetFocus.assert_called_once_with(4242)

    def test_plain_window_keeps_focus_on_itself(self):
        user32, _ = self._patch_apis(chrome_child=0)
        window_manager_mod.activate_window_safely(TARGET_HWND)
        user32.SetFocus.assert_called_once_with(TARGET_HWND)

    def test_thread_input_queues_detach_even_when_activation_raises(self):
        user32, _ = self._patch_apis()
        user32.SetForegroundWindow.side_effect = RuntimeError("simulated crash")
        with self.assertRaises(RuntimeError):
            window_manager_mod.activate_window_safely(TARGET_HWND)
        sequence = [tuple(c.args) for c in user32.AttachThreadInput.call_args_list]
        self.assertEqual(
            sequence,
            [
                (CUR_TID, FG_TID, True),
                (CUR_TID, TARGET_TID, True),
                (CUR_TID, TARGET_TID, False),
                (CUR_TID, FG_TID, False),
            ],
        )


def _make_window_enum_apis(windows):
    """Fake (user32, kernel32, dwmapi) triple for EnumWindows-based listing.

    ``windows`` maps hwnd -> info dict with keys: visible, title, pid, image,
    iconic (optional), cloaked (optional), bounds (optional 4-tuple; when None
    DWM fails and GetWindowRect feeds ``fallback_rect``).
    """
    user32 = mock.Mock()
    kernel32 = mock.Mock()
    dwmapi = mock.Mock()

    def enum_windows(callback, lparam):
        for hwnd in windows:
            callback(hwnd, 0)
        return 1

    user32.EnumWindows.side_effect = enum_windows

    def dwm_attribute(hwnd, attr, ptr, size):
        info = windows[hwnd]
        if attr == 14:  # DWMWA_CLOAKED
            ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = info.get("cloaked", 0)
            return 0
        if attr == 9:  # DWMWA_EXTENDED_FRAME_BOUNDS
            if info.get("bounds") is None:
                return 1  # non-zero HRESULT: force the GetWindowRect fallback
            rect = ctypes.cast(ptr, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = info["bounds"]
            return 0
        return -1

    dwmapi.DwmGetWindowAttribute.side_effect = dwm_attribute
    user32.IsWindowVisible.side_effect = lambda hwnd: 1 if windows[hwnd]["visible"] else 0
    user32.IsIconic.side_effect = lambda hwnd: 1 if windows[hwnd].get("iconic") else 0
    user32.GetWindowTextLengthW.side_effect = lambda hwnd: len(windows[hwnd]["title"])

    def get_window_text(hwnd, buf, size):
        buf.value = windows[hwnd]["title"]
        return len(windows[hwnd]["title"])

    user32.GetWindowTextW.side_effect = get_window_text

    def get_window_rect(hwnd, rect_ref):
        rect = ctypes.cast(rect_ref, ctypes.POINTER(wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = windows[hwnd]["fallback_rect"]
        return 1

    user32.GetWindowRect.side_effect = get_window_rect

    def get_window_thread_process_id(hwnd, pid_ref):
        ctypes.cast(pid_ref, ctypes.POINTER(wintypes.DWORD)).contents.value = windows[hwnd]["pid"]
        return hwnd

    user32.GetWindowThreadProcessId.side_effect = get_window_thread_process_id

    kernel32.OpenProcess.side_effect = lambda _access, _inherit, pid: pid
    kernel32.CloseHandle.return_value = 1
    image_by_pid = {info["pid"]: info["image"] for info in windows.values()}

    def query_full_process_image_name(handle, _flags, buf, _size_ref):
        buf.value = image_by_pid[handle]
        return 1

    kernel32.QueryFullProcessImageNameW.side_effect = query_full_process_image_name
    return user32, kernel32, dwmapi


class ListWindowsTests(unittest.TestCase):
    def test_enum_filters_and_fields(self):
        windows = {
            0x1: {"visible": True, "title": "Editor", "pid": 100, "image": "notepad.exe",
                  "bounds": (10, 20, 810, 620)},
            0x2: {"visible": False, "title": "Hidden", "pid": 101, "image": "ghost.exe",
                  "bounds": (0, 0, 100, 100)},
            0x3: {"visible": True, "title": "Minimized", "pid": 102, "image": "mini.exe",
                  "iconic": True, "bounds": (0, 0, 100, 100)},
            0x4: {"visible": True, "title": "Cloaked UWP", "pid": 103, "image": "uwp.exe",
                  "cloaked": 1, "bounds": (0, 0, 100, 100)},
            0x5: {"visible": True, "title": "Fallback Rect", "pid": 104, "image": "tool.exe",
                  "bounds": None, "fallback_rect": (1, 2, 201, 102)},
        }
        user32, kernel32, dwmapi = _make_window_enum_apis(windows)
        with mock.patch.object(
            window_manager_mod, "_get_win32_apis",
            lambda: (user32, kernel32, dwmapi, mock.Mock()),
        ):
            result = window_manager_mod.list_visible_windows()

        self.assertEqual([w["hwnd"] for w in result], [0x1, 0x5])
        editor = result[0]
        self.assertEqual(editor["title"], "Editor")
        self.assertEqual(editor["pid"], 100)
        self.assertEqual(editor["process"], "notepad.exe")
        self.assertEqual(
            editor["rect"], {"left": 10, "top": 20, "width": 800, "height": 600}
        )
        self.assertEqual(
            result[1]["rect"], {"left": 1, "top": 2, "width": 200, "height": 100}
        )

    def test_real_machine_listing_returns_structured_windows(self):
        payload = json.loads(window_manager_mod.run({"action": "list_windows"}))
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 0)
        self.assertEqual(payload["count"], len(payload["windows"]))
        for window in payload["windows"]:
            self.assertIsInstance(window["hwnd"], int)
            self.assertIsInstance(window["title"], str)
            self.assertIsInstance(window["pid"], int)
            self.assertIsInstance(window["process"], str)
            self.assertEqual(set(window["rect"]), {"left", "top", "width", "height"})
        hwnds = [w["hwnd"] for w in payload["windows"]]
        self.assertEqual(len(hwnds), len(set(hwnds)))


class WindowManagerRunTests(unittest.TestCase):
    def test_activate_requires_hwnd_or_title(self):
        result = window_manager_mod.run({"action": "activate_window"})
        self.assertIn("requires hwnd or title", result)

    def test_unknown_action_is_rejected(self):
        self.assertIn("error", window_manager_mod.run({"action": "fly_to_moon"}))

    def test_unmatched_title_reports_window_not_found(self):
        with mock.patch.object(window_manager_mod, "list_visible_windows", lambda: []):
            payload = json.loads(
                window_manager_mod.run({"action": "activate_window", "title": "no such window"})
            )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "WINDOW_NOT_FOUND")

    def test_ambiguous_title_lists_candidates_without_activating(self):
        candidates = [
            {"hwnd": 11, "title": "Test A", "pid": 1, "process": "a.exe", "rect": {}},
            {"hwnd": 22, "title": "Test B", "pid": 2, "process": "b.exe", "rect": {}},
        ]
        with mock.patch.object(
            window_manager_mod, "list_visible_windows", lambda: candidates
        ), mock.patch.object(
            window_manager_mod, "activate_window_safely",
            side_effect=AssertionError("must not activate on ambiguity"),
        ) as activate:
            payload = json.loads(
                window_manager_mod.run({"action": "activate_window", "title": "test"})
            )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "AMBIGUOUS_TARGET")
        self.assertEqual([c["hwnd"] for c in payload["candidates"]], [11, 22])
        activate.assert_not_called()

    def test_unique_title_match_activates_resolved_hwnd(self):
        matches = [
            {"hwnd": TARGET_HWND, "title": "Solo Editor", "pid": 1,
             "process": "editor.exe", "rect": {}},
        ]
        with mock.patch.object(
            window_manager_mod, "list_visible_windows", lambda: matches
        ), mock.patch.object(
            window_manager_mod, "activate_window_safely",
            return_value={"ok": True, "hwnd": TARGET_HWND},
        ) as activate:
            payload = json.loads(
                window_manager_mod.run({"action": "activate_window", "title": "solo"})
            )
        self.assertTrue(payload["ok"])
        activate.assert_called_once_with(TARGET_HWND)

    def test_non_windows_platform_is_rejected(self):
        with mock.patch("sys.platform", "linux"):
            result = window_manager_mod.run({"action": "list_windows"})
        self.assertIn("only supported on Windows", result)


@WIN32_ONLY
class RealClipboardRoundTripTests(unittest.TestCase):
    def test_text_round_trip_preserves_unicode(self):
        old = clipboard_mod.get_clipboard_text()
        payload = "Phase 10A 原子剪贴板 ✓ 中文与ASCII 123\nsecond line\ttab"
        try:
            result = json.loads(clipboard_mod.run({"action": "set_text", "text": payload}))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["chars"], len(payload))
            back = json.loads(clipboard_mod.run({"action": "get_text"}))
            self.assertTrue(back["ok"], back)
            self.assertEqual(back["text"], payload)
        finally:
            if old.get("ok") and old.get("text"):
                json.loads(clipboard_mod.run({"action": "set_text", "text": old["text"]}))

    def test_empty_string_round_trip(self):
        result = json.loads(clipboard_mod.run({"action": "set_text", "text": ""}))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["bytes"], 2)  # just the UTF-16 NUL terminator
        back = json.loads(clipboard_mod.run({"action": "get_text"}))
        self.assertTrue(back["ok"], back)
        self.assertEqual(back["text"], "")


def _fake_clip_apis(set_data_result=1, high_handle=0x123456789ABC):
    """Fake (user32, kernel32) with a real backing buffer for pointer safety."""
    user32 = mock.Mock()
    kernel32 = mock.Mock()
    buf = ctypes.create_string_buffer(256)

    user32.OpenClipboard.return_value = 1
    user32.CloseClipboard.return_value = 1
    user32.EmptyClipboard.return_value = 1
    user32.IsClipboardFormatAvailable.return_value = 1
    user32.SetClipboardData.return_value = set_data_result
    user32.GetClipboardData.return_value = high_handle
    user32.SendInput.return_value = 4

    kernel32.GlobalAlloc.return_value = high_handle
    kernel32.GlobalLock.side_effect = lambda h_mem: ctypes.addressof(buf)
    kernel32.GlobalUnlock.return_value = 1
    kernel32.GlobalSize.side_effect = lambda h_mem: ctypes.sizeof(buf)
    return user32, kernel32, buf, high_handle


class ClipboardMockedTests(unittest.TestCase):
    def _patch_apis(self, **kwargs):
        user32, kernel32, buf, high_handle = _fake_clip_apis(**kwargs)
        patcher = mock.patch.object(
            clipboard_mod, "_bind_clipboard_apis", lambda: (user32, kernel32)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return user32, kernel32, buf, high_handle

    def test_64bit_pointer_restype_declarations(self):
        user32, kernel32 = clipboard_mod._bind_clipboard_apis()
        for name in ("GetClipboardData", "SetClipboardData"):
            self.assertIs(getattr(user32, name).restype, ctypes.c_void_p, name)
        for name in ("GlobalAlloc", "GlobalLock"):
            self.assertIs(getattr(kernel32, name).restype, ctypes.c_void_p, name)
        self.assertIs(kernel32.GlobalAlloc.argtypes[1], ctypes.c_size_t)
        self.assertIs(user32.SetClipboardData.argtypes[1], ctypes.c_void_p)

    def test_successful_write_transfers_ownership_to_kernel(self):
        user32, kernel32, buf, _ = self._patch_apis()
        result = clipboard_mod.set_clipboard_text("中文A")
        self.assertTrue(result["ok"])
        self.assertEqual(result["chars"], 3)
        self.assertEqual(result["bytes"], 8)
        self.assertEqual(buf.raw[:8], "中文A".encode("utf-16-le") + b"\x00\x00")
        kernel32.GlobalFree.assert_not_called()
        user32.CloseClipboard.assert_called_once()

    def test_failed_set_clipboard_data_frees_memory(self):
        user32, kernel32, _, high_handle = self._patch_apis(set_data_result=0)
        result = clipboard_mod.set_clipboard_text("data")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "SET_CLIPBOARD_DATA_FAILED")
        kernel32.GlobalFree.assert_called_once_with(high_handle)
        user32.CloseClipboard.assert_called_once()

    def test_alloc_failure_frees_nothing_and_closes_clipboard(self):
        user32, kernel32, _, _ = self._patch_apis()
        kernel32.GlobalAlloc.return_value = 0
        result = clipboard_mod.set_clipboard_text("data")
        self.assertEqual(result["error"], "GLOBAL_ALLOC_FAILED")
        kernel32.GlobalFree.assert_not_called()
        user32.CloseClipboard.assert_called_once()

    def test_read_returns_utf16_text_from_high_pointer(self):
        user32, kernel32, buf, high_handle = self._patch_apis()
        payload = "读取剪贴板 ok"
        raw = payload.encode("utf-16-le") + b"\x00\x00"
        ctypes.memmove(ctypes.addressof(buf), raw, len(raw))
        kernel32.GlobalSize.side_effect = lambda h_mem: len(raw)
        result = clipboard_mod.get_clipboard_text()
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], payload)
        self.assertEqual(result["chars"], len(payload))
        kernel32.GlobalUnlock.assert_called_once_with(high_handle)
        user32.CloseClipboard.assert_called_once()

    def test_read_without_text_format_returns_empty_ok(self):
        user32, _, _, _ = self._patch_apis()
        user32.IsClipboardFormatAvailable.return_value = 0
        result = clipboard_mod.get_clipboard_text()
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "")

    def test_read_with_null_data_handle_reports_error(self):
        user32, _, _, _ = self._patch_apis()
        user32.GetClipboardData.return_value = 0
        result = clipboard_mod.get_clipboard_text()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "GET_CLIPBOARD_DATA_NULL")

    def test_spinlock_retries_with_stepped_backoff(self):
        user32, _, _, _ = self._patch_apis()
        user32.OpenClipboard.side_effect = [0, 0, 0, 0, 1]
        sleeps = []
        with mock.patch.object(clipboard_mod.time, "sleep", sleeps.append):
            self.assertTrue(clipboard_mod._open_clipboard_spinlock(user32))
        self.assertEqual(sleeps, [0.020, 0.040, 0.060, 0.080])
        self.assertEqual(user32.OpenClipboard.call_count, 5)

    def test_spinlock_gives_up_after_retry_budget(self):
        user32, _, _, _ = self._patch_apis()
        user32.OpenClipboard.return_value = 0
        sleeps = []
        with mock.patch.object(clipboard_mod.time, "sleep", sleeps.append):
            self.assertFalse(clipboard_mod._open_clipboard_spinlock(user32, retries=3))
        self.assertEqual(len(sleeps), 2)

    def test_locked_clipboard_reports_timeout(self):
        user32, _, _, _ = self._patch_apis()
        user32.OpenClipboard.return_value = 0
        with mock.patch.object(clipboard_mod.time, "sleep", lambda *_: None):
            result = clipboard_mod.set_clipboard_text("x")
        self.assertEqual(result["error"], "CLIPBOARD_LOCKED_TIMEOUT")
        user32.EmptyClipboard.assert_not_called()

    def test_paste_option_sends_ctrl_v_sequence(self):
        user32, _, _, _ = self._patch_apis()
        payload = json.loads(
            clipboard_mod.run({"action": "set_text", "text": "hi", "paste": True})
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["pasted"])
        user32.SendInput.assert_called_once()
        self.assertEqual(user32.SendInput.call_args.args[0], 4)

    def test_paste_defaults_to_false(self):
        user32, _, _, _ = self._patch_apis()
        payload = json.loads(clipboard_mod.run({"action": "set_text", "text": "hi"}))
        self.assertTrue(payload["ok"])
        self.assertNotIn("pasted", payload)
        user32.SendInput.assert_not_called()

    def test_run_rejects_unknown_action_and_missing_text(self):
        self.assertIn("action must be get_text|set_text", clipboard_mod.run({"action": "x"}))
        self.assertIn("text required", clipboard_mod.run({"action": "set_text"}))

    def test_non_windows_platform_is_rejected(self):
        with mock.patch("sys.platform", "linux"):
            result = clipboard_mod.run({"action": "get_text"})
        self.assertIn("only supported on Windows", result)


if __name__ == "__main__":
    unittest.main()
