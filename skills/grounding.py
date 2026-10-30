"""
Dual-modal adaptive grounding locator (Phase 10B).

Stage 1 penetrates the foreground window's UIA control tree semantically
(AutomationId -> exact Name -> case-insensitive substring) and returns the
matched interactive leaf's physical center with zero visual tokens.
Chromium/WebView2 windows that expose at most 5 effective nodes receive a
pure-ctypes MSAA wake pulse (WM_GETOBJECT 0x003D on the
Chrome_RenderWidgetHostHWND child plus AccessibleObjectFromWindow with
OBJID_CLIENT) and the tree is re-extracted 15 ms later, materializing the
DOM-level UIA tree without restarting the browser.

Stage 1.5 narrows the search space: when no interactive leaf matches but a
parent container (ToolBar, Pane, ...) whose name contains the target does,
its physical rect becomes the anchor rect for the visual fallback, shrinking
the search area by 70-90%.

Stage 2 engages whenever the anemia heuristics fire (self-drawn engine class
blacklist, hollow client-area container, low effective leaf count, low
spatial density) or the semantic search simply misses: the window's raw
physical frame is captured with mss, cropped, Lanczos-upscaled and overlaid
with the physical-offset ruler grid via skills/pic_zoom.py, and the slice
plus its affine projection metadata are returned so the caller can map the
model's slice reading back onto the virtual desktop.

The latest UIA snapshot is always persisted in the ui_tree cache format, so
any returned control_id is directly clickable through the mouse skill.
"""
from __future__ import annotations

import ctypes
import fnmatch
import json
import sys
import time
from ctypes import wintypes
from typing import Any

from PIL import Image

from core.tools import foreground_window, probe_desktop_session_state
from skills import pic_zoom
from skills import ui_tree

DESCRIPTION = (
    "Locate target interactive UI elements with dual-modal semantic-visual grounding, "
    "Chromium accessibility wake-up, and sub-pixel coordinate refinement."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": "Target element label, name, or text to match (e.g. '保存', 'Search').",
        },
        "role": {
            "type": "string",
            "description": "Optional UI element role filter (e.g. 'Button', 'Edit', 'CheckBox', 'MenuItem').",
        },
        "auto_id": {
            "type": "string",
            "description": "Optional AutomationId for exact matching.",
        },
        "candidate_box": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Optional rough bounding box [left, top, right, bottom] in window coordinates for visual zoom fallback.",
        },
        "zoom_factor": {
            "type": "integer",
            "description": "Zoom factor for visual slice fallback (default 3, range 2-4).",
        },
    },
    "required": ["target"],
}

UI_TREE_CACHE_PATH = ui_tree.UI_TREE_CACHE_PATH

_MAX_TREE_DEPTH = 5
_MAX_TREE_CONTROLS = 80
_EXTRACT_TIMEOUT_SEC = 0.8
_WAKE_SETTLE_MS = 15
_WAKE_TIMEOUT_MS = 200
_MIN_LEAF_COUNT = 3
_MIN_DENSITY_PER_MEGAPIXEL = 5.0
_HOLLOW_AREA_RATIO_NUM = 7  # hollow threshold: container area >= 7/10 of the client area
_HOLLOW_AREA_RATIO_DEN = 10
_MIN_RECT_EDGE_PX = 4
_DEFAULT_FALLBACK_CROP_PX = 640

_CHROMIUM_TOP_CLASS_PREFIX = "Chrome_WidgetWin"
_CHROMIUM_RENDERER_CLASS = "Chrome_RenderWidgetHostHWND"
_WM_GETOBJECT = 0x003D
_OBJID_CLIENT = 0xFFFFFFFC
_IID_IACCESSIBLE = "{618736E0-3C3D-11CF-810C-00AA00389B71}"

_SELF_DRAWN_CLASS_PATTERNS = [
    "Qt5QWindow*",
    "Qt6QWindow*",
    "SDL_app",
    "GHOST_WindowClass",
    "SunAwtFrame",
    "WeChatMainWndForPC",
]
_INTERACTIVE_ROLES = {
    "Button", "Calendar", "CheckBox", "ComboBox", "DataItem", "Document",
    "Edit", "Hyperlink", "ListItem", "MenuItem", "RadioButton", "Slider",
    "Spinner", "SplitButton", "TabItem", "TreeItem",
}
_CONTAINER_ANCHOR_ROLES = {
    "Pane", "ToolBar", "Group", "MenuBar", "Menu", "Tab", "StatusBar",
    "Document", "Custom", "Window",
}
_HOLLOW_CONTAINER_ROLES = {"Pane", "Custom", "Document"}


def _user32_api():
    return ctypes.windll.user32


def _oleacc_api():
    oleacc = ctypes.windll.oleacc
    oleacc.AccessibleObjectFromWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long
    return oleacc


def _window_class_name(hwnd: int) -> str:
    user32 = _user32_api()
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
        return buf.value or ""
    except Exception:
        return ""


def _client_area_px(hwnd: int) -> tuple[int, int]:
    """Client area in physical pixels (the process runs Per-Monitor V2 aware)."""
    try:
        rect = wintypes.RECT()
        if _user32_api().GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return int(rect.right) - int(rect.left), int(rect.bottom) - int(rect.top)
    except Exception:
        pass
    return 0, 0


def wake_chromium_accessibility(hwnd: int, settle_ms: int = _WAKE_SETTLE_MS) -> str:
    """Deliver the MSAA wake pulse to a lazy Chromium/WebView2 window.

    Messages the Chrome_RenderWidgetHostHWND child with WM_GETOBJECT(0x003D)
    and requests OBJID_CLIENT through oleacc.AccessibleObjectFromWindow; both
    set Chromium's assistive-technology perception flag so the renderer
    materializes its DOM-level UIA tree. The caller must re-extract the UIA
    tree after ``settle_ms``.
    """
    user32 = _user32_api()
    try:
        user32.FindWindowExW.restype = wintypes.HWND
        user32.FindWindowExW.argtypes = [
            wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ]
        renderer = int(user32.FindWindowExW(
            wintypes.HWND(hwnd), None, _CHROMIUM_RENDERER_CLASS, None
        ) or 0)
    except Exception:
        return "wake skipped: FindWindowExW failed"
    if not renderer:
        return "wake skipped: no Chrome_RenderWidgetHostHWND child"

    SMTO_BLOCK = 0x0001
    SMTO_ABORTIFHUNG = 0x0002
    getobject_result = ctypes.c_size_t(0)
    try:
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_size_t,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_size_t
        user32.SendMessageTimeoutW(
            wintypes.HWND(renderer),
            _WM_GETOBJECT,
            0,
            1,
            SMTO_BLOCK | SMTO_ABORTIFHUNG,
            wintypes.UINT(_WAKE_TIMEOUT_MS),
            ctypes.byref(getobject_result),
        )
    except Exception:
        pass

    hr = -1
    accessible = ctypes.c_void_p()
    try:
        iid = ui_tree._GUID.from_str(_IID_IACCESSIBLE)
        hr = _oleacc_api().AccessibleObjectFromWindow(
            wintypes.HWND(renderer),
            ctypes.c_uint32(_OBJID_CLIENT),
            ctypes.byref(iid),
            ctypes.byref(accessible),
        )
    except Exception:
        hr = -1
    if hr == 0 and accessible.value:
        ui_tree._com_release(accessible)

    time.sleep(max(0, int(settle_ms)) / 1000.0)
    try:
        hr_code = int(hr) & 0xFFFFFFFF
    except Exception:
        hr_code = 0
    return (
        f"wake pulse delivered to Chrome_RenderWidgetHostHWND 0x{renderer:X} "
        f"(AccessibleObjectFromWindow hr=0x{hr_code:08X})"
    )


def _extract_controls(hwnd: int) -> tuple[list[dict[str, Any]], str]:
    """Extract the UIA control tree; returns (controls, error_reason)."""
    sink, timed_out, aborted = ui_tree._run_mta_extraction(
        hwnd, _MAX_TREE_DEPTH, _MAX_TREE_CONTROLS, True, _EXTRACT_TIMEOUT_SEC
    )
    controls = sink.get("controls", [])
    if aborted:
        return controls, "aborted"
    if timed_out and not controls:
        return controls, "uia_watchdog_timeout"
    if "error" in sink and not controls:
        return controls, f"uia_extraction_failed: {sink['error']}"
    return controls, ""


def _persist_ui_snapshot(hwnd: int, controls: list[dict[str, Any]]) -> None:
    """Persist the snapshot with full overlay geometry so mouse control_id and Web Canvas stay aligned."""
    if hasattr(ui_tree, "_window_projection"):
        proj = ui_tree._window_projection(hwnd)
        origin = proj.get("origin", [0, 0])
        dpi_scale = proj.get("dpi_scale", 1.0)
        title = proj.get("window_title", "")
    else:
        rect = ui_tree.window_rect_ex(hwnd) if hasattr(ui_tree, "window_rect_ex") else None
        origin = [int(rect.left), int(rect.top)] if rect is not None else [0, 0]
        dpi_scale = float(ui_tree.get_window_dpi_scale(hwnd) or 1.0) if hasattr(ui_tree, "get_window_dpi_scale") else 1.0
        title = ""
    payload = {
        "hwnd": hwnd,
        "window_title": title,
        "origin": origin,
        "dpi_scale": dpi_scale,
        "count": len(controls),
        "controls": controls,
    }
    try:
        UI_TREE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        UI_TREE_CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _rect_of(control: dict[str, Any]) -> list[int]:
    rect = control.get("rect")
    if isinstance(rect, (list, tuple)) and len(rect) == 4:
        return [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])]
    return [0, 0, 0, 0]


def _rect_area(rect: list[int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _rect_valid(control: dict[str, Any]) -> bool:
    rect = _rect_of(control)
    return (
        rect[2] - rect[0] >= _MIN_RECT_EDGE_PX
        and rect[3] - rect[1] >= _MIN_RECT_EDGE_PX
    )


def _rect_center(rect: list[int]) -> tuple[int, int]:
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def _rect_contains_center(outer: list[int], inner: list[int]) -> bool:
    cx, cy = _rect_center(inner)
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _is_interactive_leaf(control: dict[str, Any]) -> bool:
    return control.get("role") in _INTERACTIVE_ROLES and _rect_valid(control)


def _effective_leaf_count(controls: list[dict[str, Any]]) -> int:
    """Interactive leaves with a non-empty Name/AutomationId (density dimension)."""
    return sum(
        1
        for control in controls
        if _is_interactive_leaf(control)
        and (control.get("name") or control.get("automation_id"))
    )


def _anemic_verdict(
    window_class: str, controls: list[dict[str, Any]], client_w: int, client_h: int
) -> str:
    """Anemia heuristics over the extracted tree; "" means rich enough.

    Dimensions checked in order: (1) self-drawn engine class blacklist,
    (2) hollow client-area container (a single Pane/Custom/Document covering
    >= 70% of the client area with no child controls), (3) fewer than
    _MIN_LEAF_COUNT effective interactive leaves, (4) leaf density below
    _MIN_DENSITY_PER_MEGAPIXEL. The Chromium lazy-tree dimension is handled
    separately by wake_chromium_accessibility before this verdict runs.
    """
    for pattern in _SELF_DRAWN_CLASS_PATTERNS:
        if fnmatch.fnmatch(window_class or "", pattern):
            return f"self_drawn_class: '{window_class}' matches blacklist pattern '{pattern}'"

    client_area = max(0, int(client_w)) * max(0, int(client_h))
    for control in controls:
        role = control.get("role")
        if role not in _HOLLOW_CONTAINER_ROLES:
            continue
        rect = _rect_of(control)
        if _rect_area(rect) * _HOLLOW_AREA_RATIO_DEN < client_area * _HOLLOW_AREA_RATIO_NUM:
            continue
        has_children = any(
            other is not control and _rect_contains_center(rect, _rect_of(other))
            for other in controls
        )
        if not has_children:
            coverage_pct = _rect_area(rect) * 100 // max(1, client_area)
            return (
                f"hollow_container: {role} rect={rect} covers ~{coverage_pct}% "
                "of the client area with no child controls"
            )

    leaves = _effective_leaf_count(controls)
    if leaves < _MIN_LEAF_COUNT:
        return f"low_leaf_count: {leaves} effective interactive leaves < {_MIN_LEAF_COUNT}"
    if client_area > 0:
        density = leaves / (client_area / 1_000_000)
        if density < _MIN_DENSITY_PER_MEGAPIXEL:
            return (
                f"low_density: {density:.1f} leaves/megapixel < "
                f"{_MIN_DENSITY_PER_MEGAPIXEL} over a {client_w}x{client_h} client area"
            )
    return ""


def _role_matches(control: dict[str, Any], role: str) -> bool:
    return not role or (control.get("role") or "").lower() == role.lower()


def _match_leaf(
    controls: list[dict[str, Any]], target: str, role: str, auto_id: str
) -> tuple[dict[str, Any] | None, str, int]:
    """Three-tier semantic retrieval; returns (best_control, tier, match_count).

    Tier order: exact AutomationId (the provided auto_id, or target itself),
    exact Name (role-validated when role is given), case-insensitive
    substring. The smallest-area hit wins an ambiguous tier deterministically.
    """
    candidates = [c for c in controls if _is_interactive_leaf(c)]
    lowered = target.lower()

    auto_hits = [
        c for c in candidates
        if (c.get("automation_id") or "") and _role_matches(c, role)
        and ((auto_id and c["automation_id"] == auto_id) or c["automation_id"] == target)
    ]
    exact_hits = [
        c for c in candidates
        if (c.get("name") or "") == target and _role_matches(c, role)
    ]
    substring_hits = [
        c for c in candidates
        if lowered in (c.get("name") or "").lower() and _role_matches(c, role)
    ]
    for tier, hits in (
        ("automation_id", auto_hits),
        ("exact_name", exact_hits),
        ("substring", substring_hits),
    ):
        if hits:
            best = min(hits, key=lambda c: _rect_area(_rect_of(c)))
            return best, tier, len(hits)
    return None, "", 0


def _find_anchor_container(controls: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    """Stage 1.5: the smallest matching parent container becomes the anchor."""
    lowered = target.lower()
    best = None
    best_area = 0
    for control in controls:
        if control.get("role") not in _CONTAINER_ANCHOR_ROLES:
            continue
        name = control.get("name") or ""
        if lowered not in name.lower():
            continue
        rect = _rect_of(control)
        area = _rect_area(rect)
        if area <= 0:
            continue
        if best is None or area < best_area:
            best, best_area = control, area
    return best


def _grab_window_frame(left: int, top: int, width: int, height: int) -> Image.Image:
    import mss

    with mss.MSS() as sct:
        raw = sct.grab({
            "left": int(left), "top": int(top),
            "width": int(width), "height": int(height),
        })
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def _default_crop_box(win_w: int, win_h: int) -> tuple[int, int, int, int]:
    """Centered fallback crop when no candidate box or anchor is available."""
    box_w = min(win_w, _DEFAULT_FALLBACK_CROP_PX)
    box_h = min(win_h, _DEFAULT_FALLBACK_CROP_PX)
    c_l = (win_w - box_w) // 2
    c_t = (win_h - box_h) // 2
    return c_l, c_t, c_l + box_w, c_t + box_h


def _stage2_visual_slice(
    stage2_options: dict[str, Any],
    controls: list[dict[str, Any]],
    window_class: str,
    reason: str,
    wake_status: str,
    started: float,
) -> str:
    """Capture the window frame and return the labeled grid slice + metadata."""
    frame = foreground_window()
    if frame is None:
        return "error: grounding could not obtain the foreground window frame for visual fallback"
    region, title = frame
    win_left, win_top = int(region["left"]), int(region["top"])
    win_w, win_h = int(region["width"]), int(region["height"])

    anchor = _find_anchor_container(controls, stage2_options["target"])
    anchor_line = ""
    if stage2_options.get("candidate_box"):
        crop_box = tuple(int(v) for v in stage2_options["candidate_box"])
        box_source = "candidate_box"
    elif anchor is not None:
        a_rect = _rect_of(anchor)
        crop_box = (
            a_rect[0] - win_left, a_rect[1] - win_top,
            a_rect[2] - win_left, a_rect[3] - win_top,
        )
        box_source = f'anchor {anchor.get("role")} "{anchor.get("name", "")}"'
        narrowed_pct = max(0, round(100 * (1 - _rect_area(a_rect) / max(1, win_w * win_h))))
        anchor_line = (
            f"  stage1.5 anchor: narrowed={narrowed_pct}% rect={a_rect} "
            "(visual search area shrunk accordingly)"
        )
    else:
        crop_box = _default_crop_box(win_w, win_h)
        box_source = "default_center"

    raw = _grab_window_frame(win_left, win_top, win_w, win_h)
    zoomed, meta = pic_zoom.crop_and_render_grid_slice(
        raw, win_left, win_top, crop_box, int(stage2_options["zoom_factor"])
    )

    lines = [
        f"[GROUNDING_VISUAL_SLICE] class='{window_class}' reason={reason} "
        f"box={box_source} elapsed_ms={int((time.perf_counter() - started) * 1000)}",
    ]
    if anchor_line:
        lines.append(anchor_line)
    if wake_status:
        lines.append(f"  chromium wake: {wake_status}")
    lines.extend([
        f"  slice: {meta['saved_path'] or pic_zoom.SCREEN_ZOOM_PATH} "
        f"({meta['crop_phys_size'][0]}x{meta['crop_phys_size'][1]} phys -> "
        f"{meta['zoomed_size'][0]}x{meta['zoomed_size'][1]} @ zoom={meta['zoom_factor']})",
        f"  meta: {json.dumps(meta, ensure_ascii=False)}",
        "  read the +X/+Y physical offset ruler labels on the slice, take the target's",
        "  cell center (u, v), then map back with "
        "pic_zoom.map_zoomed_to_global_physical(u, v, meta) and pass the result",
        "  as (x, y) to the mouse skill. Grid intersections map exactly: "
        "global = global_crop_origin + labeled offset.",
    ])
    if title:
        lines.append(f'  window: "{title}"')
    return "\n".join(lines)


def run(args: dict, runtime: dict | None = None) -> str:
    """Locate ``target`` via UIA semantics first, then a labeled grid slice.

    ``runtime`` is reserved for a future model-side refinement channel; the
    skill stays self-contained and never blocks on a model call.
    """
    if sys.platform != "win32":
        return "error: grounding is only supported on Windows"
    ok_desktop, desktop_message = probe_desktop_session_state()
    if not ok_desktop:
        return f"error: {desktop_message}"

    options = args or {}
    target = str(options.get("target") or "").strip()
    if not target:
        return "error: target is required (e.g. '保存', 'Search')"
    role = str(options.get("role") or "").strip()
    auto_id = str(options.get("auto_id") or "").strip()
    candidate_box = options.get("candidate_box")
    if candidate_box is not None:
        try:
            candidate_box = [int(v) for v in candidate_box]
            if len(candidate_box) != 4:
                raise ValueError("candidate_box needs exactly 4 values")
        except (TypeError, ValueError):
            return "error: candidate_box must be [left, top, right, bottom] integers"
    try:
        zoom_factor = max(2, min(4, int(options.get("zoom_factor") or 3)))
    except (TypeError, ValueError):
        return "error: zoom_factor must be an integer"

    started = time.perf_counter()
    hwnd = ui_tree._foreground_hwnd()
    if not hwnd:
        return "error: no active foreground window found"
    window_class = _window_class_name(hwnd)

    controls, extract_error = _extract_controls(hwnd)
    if extract_error == "aborted":
        return "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'grounding' was interrupted by operator."

    wake_status = ""
    if (
        (window_class or "").startswith(_CHROMIUM_TOP_CLASS_PREFIX)
        and _effective_leaf_count(controls) <= 5
    ):
        wake_status = wake_chromium_accessibility(hwnd)
        controls, extract_error = _extract_controls(hwnd)
        if extract_error == "aborted":
            return "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'grounding' was interrupted by operator."

    _persist_ui_snapshot(hwnd, controls)

    leaf, tier, match_count = _match_leaf(controls, target, role, auto_id)
    if leaf is not None:
        rect = _rect_of(leaf)
        cx, cy = _rect_center(rect)
        control_id = leaf.get("id")
        automation_note = (
            f' automation_id="{leaf.get("automation_id")}"' if leaf.get("automation_id") else ""
        )
        return "\n".join([
            f"[GROUNDING_HIT] mode=uia tier={tier} matched={match_count} "
            f"elapsed_ms={int((time.perf_counter() - started) * 1000)} visual_tokens=0",
            f'  target="{target}"' + (f" role={role}" if role else "")
            + (f" auto_id={auto_id}" if auto_id else ""),
            f'  control_id={control_id} {leaf.get("role")} "{leaf.get("name", "")}"{automation_note}',
            f"  rect={rect} center=({cx}, {cy})  # global physical px",
            f"  next: mouse click x={cx} y={cy} (or control_id={control_id})",
        ])

    client_w, client_h = _client_area_px(hwnd)
    reason = _anemic_verdict(window_class, controls, client_w, client_h) or "no_semantic_match"
    return _stage2_visual_slice(
        {
            "target": target,
            "candidate_box": candidate_box,
            "zoom_factor": zoom_factor,
        },
        controls,
        window_class,
        reason,
        wake_status,
        started,
    )
