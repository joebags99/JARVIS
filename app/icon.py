"""Programmatic tray icon generation.

Avoids committing a binary asset: the icon is drawn with Pillow at runtime. The
design is a simple cyan ring ("arc reactor" nod) on a near-black background,
with brightness varying by state so the tray reflects what JARVIS is doing.
"""

from __future__ import annotations

from .config import CONFIG, TRAY_ICON_PATH
from .logging_setup import get_logger

log = get_logger("icon")

# State -> (ring_color, glow_alpha)
_STATE_COLORS = {
    "idle": ("#006f7e", 90),       # dim
    "listening": ("#00e5ff", 220),  # bright
    "thinking": ("#00bcd4", 160),   # mid
}


def make_icon(state: str = "idle", size: int = 64):
    """Return a PIL.Image for the given state (or None if Pillow is missing)."""
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # noqa: BLE001
        log.warning("Pillow not available, cannot draw icon: %s", exc)
        return None

    ring, _alpha = _STATE_COLORS.get(state, _STATE_COLORS["idle"])
    img = Image.new("RGBA", (size, size), (15, 15, 15, 0))
    draw = ImageDraw.Draw(img)

    pad = 6
    # Outer ring
    draw.ellipse(
        [pad, pad, size - pad, size - pad],
        outline=ring,
        width=5,
    )
    # Inner core
    core_pad = size // 3
    draw.ellipse(
        [core_pad, core_pad, size - core_pad, size - core_pad],
        fill=ring,
    )
    return img


def ensure_icon_file() -> None:
    """Write a default idle icon to assets/ if one doesn't already exist."""
    if TRAY_ICON_PATH.exists():
        return
    img = make_icon("idle")
    if img is None:
        return
    try:
        img.save(TRAY_ICON_PATH)
        log.info("generated default tray icon at %s", TRAY_ICON_PATH.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save tray icon: %s", exc)


def load_icon(state: str = "idle"):
    """Load the on-disk icon, falling back to a freshly drawn one."""
    try:
        from PIL import Image

        if state == "idle" and TRAY_ICON_PATH.exists():
            return Image.open(TRAY_ICON_PATH)
    except Exception:  # noqa: BLE001
        pass
    return make_icon(state)
