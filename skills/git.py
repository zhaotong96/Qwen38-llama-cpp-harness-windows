import subprocess
from core.models import ROOT

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

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), encoding="utf-8", errors="replace")
        return f"exit={r.returncode}\n{(r.stdout or '') + (r.stderr or '')}"
    except Exception as e:
        return f"error: {e}"
