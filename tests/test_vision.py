"""Vision layer tests.

Uses synthetic screens rather than real game captures, so these run anywhere.
The important claims being tested are:
  - a screen learned from one resolution still matches at another
  - an unrelated screen is reported as unknown rather than guessed at
  - the weakest anchor decides a screen's score
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from swgoh_bot.capture import Frame
from swgoh_bot.config import WORK_HEIGHT, WORK_WIDTH
from swgoh_bot.vision import (
    Anchor,
    ScreenClassifier,
    ScreenDefinition,
    anchor_is_degenerate,
    save_screen_definition,
)


def make_screen(label: str, seed: int, width=WORK_WIDTH, height=WORK_HEIGHT):
    """A fake game screen: textured background plus a distinctive UI panel."""
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 60, size=(WORK_HEIGHT, WORK_WIDTH, 3), dtype=np.uint8)

    # A bright panel with text on it, standing in for a UI element.
    cv2.rectangle(image, (1180, 380), (1520, 620), (200, 180, 40), -1)
    cv2.rectangle(image, (1180, 380), (1520, 620), (255, 255, 255), 3)
    cv2.putText(
        image, label, (1200, 520), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (20, 20, 20), 4
    )

    if (width, height) != (WORK_WIDTH, WORK_HEIGHT):
        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
    return image


PANEL = (1180, 380, 340, 240)  # x, y, w, h


def test_learned_screen_matches_itself(tmp_path):
    frame = make_screen("HOME", seed=1)
    save_screen_definition("home", frame, PANEL, directory=tmp_path)

    result = ScreenClassifier.from_directory(tmp_path).classify(frame)

    assert result.screen == "home"
    assert result.score > 0.99


def test_screen_learned_at_900p_matches_a_1080p_capture(tmp_path):
    """The whole point of the canonical working resolution.

    We learn the screen from a 1600x900 frame and then classify a 1920x1080
    capture of the same screen. It has been upscaled and downscaled again, so
    it is genuinely lossy - but it must still be recognised.
    """
    save_screen_definition("home", make_screen("HOME", seed=1), PANEL, directory=tmp_path)

    at_1080p = make_screen("HOME", seed=1, width=1920, height=1080)
    result = ScreenClassifier.from_directory(tmp_path).classify(at_1080p)

    assert result.screen == "home"


def test_screen_learned_at_900p_matches_a_1440p_capture(tmp_path):
    save_screen_definition("home", make_screen("HOME", seed=1), PANEL, directory=tmp_path)

    at_1440p = make_screen("HOME", seed=1, width=2560, height=1440)
    result = ScreenClassifier.from_directory(tmp_path).classify(at_1440p)

    assert result.screen == "home"


def test_unrelated_screen_is_unknown_not_guessed(tmp_path):
    save_screen_definition("home", make_screen("HOME", seed=1), PANEL, directory=tmp_path)

    other = make_screen("CONQUEST", seed=2)
    result = ScreenClassifier.from_directory(tmp_path).classify(other)

    assert result.screen is None
    assert not result.recognised
    # It still reports how close it got, which is what you need when tuning.
    assert "home" in result.scores


def test_classifier_picks_the_right_screen_of_several(tmp_path):
    save_screen_definition("home", make_screen("HOME", seed=1), PANEL, directory=tmp_path)
    save_screen_definition(
        "conquest", make_screen("CONQUEST", seed=2), PANEL, directory=tmp_path
    )

    classifier = ScreenClassifier.from_directory(tmp_path)
    assert len(classifier.screens) == 2
    assert classifier.classify(make_screen("HOME", seed=1)).screen == "home"
    assert classifier.classify(make_screen("CONQUEST", seed=2)).screen == "conquest"


def test_empty_directory_gives_a_classifier_that_recognises_nothing(tmp_path):
    classifier = ScreenClassifier.from_directory(tmp_path)
    assert classifier.screens == []
    assert classifier.classify(make_screen("HOME", seed=1)).screen is None


def test_missing_directory_is_not_an_error(tmp_path):
    classifier = ScreenClassifier.from_directory(tmp_path / "not_created_yet")
    assert classifier.screens == []


def test_screen_score_is_its_weakest_anchor():
    """All anchors must be present, so the minimum decides."""
    gray = cv2.cvtColor(make_screen("HOME", seed=1), cv2.COLOR_BGR2GRAY)

    # One anchor genuinely from this screen, one taken from a different screen.
    good = gray[380:620, 1180:1520]
    wrong = cv2.cvtColor(make_screen("CONQUEST", seed=2), cv2.COLOR_BGR2GRAY)[
        380:620, 1180:1520
    ]

    screen = ScreenDefinition(
        name="home",
        anchors=(
            Anchor("real", good, PANEL),
            Anchor("from_another_screen", wrong, PANEL),
        ),
    )
    score, matches = screen.score(gray)

    assert len(matches) == 2
    assert score == min(m.score for m in matches)
    # The mismatched anchor drags the whole screen below threshold.
    assert score < 0.85
    assert max(m.score for m in matches) > 0.99


def test_flat_template_would_match_anything_so_it_is_flagged():
    """The trap that motivates MIN_ANCHOR_STDDEV.

    A featureless template scores a perfect 1.0 against unrelated pixels,
    because TM_CCOEFF_NORMED resolves its 0/0 as 1. anchor_is_degenerate is
    what stops such a crop ever becoming an anchor.
    """
    flat = np.full((40, 40), 127, dtype=np.uint8)
    unrelated = cv2.cvtColor(make_screen("CONQUEST", seed=2), cv2.COLOR_BGR2GRAY)

    result = cv2.matchTemplate(unrelated, flat, cv2.TM_CCOEFF_NORMED)
    assert cv2.minMaxLoc(result)[1] == pytest.approx(1.0, abs=1e-6)

    assert anchor_is_degenerate(flat)
    assert not anchor_is_degenerate(unrelated[380:620, 1180:1520])


def test_save_screen_definition_rejects_a_featureless_region(tmp_path):
    blank = np.full((WORK_HEIGHT, WORK_WIDTH, 3), 30, dtype=np.uint8)
    with pytest.raises(ValueError, match="featureless"):
        save_screen_definition("blank", blank, PANEL, directory=tmp_path)


def test_anchor_larger_than_its_region_scores_zero():
    gray = cv2.cvtColor(make_screen("HOME", seed=1), cv2.COLOR_BGR2GRAY)
    oversized = np.zeros((300, 300), dtype=np.uint8)
    screen = ScreenDefinition(
        name="broken", anchors=(Anchor("too_big", oversized, (0, 0, 100, 100)),)
    )
    assert screen.score(gray) == (0.0, [])


def test_anchor_match_reports_a_clickable_centre(tmp_path):
    frame = make_screen("HOME", seed=1)
    save_screen_definition("home", frame, PANEL, directory=tmp_path)

    result = ScreenClassifier.from_directory(tmp_path).classify(frame)
    match = result.matches[0]
    cx, cy = match.center

    # The panel spans x 1180-1520, y 380-620; its centre is ~(1350, 500).
    assert abs(cx - 1350) < 15
    assert abs(cy - 500) < 15


def test_classify_accepts_a_frame_and_keeps_its_source(tmp_path):
    image = make_screen("HOME", seed=1)
    save_screen_definition("home", image, PANEL, directory=tmp_path)

    result = ScreenClassifier.from_directory(tmp_path).classify(
        Frame(image=image, source="replay:home.png")
    )
    assert result.source == "replay:home.png"


def test_save_screen_definition_writes_template_manifest_and_reference(tmp_path):
    target = save_screen_definition(
        "home", make_screen("HOME", seed=1), PANEL, directory=tmp_path,
        description="the hub",
    )
    assert (target / "screen.json").is_file()
    assert (target / "anchor_home.png").is_file()
    assert (target / "reference.png").is_file()

    definition = ScreenClassifier.from_directory(tmp_path).screens[0]
    assert definition.description == "the hub"


def test_save_screen_definition_rejects_an_out_of_bounds_region(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        save_screen_definition(
            "home", make_screen("HOME", seed=1), (1500, 800, 400, 400), directory=tmp_path
        )


def test_save_screen_definition_rejects_an_empty_region(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        save_screen_definition(
            "home", make_screen("HOME", seed=1), (10, 10, 0, 50), directory=tmp_path
        )


def test_unknown_classification_stringifies_with_its_closest_guess(tmp_path):
    save_screen_definition("home", make_screen("HOME", seed=1), PANEL, directory=tmp_path)
    result = ScreenClassifier.from_directory(tmp_path).classify(make_screen("X", seed=9))
    assert "unknown" in str(result)
    assert "closest: home" in str(result)
