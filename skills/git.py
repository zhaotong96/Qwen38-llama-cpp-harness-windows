from core.tools import _run_process, execution_aborted

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

    status, stdout, stderr, returncode = _run_process(cmd, GIT_TIMEOUT)

    if status == "interrupted":
        return (
            "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'git' was interrupted by operator; "
            "its output and side-effects have been ignored."
        )
    if status == "timeout":
        return f"error: git action timed out after {GIT_TIMEOUT}s"
    if status == "spawn_error":
        return f"error: {stderr}"

    if execution_aborted():
        return (
            "[EXECUTION_INTERRUPTED_AND_IGNORED] Tool 'git' was interrupted by operator; "
            "its output and side-effects have been ignored."
        )
    return f"exit={returncode}\n{(stdout or '') + (stderr or '')}"
