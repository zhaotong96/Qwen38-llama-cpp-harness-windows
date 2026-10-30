"""
Win32 top-level window enumeration and deadlock-immune foreground activation.

``activate_window`` runs a six-step defensive state machine (Phase 10A / §2.2):

1. Dual-ended 150ms ``WM_NULL`` message-pump probes; a hung queue trips the
   ``WINDOW_HUNG_OR_INVALID`` fuse before any thread queue is touched.
2. Process Token Integrity Level comparison; a higher-integrity target makes
   the state machine skip ``AttachThreadInput`` (UIPI immunity) and report
   ``uipi_elevated=True``.
3. ``IsIconic`` guard: only a truly minimized window receives
   ``SW_RESTORE``; normal or maximized windows receive ``SW_SHOW`` so the
   user's maximized layout is never shrunk.
4. Synthetic extended ``VK_MENU`` (Alt) ``KEYEVENTF_KEYUP`` pulse that
   registers this thread as the last input context inside win32k, clearing
   the foreground lock without attaching higher-privilege queues.
5. Exception-safe ``try...finally`` ``AttachThreadInput`` choreography:
   ``SetForegroundWindow`` plus a ``Chrome_RenderWidgetHostHWND`` child
   ``SetFocus`` re-seed, with guaranteed queue detachment in ``finally``.
6. A 150ms watchdog verifies foreground ownership; if still locked out at
   half the budget it flips ``HWND_TOPMOST`` -> ``HWND_NOTOPMOST`` and calls
   ``SwitchToThisWindow`` to force the switch.

All Win32 bindings are lazy (inside function scope) to satisfy the
``core.skill_sandbox`` AST top-level zero-Call purity theorem.
"""
from __future__ import annotations

import ctypes
import json
import ntpath
import sys
import time
from ctypes import wintypes

DESCRIPTION = (
    "Enumerate visible top-level desktop windows (hwnd, title, pid, process, "
    "physical rect) or forcefully activate a target window (by hwnd or "
    "case-insensitive title/process substring) with hung-queue, UIPI, and "
    "foreground-lock immunity."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list_windows", "activate_window"],
            "description": "Action to perform: list_windows or activate_window.",
        },
        "hwnd": {
            "type": "integer",
            "description": "Target window handle (HWND) for activate_window.",
        },
        "title": {
            "type": "string",
            "description": (
                "Case-insensitive title or process name substring for "
                "activate_window when hwnd is not given."
            ),
        },
    },
    "required": ["action"],
}


def _get_win32_apis():
    """Lazy-bind Win32 APIs inside function scope to satisfy AST top-level zero-Call purity."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL

    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL

    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int

    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL

    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    user32.EnumWindows.restype = wintypes.BOOL

    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)
    ]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM

    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL

    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL

    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL

    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT
    ]
    user32.SetWindowPos.restype = wintypes.BOOL

    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND

    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND

    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL

    user32.LockSetForegroundWindow.argtypes = [wintypes.UINT]
    user32.LockSetForegroundWindow.restype = wintypes.BOOL

    user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.SwitchToThisWindow.restype = None

    user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT
    ]
    user32.SystemParametersInfoW.restype = wintypes.BOOL

    user32.FindWindowExW.argtypes = [
        wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR
    ]
    user32.FindWindowExW.restype = wintypes.HWND

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL

    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    return user32, kernel32, dwmapi, advapi32


def _probe_hwnd_alive(user32, hwnd: int, timeout_ms: int = 150) -> bool:
    """Fast 150ms WM_NULL probe to detect hung message queues before AttachThreadInput."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    SMTO_BLOCK = 0x0001
    SMTO_ABORTIFHUNG = 0x0002
    WM_NULL = 0x0000
    result = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(
        hwnd, WM_NULL, 0, 0, SMTO_BLOCK | SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
    )
    return bool(ok)


def _get_process_integrity_rid(kernel32, advapi32, pid: int) -> int:
    """Query process token Integrity Level RID (e.g. 0x2000 Medium, 0x3000 High)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TOKEN_INTEGRITY_LEVEL = 25

    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h_proc:
        return 0x3000  # Assume high integrity if inaccessible
    h_token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(h_proc, TOKEN_QUERY, ctypes.byref(h_token)):
            return 0x3000
        try:
            cb_needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(
                h_token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(cb_needed)
            )
            if cb_needed.value == 0:
                return 0x2000
            buf = ctypes.create_string_buffer(cb_needed.value)
            if not advapi32.GetTokenInformation(
                h_token, TOKEN_INTEGRITY_LEVEL, buf, cb_needed, ctypes.byref(cb_needed)
            ):
                return 0x2000
            # TOKEN_MANDATORY_LABEL.Label.Sid points into the same buffer.
            p_sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
            sub_authority_count = ctypes.cast(
                p_sid + 1, ctypes.POINTER(ctypes.c_ubyte)
            ).contents.value
            rid_ptr = ctypes.cast(
                p_sid + 8 + (sub_authority_count - 1) * 4,
                ctypes.POINTER(wintypes.DWORD),
            )
            return int(rid_ptr.contents.value)
        finally:
            kernel32.CloseHandle(h_token)
    finally:
        kernel32.CloseHandle(h_proc)


def _send_alt_unlock_pulse(user32) -> None:
    """Inject a benign extended VK_MENU keyup pulse to clear win32k foreground lock."""
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
    VK_MENU = 0x12
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002

    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = VK_MENU
    inp.u.ki.dwFlags = KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _query_process_image_name(kernel32, pid: int) -> str:
    """Return the basename of the executable image that owns ``pid`` (empty when inaccessible)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h_proc:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return ntpath.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h_proc)


def _physical_rect(user32, dwmapi, hwnd: int) -> dict:
    """DWM extended frame bounds in global physical pixels, GetWindowRect as fallback."""
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
    ) != 0:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 0, 0
    return {
        "left": int(rect.left),
        "top": int(rect.top),
        "width": int(rect.right) - int(rect.left),
        "height": int(rect.bottom) - int(rect.top),
    }


def list_visible_windows() -> list[dict]:
    """Enumerate visible, non-minimized, non-cloaked top-level windows (pure ctypes)."""
    user32, kernel32, dwmapi, _ = _get_win32_apis()
    DWMWA_CLOAKED = 14
    windows: list[dict] = []
    pid = wintypes.DWORD(0)

    def collect_windows(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.IsIconic(hwnd):
            return True
        cloaked = wintypes.DWORD(0)
        if dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        ) == 0 and cloaked.value != 0:
            return True
        length = max(0, int(user32.GetWindowTextLengthW(hwnd)))
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows.append(
            {
                "hwnd": int(hwnd),
                "title": buf.value,
                "pid": int(pid.value),
                "process": _query_process_image_name(kernel32, int(pid.value)),
                "rect": _physical_rect(user32, dwmapi, hwnd),
            }
        )
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(collect_windows), 0)
    return windows


def _resolve_target_hwnd(title_substring: str) -> tuple[int, list[dict]]:
    """Case-insensitive title/process substring match; a unique match wins."""
    needle = (title_substring or "").strip().lower()
    if not needle:
        return 0, []
    matches = []
    for window in list_visible_windows():
        if needle in window["title"].lower() or needle in window["process"].lower():
            matches.append(window)
    if len(matches) == 1:
        return int(matches[0]["hwnd"]), []
    return 0, matches


def activate_window_safely(target_hwnd: int, watchdog_ms: int = 150) -> dict:
    """Execute the 6-step deadlock-free & UIPI-aware window activation sequence."""
    user32, kernel32, _, advapi32 = _get_win32_apis()

    if not _probe_hwnd_alive(user32, target_hwnd, timeout_ms=150):
        return {
            "ok": False,
            "error": "WINDOW_HUNG_OR_INVALID",
            "hwnd": target_hwnd,
        }

    fg_hwnd = user32.GetForegroundWindow()
    if fg_hwnd == target_hwnd:
        return {"ok": True, "hwnd": target_hwnd, "method": "already_foreground"}

    # Step 2: Check UIPI Integrity Level to prevent AttachThreadInput Access Denied
    target_pid = wintypes.DWORD(0)
    target_tid = int(user32.GetWindowThreadProcessId(target_hwnd, ctypes.byref(target_pid)))
    cur_pid = int(kernel32.GetCurrentProcessId())
    cur_tid = int(kernel32.GetCurrentThreadId())

    self_rid = _get_process_integrity_rid(kernel32, advapi32, cur_pid)
    target_rid = _get_process_integrity_rid(kernel32, advapi32, int(target_pid.value))
    uipi_blocked = target_rid > self_rid

    fg_pid = wintypes.DWORD(0)
    fg_tid = int(user32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_pid))) if fg_hwnd else 0
    fg_alive = bool(_probe_hwnd_alive(user32, fg_hwnd, timeout_ms=100)) if fg_hwnd else False
    fg_uipi_ok = (
        _get_process_integrity_rid(kernel32, advapi32, int(fg_pid.value)) <= self_rid
        if fg_pid.value else False
    )

    # Step 3: Iconic Guard (Only SW_RESTORE if minimized, never un-maximize a zoomed window)
    SW_RESTORE = 9
    SW_SHOW = 5
    if user32.IsIconic(target_hwnd):
        user32.ShowWindowAsync(target_hwnd, SW_RESTORE)
    else:
        user32.ShowWindowAsync(target_hwnd, SW_SHOW)

    # Step 4: Unlock foreground state & inject benign VK_MENU keyup pulse
    LSFW_UNLOCK = 2
    ASFW_ANY = 0xFFFFFFFF
    user32.LockSetForegroundWindow(LSFW_UNLOCK)
    user32.AllowSetForegroundWindow(ASFW_ANY)
    _send_alt_unlock_pulse(user32)

    # Step 5: Guarded try...finally AttachThreadInput choreography
    attached_fg = False
    attached_target = False
    SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040
    HWND_TOP = 0

    try:
        if fg_tid and fg_tid != cur_tid and fg_alive and fg_uipi_ok:
            attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        if target_tid and target_tid != cur_tid and not uipi_blocked:
            attached_target = bool(user32.AttachThreadInput(cur_tid, target_tid, True))

        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, None, 0)
        user32.BringWindowToTop(target_hwnd)
        user32.SetWindowPos(
            target_hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        )
        user32.SetForegroundWindow(target_hwnd)
        user32.SetActiveWindow(target_hwnd)

        # Re-seed keyboard focus into inner Chromium/Electron render host if present
        render_child = user32.FindWindowExW(
            target_hwnd, None, "Chrome_RenderWidgetHostHWND", None
        )
        focus_target = render_child if render_child else target_hwnd
        user32.SetFocus(focus_target)
    finally:
        if attached_target:
            user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            user32.AttachThreadInput(cur_tid, fg_tid, False)

    # Step 6: 150ms Watchdog Verification with Topmost Toggle Fallback at 75ms
    deadline = time.monotonic() + (watchdog_ms / 1000.0)
    fallback_triggered = False
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2

    while time.monotonic() < deadline:
        current_fg = user32.GetForegroundWindow()
        if current_fg == target_hwnd:
            return {
                "ok": True,
                "hwnd": target_hwnd,
                "uipi_elevated": uipi_blocked,
                "fallback_used": fallback_triggered,
            }
        if not fallback_triggered and (deadline - time.monotonic()) <= (watchdog_ms / 2000.0):
            fallback_triggered = True
            user32.SetWindowPos(
                target_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            )
            user32.SetWindowPos(
                target_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            )
            user32.SwitchToThisWindow(target_hwnd, True)
            user32.SetForegroundWindow(target_hwnd)
        time.sleep(0.015)

    final_fg = user32.GetForegroundWindow()
    return {
        "ok": final_fg == target_hwnd,
        "hwnd": target_hwnd,
        "actual_foreground_hwnd": int(final_fg) if final_fg else 0,
        "uipi_elevated": uipi_blocked,
        "fallback_used": fallback_triggered,
        "error": None if final_fg == target_hwnd else "FOREGROUND_LOCK_REJECTED",
    }


def run(args: dict, runtime: dict | None = None) -> str:
    if sys.platform != "win32":
        return "error: window_manager is only supported on Windows"
    args = args or {}
    action = str(args.get("action", "")).strip().lower()
    try:
        if action == "list_windows":
            windows = list_visible_windows()
            return json.dumps(
                {"ok": True, "count": len(windows), "windows": windows},
                ensure_ascii=False,
            )

        if action == "activate_window":
            hwnd = args.get("hwnd")
            title = args.get("title")
            if hwnd:
                target_hwnd = int(hwnd)
            elif title:
                target_hwnd, candidates = _resolve_target_hwnd(title)
                if not target_hwnd and not candidates:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "WINDOW_NOT_FOUND",
                            "title": title,
                            "hint": "no visible window matched; call list_windows first",
                        },
                        ensure_ascii=False,
                    )
                if not target_hwnd:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": "AMBIGUOUS_TARGET",
                            "title": title,
                            "candidates": [
                                {"hwnd": w["hwnd"], "title": w["title"], "process": w["process"]}
                                for w in candidates
                            ],
                        },
                        ensure_ascii=False,
                    )
            else:
                return "error: activate_window requires hwnd or title"
            return json.dumps(activate_window_safely(target_hwnd), ensure_ascii=False)

        return "error: action must be list_windows|activate_window"
    except Exception as e:
        return f"error: {e}"
