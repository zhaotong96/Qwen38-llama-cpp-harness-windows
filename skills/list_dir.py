from core.tools import tool_ls

DESCRIPTION = "List directory contents."
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Directory path, default '.'"}
    },
    "required": [],
}
HANDLER = tool_ls
