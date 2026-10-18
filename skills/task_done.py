DESCRIPTION = "Mark current task as successfully completed."
PARAMETERS = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Summary of work completed"}
    },
    "required": ["summary"],
}


def HANDLER(summary: str = "") -> str:
    return f"TASK_DONE: {summary}"
