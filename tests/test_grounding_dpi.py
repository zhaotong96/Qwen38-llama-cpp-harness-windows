"""Phase 10B: dual-modal grounding, Chromium accessibility wake-up, anemia
heuristics, grid-slice geometry, and the zero-drift virtual-desktop ceiling
bijection."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from core.skill_sandbox import check_skill_ast, validate_openai_parameters_schema
from skills import grounding as grounding_mod
from skills import mouse as mouse_mod
from skills import pic_zoom as pic_zoom_mod

REPO_ROOT = Path(__file__).resolve().parent.parent


def _control(cid, role, name, rect, automation_id=""):
    return {
        "id": cid,
        "depth": 1,
        "role": role,
        "name": name,
        "automation_id": automation_id,
        "class_name": "",
        "rect": rect,
    }


def _skill_source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


class SpecContractTests(unittest.TestCase):
    def test_grounding_passes_ast_purity_gate(self):
        self.assertEqual(check_skill_ast(_skill_source(grounding_mod)), [])

    def test_pic_zoom_passes_ast_purity_gate(self):
        self.assertEqual(check_skill_ast(_skill_source(pic_zoom_mod)), [])

    def test_grounding_schema_contract(self):
        self.assertTrue(grounding_mod.DESCRIPTION.strip())
        self.assertEqual(validate_openai_parameters_schema(grounding_mod.PARAMETERS), [])
        self.assertEqual(grounding_mod.PARAMETERS["required"], ["target"])
        self.assertTrue(
            {"target", "role", "auto_id", "candidate_box", "zoom_factor"}
            <= set(grounding_mod.PARAMETERS["properties"])
        )

    def test_pic_zoom_schema_contract(self):
        self.assertTrue(pic_zoom_mod.DESCRIPTION.strip())
        self.assertEqual(validate_openai_parameters_schema(pic_zoom_mod.PARAMETERS), [])

    def test_pic_zoom_exports_phase10b_geometry_api(self):
        for name in (
            "crop_and_render_grid_slice",
            "map_zoomed_to_global_physical",
            "virtual_desktop_bijective_norm",
        ):
            self.assertTrue(callable(getattr(pic_zoom_mod, name)), name)
        self.assertTrue(callable(getattr(grounding_mod, "run")))
        self.assertTrue(
            callable(getattr(pic_zoom_mod, "HANDLER")) or callable(getattr(pic_zoom_mod, "run"))
        )


def _hwnd_int(value) -> int:
    """Normalize a wintypes.HWND wrapper (or plain int) to a plain int."""
    return int(getattr(value, "value", value) or 0)


class _FakeUser32FindWindow:
    """user32 fake exposing only the calls the Chromium wake pulse needs."""

    def __init__(self, renderer: int):
        self.renderer = renderer
        self.find_calls = []
        self.send_calls = []

        def find(parent, after, class_name, window_name):
            self.find_calls.append((_hwnd_int(parent), after, class_name, window_name))
            return self.renderer

        def send(hwnd, msg, wparam, lparam, flags, timeout, out_ptr):
            self.send_calls.append((_hwnd_int(hwnd), msg, wparam, lparam))
            return 1

        self.FindWindowExW = find
        self.SendMessageTimeoutW = send


class ChromiumWakeTests(unittest.TestCase):
    def test_wake_pulse_sends_wm_getobject_and_objid_client(self):
        user32 = _FakeUser32FindWindow(renderer=4321)
        oleacc_calls = []

        def acc_from_window(hwnd, objid, riid, ppv):
            oleacc_calls.append((_hwnd_int(hwnd), getattr(objid, "value", objid)))
            return 0  # S_OK with a NULL out pointer keeps the release path dormant

        oleacc = mock.Mock()
        oleacc.AccessibleObjectFromWindow.side_effect = acc_from_window
        with mock.patch.object(grounding_mod, "_user32_api", lambda: user32), \
                mock.patch.object(grounding_mod, "_oleacc_api", lambda: oleacc), \
                mock.patch.object(grounding_mod.time, "sleep", lambda seconds: None):
            status = grounding_mod.wake_chromium_accessibility(1234)

        self.assertEqual(
            user32.find_calls,
            [(1234, None, "Chrome_RenderWidgetHostHWND", None)],
        )
        self.assertEqual(user32.send_calls, [(4321, 0x003D, 0, 1)])
        self.assertEqual(oleacc_calls, [(4321, 0xFFFFFFFC)])
        self.assertIn("wake pulse delivered", status)
        self.assertIn("0x10E1", status)  # renderer hwnd in hex

    def test_wake_skips_without_renderer_child(self):
        user32 = _FakeUser32FindWindow(renderer=0)
        oleacc = mock.Mock()
        with mock.patch.object(grounding_mod, "_user32_api", lambda: user32), \
                mock.patch.object(grounding_mod, "_oleacc_api", lambda: oleacc):
            status = grounding_mod.wake_chromium_accessibility(1234)
        self.assertIn("skipped", status)
        self.assertEqual(user32.send_calls, [])
        oleacc.AccessibleObjectFromWindow.assert_not_called()

    def test_run_reextracts_after_chromium_wake_and_persists(self):
        pre_wake = []
        post_wake = [_control(3, "Button", "保存", [100, 200, 180, 240], "btn-save")]
        extract_mock = mock.Mock(side_effect=[(pre_wake, ""), (post_wake, "")])
        wake_mock = mock.Mock(return_value="wake pulse delivered")
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "ui_tree_last.json"
            with mock.patch.object(grounding_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                    mock.patch.object(grounding_mod.ui_tree, "_foreground_hwnd", lambda: 555), \
                    mock.patch.object(grounding_mod, "_window_class_name", lambda hwnd: "Chrome_WidgetWin_1"), \
                    mock.patch.object(grounding_mod, "_extract_controls", extract_mock), \
                    mock.patch.object(grounding_mod, "wake_chromium_accessibility", wake_mock), \
                    mock.patch.object(grounding_mod, "UI_TREE_CACHE_PATH", cache):
                result = grounding_mod.run({"target": "保存"})
            payload = json.loads(cache.read_text(encoding="utf-8"))

        self.assertEqual(extract_mock.call_count, 2)  # pre-wake scan, then re-extract
        wake_mock.assert_called_once_with(555)
        self.assertIn("[GROUNDING_HIT]", result)
        self.assertIn("tier=exact_name", result)
        self.assertIn("control_id=3", result)
        self.assertIn("center=(140, 220)", result)
        self.assertIn("visual_tokens=0", result)
        self.assertEqual(payload["hwnd"], 555)
        self.assertEqual(payload["count"], 1)

    def test_run_skips_wake_for_non_chromium_window(self):
        controls = [
            _control(1, "Button", "File", [10, 10, 80, 40]),
            _control(2, "Button", "Edit", [90, 10, 160, 40]),
            _control(3, "Button", "View", [170, 10, 240, 40]),
        ]
        extract_mock = mock.Mock(return_value=(controls, ""))
        wake_mock = mock.Mock()
        region = {"left": 0, "top": 0, "width": 800, "height": 600, "hwnd": 9, "dpi_scale": 1.0}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(grounding_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                    mock.patch.object(grounding_mod.ui_tree, "_foreground_hwnd", lambda: 9), \
                    mock.patch.object(grounding_mod, "_window_class_name", lambda hwnd: "Notepad"), \
                    mock.patch.object(grounding_mod, "_extract_controls", extract_mock), \
                    mock.patch.object(grounding_mod, "wake_chromium_accessibility", wake_mock), \
                    mock.patch.object(grounding_mod, "_client_area_px", lambda hwnd: (800, 600)), \
                    mock.patch.object(grounding_mod, "foreground_window", lambda: (region, "Notepad")), \
                    mock.patch.object(
                        grounding_mod,
                        "_grab_window_frame",
                        lambda left, top, width, height: Image.new("RGB", (width, height), (1, 2, 3)),
                    ), \
                    mock.patch.object(pic_zoom_mod, "SCREEN_ZOOM_PATH", Path(tmp) / "zoom.jpg"):
                result = grounding_mod.run({"target": "导出"})
        wake_mock.assert_not_called()
        self.assertEqual(extract_mock.call_count, 1)
        self.assertIn("[GROUNDING_VISUAL_SLICE]", result)
        self.assertIn("no_semantic_match", result)


class AnemicHeuristicsTests(unittest.TestCase):
    def test_self_drawn_class_blacklist(self):
        for window_class in (
            "Qt5QWindowIcon", "Qt6QWindowDual", "SDL_app",
            "GHOST_WindowClass", "SunAwtFrame", "WeChatMainWndForPC",
        ):
            with self.subTest(window_class=window_class):
                verdict = grounding_mod._anemic_verdict(window_class, [], 800, 600)
                self.assertIn("self_drawn_class", verdict)
                self.assertIn(window_class, verdict)

    def test_hollow_single_pane_detected(self):
        pane = _control(1, "Pane", "", [0, 0, 800, 560])
        verdict = grounding_mod._anemic_verdict("SomeFrame", [pane], 800, 600)
        self.assertIn("hollow_container", verdict)
        self.assertIn("Pane", verdict)

    def test_pane_with_children_and_three_leaves_is_rich(self):
        pane = _control(1, "Pane", "", [0, 0, 500, 380])
        controls = [
            pane,
            _control(2, "Button", "OK", [10, 10, 80, 40]),
            _control(3, "Button", "Cancel", [90, 10, 170, 40]),
            _control(4, "Button", "Apply", [180, 10, 260, 40]),
        ]
        self.assertEqual(grounding_mod._anemic_verdict("SomeFrame", controls, 500, 400), "")

    def test_low_leaf_count(self):
        controls = [
            _control(1, "Button", "OK", [10, 10, 80, 40]),
            _control(2, "Button", "Cancel", [90, 10, 170, 40]),
        ]
        verdict = grounding_mod._anemic_verdict("SomeFrame", controls, 800, 600)
        self.assertIn("low_leaf_count", verdict)

    def test_low_density_on_large_client(self):
        controls = [
            _control(1, "Button", "A", [10, 10, 80, 40]),
            _control(2, "Button", "B", [90, 10, 160, 40]),
            _control(3, "Button", "C", [170, 10, 240, 40]),
        ]
        verdict = grounding_mod._anemic_verdict("SomeFrame", controls, 3840, 2160)
        self.assertIn("low_density", verdict)

    def test_empty_tree_is_anemic(self):
        verdict = grounding_mod._anemic_verdict("SomeFrame", [], 800, 600)
        self.assertIn("low_leaf_count", verdict)


class SemanticMatchTests(unittest.TestCase):
    def test_automation_id_tier_wins_over_exact_name(self):
        controls = [
            _control(1, "Button", "保存", [0, 0, 40, 40]),
            _control(2, "Button", "ok-btn", [0, 0, 20, 20], "保存"),
        ]
        leaf, tier, count = grounding_mod._match_leaf(controls, "保存", "", "")
        self.assertEqual(tier, "automation_id")
        self.assertEqual(leaf["id"], 2)
        self.assertEqual(count, 1)

    def test_exact_name_beats_substring(self):
        controls = [
            _control(1, "Button", "保存并退出", [0, 0, 10, 10]),
            _control(2, "Button", "保存", [0, 0, 90, 90]),
        ]
        leaf, tier, _ = grounding_mod._match_leaf(controls, "保存", "", "")
        self.assertEqual(tier, "exact_name")
        self.assertEqual(leaf["id"], 2)

    def test_role_filter_blocks_mismatched_tiers(self):
        controls = [_control(1, "Button", "保存", [0, 0, 40, 40])]
        leaf, tier, count = grounding_mod._match_leaf(controls, "保存", "Edit", "")
        self.assertIsNone(leaf)
        self.assertEqual((tier, count), ("", 0))

    def test_substring_match_is_case_insensitive(self):
        controls = [_control(1, "Edit", "Search Box", [0, 0, 120, 30])]
        leaf, tier, _ = grounding_mod._match_leaf(controls, "search", "", "")
        self.assertEqual(tier, "substring")
        self.assertEqual(leaf["id"], 1)

    def test_smallest_area_wins_when_ambiguous(self):
        controls = [
            _control(1, "Button", "保存", [0, 0, 300, 300]),
            _control(2, "Button", "保存", [0, 0, 50, 50]),
        ]
        leaf, _, count = grounding_mod._match_leaf(controls, "保存", "", "")
        self.assertEqual(count, 2)
        self.assertEqual(leaf["id"], 2)

    def test_tiny_rects_are_not_candidates(self):
        controls = [_control(1, "Button", "保存", [0, 0, 3, 3])]
        leaf, _, _ = grounding_mod._match_leaf(controls, "保存", "", "")
        self.assertIsNone(leaf)


class GridSliceGeometryTests(unittest.TestCase):
    def test_slice_dimensions_meta_and_persistence(self):
        source = Image.new("RGB", (100, 80), (30, 30, 30))
        with tempfile.TemporaryDirectory() as tmp:
            zoom_path = Path(tmp) / "screen_zoom_last.jpg"
            with mock.patch.object(pic_zoom_mod, "SCREEN_ZOOM_PATH", zoom_path):
                zoomed, meta = pic_zoom_mod.crop_and_render_grid_slice(
                    source, -1920, 0, (10, 12, 58, 52), zoom_factor=3, grid_step_phys=16
                )
            self.assertTrue(zoom_path.is_file())
        self.assertEqual(zoomed.size, (144, 120))
        self.assertEqual(meta["win_origin"], [-1920, 0])
        self.assertEqual(meta["crop_offset"], [10, 12])
        self.assertEqual(meta["global_crop_origin"], [-1910, 12])
        self.assertEqual(meta["crop_phys_size"], [48, 40])
        self.assertEqual(meta["zoomed_size"], [144, 120])
        self.assertEqual(meta["zoom_factor"], 3)
        self.assertEqual(meta["grid_step_phys"], 16)
        self.assertEqual(meta["saved_path"], str(zoom_path))

    def test_zoom_factor_clamped_to_2_4(self):
        source = Image.new("RGB", (64, 64), (0, 0, 0))
        for requested, expected in ((1, 2), (9, 4), (3, 3)):
            with self.subTest(requested=requested):
                _, meta = pic_zoom_mod.crop_and_render_grid_slice(
                    source, 0, 0, (0, 0, 64, 64), zoom_factor=requested
                )
                self.assertEqual(meta["zoom_factor"], expected)

    def test_grid_lines_rendered_at_physical_steps(self):
        source = Image.new("RGB", (100, 80), (0, 0, 0))
        zoomed, meta = pic_zoom_mod.crop_and_render_grid_slice(
            source, 0, 0, (0, 0, 100, 80), zoom_factor=2, grid_step_phys=16
        )
        self.assertEqual(meta["crop_phys_size"], [100, 80])
        # vertical ruler columns at phys 0/16/32/... land on zoomed x = phys * 2;
        # row 55 avoids the horizontal ruler label boxes (rows 34-48 and 66-80)
        for zx in (0, 32, 64, 96, 128, 160, 192):
            pixel = zoomed.getpixel((min(zx, zoomed.size[0] - 1), 55))
            self.assertGreaterEqual(pixel[2], 100, f"missing grid line at x={zx}")
            self.assertGreaterEqual(pixel[1], 80, f"grid line not cyan at x={zx}")
        # non-grid pixel stays untouched black
        self.assertEqual(zoomed.getpixel((17, 55)), (0, 0, 0))
        # horizontal ruler rows at phys 0/16/32 land on zoomed y = phys * 2
        for zy in (0, 32, 64):
            pixel = zoomed.getpixel((40, zy))
            self.assertGreaterEqual(pixel[2], 100, f"missing grid line at y={zy}")
        self.assertEqual(zoomed.getpixel((40, 33)), (0, 0, 0))

    def test_offset_labels_overlaid(self):
        source = Image.new("RGB", (200, 120), (255, 255, 255))
        zoomed, _ = pic_zoom_mod.crop_and_render_grid_slice(
            source, 0, 0, (0, 0, 200, 120), zoom_factor=2, grid_step_phys=32
        )
        # the "+32" label box sits at zoomed (66..102, 2..16) and darkens the field
        label_region = [zoomed.getpixel((x, y)) for x in range(66, 103) for y in range(2, 16)]
        plain_region = [zoomed.getpixel((x, y)) for x in range(104, 125) for y in range(2, 16)]
        label_mean = sum(p[0] for p in label_region) / len(label_region)
        plain_mean = sum(p[0] for p in plain_region) / len(plain_region)
        self.assertLess(label_mean, 200)
        self.assertGreater(plain_mean, 240)

    def test_inverse_projection_and_clamps(self):
        meta = {
            "global_crop_origin": [-1910, 12],
            "crop_phys_size": [48, 40],
            "zoom_factor": 3,
        }
        self.assertEqual(
            pic_zoom_mod.map_zoomed_to_global_physical(72.9, 60.2, meta), (-1886, 32)
        )
        self.assertEqual(
            pic_zoom_mod.map_zoomed_to_global_physical(-1000000, -5, meta), (-1910, 12)
        )
        self.assertEqual(
            pic_zoom_mod.map_zoomed_to_global_physical(10000000, 10000000, meta), (-1863, 51)
        )

    def test_grid_offset_label_roundtrip_identity(self):
        # every ruler intersection u = phys * Z maps back to exactly phys
        z = 3
        for phys in range(0, 200):
            self.assertEqual(int((phys * z + 0.5) // z), phys)


class VirtualDesktopBijectionTests(unittest.TestCase):
    @staticmethod
    def _kernel_denorm(norm: int, span: int, origin: int) -> int:
        """win32k floor-truncating fixed-point inverse transform."""
        return origin + (int(norm) * span) // 65535

    def _assert_zero_drift(self, x_virt, y_virt, w_virt, h_virt, x_stride=1, y_stride=1,
                           x_rows=(0,), y_cols=(0,)):
        span_x, span_y = w_virt - 1, h_virt - 1
        for dy in range(0, h_virt, y_stride) if y_stride > 1 else x_rows:
            for dx in range(0, w_virt, x_stride):
                x_global, y_global = x_virt + dx, y_virt + dy
                norm_x, norm_y = pic_zoom_mod.virtual_desktop_bijective_norm(
                    x_global, y_global, x_virt, y_virt, w_virt, h_virt
                )
                self.assertEqual(
                    self._kernel_denorm(norm_x, span_x, x_virt), x_global,
                    f"x drift at {x_global}",
                )
                self.assertEqual(
                    self._kernel_denorm(norm_y, span_y, y_virt), y_global,
                    f"y drift at {y_global}",
                )

    def test_zero_drift_primary_monitor_full_x_sweep(self):
        self._assert_zero_drift(0, 0, 1920, 1080, y_stride=0, x_rows=(0, 540, 1079))
        self._assert_zero_drift(0, 0, 1920, 1080, x_stride=17, y_stride=13)

    def test_zero_drift_negative_secondary_monitor(self):
        # 4K secondary screen anchored left of the primary with a negative origin
        self._assert_zero_drift(-1920, -200, 3840, 1200, y_stride=0, x_rows=(0, 600, 1199))
        self._assert_zero_drift(-1920, -200, 3840, 1200, x_stride=37, y_stride=31)

    def test_zero_drift_full_grid_on_small_desktop(self):
        self._assert_zero_drift(-1920, 0, 800, 600, x_stride=3, y_stride=3)

    def test_norm_endpoints_span_full_u16(self):
        norm_x, norm_y = pic_zoom_mod.virtual_desktop_bijective_norm(-1920, -200, -1920, -200, 3840, 1200)
        self.assertEqual((norm_x, norm_y), (0, 0))
        norm_x, norm_y = pic_zoom_mod.virtual_desktop_bijective_norm(1919, 999, -1920, -200, 3840, 1200)
        self.assertEqual((norm_x, norm_y), (65535, 65535))

    def test_out_of_range_inputs_are_clamped(self):
        norm_x, norm_y = pic_zoom_mod.virtual_desktop_bijective_norm(-99999, 99999, 0, 0, 1920, 1080)
        self.assertEqual((norm_x, norm_y), (0, 65535))

    def test_ceiling_beats_midpoint_norm_on_large_desktop(self):
        # the previous midpoint rounding formula drifts by 1 px under the
        # kernel's floor inverse; the ceiling bijection never does.
        w_virt = h_virt = 40000
        midpoint_drift = 0
        ceiling_drift = 0
        for dx in range(0, w_virt):
            midpoint_norm = (dx * 65535 + (w_virt - 1) // 2) // (w_virt - 1)
            if self._kernel_denorm(midpoint_norm, w_virt - 1, 0) != dx:
                midpoint_drift += 1
            ceiling_norm = (dx * 65535 + (w_virt - 2)) // (w_virt - 1)
            if self._kernel_denorm(ceiling_norm, w_virt - 1, 0) != dx:
                ceiling_drift += 1
        self.assertGreater(midpoint_drift, 0)
        self.assertEqual(ceiling_drift, 0)


class GroundingStage2Tests(unittest.TestCase):
    def test_anemic_class_falls_back_to_grid_slice(self):
        controls = [_control(1, "Pane", "", [0, 0, 800, 560])]
        region = {"left": -1920, "top": 0, "width": 800, "height": 600, "hwnd": 77, "dpi_scale": 1.0}
        with tempfile.TemporaryDirectory() as tmp:
            zoom_path = Path(tmp) / "screen_zoom_last.jpg"
            with mock.patch.object(grounding_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                    mock.patch.object(grounding_mod.ui_tree, "_foreground_hwnd", lambda: 77), \
                    mock.patch.object(grounding_mod, "_window_class_name", lambda hwnd: "WeChatMainWndForPC"), \
                    mock.patch.object(grounding_mod, "_extract_controls", lambda hwnd: (controls, "")), \
                    mock.patch.object(grounding_mod, "_client_area_px", lambda hwnd: (800, 600)), \
                    mock.patch.object(grounding_mod, "foreground_window", lambda: (region, "WeChat")), \
                    mock.patch.object(
                        grounding_mod,
                        "_grab_window_frame",
                        lambda left, top, width, height: Image.new("RGB", (width, height), (10, 20, 30)),
                    ), \
                    mock.patch.object(pic_zoom_mod, "SCREEN_ZOOM_PATH", zoom_path):
                result = grounding_mod.run({
                    "target": "发送",
                    "zoom_factor": 4,
                    "candidate_box": [0, 0, 400, 300],
                })
            self.assertTrue(zoom_path.is_file())

        self.assertIn("[GROUNDING_VISUAL_SLICE]", result)
        self.assertIn("self_drawn_class", result)
        self.assertIn("WeChatMainWndForPC", result)
        self.assertIn('"zoom_factor": 4', result)
        self.assertIn('"global_crop_origin": [-1920, 0]', result)

    def test_anchor_container_narrows_crop(self):
        controls = [
            _control(1, "ToolBar", "工具栏", [100, 50, 400, 130]),
            _control(2, "Button", "File", [110, 60, 180, 90]),
            _control(3, "Button", "Edit", [190, 60, 260, 90]),
            _control(4, "Button", "View", [270, 60, 340, 90]),
        ]
        region = {"left": 0, "top": 0, "width": 800, "height": 600, "hwnd": 9, "dpi_scale": 1.0}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(grounding_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                    mock.patch.object(grounding_mod.ui_tree, "_foreground_hwnd", lambda: 9), \
                    mock.patch.object(grounding_mod, "_window_class_name", lambda hwnd: "AppFrame"), \
                    mock.patch.object(grounding_mod, "_extract_controls", lambda hwnd: (controls, "")), \
                    mock.patch.object(grounding_mod, "_client_area_px", lambda hwnd: (800, 600)), \
                    mock.patch.object(grounding_mod, "foreground_window", lambda: (region, "App")), \
                    mock.patch.object(
                        grounding_mod,
                        "_grab_window_frame",
                        lambda left, top, width, height: Image.new("RGB", (width, height), (0, 0, 0)),
                    ), \
                    mock.patch.object(pic_zoom_mod, "SCREEN_ZOOM_PATH", Path(tmp) / "zoom.jpg"):
                result = grounding_mod.run({"target": "工具栏"})

        self.assertIn("narrowed=95%", result)  # 24000 px toolbar inside a 480000 px window
        self.assertIn('"crop_offset": [100, 50]', result)
        self.assertIn('"global_crop_origin": [100, 50]', result)
        self.assertIn("no_semantic_match", result)
        self.assertIn("anchor ToolBar", result)

    def test_candidate_box_takes_priority_over_anchor(self):
        controls = [_control(1, "ToolBar", "工具栏", [100, 50, 400, 130])]
        region = {"left": 0, "top": 0, "width": 800, "height": 600, "hwnd": 9, "dpi_scale": 1.0}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(grounding_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                    mock.patch.object(grounding_mod.ui_tree, "_foreground_hwnd", lambda: 9), \
                    mock.patch.object(grounding_mod, "_window_class_name", lambda hwnd: "AppFrame"), \
                    mock.patch.object(grounding_mod, "_extract_controls", lambda hwnd: (controls, "")), \
                    mock.patch.object(grounding_mod, "_client_area_px", lambda hwnd: (800, 600)), \
                    mock.patch.object(grounding_mod, "foreground_window", lambda: (region, "App")), \
                    mock.patch.object(
                        grounding_mod,
                        "_grab_window_frame",
                        lambda left, top, width, height: Image.new("RGB", (width, height), (0, 0, 0)),
                    ), \
                    mock.patch.object(pic_zoom_mod, "SCREEN_ZOOM_PATH", Path(tmp) / "zoom.jpg"):
                result = grounding_mod.run({
                    "target": "工具栏",
                    "candidate_box": [200, 100, 500, 300],
                })
        self.assertIn("box=candidate_box", result)
        self.assertIn('"crop_offset": [200, 100]', result)

    def test_missing_target_and_malformed_args_are_rejected(self):
        self.assertIn("error: target is required", grounding_mod.run({}))
        self.assertIn(
            "error: candidate_box",
            grounding_mod.run({"target": "x", "candidate_box": [1, 2]}),
        )

    def test_hit_control_id_resolves_through_mouse_cache(self):
        controls = [_control(3, "Button", "保存", [100, 200, 180, 240])]
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "ui_tree_last.json"
            with mock.patch.object(grounding_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                    mock.patch.object(grounding_mod.ui_tree, "_foreground_hwnd", lambda: 42), \
                    mock.patch.object(grounding_mod, "_window_class_name", lambda hwnd: "Notepad"), \
                    mock.patch.object(grounding_mod, "_extract_controls", lambda hwnd: (controls, "")), \
                    mock.patch.object(grounding_mod, "UI_TREE_CACHE_PATH", cache):
                result = grounding_mod.run({"target": "保存"})
            self.assertIn("[GROUNDING_HIT]", result)
            with mock.patch.object(mouse_mod, "UI_TREE_CACHE_PATH", cache):
                resolved = mouse_mod._resolve_control_center(3)
        self.assertEqual(resolved, (140, 220, 'Button "保存"'))

    def test_non_win32_platform_is_rejected(self):
        with mock.patch.object(sys, "platform", "linux"):
            result = grounding_mod.run({"target": "x"})
        self.assertIn("only supported on Windows", result)


if __name__ == "__main__":
    unittest.main()
