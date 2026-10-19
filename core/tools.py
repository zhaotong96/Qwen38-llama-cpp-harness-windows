"""
Tool implementations and skill registration for the local agent.
"""
from __future__ import annotations

import base64
from collections import deque
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from core.config import Presets
from core.interrupt import is_step_rejected
from core.models import (
    BASE_URL,
    CHAT_EXTRA,
    CHROME_CDP_PORT,
    MODEL,
    ROOT,
    SAFETY_LOG,
    SCREEN_PATH,
    SHELL_TIMEOUT,
    SKILL_ROOTS,
)

try:
    import mss
    from PIL import Image
except ImportError:
    mss = None
    Image = None

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32

    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

PIPE_HEAD_BYTES = 256 * 1024
PIPE_TAIL_BYTES = 256 * 1024
PIPE_READ_CHUNK = 4096

NON_INTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_ASKPASS": "echo",
    "SSH_ASKPASS": "echo",
    "CI": "true",
    "TF_INPUT": "0",
    "PIP_NO_INPUT": "1",
    "NPM_CONFIG_YES": "true",
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
    "TERM": "dumb",
    "NO_COLOR": "1",
}

_screenshot_counter = 0
_active_context = threading.local()


def set_active_execution_context(task_id: str | None, base_root: Path | None = None, parent_task_id: str | None = None) -> None:
    """Bind the current thread's active task_id so tool_shell can poll abort.flag."""
    _active_context.task_id = task_id
    _active_context.base_root = base_root
    _active_context.parent_task_id = parent_task_id


def _is_active_execution_aborted() -> dict | None:
    task_id = getattr(_active_context, "task_id", None)
    base_root = getattr(_active_context, "base_root", None)
    parent_task_id = getattr(_active_context, "parent_task_id", None)
    if task_id:
        info = is_step_rejected(task_id, base_root=base_root)
        if info:
            return info
    if parent_task_id:
        info = is_step_rejected(parent_task_id)
        if info:
            return info
    return None


def execution_aborted() -> dict | None:
    """Public abort probe for skills that launch their own subprocesses."""
    return _is_active_execution_aborted()


def _create_kill_on_close_job(
    max_memory_bytes: int = 4 * 1024 ** 3,
    max_procs: int = 64,
) -> int | None:
    """Create a Win32 Job Object that reaps the whole process tree on handle close."""
    if sys.platform != "win32":
        return None
    h_job = _kernel32.CreateJobObjectW(None, None)
    if not h_job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    )
    info.JobMemoryLimit = max_memory_bytes
    info.BasicLimitInformation.ActiveProcessLimit = max_procs
    ok = _kernel32.SetInformationJobObject(
        h_job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        _kernel32.CloseHandle(h_job)
        return None
    return int(h_job)


def _assign_process_to_job(h_job: int | None, proc: subprocess.Popen) -> bool:
    if not h_job or sys.platform != "win32":
        return False
    try:
        process_handle = int(proc._handle)
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(_kernel32.AssignProcessToJobObject(h_job, process_handle))


def _terminate_job(h_job: int | None) -> None:
    if not h_job or sys.platform != "win32":
        return
    try:
        _kernel32.TerminateJobObject(h_job, 1)
    except Exception:
        pass


def _close_handle(h_job: int | None) -> None:
    if not h_job or sys.platform != "win32":
        return
    try:
        _kernel32.CloseHandle(h_job)
    except Exception:
        pass


def _drain_stream(
    stream,
    sink: list[str],
    head_limit: int = PIPE_HEAD_BYTES,
    tail_limit: int = PIPE_TAIL_BYTES,
) -> None:
    """Drain one child pipe with a bounded head/tail ring buffer.

    ``readline()`` lets CPython grow a single C-level buffer without bound when
    the child emits a huge line without newlines. Chunked ``read()`` plus a
    fixed head and a deque-backed tail caps peak retention per pipe at
    ``head_limit + tail_limit`` characters.
    """
    head_parts: list[str] = []
    head_chars = 0
    tail_parts: deque[str] = deque()
    tail_chars = 0
    omitted_chars = 0
    try:
        while True:
            chunk = stream.read(PIPE_READ_CHUNK)
            if not chunk:
                break
            if head_chars < head_limit:
                take = min(head_limit - head_chars, len(chunk))
                head_parts.append(chunk[:take])
                head_chars += take
                chunk = chunk[take:]
            if not chunk:
                continue
            tail_parts.append(chunk)
            tail_chars += len(chunk)
            while tail_chars > tail_limit:
                overflow = tail_chars - tail_limit
                first = tail_parts[0]
                if len(first) <= overflow:
                    tail_parts.popleft()
                    tail_chars -= len(first)
                    omitted_chars += len(first)
                else:
                    tail_parts[0] = first[overflow:]
                    tail_chars -= overflow
                    omitted_chars += overflow
                    break
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass
        sink.extend(head_parts)
        if omitted_chars:
            sink.append(f"\n...[output truncated: {omitted_chars} more chars omitted]...\n")
        sink.extend(tail_parts)


def _run_process(argv: list[str], timeout: float) -> tuple[str, str, str, int | None]:
    """Run argv inside the Job Object sandbox with live stdout/stderr drains.

    Returns (status, stdout, stderr, returncode) where status is one of
    "ok", "timeout", "interrupted", or "spawn_error".
    """
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": str(ROOT),
        "env": {**os.environ, **NON_INTERACTIVE_ENV},
    }
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as e:
        return "spawn_error", "", str(e), None

    h_job = _create_kill_on_close_job()
    if h_job:
        _assign_process_to_job(h_job, proc)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    drain_out = threading.Thread(target=_drain_stream, args=(proc.stdout, stdout_chunks), daemon=True)
    drain_err = threading.Thread(target=_drain_stream, args=(proc.stderr, stderr_chunks), daemon=True)
    drain_out.start()
    drain_err.start()

    status = "ok"
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        while proc.poll() is None:
            if _is_active_execution_aborted():
                status = "interrupted"
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                break
            time.sleep(0.05)
    finally:
        if status != "ok":
            _terminate_job(h_job)
        _close_handle(h_job)
        if status != "ok":
            _terminate_process_tree(proc)
        drain_out.join(timeout=5)
        drain_err.join(timeout=5)
        try:
            proc.wait(timeout=5)
        except Exception:
            _terminate_process_tree(proc)
    return status, "".join(stdout_chunks), "".join(stderr_chunks), proc.returncode


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Force-kill a running subprocess and its entire child process tree."""
    pid = proc.pid
    if pid is None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def load_text(path: str | Path, default: str = "") -> str:
    """Read a UTF-8 text file; relative paths resolve against ROOT."""
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.is_file():
        return default
    try:
        return p.read_text(encoding="utf-8").strip() or default
    except OSError as e:
        print(f"[warn] {p}: {e}")
        return default


def safe_path(path: str) -> Path:
    p = Path(path).expanduser()
    return (ROOT / p).resolve() if not p.is_absolute() else p.resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def writable_path(path: str) -> Path:
    """Resolve a write target, constraining writes to the workspace and the system temp dir."""
    p = safe_path(path)
    allowed_roots = (ROOT.resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(_is_within(p, root) for root in allowed_roots):
        raise PermissionError(
            f"write denied outside workspace and temp dir: {p} "
            f"(allowed roots: {allowed_roots[0]}, {allowed_roots[1]})"
        )
    return p


def log_safety(message: str) -> None:
    SAFETY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SAFETY_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def trunc(s: str, n: int = 12000) -> str:
    s = s if s else "(empty)"
    return s if len(s) <= n else s[:n] + f"\n...[{len(s) - n} more chars]"


def foreground_window() -> tuple[dict, str] | None:
    """Windows: focused window region for mss + title. None if unusable."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = (buf.value or "").strip() or "(no title)"

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        width, height = right - left, bottom - top
        if width < 32 or height < 32:
            return None
        region = {"left": left, "top": top, "width": width, "height": height}
        return region, title
    except Exception:
        return None


def discover_skills() -> list[dict[str, str]]:
    """Discover executable Python skill modules without importing main again."""
    skills: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in SKILL_ROOTS:
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob("*.py")):
            if skill_file.name.startswith("_"):
                continue
            module = _load_skill_module(skill_file)
            name = skill_file.stem
            if name in seen:
                continue
            skills.append(
                {
                    "name": name,
                    "description": str(getattr(module, "DESCRIPTION", "")),
                    "path": str(skill_file.resolve()),
                    "module": module,
                }
            )
            seen.add(name)
    return skills


def _load_skill_module(path: Path):
    """Load a skill module straight from its source file, never from a cached .pyc."""
    module_name = f"gemma_skill_{path.stem}_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None:
        raise ImportError(f"cannot load skill: {path}")
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    source = path.read_text(encoding="utf-8", errors="replace")
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def tool_read_skill(skill_name: str) -> str:
    if (
        not skill_name
        or skill_name in (".", "..")
        or Path(skill_name).is_absolute()
        or "/" in skill_name
        or "\\" in skill_name
    ):
        return "error: invalid skill name"
    for skill in discover_skills():
        if skill["name"] == skill_name:
            return Path(skill["path"]).read_text(encoding="utf-8", errors="replace")
    return f"error: unknown skill: {skill_name}"


def tool_shell(command: str, task_id: str | None = None) -> str:
    dangerous = Presets.DANGEROUS_SHELL_REGEX
    if re.search(dangerous, command):
        log_safety(f"BLOCK shell={command!r}")
        return "error: SECURITY_BLOCK: Execution denied for high-risk command. Operator authorization required."
    lower = command.lower()
    chrome_lifecycle = "chrome" in lower and re.search(
        r"\bstop-process\b|\bstart-process\b|\btaskkill\b|"
        r"(?:^|[;&|]\s*)start\s+chrome\b|\bchrome(?:\.exe)?\s+--remote-debugging",
        lower,
    )
    if chrome_lifecycle:
        log_safety(f"BLOCK operator-managed Chrome lifecycle shell={command!r}")
        return "error: operator-managed Chrome lifecycle is blocked; use the existing CDP session"

    if task_id:
        set_active_execution_context(task_id)

    if _is_active_execution_aborted():
        return "[EXECUTION_REJECTED] Shell command refused via abort.flag marker before execution."

    try:
        status, stdout, stderr, returncode = _run_process(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            SHELL_TIMEOUT,
        )
    except Exception as e:
        return f"error: {e}"

    if status == "interrupted":
        return (
            "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'shell' was interrupted by operator; "
            "its output and side-effects have been ignored."
        )
    if status == "timeout":
        return f"error: timed out after {SHELL_TIMEOUT}s"
    if status == "spawn_error":
        return f"error: {stderr}"
    if _is_active_execution_aborted():
        return (
            "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'shell' was interrupted by operator; "
            "its output and side-effects have been ignored."
        )
    body = trunc(((stdout or "") + (stderr or "")).strip() or "(no output)")
    return f"exit={returncode}\n{body}"


def tool_read(path: str) -> str:
    try:
        p = safe_path(path)
        if not p.is_file():
            return f"error: not a file: {p}"
        return trunc(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return f"error: {e}"


def tool_write(path: str, content: str) -> str:
    try:
        p = writable_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {p} ({len(content)} chars)"
    except Exception as e:
        return f"error: {e}"


def tool_ls(path: str = ".") -> str:
    try:
        p = safe_path(path)
        if not p.is_dir():
            return f"error: not a directory: {p}"
        lines = []
        for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            lines.append(c.name + ("/" if c.is_dir() else ""))
        return trunc("\n".join(lines) or "(empty)")
    except Exception as e:
        return f"error: {e}"


def tool_search(pattern: str, path: str = ".", glob: str = "") -> str:
    cmd = ["rg", "--line-number", "--color", "never", "--no-heading", "-m", "80"]
    if glob:
        cmd += ["--glob", glob]
    cmd += [pattern, path]
    try:
        status, stdout, stderr, returncode = _run_process(cmd, 30)
    except Exception as e:
        return f"error: {e}"

    if status == "interrupted":
        return (
            "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'search' was interrupted by operator; "
            "its output and side-effects have been ignored."
        )
    if status == "timeout":
        return "error: search timed out"
    if status == "ok":
        if returncode == 0 and stdout.strip():
            return trunc(stdout.strip())
        if returncode == 1:
            return "(no matches)"

    g = glob or "*"
    ps = (
        f"Get-ChildItem -Path '{path}' -Recurse -File -Filter '{g}' -ErrorAction SilentlyContinue "
        f"| Select-String -Pattern '{pattern}' | Select-Object -First 80 "
        f"| ForEach-Object {{ \"$($_.Path):$($_.LineNumber):$($_.Line)\" }}"
    )
    out = tool_shell(ps)
    if out.startswith("exit=0") and "(no output)" not in out:
        return trunc(out.split("\n", 1)[-1])
    if "(no output)" in out:
        return "(no matches)"
    return out


def tool_screenshot() -> tuple[str, str | None]:
    """Capture focused window; fall back to primary full screen."""
    global _screenshot_counter
    if mss is None or Image is None:
        return "error: install mss pillow  (py -3 -m pip install mss pillow)", None

    def _get_next_path() -> Path:
        global _screenshot_counter
        directory = SCREEN_PATH.parent
        directory.mkdir(parents=True, exist_ok=True)
        while True:
            _screenshot_counter += 1
            path = directory / f"screen_{_screenshot_counter:03d}.jpg"
            if not path.exists():
                return path

    def _encode(raw, mode: str, title: str = "") -> tuple[str, str]:
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        path = _get_next_path()
        img.save(path, format="JPEG", quality=60)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        b64 = base64.b64encode(buf.getvalue()).decode()
        extra = f' title="{title}"' if title else ""
        return (
            f"screenshot saved: {path} "
            f"({img.size[0]}x{img.size[1]}) mode={mode}{extra}",
            b64,
        ), b64  # wait: keep exact tuple (str, b64) below!

    try:
        with mss.MSS() as sct:
            fg = foreground_window()
            if fg:
                region, title = fg
                try:
                    raw = sct.grab(region)
                    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                    path = _get_next_path()
                    img.save(path, format="JPEG", quality=60)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=60)
                    b64 = base64.b64encode(buf.getvalue()).decode()
                    extra = f' title="{title}"' if title else ""
                    return (
                        f"screenshot saved: {path} ({img.size[0]}x{img.size[1]}) mode=focused_window{extra}",
                        b64,
                    )
                except Exception:
                    pass
            raw = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            path = _get_next_path()
            img.save(path, format="JPEG", quality=60)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=60)
            b64 = base64.b64encode(buf.getvalue()).decode()
            return (
                f"screenshot saved: {path} ({img.size[0]}x{img.size[1]}) mode=fullscreen",
                b64,
            )
    except Exception:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            path = _get_next_path()
            img.save(path, format="JPEG", quality=60)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=60)
            b64 = base64.b64encode(buf.getvalue()).decode()
            return f"screenshot saved: {path} ({img.size[0]}x{img.size[1]}) mode=fallback", b64
        except Exception:
            pass

        return (
            "error: screenshot unavailable.\n"
            "reason: Windows desktop capture failed.\n"
            "possible causes:\n"
            "- no interactive desktop session\n"
            "- locked screen\n"
            "- remote session disconnected\n"
            "- graphics subsystem unavailable\n\n"
            "Please use alternative observation methods (e.g. read_file, shell, or DOM)."
        ), None
