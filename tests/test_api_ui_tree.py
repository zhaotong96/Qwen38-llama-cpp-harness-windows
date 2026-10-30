"""Phase 10C: zero-build Web console gateway projections.

Covers the /tasks/<id>/ui_tree and /tasks/<id>/rewind_ledger read-only
endpoints, the /screens screenshot projection backing the canvas overlay,
and the SPEC §13.2 zero-build red line over the web/ directory.
"""
from __future__ import annotations

import json
import re
import tempfile
import urllib.request
import unittest
from pathlib import Path
from unittest import mock

from core import api as api_mod
from tests.test_api import ApiCase, call


def fetch_raw(base: str, path: str):
    request = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


class UiTreeEndpointTests(ApiCase):
    """GET /tasks/<id>/ui_tree must project the UIA cache with overlay geometry."""

    def setUp(self):
        super().setUp()
        self.cache_file = self.state / "ui_tree_last.json"
        self._stack.enter_context(mock.patch.object(api_mod, "UI_TREE_CACHE", self.cache_file))

    def test_returns_origin_and_dpi_scale(self):
        self.cache_file.write_text(
            json.dumps(
                {
                    "hwnd": 999,
                    "count": 1,
                    "window_title": "Demo Window",
                    "origin": [-100, 50],
                    "dpi_scale": 1.5,
                    "controls": [
                        {
                            "id": 1,
                            "depth": 2,
                            "role": "Button",
                            "name": "Save",
                            "automation_id": "btnSave",
                            "class_name": "Button",
                            "rect": [10, 20, 110, 60],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code, payload = call(self.base, "GET", "/tasks/task_ui/ui_tree")
        self.assertEqual(code, 200)
        self.assertEqual(payload["origin"], [-100, 50])
        self.assertEqual(payload["dpi_scale"], 1.5)
        self.assertEqual(payload["hwnd"], 999)
        self.assertEqual(payload["window_title"], "Demo Window")
        self.assertFalse(payload["anemic"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["controls"][0]["role"], "Button")
        self.assertEqual(payload["controls"][0]["rect"], [10, 20, 110, 60])

    def test_missing_cache_is_anemic_with_default_geometry(self):
        code, payload = call(self.base, "GET", "/tasks/task_ui/ui_tree")
        self.assertEqual(code, 200)
        self.assertTrue(payload["anemic"])
        self.assertEqual(payload["controls"], [])
        self.assertEqual(payload["origin"], [0, 0])
        self.assertEqual(payload["dpi_scale"], 1.0)

    def test_malformed_cache_falls_back_to_empty_projection(self):
        self.cache_file.write_text("{not-json", encoding="utf-8")
        code, payload = call(self.base, "GET", "/tasks/task_ui/ui_tree")
        self.assertEqual(code, 200)
        self.assertTrue(payload["anemic"])

    def test_invalid_task_identifier_is_404(self):
        code, payload = call(self.base, "GET", "/tasks/%2e%2e/ui_tree")
        self.assertEqual(code, 404)
        self.assertIn("error", payload)


class UiTreeCacheProjectionTests(unittest.TestCase):
    """skills/ui_tree.run persists origin / dpi_scale / window_title for the overlay."""

    CONTROLS = [
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

    def _run_with_mocked_window(self, tmp: Path, rect):
        from skills import ui_tree as ui_tree_mod

        cache = tmp / "ui_tree_last.json"
        with mock.patch.object(ui_tree_mod, "probe_desktop_session_state", lambda: (True, "ok")), \
                mock.patch.object(ui_tree_mod, "_foreground_hwnd", lambda: 999), \
                mock.patch.object(
                    ui_tree_mod, "_probe_hwnd_responsive", lambda hwnd, timeout_ms=150: (True, "ok")
                ), \
                mock.patch.object(
                    ui_tree_mod, "_run_mta_extraction", lambda *args: ({"controls": self.CONTROLS}, False, False)
                ), \
                mock.patch.object(ui_tree_mod, "UI_TREE_CACHE_PATH", cache), \
                mock.patch.object(ui_tree_mod, "window_rect_ex", lambda hwnd: rect), \
                mock.patch.object(ui_tree_mod, "get_window_dpi_scale", lambda hwnd: 1.75):
            text = ui_tree_mod.run({})
            return text, json.loads(cache.read_text(encoding="utf-8"))

    def test_cache_payload_contains_projection_fields(self):
        from ctypes import wintypes

        rect = wintypes.RECT()
        rect.left, rect.top, rect.right, rect.bottom = -100, 50, 400, 400
        with tempfile.TemporaryDirectory() as tmp:
            text, payload = self._run_with_mocked_window(Path(tmp), rect)
        self.assertEqual(payload["origin"], [-100, 50])
        self.assertEqual(payload["dpi_scale"], 1.75)
        self.assertEqual(payload["hwnd"], 999)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["controls"][0]["role"], "Button")
        self.assertIn("window_title", payload)
        self.assertIn('[ID: 1] Button "Save"', text)
        self.assertIn("Center: (60, 40)", text)

    def test_window_projection_falls_back_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = self._run_with_mocked_window(Path(tmp), None)
        self.assertEqual(payload["origin"], [0, 0])
        self.assertEqual(payload["dpi_scale"], 1.75)


class RewindLedgerEndpointTests(ApiCase):
    """GET /tasks/<id>/rewind_ledger returns the causal rewind JSON array."""

    def write_ledger(self, task_id: str, records) -> Path:
        task_dir = self.tasks_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "rewind_ledger.jsonl"
        lines = [json.dumps(record, ensure_ascii=False) for record in records]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_missing_ledger_returns_empty_array(self):
        code, payload = call(self.base, "GET", "/tasks/task_rw/rewind_ledger")
        self.assertEqual(code, 200)
        self.assertEqual(payload, [])

    def test_returns_structured_causal_records(self):
        record = {
            "timestamp": "2026-10-27T10:00:00",
            "from_branch": "main",
            "from_head": "n_000025_abc123",
            "target_node_id": "n_000012_def456",
            "anchor_node_id": "n_000012_def456",
            "new_branch": "fix_alt_path",
            "lesson_node_id": "n_000026_lesson",
            "failed_lesson": "registry path was read-only",
            "new_plan": "use per-user hive instead",
            "touched_files": ["skills/patch.py"],
            "plan_hash": "deadbeef00",
        }
        self.write_ledger("task_rw", [record])
        code, payload = call(self.base, "GET", "/tasks/task_rw/rewind_ledger")
        self.assertEqual(code, 200)
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["from_branch"], "main")
        self.assertEqual(payload[0]["from_head"], "n_000025_abc123")
        self.assertEqual(payload[0]["new_branch"], "fix_alt_path")
        self.assertEqual(payload[0]["failed_lesson"], "registry path was read-only")
        self.assertEqual(payload[0]["touched_files"], ["skills/patch.py"])

    def test_blank_and_malformed_lines_are_tolerated(self):
        task_dir = self.tasks_root / "task_rw"
        task_dir.mkdir(parents=True, exist_ok=True)
        record = {"from_branch": "main", "new_branch": "fix_alt_path", "failed_lesson": "dead end"}
        (task_dir / "rewind_ledger.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n\n", encoding="utf-8"
        )
        code, payload = call(self.base, "GET", "/tasks/task_rw/rewind_ledger")
        self.assertEqual(code, 200)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["new_branch"], "fix_alt_path")

    def test_invalid_task_identifier_is_404(self):
        code, payload = call(self.base, "GET", "/tasks/%2e%2e/rewind_ledger")
        self.assertEqual(code, 404)
        self.assertIn("error", payload)


class GatewayIndexTests(ApiCase):
    def test_index_lists_phase_10c_endpoints(self):
        code, payload = call(self.base, "GET", "/")
        self.assertEqual(code, 200)
        joined = "\n".join(payload["endpoints"])
        for fragment in ("/tasks/<id>/ui_tree", "/tasks/<id>/rewind_ledger", "/screens"):
            self.assertIn(fragment, joined)


class ScreensProjectionTests(ApiCase):
    """GET /screens projects captured screenshots for the canvas overlay."""

    def setUp(self):
        super().setUp()
        self.screens_dir = self.state / "shots"
        self.screens_dir.mkdir(parents=True, exist_ok=True)
        self._stack.enter_context(mock.patch.object(api_mod, "SCREENSHOTS_DIR", self.screens_dir))

    def test_lists_and_serves_screenshots(self):
        (self.screens_dir / "screen_001.jpg").write_bytes(b"\xff\xd8fake-jpeg-one")
        (self.screens_dir / "screen_002.jpg").write_bytes(b"\xff\xd8fake-jpeg-two")
        code, payload = call(self.base, "GET", "/screens")
        self.assertEqual(code, 200)
        names = [screen["name"] for screen in payload["screens"]]
        self.assertEqual(names, ["screen_001.jpg", "screen_002.jpg"])
        self.assertEqual(payload["screens"][1]["url"], "/screens/screen_002.jpg")

        code, headers, body = fetch_raw(self.base, "/screens/screen_002.jpg")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"\xff\xd8fake-jpeg-two")
        self.assertTrue(headers.get("Content-Type", "").startswith("image/jpeg"))

    def test_rejects_traversal_and_non_jpeg(self):
        code, payload, _ = fetch_raw(self.base, "/screens/..%2Fproviders.json")
        self.assertEqual(code, 400)
        code, payload, _ = fetch_raw(self.base, "/screens/providers.json")
        self.assertEqual(code, 400)
        code, payload, _ = fetch_raw(self.base, "/screens/screen_999.jpg")
        self.assertEqual(code, 404)

    def test_listing_includes_size_and_mtime_metadata(self):
        (self.screens_dir / "screen_001.jpg").write_bytes(b"\xff\xd8meta")
        code, payload = call(self.base, "GET", "/screens")
        self.assertEqual(code, 200)
        entry = payload["screens"][0]
        self.assertEqual(entry["size"], len(b"\xff\xd8meta"))
        self.assertRegex(entry["mtime"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

BUNDLER_ARTIFACTS = (
    "package.json",
    "package-lock.json",
    "node_modules",
    "webpack.config.js",
    "vite.config.js",
    "rollup.config.js",
    "tsconfig.json",
    ".babelrc",
    "yarn.lock",
    "pnpm-lock.yaml",
)


class ZeroBuildFrontendTests(unittest.TestCase):
    """SPEC §13.2: web/ loads natively in the browser with no build chain."""

    def test_no_bundler_artifacts_under_web(self):
        hits = []
        for path in WEB_ROOT.rglob("*"):
            if path.name in BUNDLER_ARTIFACTS or "node_modules" in path.parts:
                hits.append(str(path.relative_to(WEB_ROOT)))
        self.assertEqual(hits, [], f"build-chain artifacts found in web/: {hits}")

    def test_all_web_js_files_are_native_es_modules(self):
        js_files = sorted(WEB_ROOT.glob("*.js"))
        self.assertTrue(js_files)
        for path in js_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("require(", text, f"{path.name} must not use CommonJS require")
            self.assertNotIn("module.exports", text, f"{path.name} must not use CommonJS exports")
            self.assertTrue(
                re.search(r"^\s*(import|export)\b", text, re.MULTILINE),
                f"{path.name} must use native ES module import/export syntax",
            )

    def test_index_html_only_loads_local_module_scripts(self):
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        script_tags = re.findall(r"<script\b[^>]*>", html)
        self.assertTrue(script_tags)
        for tag in script_tags:
            if "src=" in tag:
                self.assertIn('type="module"', tag, f"script tag must be a native module: {tag}")
                src = re.search(r'src="([^"]+)"', tag).group(1)
                self.assertTrue(src.startswith("/web/"), f"script must be served locally: {src}")
        self.assertNotIn("http://", html.replace("http://127.0.0.1", ""))
        self.assertNotIn("https://cdn", html)

    def test_store_js_stays_within_the_120_line_budget(self):
        lines = (WEB_ROOT / "store.js").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 120, "store.js must stay within the 120-line budget")

    def test_store_uses_microtask_batching_and_area_pre_sort(self):
        store_source = (WEB_ROOT / "store.js").read_text(encoding="utf-8")
        self.assertIn("queueMicrotask", store_source)
        self.assertIn("rolePriority", store_source)
        self.assertIn("(b.depth || 0) - (a.depth || 0)", store_source)

    def test_canvas_overlay_aligns_to_physical_pixel_grid(self):
        overlay_source = (WEB_ROOT / "canvas_overlay.js").read_text(encoding="utf-8")
        self.assertIn("Math.floor(cssX * dpr) + 0.5", overlay_source)
        self.assertIn("_hitTestPhysical", overlay_source)
        self.assertIn("devicePixelRatio", overlay_source)

    def test_dag_timeline_consumes_rewind_ledger_fields(self):
        timeline_source = (WEB_ROOT / "dag_timeline.js").read_text(encoding="utf-8")
        for field in ("from_branch", "from_head", "new_branch", "failed_lesson", "touched_files"):
            self.assertIn(field, timeline_source)

    def test_dag_timeline_draws_dashed_arrow_arc(self):
        timeline_source = (WEB_ROOT / "dag_timeline.js").read_text(encoding="utf-8")
        self.assertIn('customElements.define("agent-dag-timeline"', timeline_source)
        self.assertIn("stroke-dasharray", timeline_source)
        self.assertIn("marker-end", timeline_source)
        self.assertIn("[回溯", timeline_source)

    def test_index_html_mounts_overlay_and_timeline(self):
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("<agent-canvas-overlay", html)
        self.assertIn("<agent-dag-timeline", html)
        self.assertIn("dag-timeline-drawer", html)
        self.assertIn("btn-dag-timeline", html)

    def test_styles_define_screen_stage_and_rewind_drawer(self):
        css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
        for selector in (".screen-stage", ".dag-drawer", ".rewind-arc-svg", ".rewind-file-chip"):
            self.assertIn(selector, css)

    def test_app_js_wires_overlay_projections_and_intervention_fill(self):
        app_source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("projectConsoleOverlays", app_source)
        self.assertIn('"/screens"', app_source)
        self.assertIn("/ui_tree", app_source)
        self.assertIn("/rewind_ledger", app_source)
        self.assertIn("slice:intervention", app_source)
        self.assertIn("setUiTreeSnapshot", app_source)

    def test_store_canvas_directive_strings_match_contract(self):
        store_source = (WEB_ROOT / "store.js").read_text(encoding="utf-8")
        self.assertIn('mouse(action="click", control_id=${controlId})', store_source)
        self.assertIn("目标为自绘区域", store_source)
        self.assertIn("extends EventTarget", store_source)
        self.assertIn("state:change", store_source)

    def test_canvas_overlay_registers_element_and_click_pipeline(self):
        overlay_source = (WEB_ROOT / "canvas_overlay.js").read_text(encoding="utf-8")
        self.assertIn('customElements.define("agent-canvas-overlay"', overlay_source)
        self.assertIn("selectCanvasTarget", overlay_source)
        self.assertIn("ResizeObserver", overlay_source)
        self.assertIn("requestAnimationFrame", overlay_source)

    def test_dag_timeline_links_ledger_section_4(self):
        timeline_source = (WEB_ROOT / "dag_timeline.js").read_text(encoding="utf-8")
        self.assertIn("factLedgerSection4", timeline_source)
        self.assertIn("第 4 节", timeline_source)
        self.assertIn("slice:ledger", timeline_source)


if __name__ == "__main__":
    unittest.main()
