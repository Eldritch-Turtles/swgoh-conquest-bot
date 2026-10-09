"""Getting pixels out of the game.

Three backends, all producing the same thing so the rest of the bot never has
to care where a frame came from:

  MSSBackend     - screenshots the game window on Windows. Simple, no extra
                   dependencies, but the window must be visible on screen.
  WGCBackend     - Windows Graphics Capture. Works even when the window is
                   behind others. Needs `windows-capture` installed. Optional.
  ReplayBackend  - reads PNGs off disk. Runs anywhere, which is what makes the
                   vision code testable without a Windows box or the game.

Frames are BGR numpy arrays, matching OpenCV's convention.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from swgoh_bot.config import WORK_HEIGHT, WORK_WIDTH
from swgoh_bot.window import WindowInfo, find_game_window


@dataclass
class Frame:
    """One captured image plus where it came from."""

    image: np.ndarray  # BGR, shape (h, w, 3)
    source: str
    captured_at: float = field(default_factory=time.time)

    @property
    def size(self) -> tuple[int, int]:
        """(width, height)."""
        return self.image.shape[1], self.image.shape[0]

    def to_working_size(self) -> "Frame":
        """Scale to the canonical working resolution used by all matching."""
        return Frame(
            image=to_working_size(self.image),
            source=self.source,
            captured_at=self.captured_at,
        )


def to_working_size(image: np.ndarray) -> np.ndarray:
    """Resize an image to WORK_WIDTH x WORK_HEIGHT.

    Aspect ratio is deliberately *not* preserved. Everything - frames and
    reference templates alike - goes through this function, so as long as the
    game window keeps a consistent aspect ratio the distortion is identical on
    both sides and matching is unaffected.

    INTER_AREA is the right choice when shrinking, which is the normal case
    (1080p and up down to 1600x900).
    """
    if image.shape[1] == WORK_WIDTH and image.shape[0] == WORK_HEIGHT:
        return image
    shrinking = image.shape[1] > WORK_WIDTH
    interp = cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR
    return cv2.resize(image, (WORK_WIDTH, WORK_HEIGHT), interpolation=interp)


class CaptureBackend(ABC):
    """Anything that can hand us a frame."""

    @abstractmethod
    def grab(self) -> Frame:
        """Capture one frame."""

    def close(self) -> None:
        """Release resources. Safe to call more than once."""

    def __enter__(self) -> "CaptureBackend":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class ReplayBackend(CaptureBackend):
    """Serves images from disk instead of a live game.

    Point it at a file or a directory of PNGs. Each grab() returns the next
    image, stopping on the last one rather than raising, so a caller polling in
    a loop just sees a frozen screen.

    This is how the vision layer gets developed and tested without Windows.
    """

    def __init__(self, path: str | Path, loop: bool = False):
        self.path = Path(path)
        if self.path.is_dir():
            self.paths = sorted(
                p
                for p in self.path.iterdir()
                if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
            )
        elif self.path.is_file():
            self.paths = [self.path]
        else:
            raise FileNotFoundError(f"No such file or directory: {self.path}")

        if not self.paths:
            raise FileNotFoundError(f"No images found in {self.path}")

        self.loop = loop
        self._index = 0

    def grab(self) -> Frame:
        path = self.paths[self._index]
        if self.loop:
            self._index = (self._index + 1) % len(self.paths)
        elif self._index < len(self.paths) - 1:
            self._index += 1

        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Could not decode image: {path}")
        return Frame(image=image, source=f"replay:{path.name}")


class MSSBackend(CaptureBackend):
    """Screenshots a region of the desktop with `mss`.

    Re-resolves the game window on every grab by default, so moving or resizing
    the window mid-run doesn't break things. Note the window must actually be
    visible: minimise it or bury it behind another window and you capture
    whatever is on top instead.
    """

    def __init__(self, window: WindowInfo | None = None, follow_window: bool = True):
        import mss  # imported here so the module still loads without it

        self._sct = mss.mss()
        self._window = window
        self.follow_window = follow_window and window is None

    def _resolve(self) -> WindowInfo:
        if self.follow_window or self._window is None:
            self._window = find_game_window()
        return self._window

    def grab(self) -> Frame:
        window = self._resolve()
        raw = self._sct.grab(window.bbox)
        # mss gives BGRA; drop alpha to get OpenCV's BGR.
        image = np.asarray(raw, dtype=np.uint8)[:, :, :3]
        return Frame(image=np.ascontiguousarray(image), source=f"mss:{window.title}")

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass


class WGCBackend(CaptureBackend):
    """Windows Graphics Capture - grabs the window even when it's occluded.

    Preferred over MSS once it's working, because it captures the game's own
    surface rather than whatever pixels happen to be on screen. Requires
    `pip install windows-capture`. Treated as optional throughout.
    """

    def __init__(self, window: WindowInfo | None = None):
        from windows_capture import WindowsCapture  # type: ignore

        self._window = window or find_game_window()
        self._latest: np.ndarray | None = None

        self._capture = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            window_name=self._window.title,
        )

        @self._capture.event
        def on_frame_arrived(frame, _control):  # pragma: no cover - Windows only
            self._latest = frame.frame_buffer[:, :, :3].copy()

        @self._capture.event
        def on_closed():  # pragma: no cover - Windows only
            self._latest = None

        self._control = self._capture.start_free_threaded()

    def grab(self) -> Frame:
        deadline = time.time() + 2.0
        while self._latest is None and time.time() < deadline:
            time.sleep(0.01)
        if self._latest is None:
            raise RuntimeError(
                "Windows Graphics Capture produced no frames. Is the game running?"
            )
        return Frame(
            image=np.ascontiguousarray(self._latest),
            source=f"wgc:{self._window.title}",
        )

    def close(self) -> None:
        try:
            self._control.stop()
        except Exception:
            pass


def create_backend(kind: str = "auto", **kwargs) -> CaptureBackend:
    """Build a capture backend by name.

    "auto" picks WGC if it's installed, otherwise MSS. Pass "replay" with
    `path=...` to read from disk.
    """
    kind = kind.lower()

    if kind == "replay":
        return ReplayBackend(**kwargs)
    if kind == "mss":
        return MSSBackend(**kwargs)
    if kind == "wgc":
        return WGCBackend(**kwargs)
    if kind == "auto":
        try:
            return WGCBackend(**kwargs)
        except Exception:
            return MSSBackend(**kwargs)

    raise ValueError(f"Unknown capture backend: {kind!r}")


# How long to wait between frames in a burst. Long enough that an animation
# has visibly moved, short enough that a burst is still quick.
BURST_INTERVAL = 0.2


def grab_burst(
    backend: CaptureBackend, count: int = 5, interval: float = BURST_INTERVAL
) -> list[Frame]:
    """Capture several frames spaced apart in time.

    A burst is what lets us tell static UI from animation: diff the frames and
    whatever changed is moving. See vision.stability_mask.
    """
    if count < 2:
        raise ValueError("A burst needs at least 2 frames to show movement")

    frames = [backend.grab()]
    for _ in range(count - 1):
        time.sleep(interval)
        frames.append(backend.grab())
    return frames


def frames_are_identical(frames: list[Frame], tolerance: int = 2) -> bool:
    """True if nothing moved at all across the burst.

    Usually means the burst came from a single replayed PNG rather than a live
    game, which would make any stability analysis meaningless.
    """
    if len(frames) < 2:
        return True
    first = frames[0].image
    return all(
        other.image.shape == first.shape
        and int(np.abs(other.image.astype(np.int16) - first.astype(np.int16)).max())
        <= tolerance
        for other in frames[1:]
    )


def grab_stable(
    backend: CaptureBackend,
    tolerance: float = 1.5,
    max_wait: float = 8.0,
    interval: float = 0.25,
) -> tuple[Frame, bool]:
    """Wait for the screen to settle, then return a frame.

    Screen transitions in SWGOH slide and fade. Classifying mid-transition
    gives a blend of two screens that matches neither, so anything that acts on
    the current screen should settle first.

    Compares successive frames by mean absolute difference and returns as soon
    as two in a row are near-identical. Returns (frame, settled) - settled is
    False if max_wait ran out, which is normal on a permanently animated
    screen, and the caller can still use the frame.
    """
    deadline = time.time() + max_wait
    previous = backend.grab()

    while time.time() < deadline:
        time.sleep(interval)
        current = backend.grab()
        if current.image.shape == previous.image.shape:
            difference = float(
                np.abs(
                    current.image.astype(np.int16) - previous.image.astype(np.int16)
                ).mean()
            )
            if difference <= tolerance:
                return current, True
        previous = current

    return previous, False
