import subprocess
from core.models import ROOT
from core.tools import execution_aborted

DESCRIPTION = "Execute git operations (status, add, commit, diff, log)."
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "status|add|commit|diff|log"},
        "message": {"type": "string", "description": "Commit message if action is commit"},
        "target": {"type": "string", "description": "File or path target"},
    },
    "required": ["action"],
}

GIT_TIMEOUT = 60


def HANDLER(action: str, message: str = "", target: str = "") -> str:
    action = action.lower().strip()
    cmd = ["git"]
    if action == "status":
        cmd += ["status", "--short"]
    elif action == "add":
        cmd += ["add", target or "."]
    elif action == "commit":
        if not message:
            return "error: commit requires message"
        cmd += ["commit", "-m", message]
    elif action == "diff":
        cmd += ["diff"]
        if target:
            cmd.append(target)
    elif action == "log":
        cmd += ["log", "-n", "10", "--oneline"]
    else:
        return f"error: unknown git action {action}"

    if execution_aborted():
        return (
            "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'git' was interrupted by operator; "
            "its output and side-effects have been ignored."
        )

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"error: git action timed out after {GIT_TIMEOUT}s"
    except Exception as e:
        return f"error: {e}"

    if execution_aborted():
        return (
            "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'git' was interrupted by operator; "
            "its output and side-effects have been ignored."
        )
    return f"exit={r.returncode}\n{(r.stdout or '') + (r.stderr or '')}"
