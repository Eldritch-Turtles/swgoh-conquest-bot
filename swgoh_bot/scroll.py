"""Scrollable screens.

The Conquest sector map is a canvas larger than the window. The player drags it
left and right, so the same *screen* has endless different appearances and a
node's pixel position means nothing on its own. Two consequences:

  1. Screen identity cannot come from the scrolling content. It has to come
     from the fixed HUD - the header, the sector label, the energy bar - which
     stays put however far you scroll. Capture a burst *while scrolling* and
     vision.stability_mask finds that HUD for you: whatever holds still across
     a scroll is, by definition, not part of the scrolling content.

  2. Finding something in the content needs position-independent search
     (vision.find_all) plus a way to convert between what is on screen now and
     a stable world coordinate. That is what this module provides.

Scroll is measured with phase correlation, which recovers the translation
between two images in the frequency domain. Measured against known shifts it
is accurate to a hundredth of a pixel, and it is not thrown off by the HUD
sitting in frame.

Conventions, since sign errors here are easy and silent:

  Shift.dx is how far the *content* moved inside the viewport. Drag the map to
  reveal things on the right and the content slides left, so dx is negative.
  The scroll position moves the opposite way, by -dx. ScrollTracker handles
  that, and `world = viewport + offset`.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from swgoh_bot.capture import Frame, to_working_size

# Phase correlation returns a response in roughly 0..1. A featureless or
# radically changed pair scores low, and acting on such a reading would corrupt
# the tracked offset, so measurements below this are treated as untrusted.
MIN_RESPONSE = 0.05

Region = tuple[int, int, int, int]


@dataclass(frozen=True)
class Shift:
    """How far content moved between two frames, in working-resolution pixels."""

    dx: float
    dy: float
    response: float

    @property
    def trustworthy(self) -> bool:
        return self.response >= MIN_RESPONSE

    @property
    def magnitude(self) -> float:
        return float(np.hypot(self.dx, self.dy))

    def __str__(self) -> str:
        return f"dx={self.dx:+.1f} dy={self.dy:+.1f} (response {self.response:.3f})"


def _prepare(frame, region: Region | None) -> np.ndarray:
    image = frame.image if isinstance(frame, Frame) else frame
    work = to_working_size(image)
    gray = work if work.ndim == 2 else cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    if region is not None:
        x, y, w, h = region
        gray = gray[y : y + h, x : x + w]
    return gray.astype(np.float32)


def measure_shift(before, after, region: Region | None = None) -> Shift:
    """Measure how far the content moved between two frames.

    Pass `region` to restrict the measurement to the scrolling viewport,
    excluding the fixed HUD. Not strictly required - the measurement survives
    the HUD being included - but it raises the confidence response.
    """
    first = _prepare(before, region)
    second = _prepare(after, region)
    if first.shape != second.shape:
        raise ValueError(
            f"Frames must be the same size to compare: {first.shape} vs {second.shape}"
        )

    # Hanning window suppresses the edge discontinuity that would otherwise
    # dominate the transform.
    window = cv2.createHanningWindow((first.shape[1], first.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(first, second, window)
    return Shift(dx=float(dx), dy=float(dy), response=float(response))


class ScrollTracker:
    """Keeps a running scroll position so screen pixels map to stable coordinates.

    Feed it each new frame and it accumulates how far the content has moved.
    `to_world` then converts a position on screen now into a coordinate that
    stays valid as the map scrolls, and `to_viewport` converts back - which is
    how the bot works out whether a target is on screen and, if not, which way
    to drag.

    Accumulated offsets drift, since every measurement carries a little error.
    Re-anchor on something known rather than trusting this indefinitely.
    """

    def __init__(self, region: Region | None = None, min_response: float = MIN_RESPONSE):
        self.region = region
        self.min_response = min_response
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.rejected = 0
        self._previous = None

    def update(self, frame) -> Shift | None:
        """Add a frame. Returns the measured shift, or None if it was the first
        frame or the measurement was too weak to trust."""
        if self._previous is None:
            self._previous = _prepare(frame, self.region)
            return None

        current = _prepare(frame, self.region)
        if current.shape != self._previous.shape:
            raise ValueError("Frame size changed mid-track")

        window = cv2.createHanningWindow(
            (current.shape[1], current.shape[0]), cv2.CV_32F
        )
        (dx, dy), response = cv2.phaseCorrelate(self._previous, current, window)
        shift = Shift(dx=float(dx), dy=float(dy), response=float(response))

        if shift.response < self.min_response:
            # Probably a screen change rather than a scroll. Re-base on this
            # frame but leave the offset alone.
            self._previous = current
            self.rejected += 1
            return None

        self.offset_x -= shift.dx
        self.offset_y -= shift.dy
        self._previous = current
        return shift

    @property
    def offset(self) -> tuple[float, float]:
        return self.offset_x, self.offset_y

    def to_world(self, x: float, y: float) -> tuple[float, float]:
        """Screen position now -> stable world coordinate."""
        return x + self.offset_x, y + self.offset_y

    def to_viewport(self, world_x: float, world_y: float) -> tuple[float, float]:
        """World coordinate -> where it sits on screen now (may be off-screen)."""
        return world_x - self.offset_x, world_y - self.offset_y

    def reset(self) -> None:
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.rejected = 0
        self._previous = None


@dataclass
class Panorama:
    """A scrolling area reassembled into one image."""

    image: np.ndarray
    offsets: list[tuple[float, float]]  # each source frame's position on the canvas

    @property
    def size(self) -> tuple[int, int]:
        return self.image.shape[1], self.image.shape[0]


def stitch(frames, region: Region | None = None, min_response: float = MIN_RESPONSE) -> Panorama:
    """Reassemble a scrolled sequence into a single wide image.

    Drag the map from one end to the other while capturing, pass the frames in
    that order, and this reconstructs the whole sector as one picture. Every
    node can then be located once, in coordinates that do not change as the
    view scrolls.

    `region` should be the scrolling viewport. Without it the fixed HUD is
    pasted repeatedly across the canvas and smears over the content.
    """
    if len(frames) < 2:
        raise ValueError("Need at least 2 frames to stitch")

    def pixels(frame):
        image = frame.image if isinstance(frame, Frame) else frame
        work = to_working_size(image)
        if region is None:
            return work
        x, y, w, h = region
        return work[y : y + h, x : x + w]

    tiles = [pixels(f) for f in frames]

    # Each frame's scroll position, relative to the first.
    positions: list[tuple[float, float]] = [(0.0, 0.0)]
    for before, after in zip(frames, frames[1:]):
        shift = measure_shift(before, after, region)
        previous_x, previous_y = positions[-1]
        if shift.response < min_response:
            # Unmeasurable - assume it did not move rather than inventing a jump.
            positions.append((previous_x, previous_y))
        else:
            positions.append((previous_x - shift.dx, previous_y - shift.dy))

    min_x = min(p[0] for p in positions)
    min_y = min(p[1] for p in positions)
    canvas_positions = [
        (int(round(px - min_x)), int(round(py - min_y))) for px, py in positions
    ]

    tile_h, tile_w = tiles[0].shape[:2]
    width = max(px for px, _ in canvas_positions) + tile_w
    height = max(py for _, py in canvas_positions) + tile_h

    channels = tiles[0].shape[2] if tiles[0].ndim == 3 else 1
    shape = (height, width, channels) if channels > 1 else (height, width)
    canvas = np.zeros(shape, dtype=np.uint8)

    # Paste in order; later frames overwrite earlier ones where they overlap.
    for tile, (px, py) in zip(tiles, canvas_positions):
        canvas[py : py + tile_h, px : px + tile_w] = tile

    return Panorama(
        image=canvas, offsets=[(float(px), float(py)) for px, py in canvas_positions]
    )
