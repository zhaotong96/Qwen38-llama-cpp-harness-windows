"""
Tool implementations and skill registration for the local agent.
"""
from __future__ import annotations

import base64
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


def writable_path(path: str) -> Path:
    """Resolve path cleanly according to OS permission semantics."""
    return safe_path(path)


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

    popen_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": str(ROOT),
    }
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            ["pwsh", "-NoProfile", "-Command", command],
            **popen_kwargs,
        )
        deadline = time.time() + SHELL_TIMEOUT
        while proc.poll() is None:
            if _is_active_execution_aborted():
                _terminate_process_tree(proc)
                return (
                    "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'shell' was interrupted by operator; "
                    "its output and side-effects have been ignored."
                )
            if time.time() >= deadline:
                _terminate_process_tree(proc)
                return f"error: timed out after {SHELL_TIMEOUT}s"
            time.sleep(0.05)

        stdout, stderr = proc.communicate(timeout=5)
        if _is_active_execution_aborted():
            return (
                "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'shell' was interrupted by operator; "
                "its output and side-effects have been ignored."
            )
        body = trunc(((stdout or "") + (stderr or "")).strip() or "(no output)")
        return f"exit={proc.returncode}\n{body}"
    except subprocess.TimeoutExpired:
        return f"error: timed out after {SHELL_TIMEOUT}s"
    except Exception as e:
        return f"error: {e}"


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
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
        )
        if r.returncode == 0 and r.stdout.strip():
            return trunc(r.stdout.strip())
        if r.returncode == 1:
            return "(no matches)"
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        return "error: search timed out"
    except Exception as e:
        return f"error: {e}"

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
