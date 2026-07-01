"""Tests for the screenshot capture/encoding helpers (app/screenshot.py).

Only the pure image I/O is covered here (resize + base64 PNG encode) — no
display/X server needed for that. capture_region() itself calls
PIL.ImageGrab.grab(), which needs a real screen, so it isn't exercised here;
that part needs to be confirmed on the user's actual machine.
"""

from __future__ import annotations

import base64
import io

from PIL import Image

from app.screenshot import MAX_DIMENSION, capture_available, normalize_bbox, to_base64_png


# ── normalize_bbox ────────────────────────────────────────────────────────────
def test_normalize_bbox_top_left_to_bottom_right():
    assert normalize_bbox(10, 20, 110, 220) == (10, 20, 110, 220)


def test_normalize_bbox_bottom_right_to_top_left():
    # Dragging in the opposite direction must produce the same rectangle.
    assert normalize_bbox(110, 220, 10, 20) == (10, 20, 110, 220)


def test_normalize_bbox_mixed_corners():
    assert normalize_bbox(110, 20, 10, 220) == (10, 20, 110, 220)


def test_normalize_bbox_floats_are_truncated_to_ints():
    left, top, right, bottom = normalize_bbox(10.7, 20.2, 110.4, 220.9)
    assert (left, top, right, bottom) == (10, 20, 110, 220)
    assert all(isinstance(v, int) for v in (left, top, right, bottom))


def _decode(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def test_capture_available_true_when_pillow_has_imagegrab():
    # PIL is installed in this environment (a dev-test dependency), and
    # ImageGrab always exposes .grab() regardless of platform support.
    assert capture_available() is True


def test_small_image_is_not_resized():
    img = Image.new("RGB", (400, 300), color=(1, 2, 3))
    decoded = _decode(to_base64_png(img))
    assert decoded.size == (400, 300)
    assert decoded.format == "PNG"


def test_oversized_image_is_downscaled_to_max_dimension():
    img = Image.new("RGB", (3840, 2160), color=(10, 20, 30))
    decoded = _decode(to_base64_png(img))
    assert max(decoded.size) == MAX_DIMENSION
    # Aspect ratio preserved (16:9 in, 16:9 out).
    assert decoded.size == (MAX_DIMENSION, round(MAX_DIMENSION * 2160 / 3840))


def test_downscale_respects_custom_max_dim():
    img = Image.new("RGB", (2000, 1000), color=(0, 0, 0))
    decoded = _decode(to_base64_png(img, max_dim=500))
    assert max(decoded.size) == 500


def test_encoded_output_is_valid_base64_png_round_trip():
    img = Image.new("RGB", (100, 50), color=(255, 0, 0))
    b64 = to_base64_png(img)
    # Round-trips to an image with matching pixel data (lossless PNG).
    decoded = _decode(b64)
    assert decoded.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
