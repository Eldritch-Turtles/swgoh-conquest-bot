"""Tests for surviving animated screens.

SWGOH screens move: panning backdrops, drifting scenery, breathing portraits.
A template cropped from a moving region never matches twice. The strategy is to
capture a burst, work out which pixels held still, and compare only those.

These tests pin down where that works and where it doesn't.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from swgoh_bot import vision
from swgoh_bot.capture import (
    Frame,
    ReplayBackend,
    frames_are_identical,
    grab_burst,
    grab_stable,
)
from swgoh_bot.vision import (
    ScreenClassifier,
    save_screen_definition,
    stability_mask,
    suggest_anchors,
)

TITLE_REGION = (80, 60, 300, 110)


def animated_screen(title: str, seed: int, tick: int) -> np.ndarray:
    """A screen with coherent moving scenery behind antialiased text.

    The movement is large and structured rather than fine noise, because fine
    noise averages out under correlation and would make this test too easy.
    The text is drawn with an outline so its pixels span a range of values -
    which, as test_uniform_kept_pixels_are_useless shows, is what masked
    matching needs.
    """
    image = np.zeros((900, 1600, 3), dtype=np.uint8)

    shift = (tick * 140) % 1600
    xs = (np.arange(1600) + shift) % 1600
    gradient = (40 + 90 * np.sin(xs / 1600 * 2 * np.pi)).astype(np.uint8)
    image[:, :, 0] = gradient[None, :]
    image[:, :, 2] = gradient[None, :] // 2

    rng = np.random.default_rng(seed)
    for _ in range(7):
        base_x = int(rng.integers(0, 1600))
        base_y = int(rng.integers(0, 900))
        x = (base_x + tick * 160) % 1700 - 50
        cv2.circle(image, (x, base_y), 110, (200, 180, 120), -1)

    # Outline then fill, giving the glyphs internal contrast.
    cv2.putText(image, title, (95, 150), cv2.FONT_HERSHEY_DUPLEX, 1.9, (30, 25, 10), 9)
    cv2.putText(image, title, (95, 150), cv2.FONT_HERSHEY_DUPLEX, 1.9, (255, 250, 200), 3)
    return image


def burst(title: str, seed: int, ticks=range(6)) -> list[np.ndarray]:
    return [animated_screen(title, seed, tick) for tick in ticks]


@pytest.fixture
def screens_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "screens"
    directory.mkdir()
    monkeypatch.setattr(vision, "SCREENS_DIR", directory)
    return directory


# --------------------------------------------------------------------------
# stability_mask
# --------------------------------------------------------------------------


def test_stability_mask_separates_static_text_from_moving_scenery():
    mask = stability_mask(burst("CONQUEST", 1))

    # Almost the whole screen moves.
    assert mask.mean() / 255 < 0.05
    # But the glyph area holds still.
    x, y, w, h = TITLE_REGION
    assert mask[y : y + h, x : x + w].mean() / 255 > 0.10


def test_stability_mask_marks_a_frozen_screen_entirely_static():
    still = animated_screen("CONQUEST", 1, tick=0)
    mask = stability_mask([still, still.copy(), still.copy()])
    assert mask.mean() / 255 == pytest.approx(1.0)


def test_stability_mask_needs_two_frames():
    with pytest.raises(ValueError, match="at least 2 frames"):
        stability_mask([animated_screen("CONQUEST", 1, 0)])


def test_stability_mask_accepts_frames_or_arrays():
    images = burst("CONQUEST", 1, ticks=range(3))
    wrapped = [Frame(image=i, source="t") for i in images]
    assert np.array_equal(stability_mask(images), stability_mask(wrapped))


# --------------------------------------------------------------------------
# suggest_anchors
# --------------------------------------------------------------------------


def test_suggest_anchors_finds_the_text_and_ignores_the_scenery():
    found = suggest_anchors(burst("CONQUEST", 1), count=5, min_stable=0.10)
    assert found

    x, y, w, h = TITLE_REGION
    # Every suggestion should land on or near the static title, not out in
    # the moving background.
    for suggestion in found:
        assert suggestion.x < x + w + 200
        assert suggestion.y < y + h + 200


def test_suggest_anchors_rejects_pure_noise_despite_its_high_variance():
    """Detail must be measured over static pixels only.

    Animated noise has a large local standard deviation. Scoring the whole
    window would rank a patch of moving starfield above a line of text, so
    detail is computed across the kept pixels alone.
    """
    rng = np.random.default_rng(0)
    frames = [
        rng.integers(0, 255, size=(900, 1600, 3), dtype=np.uint8) for _ in range(4)
    ]
    assert suggest_anchors(frames, min_stable=0.10) == []


def test_suggest_anchors_returns_non_overlapping_regions():
    found = suggest_anchors(burst("CONQUEST", 1), count=4, min_stable=0.10)
    for i, a in enumerate(found):
        for b in found[i + 1 :]:
            assert not a.overlaps(b)


def test_suggestion_formats_a_region_for_the_cli():
    found = suggest_anchors(burst("CONQUEST", 1), count=1, min_stable=0.10)
    assert found[0].cli_region.count(",") == 3


# --------------------------------------------------------------------------
# The headline claim: masking survives unseen animation
# --------------------------------------------------------------------------


def test_masked_anchors_identify_unseen_animation_frames(screens_dir):
    """Teach from ticks 0-5, then identify tick 11, never seen before."""
    for name, (title, seed) in {
        "conquest_map": ("CONQUEST", 1),
        "squad_select": ("SELECT SQUAD", 2),
    }.items():
        frames = burst(title, seed)
        save_screen_definition(
            name, frames[0], TITLE_REGION, directory=screens_dir, burst=frames
        )

    classifier = ScreenClassifier.from_directory(screens_dir)

    conquest = classifier.classify(animated_screen("CONQUEST", 1, tick=11))
    squad = classifier.classify(animated_screen("SELECT SQUAD", 2, tick=11))

    assert conquest.screen == "conquest_map"
    assert squad.screen == "squad_select"
    # And they are not near-misses: the wrong screen should score far lower.
    assert conquest.scores["conquest_map"] - conquest.scores["squad_select"] > 0.3
    assert squad.scores["squad_select"] - squad.scores["conquest_map"] > 0.3


def test_masking_beats_no_masking_on_an_animated_screen(screens_dir, tmp_path):
    """The margin is the point.

    An unmasked anchor on an animated screen may still match, but it sits far
    closer to the threshold - one UI tweak from a false negative. Masking
    restores the headroom.
    """
    frames = burst("CONQUEST", 1)
    unseen = animated_screen("CONQUEST", 1, tick=11)

    masked_dir = tmp_path / "masked"
    masked_dir.mkdir()
    save_screen_definition(
        "home", frames[0], TITLE_REGION, directory=masked_dir, burst=frames
    )
    masked_score = ScreenClassifier.from_directory(masked_dir).classify(unseen).scores[
        "home"
    ]

    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    save_screen_definition("home", frames[0], TITLE_REGION, directory=plain_dir)
    plain_score = ScreenClassifier.from_directory(plain_dir).classify(unseen).scores[
        "home"
    ]

    assert masked_score > plain_score
    assert masked_score > 0.95


# --------------------------------------------------------------------------
# Where masking does NOT help
# --------------------------------------------------------------------------


def test_uniform_kept_pixels_are_useless_and_rejected(screens_dir):
    """Solid-fill text over animation gives nothing to correlate.

    If every surviving pixel shares one value, the mean-subtracted template is
    all zeros and masked matching returns 0.0 against everything - matching and
    unrelated scenes alike. So this is rejected at save time rather than
    producing an anchor that can never fire.

    Note the contrast with a merely *low* variance masked anchor, which is
    fine: see test_low_variance_masked_anchor_is_allowed.
    """
    frames = []
    rng = np.random.default_rng(4)
    for tick in range(5):
        image = rng.integers(0, 255, size=(900, 1600, 3), dtype=np.uint8)
        # Solid block, identical every frame, no internal variation.
        image[60:170, 80:380] = 250
        frames.append(image)

    with pytest.raises(ValueError, match="same value|never match|never fire"):
        save_screen_definition(
            "flat", frames[0], TITLE_REGION, directory=screens_dir, burst=frames
        )


def test_low_variance_masked_anchor_is_allowed(screens_dir):
    """A masked anchor needs contrast, but far less than an unmasked one.

    The title anchor in these fixtures keeps pixels with a standard deviation
    around 6 - under MIN_ANCHOR_STDDEV, which would reject it outright if it
    were unmasked. Masked, it separates its own screen from another at better
    than 0.9, so rejecting it would be wrong.
    """
    frames = burst("CONQUEST", 1)
    target = save_screen_definition(
        "home", frames[0], TITLE_REGION, directory=screens_dir, burst=frames
    )
    anchor = ScreenClassifier.from_directory(screens_dir).screens[0].anchors[0]

    kept = anchor.template[anchor.mask > 0]
    assert float(np.std(kept)) < vision.MIN_ANCHOR_STDDEV
    assert float(np.std(kept)) >= vision.MIN_MASKED_ANCHOR_STDDEV
    assert target.is_dir()


def test_anchor_scores_a_perfect_self_match_when_saved(screens_dir):
    """A healthy anchor matches the frame it was cut from exactly.

    This is why the self-match check is a backstop rather than the primary
    guard: by construction a freshly cut template scores 1.0 against its own
    source, so no realistic threshold can fail it. It earns its place by
    catching the degenerate cases, where masked correlation collapses to 0.0.
    """
    frames = burst("CONQUEST", 1)
    save_screen_definition(
        "home", frames[0], TITLE_REGION, directory=screens_dir, burst=frames
    )
    classifier = ScreenClassifier.from_directory(screens_dir)
    result = classifier.classify(frames[0])
    assert result.screen == "home"
    assert result.score == pytest.approx(1.0, abs=1e-3)


def test_self_match_check_rejects_an_unreachable_threshold(screens_dir):
    """Proves the backstop is wired up.

    matchTemplate cannot exceed 1.0, so a threshold above it is unreachable by
    definition and must be refused at save time rather than written out as an
    anchor that can never fire.
    """
    frames = burst("CONQUEST", 1)
    with pytest.raises(ValueError, match="against the frame it was cut from"):
        save_screen_definition(
            "home",
            frames[0],
            TITLE_REGION,
            directory=screens_dir,
            burst=frames,
            threshold=1.01,
        )


def test_fully_animated_region_is_rejected(screens_dir):
    rng = np.random.default_rng(5)
    frames = [
        rng.integers(0, 255, size=(900, 1600, 3), dtype=np.uint8) for _ in range(4)
    ]
    with pytest.raises(ValueError, match="animated"):
        save_screen_definition(
            "moving", frames[0], TITLE_REGION, directory=screens_dir, burst=frames
        )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_burst_writes_a_mask_and_the_manifest_references_it(screens_dir):
    frames = burst("CONQUEST", 1)
    target = save_screen_definition(
        "home", frames[0], TITLE_REGION, directory=screens_dir, burst=frames
    )

    assert (target / "mask_home.png").is_file()

    anchor = ScreenClassifier.from_directory(screens_dir).screens[0].anchors[0]
    assert anchor.mask is not None
    assert anchor.mask.shape == anchor.template.shape


def test_no_burst_means_no_mask(screens_dir):
    frames = burst("CONQUEST", 1)
    target = save_screen_definition(
        "home", frames[0], TITLE_REGION, directory=screens_dir
    )

    assert not (target / "mask_home.png").exists()
    assert ScreenClassifier.from_directory(screens_dir).screens[0].anchors[0].mask is None


def test_mismatched_mask_size_is_rejected_on_load(screens_dir):
    frames = burst("CONQUEST", 1)
    target = save_screen_definition(
        "home", frames[0], TITLE_REGION, directory=screens_dir, burst=frames
    )
    # Corrupt the mask to the wrong shape.
    cv2.imwrite(str(target / "mask_home.png"), np.zeros((5, 5), dtype=np.uint8))

    with pytest.raises(ValueError, match="must match"):
        ScreenClassifier.from_directory(screens_dir)


# --------------------------------------------------------------------------
# Burst capture plumbing
# --------------------------------------------------------------------------


def test_grab_burst_collects_the_requested_number(tmp_path):
    for index in range(3):
        cv2.imwrite(
            str(tmp_path / f"{index:02d}.png"),
            animated_screen("CONQUEST", 1, index),
        )
    with ReplayBackend(tmp_path, loop=True) as backend:
        frames = grab_burst(backend, count=3, interval=0.0)
    assert len(frames) == 3


def test_grab_burst_needs_at_least_two(tmp_path):
    cv2.imwrite(str(tmp_path / "01.png"), animated_screen("CONQUEST", 1, 0))
    with ReplayBackend(tmp_path) as backend:
        with pytest.raises(ValueError, match="at least 2"):
            grab_burst(backend, count=1)


def test_frames_are_identical_detects_a_replayed_single_image(tmp_path):
    """A burst from one PNG has no movement, so stability analysis is void."""
    cv2.imwrite(str(tmp_path / "01.png"), animated_screen("CONQUEST", 1, 0))
    with ReplayBackend(tmp_path) as backend:
        assert frames_are_identical(grab_burst(backend, count=4, interval=0.0))


def test_frames_are_identical_is_false_for_real_movement(tmp_path):
    for index in range(3):
        cv2.imwrite(
            str(tmp_path / f"{index:02d}.png"),
            animated_screen("CONQUEST", 1, index),
        )
    with ReplayBackend(tmp_path, loop=True) as backend:
        assert not frames_are_identical(grab_burst(backend, count=3, interval=0.0))


def test_grab_stable_reports_settled_on_a_static_screen(tmp_path):
    cv2.imwrite(str(tmp_path / "01.png"), animated_screen("CONQUEST", 1, 0))
    with ReplayBackend(tmp_path) as backend:
        frame, settled = grab_stable(backend, interval=0.0, max_wait=1.0)
    assert settled
    assert frame.image.shape == (900, 1600, 3)


def test_grab_stable_gives_up_but_still_returns_a_frame(tmp_path):
    for index in range(8):
        cv2.imwrite(
            str(tmp_path / f"{index:02d}.png"),
            animated_screen("CONQUEST", 1, index),
        )
    with ReplayBackend(tmp_path, loop=True) as backend:
        frame, settled = grab_stable(backend, interval=0.0, max_wait=0.3)
    assert not settled
    assert frame is not None
