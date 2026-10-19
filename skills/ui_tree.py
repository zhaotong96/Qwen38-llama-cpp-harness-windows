"""
Lightweight pure-ctypes Windows UI Automation (IUIAutomation) extractor.

The foreground control tree is protected by three layers:
1. a 150 ms WM_NULL message-pump probe that fast-fails hung windows;
2. an independent MTA COM thread with an 800 ms watchdog and abort polling;
3. a bounded BFS TreeWalker (max_depth=4, max_controls=50) with explicit
   COM Release and BSTR SysFreeString cleanup.

The latest snapshot is persisted to ``.agent/ui_tree_last.json`` so the mouse
skill can resolve a semantic ``control_id`` to physical pixel coordinates.
"""
from __future__ import annotations

from collections import deque
import ctypes
import json
import sys
import threading
import time
from typing import Any

from core.models import AGENT_DIR
from core.tools import execution_aborted, probe_desktop_session_state

DESCRIPTION = (
    "Extract the interactive UI Automation control tree (ID, Role, Name, "
    "AutomationId, physical Rect) of the active foreground window using pure "
    "Win32 COM with a non-blocking watchdog."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "max_depth": {
            "type": "integer",
            "description": "Maximum BFS traversal depth (default 4, max 8)",
        },
        "max_controls": {
            "type": "integer",
            "description": "Maximum interactive controls to return (default 50, max 120)",
        },
        "timeout_ms": {
            "type": "integer",
            "description": "Watchdog timeout in milliseconds (default 800, max 3000)",
        },
        "include_non_focusable": {
            "type": "boolean",
            "description": "Include static text/group elements in addition to interactive controls",
        },
    },
    "required": [],
}

UI_TREE_CACHE_PATH = AGENT_DIR / "ui_tree_last.json"

_CONTROL_ROLES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar", 50015: "Slider",
    50016: "Spinner", 50017: "StatusBar", 50018: "Tab", 50019: "TabItem",
    50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50029: "DataItem",
    50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
}
_INTERACTIVE_TYPES = frozenset({
    50000, 50002, 50003, 50004, 50005, 50007, 50011, 50013, 50015, 50016,
    50019, 50024, 50029, 50030, 50031,
})


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, text: str) -> "_GUID":
        import uuid

        value = uuid.UUID(text)
        d1, d2, d3, d4, d5, d6 = value.fields
        data4 = (ctypes.c_ubyte * 8)(d4, d5, *(d6.to_bytes(6, "big")))
        return cls(d1, d2, d3, data4)


CLSID_CUIAutomation = _GUID.from_str("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")
IID_IUIAutomation = _GUID.from_str("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}")


def _user32_api():
    return ctypes.windll.user32


def _ole32_api():
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    return ole32


def _oleaut32_api():
    oleaut32 = ctypes.windll.oleaut32
    oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
    oleaut32.SysFreeString.restype = None
    return oleaut32


def _vtable_fn(obj_ptr: ctypes.c_void_p, index: int, restype, *argtypes):
    vtable = ctypes.cast(
        obj_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(vtable[index])


def _com_release(obj_ptr: ctypes.c_void_p) -> None:
    if obj_ptr and obj_ptr.value:
        try:
            _vtable_fn(obj_ptr, 2, ctypes.c_ulong)(obj_ptr)
        except Exception:
            pass


def _bstr_to_str(bstr_ptr: ctypes.c_void_p, oleaut32) -> str:
    if not bstr_ptr or not bstr_ptr.value:
        return ""
    try:
        return ctypes.wstring_at(bstr_ptr.value).strip()
    finally:
        oleaut32.SysFreeString(bstr_ptr)


def _foreground_hwnd() -> int:
    try:
        return int(_user32_api().GetForegroundWindow() or 0)
    except Exception:
        return 0


def _probe_hwnd_responsive(hwnd: int, timeout_ms: int = 150) -> tuple[bool, str]:
    """Layer 1: fast-fail when the target window's message pump is hung."""
    from ctypes import wintypes

    user32 = _user32_api()
    try:
        user32.IsHungAppWindow.argtypes = [wintypes.HWND]
        user32.IsHungAppWindow.restype = wintypes.BOOL
        if user32.IsHungAppWindow(wintypes.HWND(hwnd)):
            return False, f"window hwnd=0x{hwnd:X} is marked hung by OS (IsHungAppWindow=True)"
    except Exception:
        pass

    result = ctypes.c_size_t(0)
    SMTO_BLOCK = 0x0001
    SMTO_ABORTIFHUNG = 0x0002
    WM_NULL = 0x0000
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
        ok = user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd),
            WM_NULL,
            0,
            0,
            SMTO_BLOCK | SMTO_ABORTIFHUNG,
            wintypes.UINT(timeout_ms),
            ctypes.byref(result),
        )
    except Exception as exc:
        return False, f"SendMessageTimeoutW failed: {exc}"
    if not ok:
        return False, f"window hwnd=0x{hwnd:X} failed to answer WM_NULL within {timeout_ms}ms"
    return True, "ok"


def _extract_tree_worker(
    hwnd: int,
    max_depth: int,
    max_controls: int,
    include_non_focusable: bool,
    deadline: float,
    sink: dict[str, Any],
) -> None:
    """Layers 2 and 3: independent MTA COM thread plus bounded BFS TreeWalker."""
    from ctypes import wintypes

    ole32 = _ole32_api()
    oleaut32 = _oleaut32_api()
    user32 = _user32_api()

    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

    try:
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
    except Exception:
        pass

    ole32.CoInitializeEx(None, 0x0)
    uia = ctypes.c_void_p()
    root_element = ctypes.c_void_p()
    walker = ctypes.c_void_p()
    queue: deque[tuple[ctypes.c_void_p, int]] = deque()

    try:
        CLSCTX_INPROC_SERVER = 0x1
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_CUIAutomation),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_IUIAutomation),
            ctypes.byref(uia),
        )
        if hr != 0 or not uia.value:
            sink["error"] = f"CoCreateInstance(CUIAutomation) failed: HRESULT=0x{hr & 0xFFFFFFFF:08X}"
            return

        element_from_handle = _vtable_fn(
            uia, 6, ctypes.c_long, wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)
        )
        if (
            element_from_handle(uia, wintypes.HWND(hwnd), ctypes.byref(root_element)) != 0
            or not root_element.value
        ):
            sink["error"] = f"ElementFromHandle(0x{hwnd:X}) failed"
            return

        get_control_walker = _vtable_fn(
            uia, 14, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
        )
        if get_control_walker(uia, ctypes.byref(walker)) != 0 or not walker.value:
            sink["error"] = "Get_ControlViewWalker failed"
            return

        first_child = _vtable_fn(
            walker, 4, ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )
        next_sibling = _vtable_fn(
            walker, 6, ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )

        controls: list[dict[str, Any]] = []
        queue.append((root_element, 0))
        root_element = ctypes.c_void_p()

        while queue and len(controls) < max_controls:
            if time.monotonic() >= deadline or execution_aborted():
                sink["truncated_by_watchdog"] = True
                break

            element, depth = queue.popleft()
            try:
                if depth > 0:
                    control_type = ctypes.c_int(0)
                    _vtable_fn(
                        element, 21, ctypes.c_long, ctypes.POINTER(ctypes.c_int)
                    )(element, ctypes.byref(control_type))
                    focusable = ctypes.c_int(0)
                    _vtable_fn(
                        element, 27, ctypes.c_long, ctypes.POINTER(ctypes.c_int)
                    )(element, ctypes.byref(focusable))
                    enabled = ctypes.c_int(1)
                    _vtable_fn(
                        element, 28, ctypes.c_long, ctypes.POINTER(ctypes.c_int)
                    )(element, ctypes.byref(enabled))
                    offscreen = ctypes.c_int(0)
                    _vtable_fn(
                        element, 38, ctypes.c_long, ctypes.POINTER(ctypes.c_int)
                    )(element, ctypes.byref(offscreen))
                    rect = wintypes.RECT()
                    _vtable_fn(
                        element, 43, ctypes.c_long, ctypes.POINTER(wintypes.RECT)
                    )(element, ctypes.byref(rect))

                    width = rect.right - rect.left
                    height = rect.bottom - rect.top
                    interactive = (
                        control_type.value in _INTERACTIVE_TYPES
                    ) or bool(focusable.value)

                    if (
                        enabled.value
                        and not offscreen.value
                        and width >= 4
                        and height >= 4
                        and (interactive or include_non_focusable)
                    ):
                        name_bstr = ctypes.c_void_p()
                        _vtable_fn(
                            element, 23, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
                        )(element, ctypes.byref(name_bstr))
                        name_text = _bstr_to_str(name_bstr, oleaut32)

                        automation_bstr = ctypes.c_void_p()
                        _vtable_fn(
                            element, 29, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
                        )(element, ctypes.byref(automation_bstr))
                        automation_id = _bstr_to_str(automation_bstr, oleaut32)

                        class_bstr = ctypes.c_void_p()
                        _vtable_fn(
                            element, 30, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
                        )(element, ctypes.byref(class_bstr))
                        class_name = _bstr_to_str(class_bstr, oleaut32)

                        if name_text or automation_id or interactive:
                            controls.append({
                                "id": len(controls) + 1,
                                "depth": depth,
                                "role": _CONTROL_ROLES.get(
                                    control_type.value, f"Type_{control_type.value}"
                                ),
                                "name": name_text[:80],
                                "automation_id": automation_id[:40],
                                "class_name": class_name[:40],
                                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                            })

                if depth < max_depth and time.monotonic() < deadline:
                    child = ctypes.c_void_p()
                    if first_child(walker, element, ctypes.byref(child)) == 0 and child.value:
                        sibling_count = 0
                        current = child
                        while (
                            current.value
                            and sibling_count < 64
                            and time.monotonic() < deadline
                        ):
                            sibling_count += 1
                            following = ctypes.c_void_p()
                            has_next = (
                                next_sibling(walker, current, ctypes.byref(following)) == 0
                                and bool(following.value)
                            )
                            queue.append((current, depth + 1))
                            current = following if has_next else ctypes.c_void_p()
            finally:
                _com_release(element)

        sink["controls"] = controls
    except Exception as exc:
        sink["error"] = str(exc)
    finally:
        while queue:
            remaining, _ = queue.popleft()
            _com_release(remaining)
        _com_release(walker)
        _com_release(root_element)
        _com_release(uia)
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


def _run_mta_extraction(
    hwnd: int,
    max_depth: int,
    max_controls: int,
    include_non_focusable: bool,
    timeout_sec: float,
) -> tuple[dict[str, Any], bool, bool]:
    """Run the COM extraction off-thread; return (sink, timed_out, aborted)."""
    deadline = time.monotonic() + timeout_sec
    sink: dict[str, Any] = {}
    worker = threading.Thread(
        target=_extract_tree_worker,
        args=(hwnd, max_depth, max_controls, include_non_focusable, deadline, sink),
        daemon=True,
    )
    worker.start()

    aborted = False
    while worker.is_alive():
        if execution_aborted():
            aborted = True
            break
        if time.monotonic() >= deadline + 0.05:
            break
        worker.join(timeout=0.025)

    timed_out = worker.is_alive() and "controls" not in sink
    return sink, timed_out, aborted


def _format_outline(hwnd: int, controls: list[dict[str, Any]], timeout_ms: int, truncated: bool) -> str:
    lines = [
        f"Foreground HWND=0x{hwnd:X} Controls={len(controls)} "
        "(pass control_id to mouse skill to click center):"
    ]
    for control in controls:
        rect = control["rect"]
        center_x = (rect[0] + rect[2]) // 2
        center_y = (rect[1] + rect[3]) // 2
        automation = (
            f' id="{control["automation_id"]}"' if control.get("automation_id") else ""
        )
        lines.append(
            f'[ID: {control["id"]}] {control["role"]} "{control["name"]}"{automation} '
            f'Rect: ({rect[0]}, {rect[1]}, {rect[2]}, {rect[3]}) '
            f"Center: ({center_x}, {center_y})"
        )
    if truncated:
        lines.append(f"...[truncated at {timeout_ms}ms watchdog budget]")
    return "\n".join(lines)


def run(args: dict, runtime: dict | None = None) -> str:
    if sys.platform != "win32":
        return "error: ui_tree is only supported on Windows"

    ok_desktop, desktop_message = probe_desktop_session_state()
    if not ok_desktop:
        return f"error: {desktop_message}"

    hwnd = _foreground_hwnd()
    if not hwnd:
        return "error: no active foreground window found"

    ok_live, live_message = _probe_hwnd_responsive(hwnd, timeout_ms=150)
    if not ok_live:
        return f"error: UIA_WATCHDOG_FAST_FAIL: {live_message}"

    options = args or {}
    try:
        max_depth = max(1, min(int(options.get("max_depth") or 4), 8))
        max_controls = max(1, min(int(options.get("max_controls") or 50), 120))
        timeout_ms = max(200, min(int(options.get("timeout_ms") or 800), 3000))
    except (TypeError, ValueError):
        return "error: max_depth, max_controls, and timeout_ms must be integers"
    include_non_focusable = bool(options.get("include_non_focusable", False))

    sink, timed_out, aborted = _run_mta_extraction(
        hwnd,
        max_depth,
        max_controls,
        include_non_focusable,
        timeout_ms / 1000.0,
    )
    if aborted:
        return "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'ui_tree' was interrupted by operator."
    if timed_out:
        return (
            f"error: UIA_WATCHDOG_TIMEOUT: foreground window (0x{hwnd:X}) "
            f"exceeded {timeout_ms}ms UIA budget"
        )
    if "error" in sink and "controls" not in sink:
        return f"error: UIA extraction failed: {sink['error']}"

    controls = sink.get("controls", [])
    payload = {"hwnd": hwnd, "count": len(controls), "controls": controls}
    try:
        UI_TREE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        UI_TREE_CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    return _format_outline(
        hwnd,
        controls,
        timeout_ms,
        bool(sink.get("truncated_by_watchdog")),
    )
