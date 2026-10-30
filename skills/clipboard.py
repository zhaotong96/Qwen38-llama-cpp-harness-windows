"""
Atomic Unicode text access through the native Windows clipboard.

``CF_UNICODETEXT`` (13) payloads travel as UTF-16LE inside a
``GMEM_MOVEABLE | GMEM_ZEROINIT`` global memory block. Three invariants keep
this module safe on 64-bit Windows:

* 64-bit pointer typing — ``GlobalAlloc``, ``GlobalLock`` and
  ``GetClipboardData`` declare ``restype = ctypes.c_void_p`` so high heap
  addresses are never truncated to 32 bits (the classic ``0xC0000005``
  access-violation crash).
* Kernel ownership transfer — once ``SetClipboardData`` succeeds the global
  block belongs to the system clipboard; ``GlobalFree`` is only called in
  the failure paths before ownership was transferred.
* Contention spinlock — ``OpenClipboard`` is retried with a 20ms stepped
  backoff to ride out transient locks held by Clipboard History / RDP.

All Win32 bindings are lazy (inside function scope) to satisfy the
``core.skill_sandbox`` AST top-level zero-Call purity theorem.
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import wintypes

DESCRIPTION = (
    "Read or write Unicode text atomically via the native Windows clipboard "
    "(CF_UNICODETEXT) with a contention spinlock and 64-bit-safe pointer "
    "typing. Optionally synthesize Ctrl+V after a write to paste the payload "
    "in one shot: zero per-char delay and immune to Chinese IME interception, "
    "unlike char-by-char keyboard typing."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["get_text", "set_text"],
            "description": "Clipboard operation: get_text or set_text.",
        },
        "text": {
            "type": "string",
            "description": "Unicode text payload to write when action is set_text.",
        },
        "paste": {
            "type": "boolean",
            "description": (
                "After a successful set_text, synthesize Ctrl+V to paste the "
                "text into the focused window (IME-immune, zero delay)."
            ),
        },
    },
    "required": ["action"],
}


def _bind_clipboard_apis():
    """Bind 64-bit safe Win32 Clipboard & Global Memory APIs inside function scope."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL

    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL

    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p

    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p

    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    return user32, kernel32


def _open_clipboard_spinlock(user32, retries: int = 5, backoff_sec: float = 0.020) -> bool:
    """Spinlock with 20ms stepped backoff to handle temporary locks by Clipboard History / RDP."""
    for attempt in range(retries):
        if user32.OpenClipboard(None):
            return True
        if attempt < retries - 1:
            time.sleep(backoff_sec * (attempt + 1))
    return False


def set_clipboard_text(text: str) -> dict:
    """Write UTF-16LE null-terminated string into CF_UNICODETEXT (13) safely."""
    user32, kernel32 = _bind_clipboard_apis()
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    GMEM_ZEROINIT = 0x0040

    encoded = str(text).encode("utf-16-le") + b"\x00\x00"
    byte_len = len(encoded)

    if not _open_clipboard_spinlock(user32, retries=5, backoff_sec=0.020):
        return {"ok": False, "error": "CLIPBOARD_LOCKED_TIMEOUT"}

    h_mem = None
    ownership_transferred = False
    try:
        if not user32.EmptyClipboard():
            return {"ok": False, "error": "EMPTY_CLIPBOARD_FAILED"}

        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, byte_len)
        if not h_mem:
            return {"ok": False, "error": "GLOBAL_ALLOC_FAILED"}

        locked_ptr = kernel32.GlobalLock(h_mem)
        if not locked_ptr:
            return {"ok": False, "error": "GLOBAL_LOCK_FAILED"}

        try:
            ctypes.memmove(locked_ptr, encoded, byte_len)
        finally:
            kernel32.GlobalUnlock(h_mem)

        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            return {"ok": False, "error": "SET_CLIPBOARD_DATA_FAILED"}

        ownership_transferred = True
        return {"ok": True, "chars": len(str(text)), "bytes": byte_len}
    finally:
        if h_mem and not ownership_transferred:
            kernel32.GlobalFree(h_mem)
        user32.CloseClipboard()


def get_clipboard_text() -> dict:
    """Read UTF-16LE string from CF_UNICODETEXT (13) with bounded buffer safety."""
    user32, kernel32 = _bind_clipboard_apis()
    CF_UNICODETEXT = 13

    if not _open_clipboard_spinlock(user32, retries=5, backoff_sec=0.020):
        return {"ok": False, "error": "CLIPBOARD_LOCKED_TIMEOUT"}

    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return {"ok": True, "text": "", "chars": 0}

        h_data = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_data:
            return {"ok": False, "error": "GET_CLIPBOARD_DATA_NULL"}

        byte_size = int(kernel32.GlobalSize(h_data))
        locked_ptr = kernel32.GlobalLock(h_data)
        if not locked_ptr:
            return {"ok": False, "error": "GLOBAL_LOCK_READ_FAILED"}

        try:
            if byte_size > 0:
                raw = ctypes.string_at(locked_ptr, byte_size)
                text = raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
            else:
                text = ctypes.wstring_at(locked_ptr)
            return {"ok": True, "text": text, "chars": len(text)}
        finally:
            kernel32.GlobalUnlock(h_data)
    finally:
        user32.CloseClipboard()


def _send_ctrl_v_paste(user32) -> bool:
    """Synthesize a full Ctrl+V press via SendInput: one atomic IME-transparent paste."""
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    INPUT_KEYBOARD = 1
    VK_CONTROL = 0x11
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x0002

    sequence = [(VK_CONTROL, False), (VK_V, False), (VK_V, True), (VK_CONTROL, True)]
    events = (INPUT * len(sequence))()
    for index, (vk, is_up) in enumerate(sequence):
        events[index].type = INPUT_KEYBOARD
        events[index].u.ki.wVk = vk
        if is_up:
            events[index].u.ki.dwFlags = KEYEVENTF_KEYUP
    sent = user32.SendInput(len(sequence), ctypes.byref(events), ctypes.sizeof(INPUT))
    return int(sent) == len(sequence)


def run(args: dict, runtime: dict | None = None) -> str:
    if sys.platform != "win32":
        return "error: clipboard is only supported on Windows"
    args = args or {}
    action = str(args.get("action", "")).strip().lower()
    try:
        if action == "set_text":
            if "text" not in args:
                return "error: text required for set_text"
            result = set_clipboard_text(args.get("text") or "")
            if result.get("ok") and args.get("paste"):
                user32, _ = _bind_clipboard_apis()
                result["pasted"] = _send_ctrl_v_paste(user32)
            return json.dumps(result, ensure_ascii=False)

        if action == "get_text":
            return json.dumps(get_clipboard_text(), ensure_ascii=False)

        return "error: action must be get_text|set_text"
    except Exception as e:
        return f"error: {e}"
