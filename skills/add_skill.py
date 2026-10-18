from pathlib import Path
from core.models import ROOT

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
    name = Path(skill_name.strip()).name.removesuffix(".py")
    skills_dir = (ROOT / "skills").resolve()
    p = (skills_dir / f"{name}.py").resolve()
    if not str(p).startswith(str(skills_dir)):
        return f"error: path outside skills directory"
    if p.exists():
        return f"error: skill {name}.py already exists"
    p.write_text(code, encoding="utf-8")
    return f"created skill {p.name}"
