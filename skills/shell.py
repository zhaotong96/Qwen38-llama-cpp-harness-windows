from core.tools import tool_shell

DESCRIPTION = "Execute a PowerShell 7 command line in the workspace."
PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "PowerShell 7 command to run"}
    },
    "required": ["command"],
}
HANDLER = tool_shell
