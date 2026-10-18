"""
Simulate mouse movement or clicks.
"""
from __future__ import annotations

import sys
import time

DESCRIPTION = "Simulate mouse movement or clicks."
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "click|dblclick|move|down|up|scroll"},
        "x": {"type": "integer", "description": "X coordinate"},
        "y": {"type": "integer", "description": "Y coordinate"},
        "button": {"type": "string", "description": "left|right|middle"},
        "clicks": {"type": "integer", "description": "Click count"},
        "dy": {"type": "integer", "description": "Scroll delta"},
    },
    "required": [],
}


def tool_mouse(
    action: str = "click",
    x: int = 0,
    y: int = 0,
    button: str = "left",
    clicks: int = 1,
    dy: int = 0,
) -> str:
    """Screen coordinates: absolute desktop pixels, origin top-left."""
    if sys.platform != "win32":
        return "error: mouse only on Windows"
    action = (action or "click").strip().lower()
    button = (button or "left").strip().lower()
    try:
        import ctypes

        user32 = ctypes.windll.user32
        x, y = int(x), int(y)
        clicks = max(1, min(int(clicks or 1), 5))
        dy = int(dy or 0)
        LEFTDOWN, LEFTUP = 0x0002, 0x0004
        RIGHTDOWN, RIGHTUP = 0x0008, 0x0010
        MIDDLEDOWN, MIDDLEUP = 0x0020, 0x0040
        WHEEL = 0x0800
        down_up = {
            "left": (LEFTDOWN, LEFTUP),
            "right": (RIGHTDOWN, RIGHTUP),
            "middle": (MIDDLEDOWN, MIDDLEUP),
        }.get(button, (LEFTDOWN, LEFTUP))

        def go():
            user32.SetCursorPos(x, y)

        if action in ("move", "mousemove"):
            go()
            return f"mouse move -> ({x},{y})"

        if action in ("click", "dblclick", "double_click"):
            n = 2 if action in ("dblclick", "double_click") else clicks
            go()
            time.sleep(0.02)
            for _ in range(n):
                user32.mouse_event(down_up[0], 0, 0, 0, 0)
                time.sleep(0.02)
                user32.mouse_event(down_up[1], 0, 0, 0, 0)
                time.sleep(0.05)
            return f"mouse {action} {button} x{n} @ ({x},{y})"

        if action == "down":
            go()
            user32.mouse_event(down_up[0], 0, 0, 0, 0)
            return f"mouse down {button} @ ({x},{y})"

        if action in ("up", "mouseup"):
            go()
            user32.mouse_event(down_up[1], 0, 0, 0, 0)
            return f"mouse up {button} @ ({x},{y})"

        if action in ("scroll", "wheel"):
            go()
            user32.mouse_event(WHEEL, 0, 0, int(dy) * 120, 0)
            return f"mouse scroll dy={dy} @ ({x},{y})"

        return "error: action must be move|click|dblclick|down|up|scroll"
    except Exception as e:
        return f"error: {e}"


HANDLER = tool_mouse
