"""Screen capture for vision-aware questions — JARVIS can see what you select.

A drag-to-select capture of a screen region, downscaled and base64-encoded so
it can ride alongside a chat message as an image content block (see
``claude_client.send(image_b64=...)``). Pure image I/O only — no window
management here; ``app/overlay.py`` owns the selection UI and calls
:func:`capture_region` with the coordinates the user dragged.

Degrades the same way the rest of the optional features do:
:func:`capture_available` is False when Pillow's screen-grab isn't usable on
this platform (``ImageGrab.grab()`` needs extra tooling on Linux/macOS; it
works natively on Windows, the primary target), and the capture button simply
disappears rather than crashing.
"""

from __future__ import annotations

import base64
import io

from .logging_setup import get_logger

log = get_logger("screenshot")

# Anthropic's documented sweet spot for image cost/quality — a screenshot
# larger than this on its longest side is downscaled before encoding so a 4K
# capture doesn't balloon token cost for no quality benefit.
MAX_DIMENSION = 1568


def capture_available() -> bool:
    """Whether screen capture is possible on this platform (best-effort probe)."""
    try:
        from PIL import ImageGrab
        return hasattr(ImageGrab, "grab")
    except Exception as exc:  # noqa: BLE001
        log.debug("screen capture unavailable: %s", exc)
        return False


def normalize_bbox(x1: float, y1: float, x2: float, y2: float) -> tuple[int, int, int, int]:
    """``(left, top, right, bottom)`` regardless of which corner the drag
    started from — a bottom-right-to-top-left drag is just as valid as any
    other direction."""
    left, right = sorted((int(x1), int(x2)))
    top, bottom = sorted((int(y1), int(y2)))
    return left, top, right, bottom


def capture_region(x1: float, y1: float, x2: float, y2: float):
    """Grab the screen rectangle between the two points. Returns a PIL Image."""
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=normalize_bbox(x1, y1, x2, y2))


def to_base64_png(image, max_dim: int = MAX_DIMENSION) -> str:
    """Downscale *image* to fit within *max_dim* on its longest side (if
    needed) and base64-encode it as PNG for an Anthropic image content block.
    """
    from PIL import Image

    w, h = image.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
        image = image.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
