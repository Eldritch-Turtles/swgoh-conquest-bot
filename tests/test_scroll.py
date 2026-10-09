"""Tests for scrollable screens.

The sector map is a canvas wider than the window, dragged left and right. The
same screen therefore has endless appearances, and a node's pixel position is
meaningless on its own.

What has to hold:
  - screen identity comes from the fixed HUD and survives any scroll
  - an anchor cropped from the scrolling content does not, and we can tell
  - scroll distance is measurable, so screen pixels map to stable coordinates
  - content can be found by appearance rather than position
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from swgoh_bot import vision
from swgoh_bot.scroll import (
    ScrollTracker,
    measure_shift,
    stitch,
)
from swgoh_bot.vision import (
    ScreenClassifier,
    find_all,
    save_screen_definition,
    stability_mask,
)

WORLD_WIDTH = 4200
NODE_COUNT = 14
NODE_SPACING = 280
NODE_FIRST_X = 200

# The scrolling area. Header above it, footer below, both fixed.
VIEWPORT = (0, 110, 1600, 710)
HEADER_ANCHOR = (30, 20, 320, 80)      # "SECTOR 1" - fixed HUD
CONTENT_ANCHOR = (120, 300, 300, 200)  # inside the map - scrolls away


def build_world(seed: int = 0) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """A sector map far wider than the screen, with identical node icons.

    The index label sits below each node rather than on it, so every node icon
    is pixel-identical and find_all can legitimately match them all.
    """
    rng = np.random.default_rng(seed)
    world = rng.integers(20, 70, size=(900, WORLD_WIDTH, 3), dtype=np.uint8)

    centres = []
    for index in range(NODE_COUNT):
        x = NODE_FIRST_X + index * NODE_SPACING
        y = 420 + (80 if index % 2 else -80)
        cv2.circle(world, (x, y), 52, (190, 160, 60), -1)
        cv2.circle(world, (x, y), 52, (240, 230, 200), 4)
        cv2.circle(world, (x, y), 22, (60, 40, 20), -1)
        cv2.putText(
            world, str(index + 1), (x - 12, y + 95),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2,
        )
        centres.append((x, y))
    return world, centres


MAX_SCROLL = WORLD_WIDTH - 1600


def screen_at(world: np.ndarray, scroll_x: int, sector: str = "SECTOR 1") -> np.ndarray:
    """What the window shows at a given scroll position, HUD drawn on top."""
    if not 0 <= scroll_x <= MAX_SCROLL:
        # Guard the fixture itself: a short slice would be stretched to screen
        # width by to_working_size, silently corrupting every measurement.
        raise AssertionError(
            f"scroll_x={scroll_x} is outside 0..{MAX_SCROLL}; the frame would "
            f"be clipped and then distorted"
        )
    view = world[:, scroll_x : scroll_x + 1600].copy()

    cv2.rectangle(view, (0, 0), (1600, 110), (35, 35, 45), -1)
    cv2.putText(view, sector, (40, 75), cv2.FONT_HERSHEY_DUPLEX, 1.6, (240, 230, 180), 4)
    cv2.rectangle(view, (0, 820), (1600, 900), (35, 35, 45), -1)
    cv2.putText(
        view, "ENERGY 142", (40, 878), cv2.FONT_HERSHEY_DUPLEX, 1.0, (200, 220, 240), 2
    )
    return view


def node_template(world: np.ndarray, centres) -> np.ndarray:
    """A node icon, cropped tight so it excludes the index label."""
    x, y = centres[3]
    return cv2.cvtColor(world[y - 56 : y + 56, x - 56 : x + 56], cv2.COLOR_BGR2GRAY)


@pytest.fixture(scope="module")
def world():
    return build_world()


@pytest.fixture
def screens_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "screens"
    directory.mkdir()
    monkeypatch.setattr(vision, "SCREENS_DIR", directory)
    return directory


# --------------------------------------------------------------------------
# Identity under scroll - the central claim
# --------------------------------------------------------------------------


def test_hud_anchor_identifies_the_screen_at_every_scroll_position(world, screens_dir):
    world_image, _ = world
    save_screen_definition(
        "sector_map",
        screen_at(world_image, 0),
        HEADER_ANCHOR,
        directory=screens_dir,
        viewport=VIEWPORT,
    )
    classifier = ScreenClassifier.from_directory(screens_dir)

    for scroll_x in (0, 400, 1200, 1900, 2600):
        result = classifier.classify(screen_at(world_image, scroll_x))
        assert result.screen == "sector_map", f"failed at scroll {scroll_x}"
        assert result.score > 0.95


def test_content_anchor_matches_the_wrong_thing_after_scrolling(world, screens_dir):
    """Why anchoring on scrolling content is worse than it first looks.

    The obvious failure is that the anchor stops matching. The real one is
    subtler: sector maps are full of repeated node icons, so after scrolling
    the anchor happily matches a *different* node at high confidence. It
    reports the right screen for the wrong reason, and the match position is
    meaningless - which is why identity has to come from the fixed HUD.
    """
    world_image, _ = world
    with pytest.warns(UserWarning, match="overlaps the scrolling viewport"):
        save_screen_definition(
            "sector_map",
            screen_at(world_image, 0),
            CONTENT_ANCHOR,
            directory=screens_dir,
            viewport=VIEWPORT,
        )
    classifier = ScreenClassifier.from_directory(screens_dir)

    taught = classifier.classify(screen_at(world_image, 0))
    assert taught.screen == "sector_map"

    # Scroll by exactly two node spacings: a different node now sits where the
    # anchor was cropped from, and it matches it.
    scrolled = classifier.classify(screen_at(world_image, NODE_SPACING * 2))
    assert scrolled.recognised
    assert scrolled.score > 0.85


def test_anchors_in_viewport_flags_the_bad_placement(world, screens_dir):
    world_image, _ = world
    with pytest.warns(UserWarning):
        save_screen_definition(
            "bad", screen_at(world_image, 0), CONTENT_ANCHOR,
            directory=screens_dir, viewport=VIEWPORT,
        )
    screen = ScreenClassifier.from_directory(screens_dir).screens[0]
    assert screen.scrollable
    assert len(screen.anchors_in_viewport()) == 1


def test_hud_anchor_is_not_flagged(world, screens_dir):
    world_image, _ = world
    save_screen_definition(
        "good", screen_at(world_image, 0), HEADER_ANCHOR,
        directory=screens_dir, viewport=VIEWPORT,
    )
    screen = ScreenClassifier.from_directory(screens_dir).screens[0]
    assert screen.anchors_in_viewport() == ()


def test_scrolling_burst_reveals_the_hud(world):
    """stability_mask, fed a scroll instead of an idle wait, finds the HUD.

    Whatever holds still across a scroll is by definition not scrolling
    content - so this is how you discover where it is safe to anchor.
    """
    world_image, _ = world
    frames = [screen_at(world_image, 800 + i * 300) for i in range(5)]
    mask = stability_mask(frames)

    assert mask[0:110].mean() / 255 > 0.98      # header fixed
    assert mask[820:900].mean() / 255 > 0.98    # footer fixed
    assert mask[110:820].mean() / 255 < 0.15    # content moved


# --------------------------------------------------------------------------
# Measuring scroll
# --------------------------------------------------------------------------


@pytest.mark.parametrize("truth", [60, 120, 357, -240, -480])
def test_measure_shift_recovers_a_known_scroll(world, truth):
    world_image, _ = world
    before = screen_at(world_image, 1500)
    after = screen_at(world_image, 1500 + truth)

    shift = measure_shift(before, after, region=VIEWPORT)

    # Content moves opposite to the scroll.
    assert shift.dx == pytest.approx(-truth, abs=1.0)
    assert shift.dy == pytest.approx(0.0, abs=1.0)
    assert shift.trustworthy


def test_measure_shift_survives_the_hud_being_included(world):
    world_image, _ = world
    shift = measure_shift(
        screen_at(world_image, 1500), screen_at(world_image, 1857)
    )
    assert shift.dx == pytest.approx(-357, abs=2.0)


def test_measure_shift_reports_no_movement_for_a_still_screen(world):
    world_image, _ = world
    frame = screen_at(world_image, 1000)
    shift = measure_shift(frame, frame.copy(), region=VIEWPORT)
    assert shift.magnitude == pytest.approx(0.0, abs=0.5)


def test_measure_shift_normalizes_differing_capture_resolutions(world):
    """A 1080p and a 1440p capture of the same scroll compare cleanly.

    Both are rescaled to the working resolution first, which is also why
    measure_shift's size guard is defensive rather than reachable here.
    """
    world_image, _ = world
    before = cv2.resize(screen_at(world_image, 1500), (1920, 1080))
    after = cv2.resize(screen_at(world_image, 1857), (2560, 1440))

    shift = measure_shift(before, after, region=VIEWPORT)
    assert shift.dx == pytest.approx(-357, abs=4.0)


# --------------------------------------------------------------------------
# ScrollTracker
# --------------------------------------------------------------------------


def test_tracker_accumulates_offset_across_several_scrolls(world):
    world_image, _ = world
    tracker = ScrollTracker(region=VIEWPORT)

    positions = [500, 700, 1100, 1400, 1250]
    for scroll_x in positions:
        tracker.update(screen_at(world_image, scroll_x))

    # Offset is relative to the first frame.
    assert tracker.offset_x == pytest.approx(positions[-1] - positions[0], abs=3.0)
    assert tracker.offset_y == pytest.approx(0.0, abs=2.0)


def test_tracker_first_frame_yields_no_shift(world):
    world_image, _ = world
    tracker = ScrollTracker(region=VIEWPORT)
    assert tracker.update(screen_at(world_image, 0)) is None
    assert tracker.offset == (0.0, 0.0)


def test_tracker_round_trips_world_and_viewport_coordinates(world):
    world_image, _ = world
    tracker = ScrollTracker(region=VIEWPORT)
    tracker.update(screen_at(world_image, 500))
    tracker.update(screen_at(world_image, 900))

    world_point = tracker.to_world(300, 400)
    assert tracker.to_viewport(*world_point) == pytest.approx((300, 400), abs=0.01)


def test_tracker_converts_a_node_seen_now_into_a_stable_coordinate(world):
    """Locate a node at one scroll position, then predict where it is at another.

    This is what actually lets the bot click the right node after scrolling.
    """
    world_image, centres = world
    template = node_template(world_image, centres)

    tracker = ScrollTracker(region=VIEWPORT)
    start, end = 600, 1100

    first = screen_at(world_image, start)
    tracker.update(first)
    found = find_all(first, template, threshold=0.9, region=VIEWPORT)
    assert found
    # Take the rightmost node, so it is still on screen after scrolling right.
    seen = max(found, key=lambda m: m.x)
    world_point = tracker.to_world(*seen.center)

    second = screen_at(world_image, end)
    tracker.update(second)
    predicted_x, predicted_y = tracker.to_viewport(*world_point)

    # The same node, now shifted left by the scroll distance.
    assert predicted_x == pytest.approx(seen.center[0] - (end - start), abs=4.0)

    # And it really is a node there.
    again = find_all(second, template, threshold=0.9, region=VIEWPORT)
    assert any(
        abs(m.center[0] - predicted_x) < 12 and abs(m.center[1] - predicted_y) < 12
        for m in again
    )


def test_tracker_rejects_an_untrustworthy_jump(world):
    """A screen change is not a scroll, and must not corrupt the offset."""
    world_image, _ = world
    tracker = ScrollTracker(region=VIEWPORT, min_response=0.9)
    tracker.update(screen_at(world_image, 500))

    rng = np.random.default_rng(99)
    unrelated = rng.integers(0, 255, size=(900, 1600, 3), dtype=np.uint8)
    assert tracker.update(unrelated) is None
    assert tracker.rejected == 1
    assert tracker.offset == (0.0, 0.0)


def test_tracker_reset_clears_state(world):
    world_image, _ = world
    tracker = ScrollTracker(region=VIEWPORT)
    tracker.update(screen_at(world_image, 500))
    tracker.update(screen_at(world_image, 900))
    tracker.reset()
    assert tracker.offset == (0.0, 0.0)
    assert tracker.update(screen_at(world_image, 0)) is None


# --------------------------------------------------------------------------
# find_all
# --------------------------------------------------------------------------


def test_find_all_locates_every_visible_node(world):
    world_image, centres = world
    template = node_template(world_image, centres)
    scroll_x = 700

    found = find_all(screen_at(world_image, scroll_x), template, threshold=0.9, region=VIEWPORT)

    expected = [
        (x - scroll_x, y)
        for x, y in centres
        if 60 <= x - scroll_x <= 1540
    ]
    assert len(found) == len(expected)
    for ex, ey in expected:
        assert any(
            abs(m.center[0] - ex) < 12 and abs(m.center[1] - ey) < 12 for m in found
        )


def test_find_all_collapses_overlapping_detections(world):
    """One node reported once, not as a cluster of near-identical hits."""
    world_image, centres = world
    template = node_template(world_image, centres)
    found = find_all(screen_at(world_image, 0), template, threshold=0.75, region=VIEWPORT)

    for i, a in enumerate(found):
        for b in found[i + 1 :]:
            assert abs(a.x - b.x) >= template.shape[1] * 0.5 or abs(
                a.y - b.y
            ) >= template.shape[0] * 0.5


def test_find_all_returns_nothing_when_absent(world):
    world_image, _ = world
    rng = np.random.default_rng(5)
    nonsense = rng.integers(0, 255, size=(60, 60), dtype=np.uint8)
    assert find_all(screen_at(world_image, 0), nonsense, threshold=0.9) == []


def test_find_all_respects_max_results(world):
    world_image, centres = world
    template = node_template(world_image, centres)
    found = find_all(
        screen_at(world_image, 0), template, threshold=0.9,
        region=VIEWPORT, max_results=2,
    )
    assert len(found) == 2


def test_find_all_handles_an_oversized_template(world):
    world_image, _ = world
    huge = np.zeros((2000, 2000), dtype=np.uint8)
    assert find_all(screen_at(world_image, 0), huge) == []


# --------------------------------------------------------------------------
# Stitching
# --------------------------------------------------------------------------


def test_stitch_reconstructs_the_scrolled_extent(world):
    world_image, _ = world
    start, step, count = 0, 300, 9
    frames = [screen_at(world_image, start + i * step) for i in range(count)]

    panorama = stitch(frames, region=VIEWPORT)

    # One screen wide, plus everything we scrolled past.
    expected_width = 1600 + step * (count - 1)
    assert panorama.size[0] == pytest.approx(expected_width, abs=12)
    assert panorama.size[1] == VIEWPORT[3]
    assert len(panorama.offsets) == count


def test_stitched_panorama_contains_every_node_it_scrolled_past(world):
    """The payoff: one image of the whole sector, every node locatable once."""
    world_image, centres = world
    template = node_template(world_image, centres)
    steps = 11
    frames = [screen_at(world_image, i * 250) for i in range(steps)]

    panorama = stitch(frames, region=VIEWPORT)
    found = find_all(
        panorama.image, template, threshold=0.9, region=None,
        max_results=60, normalize=False,
    )

    furthest = (steps - 1) * 250 + 1600
    expected = [x for x, _ in centres if 60 <= x <= furthest - 60]
    assert len(found) == len(expected)


def test_stitch_needs_two_frames(world):
    world_image, _ = world
    with pytest.raises(ValueError, match="at least 2"):
        stitch([screen_at(world_image, 0)], region=VIEWPORT)


def test_stitch_of_a_static_screen_is_one_screen_wide(world):
    world_image, _ = world
    frame = screen_at(world_image, 500)
    panorama = stitch([frame, frame.copy(), frame.copy()], region=VIEWPORT)
    assert panorama.size[0] == pytest.approx(VIEWPORT[2], abs=4)
