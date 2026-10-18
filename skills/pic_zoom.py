from pathlib import Path
from core.models import ROOT

try:
    from PIL import Image
except ImportError:
    Image = None

DESCRIPTION = "Crop and zoom a region of an image file."
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Source image path"},
        "left": {"type": "integer", "description": "Left crop coordinate"},
        "top": {"type": "integer", "description": "Top crop coordinate"},
        "right": {"type": "integer", "description": "Right crop coordinate"},
        "bottom": {"type": "integer", "description": "Bottom crop coordinate"},
        "output_path": {"type": "string", "description": "Output zoomed image path"},
    },
    "required": ["path", "left", "top", "right", "bottom"],
}


def HANDLER(path: str, left: int, top: int, right: int, bottom: int, output_path: str = "") -> str:
    if Image is None:
        return "error: PIL package required (pip install pillow)"
    p = (ROOT / path).resolve()
    if not p.is_file():
        return f"error: image not found {p}"
    try:
        img = Image.open(p)
        cropped = img.crop((left, top, right, bottom))
        out_p = (ROOT / output_path).resolve() if output_path else p.parent / f"zoomed_{p.name}"
        cropped.save(out_p)
        return f"saved zoomed image to {out_p} ({cropped.size[0]}x{cropped.size[1]})"
    except Exception as e:
        return f"error: {e}"
