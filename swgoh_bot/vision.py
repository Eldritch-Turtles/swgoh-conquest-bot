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

# How much a pixel may vary across a burst of frames and still count as
# static. Generous enough to absorb compression and dithering noise, tight
# enough to catch a slow pulse or drifting background.
STABILITY_TOLERANCE = 10

# The variance floor for a *masked* anchor is far lower than MIN_ANCHOR_STDDEV,
# because the two cases fail in opposite directions.
#
# An unmasked flat template is dangerous: it scores a perfect 1.0 against every
# screen, so it must be rejected outright. A masked template whose surviving
# pixels are all one value is merely useless: masked correlation returns 0.0
# against everything, matching and unrelated scenes alike, so the anchor simply
# never fires. Measured: a masked anchor with a standard deviation of only 6.4
# still separated its own screen from another at 0.998 versus 0.065.
#
# So masked anchors get a floor that catches only the perfectly uniform case,
# and correctness is established by the self-match check below instead of by
# this statistic.
MIN_MASKED_ANCHOR_STDDEV = 1.0


def anchor_is_degenerate(template: np.ndarray) -> bool:
    """True if a template is too featureless to identify anything."""
    return float(np.std(template)) < MIN_ANCHOR_STDDEV


def _as_gray(frame) -> np.ndarray:
    """Accept a Frame or an array; return working-size grayscale."""
    image = frame.image if isinstance(frame, Frame) else frame
    work = to_working_size(image)
    if work.ndim == 2:
        return work
    return cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)


def stability_mask(frames, tolerance: int = STABILITY_TOLERANCE) -> np.ndarray:
    """Find which pixels held still across a burst of frames.

    Returns a mask: 255 where the pixel barely changed, 0 where it moved.

    This is what makes matching survive an animated game. SWGOH screens have
    drifting starfields, breathing character portraits, pulsing buttons and
    scrolling banners. A template cropped from such a region will never match
    twice. Capture several frames a moment apart, diff them, and the animated
    pixels reveal themselves - then we either avoid them or mask them out.

    Uses max-min spread rather than variance so a single flickering frame
    can't be averaged away.
    """
    if len(frames) < 2:
        raise ValueError("Need at least 2 frames to tell what is moving")

    stack = np.stack([_as_gray(f) for f in frames]).astype(np.int16)
    spread = stack.max(axis=0) - stack.min(axis=0)
    return np.where(spread <= tolerance, 255, 0).astype(np.uint8)


def detail_map(gray: np.ndarray, ksize: int = 9) -> np.ndarray:
    """Local standard deviation - how much texture sits around each pixel.

    Used to steer anchor suggestions towards text and icons rather than flat
    panels, since a flat crop matches everything (see MIN_ANCHOR_STDDEV).
    """
    values = gray.astype(np.float32)
    mean = cv2.blur(values, (ksize, ksize))
    mean_square = cv2.blur(values * values, (ksize, ksize))
    return np.sqrt(np.maximum(mean_square - mean * mean, 0.0))


@dataclass(frozen=True)
class AnchorSuggestion:
    """A proposed anchor region: stable across the burst and full of detail."""

    x: int
    y: int
    width: int
    height: int
    stable_fraction: float
    detail: float

    @property
    def region(self) -> Region:
        return self.x, self.y, self.width, self.height

    @property
    def cli_region(self) -> str:
        """Formatted for `learn --region`."""
        return f"{self.x},{self.y},{self.width},{self.height}"

    def overlaps(self, other: "AnchorSuggestion") -> bool:
        return not (
            self.x + self.width <= other.x
            or other.x + other.width <= self.x
            or self.y + self.height <= other.y
            or other.y + other.height <= self.y
        )


def suggest_anchors(
    frames,
    count: int = 5,
    size: tuple[int, int] = (220, 80),
    stride: int = 40,
    min_stable: float = 0.995,
    min_detail: float = MIN_ANCHOR_STDDEV,
    tolerance: int = STABILITY_TOLERANCE,
) -> list[AnchorSuggestion]:
    """Propose anchor regions that are both static and distinctive.

    Slides a window over the frame and keeps the ones that are stable enough
    across the burst and rich enough in detail, then returns the best
    non-overlapping few. This answers "where should I crop?" with evidence
    instead of guesswork.

    `min_stable` is the fraction of the window that must hold still. The
    default demands a near-perfectly static region, which gives the simplest
    anchors. Lower it to find regions that are only partly static - text over
    a moving backdrop, say - which still work as anchors provided they are
    taught with a burst so the moving pixels get masked out.
    """
    mask = stability_mask(frames, tolerance=tolerance)
    mean_gray = np.mean(
        np.stack([_as_gray(f) for f in frames]).astype(np.float32), axis=0
    ).astype(np.uint8)
    detail = detail_map(mean_gray)

    width, height = size
    frame_h, frame_w = mask.shape
    candidates: list[AnchorSuggestion] = []

    for y in range(0, frame_h - height + 1, stride):
        for x in range(0, frame_w - width + 1, stride):
            window = mask[y : y + height, x : x + width]
            stable_fraction = float(window.mean()) / 255.0
            if stable_fraction < min_stable:
                continue

            # Measure detail over the static pixels only. Animated noise has a
            # high local standard deviation, so scoring the whole window would
            # rank a patch of moving starfield above a line of solid text.
            static = detail[y : y + height, x : x + width][window > 0]
            if static.size == 0:
                continue
            window_detail = float(static.mean())
            if window_detail < min_detail:
                continue
            candidates.append(
                AnchorSuggestion(
                    x, y, width, height, stable_fraction, window_detail
                )
            )

    candidates.sort(key=lambda c: c.detail, reverse=True)

    chosen: list[AnchorSuggestion] = []
    for candidate in candidates:
        if any(candidate.overlaps(picked) for picked in chosen):
            continue
        chosen.append(candidate)
        if len(chosen) >= count:
            break
    return chosen

Region = tuple[int, int, int, int]  # x, y, w, h at working resolution


@dataclass(frozen=True)
class Anchor:
    """A small image expected to appear in a given part of the screen.

    An optional mask marks which of the template's own pixels to compare.
    Pixels set to 0 are ignored entirely, which is how an anchor can span a
    region that is partly animated: the static text is matched, the moving
    background behind it is skipped.
    """

    name: str
    template: np.ndarray  # grayscale
    region: Region | None = None  # None means "search the whole frame"
    mask: np.ndarray | None = None  # grayscale; 0 = ignore this pixel

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
            if anchor.mask is None:
                result = cv2.matchTemplate(area, anchor.template, cv2.TM_CCOEFF_NORMED)
            else:
                result = cv2.matchTemplate(
                    area, anchor.template, cv2.TM_CCOEFF_NORMED, mask=anchor.mask
                )
                # Masked correlation can emit NaN or drift slightly outside
                # [-1, 1] where a window is almost entirely masked out.
                result = np.clip(np.nan_to_num(result, nan=0.0), -1.0, 1.0)
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
        mask = None
        if entry.get("mask"):
            mask_path = directory / entry["mask"]
            mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
            if mask is None:
                raise FileNotFoundError(f"Could not read anchor mask: {mask_path}")
            if mask.shape != template.shape:
                raise ValueError(
                    f"Mask {mask_path.name} is {mask.shape} but template is "
                    f"{template.shape}; they must match"
                )

        region = entry.get("region")
        anchors.append(
            Anchor(
                name=entry.get("name", Path(entry["template"]).stem),
                template=template,
                region=tuple(region) if region else None,
                mask=mask,
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
    burst: list | None = None,
) -> Path:
    """Create a screen definition by cropping an anchor out of a frame.

    This is the tool we use to teach the bot a new screen: capture a frame,
    pick out a region that uniquely identifies it, and this writes the template
    and manifest to disk.

    The anchor is searched for in a slightly larger box than it was cropped
    from (`search_padding`), which absorbs small layout shifts between the
    reference and a live frame.

    Pass `burst` - several frames of the same screen - and any pixels that
    moved between them are written to a mask and excluded from matching. That
    is what lets an anchor sit on an animated screen: the static text is
    compared, the drifting background behind it is ignored. The degenerate
    check then applies only to the surviving pixels, so a crop that is mostly
    animation but carries solid text still passes.
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
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

    mask = None
    mask_name = None
    if burst:
        full_mask = stability_mask(burst)
        mask = full_mask[y : y + h, x : x + w]
        stable_fraction = float(mask.mean()) / 255.0
        if stable_fraction < 0.02:
            raise ValueError(
                f"Region {region} is {100 * (1 - stable_fraction):.0f}% animated - "
                f"almost nothing in it holds still. Pick a region with static "
                f"text or an icon."
            )
        if float(np.std(template_gray[mask > 0])) < MIN_MASKED_ANCHOR_STDDEV:
            raise ValueError(
                f"Every pixel that holds still in region {region} is the same "
                f"value, so there is nothing to correlate and this anchor would "
                f"never match. Pick a region whose static part has some "
                f"contrast - text with an outline, or an icon."
            )
        mask_name = f"mask_{name}.png"
    elif anchor_is_degenerate(template_gray):
        raise ValueError(
            f"Region {region} is nearly featureless and would match any screen. "
            f"Pick a region containing text, an icon, or a strong edge."
        )

    # Widen the search window around where the anchor was cropped from.
    sx = max(0, x - search_padding)
    sy = max(0, y - search_padding)
    sw = min(WORK_WIDTH - sx, w + search_padding * 2)
    sh = min(WORK_HEIGHT - sy, h + search_padding * 2)

    # Prove the anchor works before writing it, by matching it against the very
    # frame it was cut from. An anchor that cannot recognise its own source
    # will certainly not recognise a live screen, and this catches the failure
    # directly rather than inferring it from a statistic. load_screen reads
    # templates as grayscale, so verify against grayscale too.
    probe = ScreenDefinition(
        name=name,
        anchors=(Anchor(name, template_gray, (sx, sy, sw, sh), mask),),
        threshold=threshold,
    )
    self_score, _ = probe.score(cv2.cvtColor(work, cv2.COLOR_BGR2GRAY))
    if self_score < threshold:
        raise ValueError(
            f"This anchor only scores {self_score:.3f} against the frame it was "
            f"cut from, below the {threshold} threshold, so it would never fire. "
            f"Pick a region with clearer, more static detail."
        )

    template_name = f"anchor_{name}.png"
    cv2.imwrite(str(target / template_name), template)
    if mask_name is not None:
        cv2.imwrite(str(target / mask_name), mask)

    anchor_entry: dict = {
        "name": name,
        "template": template_name,
        "region": [sx, sy, sw, sh],
    }
    if mask_name:
        anchor_entry["mask"] = mask_name

    manifest = {
        "name": name,
        "description": description,
        "threshold": threshold,
        "anchors": [anchor_entry],
    }
    (target / "screen.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    # Keep the full reference frame too. Invaluable when a match starts failing
    # and you need to see what the screen actually looked like.
    cv2.imwrite(str(target / "reference.png"), work)

    return target
