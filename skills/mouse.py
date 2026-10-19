"""
Simulate mouse movement or clicks with Per-Monitor V2 DPI and multi-monitor
virtual-desktop accuracy.
"""
from __future__ import annotations

import json
import sys
import time

from core.models import AGENT_DIR

DESCRIPTION = (
    "Simulate mouse movement or clicks using physical desktop coordinates "
    "or a ui_tree control_id."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "click|dblclick|move|down|up|scroll"},
        "x": {"type": "integer", "description": "Physical desktop X coordinate (supports negative multi-monitor coords)"},
        "y": {"type": "integer", "description": "Physical desktop Y coordinate (supports negative multi-monitor coords)"},
        "control_id": {"type": "integer", "description": "Optional control ID from ui_tree to click its exact center"},
        "button": {"type": "string", "description": "left|right|middle"},
        "clicks": {"type": "integer", "description": "Click count"},
        "dy": {"type": "integer", "description": "Scroll delta"},
    },
    "required": [],
}

UI_TREE_CACHE_PATH = AGENT_DIR / "ui_tree_last.json"


def _resolve_control_center(control_id: int) -> tuple[int, int, str] | None:
    """Resolve a control_id from the latest disk-persisted ui_tree snapshot."""
    if not UI_TREE_CACHE_PATH.is_file():
        return None
    try:
        data = json.loads(UI_TREE_CACHE_PATH.read_text(encoding="utf-8"))
        for item in data.get("controls", []):
            if int(item.get("id", -1)) == int(control_id):
                rect = item["rect"]
                center_x = int((rect[0] + rect[2]) // 2)
                center_y = int((rect[1] + rect[3]) // 2)
                label = f'{item.get("role", "Control")} "{item.get("name", "")}"'
                return center_x, center_y, label
    except Exception:
        pass
    return None


def _virtual_desktop_norm(px: int, py: int, user32) -> tuple[int, int, int, int]:
    """Map physical desktop pixels to the [0, 65535] MOUSEEVENTF_VIRTUALDESK space."""
    origin_x = int(user32.GetSystemMetrics(76))
    origin_y = int(user32.GetSystemMetrics(77))
    width = max(2, int(user32.GetSystemMetrics(78)))
    height = max(2, int(user32.GetSystemMetrics(79)))
    clamped_x = max(origin_x, min(int(px), origin_x + width - 1))
    clamped_y = max(origin_y, min(int(py), origin_y + height - 1))
    norm_x = ((clamped_x - origin_x) * 65535 + (width - 1) // 2) // (width - 1)
    norm_y = ((clamped_y - origin_y) * 65535 + (height - 1) // 2) // (height - 1)
    return clamped_x, clamped_y, norm_x, norm_y


def _virtual_desktop_denorm(norm_x: int, norm_y: int, user32) -> tuple[int, int]:
    """Inverse of ``_virtual_desktop_norm`` for exact round-trip verification."""
    origin_x = int(user32.GetSystemMetrics(76))
    origin_y = int(user32.GetSystemMetrics(77))
    width = max(2, int(user32.GetSystemMetrics(78)))
    height = max(2, int(user32.GetSystemMetrics(79)))
    px = origin_x + int(round(int(norm_x) * (width - 1) / 65535))
    py = origin_y + int(round(int(norm_y) * (height - 1) / 65535))
    return px, py


def tool_mouse(
    action: str = "click",
    x: int = 0,
    y: int = 0,
    control_id: int | None = None,
    button: str = "left",
    clicks: int = 1,
    dy: int = 0,
) -> str:
    """Click physical virtual-desktop pixels, or a semantic ui_tree control center."""
    if sys.platform != "win32":
        return "error: mouse only on Windows"
    action = (action or "click").strip().lower()
    button = (button or "left").strip().lower()
    target_label = ""
    if control_id is not None:
        resolved = _resolve_control_center(int(control_id))
        if resolved is None:
            return (
                f"error: control_id={control_id} not found in .agent/ui_tree_last.json; "
                "run ui_tree first"
            )
        x, y, target_label = resolved

    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass

        cursor_x, cursor_y, norm_x, norm_y = _virtual_desktop_norm(int(x), int(y), user32)
        clicks = max(1, min(int(clicks or 1), 5))
        dy = int(dy or 0)

        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
        MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
        MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
        MOUSEEVENTF_WHEEL = 0x0800
        MOUSEEVENTF_VIRTUALDESK = 0x4000
        MOUSEEVENTF_ABSOLUTE = 0x8000

        down_up = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))

        def go():
            user32.SetCursorPos(cursor_x, cursor_y)
            user32.mouse_event(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                norm_x,
                norm_y,
                0,
                0,
            )

        suffix = f" [control_id={control_id}: {target_label}]" if target_label else ""

        if action in ("move", "mousemove"):
            go()
            return f"mouse move -> ({cursor_x},{cursor_y}){suffix}"

        if action in ("click", "dblclick", "double_click"):
            n = 2 if action in ("dblclick", "double_click") else clicks
            go()
            time.sleep(0.02)
            for _ in range(n):
                user32.mouse_event(down_up[0], 0, 0, 0, 0)
                time.sleep(0.02)
                user32.mouse_event(down_up[1], 0, 0, 0, 0)
                time.sleep(0.05)
            return f"mouse {action} {button} x{n} @ ({cursor_x},{cursor_y}){suffix}"

        if action == "down":
            go()
            user32.mouse_event(down_up[0], 0, 0, 0, 0)
            return f"mouse down {button} @ ({cursor_x},{cursor_y}){suffix}"

        if action in ("up", "mouseup"):
            go()
            user32.mouse_event(down_up[1], 0, 0, 0, 0)
            return f"mouse up {button} @ ({cursor_x},{cursor_y}){suffix}"

        if action in ("scroll", "wheel"):
            go()
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, dy * 120, 0)
            return f"mouse scroll dy={dy} @ ({cursor_x},{cursor_y}){suffix}"

        return "error: action must be move|click|dblclick|down|up|scroll"
    except Exception as e:
        return f"error: {e}"


HANDLER = tool_mouse
