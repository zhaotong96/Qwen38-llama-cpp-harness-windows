from core.tools import tool_search

DESCRIPTION = "Search file content with ripgrep."
PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex pattern"},
        "path": {"type": "string", "description": "Directory, default '.'"},
        "glob": {"type": "string", "description": "File pattern e.g. '*.py'"},
    },
    "required": ["pattern"],
}
HANDLER = tool_search
