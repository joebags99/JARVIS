"""Small Win32 helpers shared by every pywebview window JARVIS owns.

Split out of overlay.py once a second persistent window (app/hud.py) needed
the same primary-display-size and layered-transparency logic — kept here so
neither module has to import the other's "private" helpers or duplicate the
ctypes code.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from .logging_setup import get_logger

log = get_logger("screen")


def screen_size() -> tuple[int, int]:
    """Primary display size, used to position a window on startup."""
    if sys.platform.startswith("win"):
        try:
            user32 = ctypes.windll.user32
            return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:  # noqa: BLE001
            pass
    return 1920, 1080


def enable_real_transparency(title: str, alpha: int) -> None:
    """Blend a top-level window against the desktop with a constant alpha.

    pywebview's own ``transparent=True`` only sets the WebView2 control's
    background to transparent *inside* the host Form (avoiding a white
    flash and giving the rounded corners clean edges) — the Form itself is
    still a perfectly opaque top-level window as far as the desktop
    compositor is concerned, which is why the window never actually showed
    anything behind it no matter what alpha the CSS background used. Real
    desktop blending needs the Win32 layered-window attribute, which
    pywebview doesn't set up, so we do it ourselves here. *title* must match
    the window's exact title (used to look up its HWND via FindWindowW).
    """
    if not sys.platform.startswith("win"):
        return
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            log.warning("could not find window handle for layered transparency (%s)", title)
            return

        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        LWA_ALPHA = 0x2

        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
        log.info("layered window transparency enabled (%s, alpha=%d)", title, alpha)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not enable layered transparency (%s): %s", title, exc)
