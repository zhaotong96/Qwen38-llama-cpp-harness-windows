from core import skill_sandbox

DESCRIPTION = "Create a new declarative or custom Python skill file in skills/."
PARAMETERS = {
    "type": "object",
    "properties": {
        "skill_name": {"type": "string", "description": "Name of skill (e.g. my_tool)"},
        "description": {"type": "string", "description": "Description of what skill does"},
        "code": {"type": "string", "description": "Python source code for skill"},
    },
    "required": ["skill_name", "description", "code"],
}


def HANDLER(skill_name: str, description: str, code: str) -> str:
    del description
    return skill_sandbox.verify_and_deploy_skill(skill_name, code, overwrite=False)
