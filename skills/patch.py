from pathlib import Path
from core.tools import writable_path

DESCRIPTION = "Apply line-based replacement patch to a file."
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Target file path"},
        "old_text": {"type": "string", "description": "Text to replace"},
        "new_text": {"type": "string", "description": "Replacement text"},
    },
    "required": ["path", "old_text", "new_text"],
}


def HANDLER(path: str, old_text: str, new_text: str) -> str:
    try:
        p = writable_path(path)
    except PermissionError as e:
        return f"error: {e}"
    if not p.is_file():
        return f"error: file not found {p}"
    content = p.read_text(encoding="utf-8", errors="replace")
    if old_text not in content:
        return f"error: old_text not found in {p.name}"
    updated = content.replace(old_text, new_text, 1)
    p.write_text(updated, encoding="utf-8")
    return f"successfully patched {p.name}"
