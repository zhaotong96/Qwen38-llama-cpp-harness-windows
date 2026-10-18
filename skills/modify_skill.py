from pathlib import Path
from core.models import ROOT

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
    name = Path(skill_name.strip()).name.removesuffix(".py")
    skills_dir = (ROOT / "skills").resolve()
    p = (skills_dir / f"{name}.py").resolve()
    if not str(p).startswith(str(skills_dir)):
        return f"error: path outside skills directory"
    if not p.exists():
        return f"error: skill {name}.py does not exist"
    p.write_text(code, encoding="utf-8")
    return f"updated skill {p.name}"
