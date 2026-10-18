from core.tools import tool_write

DESCRIPTION = "Write content to a file in workspace."
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path"},
        "content": {"type": "string", "description": "UTF-8 content to write"},
    },
    "required": ["path", "content"],
}
HANDLER = tool_write
