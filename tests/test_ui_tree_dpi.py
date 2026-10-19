"""Phase 8A: DPI/physical-pixel geometry, DWM bounds, and ui_tree guardrails."""
from __future__ import annotations

import ctypes
import json
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from core import tools as tools_mod
from skills import mouse as mouse_mod
from skills import ui_tree as ui_tree_mod


class _FakeUser32:
    """Minimal GetSystemMetrics virtual-desktop fake for geometry unit tests."""

    def __init__(self, origin=(-1920, 0), size=(3840, 1080)):
        self.metrics = {76: origin[0], 77: origin[1], 78: size[0], 79: size[1]}

    def GetSystemMetrics(self, index):
        return self.metrics[index]


class VirtualDesktopGeometryTests(unittest.TestCase):
    def test_negative_origin_round_trip(self):
        fake = _FakeUser32(origin=(-1920, -200), size=(3840, 1200))
        for point in ((-1920, -200), (0, 0), (1919, 999)):
            px, py, norm_x, norm_y = mouse_mod._virtual_desktop_norm(*point, fake)
            self.assertEqual((px, py), point)
            self.assertEqual(mouse_mod._virtual_desktop_denorm(norm_x, norm_y, fake), point)

    def test_norm_space_is_full_u16(self):
        fake = _FakeUser32(origin=(0, 0), size=(1920, 1080))
        _, _, norm_x, norm_y = mouse_mod._virtual_desktop_norm(1919, 1079, fake)
        self.assertEqual((norm_x, norm_y), (65535, 65535))

    def test_out_of_bounds_pixels_are_clamped_not_rejected(self):
        fake = _FakeUser32(origin=(0, 0), size=(1920, 1080))
        px, py, _, _ = mouse_mod._virtual_desktop_norm(-5000, 5000, fake)
        self.assertEqual((px, py), (0, 1079))


class ControlIdResolutionTests(unittest.TestCase):
    def test_resolves_center_from_disk_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "ui_tree_last.json"
            cache.write_text(
                json.dumps(
                    {
                        "controls": [
                            {
                                "id": 2,
                                "role": "Button",
                                "name": "OK",
                                "rect": [100, 200, 180, 240],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(mouse_mod, "UI_TREE_CACHE_PATH", cache):
                self.assertEqual(
                    mouse_mod._resolve_control_center(2),
                    (140, 220, 'Button "OK"'),
                )

    def test_missing_control_reports_cache_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                mouse_mod, "UI_TREE_CACHE_PATH", Path(tmp) / "missing.json"
            ):
                result = mouse_mod.tool_mouse(action="click", control_id=99)
        self.assertIn("ui_tree", result)
        self.assertIn("control_id=99", result)


class ForegroundWindowDwmTests(unittest.TestCase):
    def _fake_apis(self, dwm_ok=True):
        user32 = mock.Mock()
        user32.GetForegroundWindow.return_value = 123
        user32.GetWindowTextLengthW.return_value = 4

        def get_text(hwnd, buffer, size):
            buffer.value = "Test"
            return 4

        user32.GetWindowTextW.side_effect = get_text

        def window_rect(hwnd, rect_ref):
            rect = ctypes.cast(rect_ref, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1000, 800
            return 1

        user32.GetWindowRect.side_effect = window_rect
        user32.GetDpiForWindow.return_value = 144

        dwmapi = mock.Mock()

        def dwm_bounds(hwnd, attribute, rect_ref, size):
            if not dwm_ok:
                return -1
            rect = ctypes.cast(rect_ref, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 100, 100, 500, 400
            return 0

        dwmapi.DwmGetWindowAttribute.side_effect = dwm_bounds
        return user32, dwmapi

    def test_dwm_extended_bounds_are_used(self):
        user32, dwmapi = self._fake_apis(dwm_ok=True)
        with mock.patch.object(tools_mod, "_user32", user32), \
                mock.patch.object(tools_mod, "_dwmapi", dwmapi):
            region, title = tools_mod.foreground_window()
        self.assertEqual(title, "Test")
        self.assertEqual(region["left"], 100)
        self.assertEqual(region["top"], 100)
        self.assertEqual(region["width"], 400)
        self.assertEqual(region["height"], 300)
        self.assertEqual(region["hwnd"], 123)
        self.assertEqual(region["dpi_scale"], 1.5)

    def test_getwindowrect_is_the_fallback_when_dwm_fails(self):
        user32, dwmapi = self._fake_apis(dwm_ok=False)
        with mock.patch.object(tools_mod, "_user32", user32), \
                mock.patch.object(tools_mod, "_dwmapi", dwmapi):
            region, _ = tools_mod.foreground_window()
        self.assertEqual(region["width"], 1000)
        self.assertEqual(region["height"], 800)

    def test_screenshot_reports_origin_and_dpi_scale(self):
        import io

        from PIL import Image

        raw = type("Raw", (), {})()
        raw.size = (200, 100)
        raw.bgra = bytes([0, 0, 0, 255]) * 200 * 100

        class FakeMss:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def grab(self, region):
                return raw

        fake_region = {
            "left": -100,
            "top": 50,
            "width": 200,
            "height": 100,
            "hwnd": 7,
            "dpi_scale": 1.75,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tools_mod, "mss", mock.Mock(MSS=lambda: FakeMss())), \
                    mock.patch.object(tools_mod, "foreground_window", lambda: (fake_region, "Demo")), \
                    mock.patch.object(tools_mod, "SCREEN_PATH", Path(tmp) / "screen.jpg"):
                text, b64 = tools_mod.tool_screenshot()
        self.assertIn("mode=focused_window", text)
        self.assertIn("origin=(-100,50)", text)
        self.assertIn("dpi_scale=1.75", text)
        self.assertTrue(b64)


class UiTreeGuardrailTests(unittest.TestCase):
    def test_no_foreground_window_is_an_error(self):
        with mock.patch.object(ui_tree_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                mock.patch.object(ui_tree_mod, "_foreground_hwnd", lambda: 0):
            result = ui_tree_mod.run({})
        self.assertIn("no active foreground window", result)

    def test_message_pump_probe_rejects_a_non_responding_window(self):
        fake_user32 = mock.Mock()
        fake_user32.IsHungAppWindow.return_value = 0
        fake_user32.SendMessageTimeoutW.return_value = 0
        with mock.patch.object(ui_tree_mod, "_user32_api", lambda: fake_user32):
            started = time.monotonic()
            ok, message = ui_tree_mod._probe_hwnd_responsive(777, timeout_ms=150)
            elapsed = time.monotonic() - started
        self.assertFalse(ok)
        self.assertIn("WM_NULL", message)
        self.assertLess(elapsed, 0.2)

    def test_hung_window_fast_fails_before_com(self):
        with mock.patch.object(ui_tree_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                mock.patch.object(ui_tree_mod, "_foreground_hwnd", lambda: 321), \
                mock.patch.object(
                    ui_tree_mod, "_probe_hwnd_responsive", lambda hwnd, timeout_ms=150: (False, "hung")
                ):
            result = ui_tree_mod.run({})
        self.assertIn("UIA_WATCHDOG_FAST_FAIL", result)

    def test_watchdog_timeout_is_reported(self):
        with mock.patch.object(ui_tree_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                mock.patch.object(ui_tree_mod, "_foreground_hwnd", lambda: 321), \
                mock.patch.object(ui_tree_mod, "_probe_hwnd_responsive", lambda hwnd, timeout_ms=150: (True, "ok")), \
                mock.patch.object(ui_tree_mod, "_run_mta_extraction", lambda *args: ({}, True, False)):
            result = ui_tree_mod.run({"timeout_ms": 500})
        self.assertIn("UIA_WATCHDOG_TIMEOUT", result)

    def test_abort_is_surfaced(self):
        with mock.patch.object(ui_tree_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                mock.patch.object(ui_tree_mod, "_foreground_hwnd", lambda: 321), \
                mock.patch.object(ui_tree_mod, "_probe_hwnd_responsive", lambda hwnd, timeout_ms=150: (True, "ok")), \
                mock.patch.object(ui_tree_mod, "_run_mta_extraction", lambda *args: ({}, False, True)):
            result = ui_tree_mod.run({})
        self.assertIn("[EXECUTION_INTERRUPTED_AND_IGNORED]", result)

    def test_snapshot_is_persisted_and_mouse_clickable(self):
        controls = [
            {
                "id": 1,
                "depth": 1,
                "role": "Button",
                "name": "Save",
                "automation_id": "btnSave",
                "class_name": "Button",
                "rect": [10, 20, 110, 60],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "ui_tree_last.json"
            with mock.patch.object(ui_tree_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                    mock.patch.object(ui_tree_mod, "_foreground_hwnd", lambda: 999), \
                    mock.patch.object(ui_tree_mod, "_probe_hwnd_responsive", lambda hwnd, timeout_ms=150: (True, "ok")), \
                    mock.patch.object(
                        ui_tree_mod, "_run_mta_extraction", lambda *args: ({"controls": controls}, False, False)
                    ), \
                    mock.patch.object(ui_tree_mod, "UI_TREE_CACHE_PATH", cache):
                text = ui_tree_mod.run({})
                payload = json.loads(cache.read_text(encoding="utf-8"))

        self.assertIn('[ID: 1] Button "Save"', text)
        self.assertIn("Center: (60, 40)", text)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["hwnd"], 999)


if __name__ == "__main__":
    unittest.main()
