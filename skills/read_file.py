from core.tools import tool_read

DESCRIPTION = "Read UTF-8 text file from local workspace."
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path (relative to workspace or absolute)"}
    },
    "required": ["path"],
}
HANDLER = tool_read
