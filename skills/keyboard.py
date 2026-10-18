"""
Simulate keyboard typing or shortcut key presses.
"""
from __future__ import annotations

import sys
import time

DESCRIPTION = "Simulate keyboard typing or shortcut key presses."
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "type|key|hotkey"},
        "text": {"type": "string", "description": "Text to type"},
        "key": {"type": "string", "description": "Key to press e.g. enter, tab, f5"},
        "keys": {"type": "string", "description": "Hotkey combo e.g. ctrl+c"},
    },
    "required": [],
}

_VK = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "cmd": 0x5B,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
}


def _key_event(vk: int, up: bool = False) -> None:
    import ctypes

    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _resolve_vk(name: str) -> int | None:
    n = (name or "").strip().lower()
    if not n:
        return None
    if n in _VK:
        return _VK[n]
    if len(n) == 1:
        import ctypes

        r = ctypes.windll.user32.VkKeyScanW(ord(n))
        if r == -1:
            return None
        return r & 0xFF
    if n.startswith("f") and n[1:].isdigit():
        return 0x70 + int(n[1:]) - 1
    return None


def tool_keyboard(action: str = "type", text: str = "", key: str = "", keys: str = "") -> str:
    if sys.platform != "win32":
        return "error: keyboard only on Windows"
    action = (action or "type").strip().lower()
    try:
        import ctypes

        if action in ("type", "text", "write"):
            if not text:
                return "error: text required for type"
            for ch in text:
                if ch == "\n":
                    _key_event(0x0D, False)
                    _key_event(0x0D, True)
                    continue
                if ch == "\t":
                    _key_event(0x09, False)
                    _key_event(0x09, True)
                    continue
                vk_scan = ctypes.windll.user32.VkKeyScanW(ord(ch))
                if vk_scan == -1:
                    continue
                vk = vk_scan & 0xFF
                shift = (vk_scan >> 8) & 1
                if shift:
                    _key_event(0x10, False)
                _key_event(vk, False)
                _key_event(vk, True)
                if shift:
                    _key_event(0x10, True)
                time.sleep(0.008)
            return f"typed {len(text)} chars"

        if action in ("key", "press"):
            k = key or keys
            vk = _resolve_vk(k)
            if vk is None:
                return f"error: unknown key: {k}"
            _key_event(vk, False)
            time.sleep(0.02)
            _key_event(vk, True)
            return f"key press {k}"

        if action in ("hotkey", "combo"):
            parts = [p for p in (keys or key).replace("+", " ").split() if p]
            if not parts:
                return "error: keys required e.g. ctrl+c"
            vks = []
            for p in parts:
                vk = _resolve_vk(p)
                if vk is None:
                    return f"error: unknown key in hotkey: {p}"
                vks.append(vk)
            for vk in vks:
                _key_event(vk, False)
                time.sleep(0.02)
            for vk in reversed(vks):
                _key_event(vk, True)
                time.sleep(0.02)
            return f"hotkey {'+'.join(parts)}"

        return "error: action must be type|key|hotkey"
    except Exception as e:
        return f"error: {e}"


HANDLER = tool_keyboard
