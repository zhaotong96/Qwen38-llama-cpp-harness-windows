from core.tools import tool_read_skill

DESCRIPTION = "Read full implementation of a skill."
PARAMETERS = {
    "type": "object",
    "properties": {
        "skill_name": {"type": "string", "description": "Name of skill"}
    },
    "required": ["skill_name"],
}
HANDLER = tool_read_skill
