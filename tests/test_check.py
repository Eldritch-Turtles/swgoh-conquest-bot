"""Tests for the `check` command, which grades anchors against a capture set.

The scenario that matters: a user crops persistent UI chrome as an anchor. It
scores 1.000 against its own screen and looks perfect, but also matches every
other screen. `check` has to catch that.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from swgoh_bot import vision
from swgoh_bot.cli import expected_label, main
from swgoh_bot.vision import save_screen_definition

from pathlib import Path

# Unique to one screen.
BANNER = (80, 80, 540, 120)
# Identical on every screen - the trap.
CHROME = (1400, 820, 190, 70)


def fake_screen(title: str, seed: int, width=1920, height=1080) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 50, size=(900, 1600, 3), dtype=np.uint8)

    cv2.rectangle(image, (80, 80), (620, 200), (190, 150, 30), -1)
    cv2.putText(
        image, title, (100, 165), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (15, 15, 15), 3
    )
    # Persistent chrome, same on all screens.
    cv2.rectangle(image, (1400, 820), (1590, 890), (60, 60, 70), -1)
    cv2.putText(
        image, "MENU", (1415, 870), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (230, 230, 230), 2
    )
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)


SCREENS = {"home": ("HOME", 1), "conquest_map": ("CONQUEST", 2), "squad_select": ("SQUAD", 3)}


@pytest.fixture
def captures(tmp_path) -> Path:
    """A folder named the way `grab --label` names things."""
    directory = tmp_path / "captures"
    directory.mkdir()
    for name, (title, seed) in SCREENS.items():
        cv2.imwrite(str(directory / f"{name}-20261009-143000.png"), fake_screen(title, seed))
    return directory


@pytest.fixture
def screens_dir(tmp_path, monkeypatch) -> Path:
    """Redirect screen definitions somewhere disposable.

    vision.py binds SCREENS_DIR at import, so patch it there rather than on
    the config module.
    """
    directory = tmp_path / "screens"
    directory.mkdir()
    monkeypatch.setattr(vision, "SCREENS_DIR", directory)
    return directory


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("home-20261009-143000.png", "home"),
        ("conquest_map-20261009-143000.png", "conquest_map"),
        ("battle_result-20260101-000000.png", "battle_result"),
        ("something_else.png", "something_else"),
        # Only the two trailing numeric parts are stripped, so a label that
        # happens to contain a dash survives.
        ("sector-2-boss-20261009-143000.png", "sector-2-boss"),
    ],
)
def test_expected_label_recovers_the_grab_label(filename, expected):
    assert expected_label(Path(filename)) == expected


def test_chrome_anchor_is_reported_as_a_leak(captures, screens_dir, capsys):
    """The headline case: an anchor cropped from persistent UI."""
    save_screen_definition(
        "home", fake_screen(*SCREENS["home"]), CHROME, directory=screens_dir
    )

    exit_code = main(["check", "--dir", str(captures)])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert out.count("LEAK") == 2  # conquest_map and squad_select
    assert "Non-discriminating anchors detected" in out
    assert "conquest_map, squad_select" in out


def test_distinctive_anchors_all_pass(captures, screens_dir, capsys):
    for name, (title, seed) in SCREENS.items():
        save_screen_definition(
            name, fake_screen(title, seed), BANNER, directory=screens_dir
        )

    exit_code = main(["check", "--dir", str(captures)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "3 correct, 0 wrong" in out
    assert "LEAK" not in out


def test_untaught_screens_are_not_counted_as_failures(captures, screens_dir, capsys):
    """Partially taught is a normal state, not an error."""
    save_screen_definition(
        "home", fake_screen(*SCREENS["home"]), BANNER, directory=screens_dir
    )

    exit_code = main(["check", "--dir", str(captures)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1 correct, 0 wrong, 2 not yet taught" in out
    assert out.count("(not taught)") == 2


def test_mislabelled_screen_is_reported_wrong(captures, screens_dir, capsys):
    """A screen taught from the wrong image should be caught.

    Both names are known, so this is a WRONG rather than a LEAK.
    """
    save_screen_definition(
        "home", fake_screen(*SCREENS["home"]), BANNER, directory=screens_dir
    )
    # Teach "conquest_map" from the squad_select image by mistake.
    save_screen_definition(
        "conquest_map", fake_screen(*SCREENS["squad_select"]), BANNER,
        directory=screens_dir,
    )

    exit_code = main(["check", "--dir", str(captures)])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "WRONG" in out


def test_check_needs_screens_first(captures, screens_dir, capsys):
    exit_code = main(["check", "--dir", str(captures)])
    assert exit_code == 1
    assert "No screens taught yet" in capsys.readouterr().out


def test_check_reports_a_missing_folder(screens_dir, tmp_path, capsys):
    exit_code = main(["check", "--dir", str(tmp_path / "nope")])
    assert exit_code == 1
    assert "No such folder" in capsys.readouterr().out


def test_check_reports_an_empty_folder(screens_dir, tmp_path, capsys):
    save_screen_definition(
        "home", fake_screen(*SCREENS["home"]), BANNER, directory=screens_dir
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["check", "--dir", str(empty)]) == 1
    assert "No images found" in capsys.readouterr().out
