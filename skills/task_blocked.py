DESCRIPTION = "Mark current task as blocked or unable to proceed."
PARAMETERS = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "Reason task is blocked"}
    },
    "required": ["reason"],
}


def HANDLER(reason: str = "") -> str:
    return f"TASK_BLOCKED: {reason}"
