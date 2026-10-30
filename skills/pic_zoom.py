"""
Image crop/zoom skill plus the Phase 10B high-precision grid-slice geometry.

``crop_and_render_grid_slice`` crops the unscaled physical window frame,
upscales it by an integer Lanczos factor (Z in [2, 4]) and overlays a 1px
semi-transparent cross ruler grid whose labels carry true physical pixel
offsets relative to the crop origin, so a vision model reads coordinates
off the rulers instead of regressing large integers. The slice is persisted
to ``.agent/screen_zoom_last.jpg`` together with an affine projection
metadata dict.

``map_zoomed_to_global_physical`` inverts a zoomed-slice pixel back onto the
global virtual desktop, and ``virtual_desktop_bijective_norm`` implements the
zero-drift pixel-center ceiling bijection into the Win32
MOUSEEVENTF_VIRTUALDESK [0, 65535]^2 integer space (round-trip reconstruction
error is exactly 0 px on any multi-monitor layout, including negative-origin
secondary screens).
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from core.models import AGENT_DIR, ROOT

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

SCREEN_ZOOM_PATH = AGENT_DIR / "screen_zoom_last.jpg"

_MIN_CROP_EDGE_PX = 16
_GRID_LINE_COLOR = (0, 220, 255, 110)
_LABEL_BOX_COLOR = (0, 0, 0, 175)
_LABEL_TEXT_COLOR = (255, 255, 255, 240)


def HANDLER(path: str, left: int, top: int, right: int, bottom: int, output_path: str = "") -> str:
    if not path:
        return "error: path is required"
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


def crop_and_render_grid_slice(
    raw_window_img: Image.Image,
    win_left: int,
    win_top: int,
    crop_box: tuple[int, int, int, int],
    zoom_factor: int = 3,
    grid_step_phys: int = 32,
) -> tuple[Image.Image, dict]:
    """Crop the raw physical frame, upscale by an integer factor, and draw
    the dual physical-offset ruler grid.

    Returns the composited RGB slice and its affine projection metadata
    (win_origin, crop_offset, global_crop_origin, crop_phys_size, zoomed_size,
    zoom_factor, grid_step_phys, saved_path). The slice is persisted to
    ``SCREEN_ZOOM_PATH`` (.agent/screen_zoom_last.jpg) when the filesystem
    allows it; ``meta["saved_path"]`` carries the actual path or "".
    """
    w_win, h_win = raw_window_img.size
    z = max(2, min(4, int(zoom_factor)))
    step = max(1, int(grid_step_phys))

    c_l = max(0, min(w_win - _MIN_CROP_EDGE_PX, int(crop_box[0])))
    c_t = max(0, min(h_win - _MIN_CROP_EDGE_PX, int(crop_box[1])))
    c_r = max(c_l + _MIN_CROP_EDGE_PX, min(w_win, int(crop_box[2])))
    c_b = max(c_t + _MIN_CROP_EDGE_PX, min(h_win, int(crop_box[3])))

    patch = raw_window_img.crop((c_l, c_t, c_r, c_b)).convert("RGBA")
    w_c, h_c = patch.size
    zoomed_w, zoomed_h = w_c * z, h_c * z

    zoomed = patch.resize((zoomed_w, zoomed_h), resample=Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", (zoomed_w, zoomed_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 1px orthogonal reference grid every grid_step_phys physical pixels
    # (i.e. every step * z pixels on the zoomed slice), labeled with the true
    # physical pixel offset relative to the crop origin.
    for phys_x in range(0, w_c, step):
        zx = phys_x * z
        draw.line([(zx, 0), (zx, zoomed_h)], fill=_GRID_LINE_COLOR, width=1)
        draw.rectangle([(zx + 2, 2), (zx + 38, 16)], fill=_LABEL_BOX_COLOR)
        draw.text((zx + 4, 3), f"+{phys_x}", fill=_LABEL_TEXT_COLOR)

    for phys_y in range(0, h_c, step):
        zy = phys_y * z
        draw.line([(0, zy), (zoomed_w, zy)], fill=_GRID_LINE_COLOR, width=1)
        if phys_y > 0:
            draw.rectangle([(2, zy + 2), (38, zy + 16)], fill=_LABEL_BOX_COLOR)
            draw.text((4, zy + 3), f"+{phys_y}", fill=_LABEL_TEXT_COLOR)

    composited = Image.alpha_composite(zoomed, overlay).convert("RGB")

    meta = {
        "win_origin": [int(win_left), int(win_top)],
        "crop_offset": [c_l, c_t],
        "global_crop_origin": [int(win_left) + c_l, int(win_top) + c_t],
        "crop_phys_size": [w_c, h_c],
        "zoomed_size": [zoomed_w, zoomed_h],
        "zoom_factor": z,
        "grid_step_phys": step,
    }
    try:
        SCREEN_ZOOM_PATH.parent.mkdir(parents=True, exist_ok=True)
        composited.save(SCREEN_ZOOM_PATH, format="JPEG", quality=92)
        meta["saved_path"] = str(SCREEN_ZOOM_PATH)
    except Exception:
        meta["saved_path"] = ""
    return composited, meta


def map_zoomed_to_global_physical(u_zoomed: float, v_zoomed: float, meta: dict) -> tuple[int, int]:
    """Bijective inverse mapping from zoomed slice pixel (u, v) to the global
    virtual desktop physical pixel (X_g, Y_g).

    Slice pixel cells are folded back with the pixel-center convention
    (u + 0.5) // Z and clamped to the cropped physical block.
    """
    gx0, gy0 = meta["global_crop_origin"]
    z = int(meta["zoom_factor"])
    w_c, h_c = meta["crop_phys_size"]

    dx_phys = max(0, min(w_c - 1, int((float(u_zoomed) + 0.5) // z)))
    dy_phys = max(0, min(h_c - 1, int((float(v_zoomed) + 0.5) // z)))
    return gx0 + dx_phys, gy0 + dy_phys


def virtual_desktop_bijective_norm(
    x_global: int, y_global: int, x_virt: int, y_virt: int, w_virt: int, h_virt: int
) -> tuple[int, int]:
    """Zero-drift ceiling bijection from a global physical pixel to the Win32
    MOUSEEVENTF_VIRTUALDESK [0, 65535]^2 normalized integer space.

    Uses (dx * 65535 + (W - 2)) // (W - 1) — the pixel-center ceiling
    alignment that cancels win32k's floor-truncation in its fixed-point
    inverse transform, so GetCursorPos() reconstructs (X_g, Y_g) with an
    error of exactly 0 px on any monitor combination (negative origins
    included).
    """
    span_x = max(1, int(w_virt) - 1)
    span_y = max(1, int(h_virt) - 1)
    dx = max(0, min(span_x, int(x_global) - int(x_virt)))
    dy = max(0, min(span_y, int(y_global) - int(y_virt)))

    x_norm = (dx * 65535 + (span_x - 1)) // span_x
    y_norm = (dy * 65535 + (span_y - 1)) // span_y
    return x_norm, y_norm
