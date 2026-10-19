from core import skill_sandbox

DESCRIPTION = "Modify or update an existing skill file in skills/."
PARAMETERS = {
    "type": "object",
    "properties": {
        "skill_name": {"type": "string", "description": "Name of skill (e.g. shell)"},
        "code": {"type": "string", "description": "New Python code for skill"},
    },
    "required": ["skill_name", "code"],
}


def HANDLER(skill_name: str, code: str) -> str:
    return skill_sandbox.verify_and_deploy_skill(skill_name, code, overwrite=True)
