"""Find the SWGOH PC client window on Windows.

Uses ctypes against user32 directly rather than pywin32, so there is nothing
extra to install. Everything here is a no-op on non-Windows machines - the
module still imports, so the rest of the bot can be developed and tested on
Linux or macOS.

We report the *client* rectangle (the game's actual pixels) rather than the
window rectangle, which would include the title bar and borders.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Iterable

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class WindowInfo:
    """A top-level window's client area, in screen coordinates."""

    hwnd: int
    title: str
    class_name: str
    left: int
    top: int
    width: int
    height: int

    @property
    def bbox(self) -> dict[str, int]:
        """Bounding box in the shape `mss` wants for a region grab."""
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }

    def __str__(self) -> str:
        return (
            f"hwnd={self.hwnd:<10} {self.width:>5}x{self.height:<5} "
            f"@({self.left},{self.top})  [{self.class_name}]  {self.title!r}"
        )


class WindowNotFoundError(RuntimeError):
    """Raised when the game window could not be located."""


if IS_WINDOWS:  # pragma: no cover - needs a real Windows host
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]


def ensure_dpi_aware() -> None:
    """Tell Windows we handle display scaling ourselves.

    Without this, a machine on 125%/150% scaling reports fake "logical" pixel
    coordinates. Screenshots then come back the wrong size and every click
    lands slightly off. Call this once, early, before touching any window.

    Tries the modern API first and falls back for older Windows versions.
    """
    if not IS_WINDOWS:
        return
    # -4 == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _describe(hwnd: int, title: str, class_name: str) -> WindowInfo | None:
    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    origin = wintypes.POINT(rect.left, rect.top)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    return WindowInfo(hwnd, title, class_name, origin.x, origin.y, width, height)


def enumerate_windows(visible_only: bool = True) -> list[WindowInfo]:
    """List top-level windows that have a title.

    Returns an empty list off Windows. This is the discovery tool: run it with
    the game open to find out what the client actually calls itself.
    """
    if not IS_WINDOWS:
        return []

    ensure_dpi_aware()
    found: list[WindowInfo] = []

    def callback(hwnd, _lparam):  # pragma: no cover - needs Windows
        if visible_only and not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        info = _describe(hwnd, buf.value, cls.value)
        if info is not None:
            found.append(info)
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found


def find_window(title_contains: str | Iterable[str]) -> WindowInfo:
    """Find one window whose title contains any of the given substrings.

    Matching is case-insensitive. Candidates are tried in order, so put your
    best guess first. Raises WindowNotFoundError with the list of what *was*
    open, which is far more useful than a bare failure.
    """
    if isinstance(title_contains, str):
        candidates = [title_contains]
    else:
        candidates = list(title_contains)

    windows = enumerate_windows()
    for needle in candidates:
        lowered = needle.lower()
        for win in windows:
            if lowered in win.title.lower():
                return win

    if not IS_WINDOWS:
        raise WindowNotFoundError(
            "Window lookup only works on Windows. On this machine, use the "
            "replay capture backend instead (see README)."
        )
    listing = "\n".join(f"  {w}" for w in windows) or "  (no titled windows found)"
    raise WindowNotFoundError(
        f"No window matched any of {candidates}.\nOpen windows were:\n{listing}"
    )


def find_game_window() -> WindowInfo:
    """Locate the SWGOH PC client using the configured title guesses."""
    from swgoh_bot.config import WINDOW_TITLE_CANDIDATES

    return find_window(WINDOW_TITLE_CANDIDATES)
