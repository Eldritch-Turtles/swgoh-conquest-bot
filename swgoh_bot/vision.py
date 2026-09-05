"""Working out which screen the game is showing.

The approach is template matching, not machine learning. Each screen we care
about ("home", "conquest_map", "battle_result") gets a definition: one or more
small cropped images ("anchors") that are reliably present on that screen and
nowhere else, plus roughly where on the screen to look for them.

To classify a frame we score every known screen and take the best one that
clears its threshold. All anchors for a screen must match - scoring uses the
*weakest* anchor - which keeps false positives down.

Everything happens at the canonical working resolution (see config.py), so a
reference captured at 1080p still matches a frame captured at 1440p.

On disk a screen definition looks like:

    data/screens/home/
        screen.json
        anchor_conquest_tile.png

with screen.json:

    {
      "name": "home",
      "description": "Main hub, Conquest tile visible on the right",
      "threshold": 0.85,
      "anchors": [
        {"template": "anchor_conquest_tile.png", "region": [1180, 380, 340, 240]}
      ]
    }
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from swgoh_bot.capture import Frame, to_working_size
from swgoh_bot.config import SCREENS_DIR, WORK_HEIGHT, WORK_WIDTH

# Anchors have to clear this similarity score unless a screen overrides it.
# 0.85 is a reasonable starting point for TM_CCOEFF_NORMED on UI chrome; tune
# per screen once we have real reference images.
DEFAULT_THRESHOLD = 0.85

# Minimum pixel standard deviation an anchor template must have.
#
# This guard matters more than it looks. TM_CCOEFF_NORMED correlates against
# the mean-subtracted template, so a flat template has a zero numerator *and* a
# zero denominator, and OpenCV resolves that 0/0 as a perfect 1.0 score. Crop a
# plain slab of UI background as your anchor and it will match every screen in
# the game at full confidence. Rejecting flat crops up front is the fix.
MIN_ANCHOR_STDDEV = 8.0


def anchor_is_degenerate(template: np.ndarray) -> bool:
    """True if a template is too featureless to identify anything."""
    return float(np.std(template)) < MIN_ANCHOR_STDDEV

Region = tuple[int, int, int, int]  # x, y, w, h at working resolution


@dataclass(frozen=True)
class Anchor:
    """A small image expected to appear in a given part of the screen."""

    name: str
    template: np.ndarray  # grayscale
    region: Region | None = None  # None means "search the whole frame"

    def search_area(self, frame_gray: np.ndarray) -> tuple[np.ndarray, int, int]:
        """Crop the frame to this anchor's region.

        Returns the cropped image plus the (x, y) offset it was taken from, so
        match coordinates can be translated back to full-frame coordinates.
        """
        if self.region is None:
            return frame_gray, 0, 0
        x, y, w, h = self.region
        x = max(0, min(x, frame_gray.shape[1] - 1))
        y = max(0, min(y, frame_gray.shape[0] - 1))
        w = max(1, min(w, frame_gray.shape[1] - x))
        h = max(1, min(h, frame_gray.shape[0] - y))
        return frame_gray[y : y + h, x : x + w], x, y


@dataclass(frozen=True)
class AnchorMatch:
    """Where an anchor was found and how confidently."""

    anchor: str
    score: float
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        """Centre point, in working-resolution coordinates.

        This is what the (future) click layer will aim at.
        """
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(frozen=True)
class ScreenDefinition:
    """One recognisable game screen."""

    name: str
    anchors: tuple[Anchor, ...]
    threshold: float = DEFAULT_THRESHOLD
    description: str = ""

    def score(self, frame_gray: np.ndarray) -> tuple[float, list[AnchorMatch]]:
        """Score this screen against a frame.

        The screen's score is its weakest anchor: every anchor must be present
        for the screen to count as matched.
        """
        if not self.anchors:
            return 0.0, []

        matches: list[AnchorMatch] = []
        for anchor in self.anchors:
            area, off_x, off_y = anchor.search_area(frame_gray)
            th, tw = anchor.template.shape[:2]
            if th > area.shape[0] or tw > area.shape[1]:
                # Template bigger than the area we were told to search in;
                # that anchor simply cannot match.
                return 0.0, []
            result = cv2.matchTemplate(area, anchor.template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            matches.append(
                AnchorMatch(
                    anchor=anchor.name,
                    score=float(max_val),
                    x=off_x + max_loc[0],
                    y=off_y + max_loc[1],
                    width=tw,
                    height=th,
                )
            )

        return min(m.score for m in matches), matches


@dataclass
class Classification:
    """The verdict on one frame."""

    screen: str | None
    score: float
    scores: dict[str, float] = field(default_factory=dict)
    matches: list[AnchorMatch] = field(default_factory=list)
    source: str = ""

    @property
    def recognised(self) -> bool:
        return self.screen is not None

    def __str__(self) -> str:
        if self.screen is None:
            best = ""
            if self.scores:
                name, val = max(self.scores.items(), key=lambda kv: kv[1])
                best = f" (closest: {name} at {val:.3f})"
            return f"unknown{best}"
        return f"{self.screen} ({self.score:.3f})"


def load_screen(directory: Path) -> ScreenDefinition:
    """Load one screen definition from its folder."""
    manifest_path = directory / "screen.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    anchors: list[Anchor] = []
    for entry in manifest.get("anchors", []):
        template_path = directory / entry["template"]
        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise FileNotFoundError(f"Could not read anchor template: {template_path}")
        if anchor_is_degenerate(template):
            warnings.warn(
                f"Anchor {template_path} is nearly featureless "
                f"(stddev {float(np.std(template)):.1f}); it will match almost "
                f"any screen. Re-crop it around something distinctive.",
                stacklevel=2,
            )
        region = entry.get("region")
        anchors.append(
            Anchor(
                name=entry.get("name", Path(entry["template"]).stem),
                template=template,
                region=tuple(region) if region else None,
            )
        )

    return ScreenDefinition(
        name=manifest.get("name", directory.name),
        anchors=tuple(anchors),
        threshold=float(manifest.get("threshold", DEFAULT_THRESHOLD)),
        description=manifest.get("description", ""),
    )


class ScreenClassifier:
    """Holds every known screen and identifies frames against them."""

    def __init__(self, screens: list[ScreenDefinition] | None = None):
        self.screens = screens or []

    @classmethod
    def from_directory(cls, directory: Path | None = None) -> "ScreenClassifier":
        """Load every screen definition under a directory.

        An empty or missing directory is fine - you get a classifier that
        recognises nothing, which is exactly where we are before any reference
        screenshots have been captured.
        """
        root = Path(directory or SCREENS_DIR)
        screens: list[ScreenDefinition] = []
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if (child / "screen.json").is_file():
                    screens.append(load_screen(child))
        return cls(screens)

    def classify(self, frame: Frame | np.ndarray) -> Classification:
        """Identify a frame. Returns screen=None when nothing matches."""
        if isinstance(frame, Frame):
            image, source = frame.image, frame.source
        else:
            image, source = frame, ""

        gray = cv2.cvtColor(to_working_size(image), cv2.COLOR_BGR2GRAY)

        best: tuple[ScreenDefinition, float, list[AnchorMatch]] | None = None
        scores: dict[str, float] = {}

        for screen in self.screens:
            score, matches = screen.score(gray)
            scores[screen.name] = score
            if score >= screen.threshold and (best is None or score > best[1]):
                best = (screen, score, matches)

        if best is None:
            return Classification(screen=None, score=0.0, scores=scores, source=source)
        return Classification(
            screen=best[0].name,
            score=best[1],
            scores=scores,
            matches=best[2],
            source=source,
        )


def save_screen_definition(
    name: str,
    frame: Frame | np.ndarray,
    region: Region,
    directory: Path | None = None,
    description: str = "",
    threshold: float = DEFAULT_THRESHOLD,
    search_padding: int = 40,
) -> Path:
    """Create a screen definition by cropping an anchor out of a frame.

    This is the tool we use to teach the bot a new screen: capture a frame,
    pick out a region that uniquely identifies it, and this writes the template
    and manifest to disk.

    The anchor is searched for in a slightly larger box than it was cropped
    from (`search_padding`), which absorbs small layout shifts between the
    reference and a live frame.
    """
    image = frame.image if isinstance(frame, Frame) else frame
    work = to_working_size(image)

    x, y, w, h = region
    if w <= 0 or h <= 0:
        raise ValueError(f"Region must have positive width and height, got {region}")
    if x < 0 or y < 0 or x + w > WORK_WIDTH or y + h > WORK_HEIGHT:
        raise ValueError(
            f"Region {region} falls outside the {WORK_WIDTH}x{WORK_HEIGHT} working frame"
        )

    target = Path(directory or SCREENS_DIR) / name
    target.mkdir(parents=True, exist_ok=True)

    template = work[y : y + h, x : x + w]
    if anchor_is_degenerate(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)):
        raise ValueError(
            f"Region {region} is nearly featureless and would match any screen. "
            f"Pick a region containing text, an icon, or a strong edge."
        )
    template_name = f"anchor_{name}.png"
    cv2.imwrite(str(target / template_name), template)

    # Widen the search window around where the anchor was cropped from.
    sx = max(0, x - search_padding)
    sy = max(0, y - search_padding)
    sw = min(WORK_WIDTH - sx, w + search_padding * 2)
    sh = min(WORK_HEIGHT - sy, h + search_padding * 2)

    manifest = {
        "name": name,
        "description": description,
        "threshold": threshold,
        "anchors": [
            {"name": name, "template": template_name, "region": [sx, sy, sw, sh]}
        ],
    }
    (target / "screen.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    # Keep the full reference frame too. Invaluable when a match starts failing
    # and you need to see what the screen actually looked like.
    cv2.imwrite(str(target / "reference.png"), work)

    return target
