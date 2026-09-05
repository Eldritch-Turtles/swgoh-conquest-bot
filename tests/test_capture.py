"""Capture layer tests. All run without Windows or the game."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from swgoh_bot.capture import Frame, ReplayBackend, create_backend, to_working_size
from swgoh_bot.config import WORK_HEIGHT, WORK_WIDTH


def _write(path, colour):
    image = np.full((90, 160, 3), colour, dtype=np.uint8)
    cv2.imwrite(str(path), image)
    return image


def test_to_working_size_downscales(tmp_path):
    big = np.zeros((1080, 1920, 3), dtype=np.uint8)
    assert to_working_size(big).shape == (WORK_HEIGHT, WORK_WIDTH, 3)


def test_to_working_size_upscales():
    small = np.zeros((360, 640, 3), dtype=np.uint8)
    assert to_working_size(small).shape == (WORK_HEIGHT, WORK_WIDTH, 3)


def test_to_working_size_is_a_noop_at_working_size():
    exact = np.zeros((WORK_HEIGHT, WORK_WIDTH, 3), dtype=np.uint8)
    # Same object back: no pointless copy on the hot path.
    assert to_working_size(exact) is exact


def test_frame_to_working_size_preserves_metadata():
    frame = Frame(image=np.zeros((1080, 1920, 3), dtype=np.uint8), source="unit-test")
    resized = frame.to_working_size()
    assert resized.size == (WORK_WIDTH, WORK_HEIGHT)
    assert resized.source == "unit-test"
    assert resized.captured_at == frame.captured_at


def test_replay_backend_walks_a_directory_then_holds(tmp_path):
    _write(tmp_path / "01.png", (10, 20, 30))
    _write(tmp_path / "02.png", (40, 50, 60))

    backend = ReplayBackend(tmp_path)
    first = backend.grab()
    second = backend.grab()
    third = backend.grab()

    assert first.source == "replay:01.png"
    assert second.source == "replay:02.png"
    # Past the end it holds on the last frame rather than blowing up, so a
    # polling loop just sees a static screen.
    assert third.source == "replay:02.png"


def test_replay_backend_can_loop(tmp_path):
    _write(tmp_path / "01.png", (10, 20, 30))
    _write(tmp_path / "02.png", (40, 50, 60))

    backend = ReplayBackend(tmp_path, loop=True)
    sources = [backend.grab().source for _ in range(4)]
    assert sources == [
        "replay:01.png",
        "replay:02.png",
        "replay:01.png",
        "replay:02.png",
    ]


def test_replay_backend_accepts_a_single_file(tmp_path):
    path = tmp_path / "only.png"
    _write(path, (1, 2, 3))
    assert ReplayBackend(path).grab().source == "replay:only.png"


def test_replay_backend_rejects_empty_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplayBackend(tmp_path)


def test_replay_backend_rejects_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplayBackend(tmp_path / "nope")


def test_create_backend_rejects_unknown_kind():
    with pytest.raises(ValueError, match="Unknown capture backend"):
        create_backend("telepathy")


def test_create_backend_builds_replay(tmp_path):
    _write(tmp_path / "01.png", (7, 7, 7))
    backend = create_backend("replay", path=tmp_path)
    assert isinstance(backend, ReplayBackend)


def test_backend_works_as_context_manager(tmp_path):
    _write(tmp_path / "01.png", (7, 7, 7))
    with ReplayBackend(tmp_path) as backend:
        assert backend.grab().image.shape == (90, 160, 3)
