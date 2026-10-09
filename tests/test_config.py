"""Settings file tests.

Each test redirects SETTINGS_PATH into tmp_path so the real
bot_settings.json is never touched.
"""

from __future__ import annotations

import pytest

from swgoh_bot import config


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "bot_settings.json")


def test_missing_settings_file_reads_as_empty():
    assert config.load_settings() == {}


def test_settings_round_trip():
    config.save_settings({"window_title": "Galaxy of Heroes"})
    assert config.load_settings() == {"window_title": "Galaxy of Heroes"}


def test_update_settings_merges_rather_than_replaces():
    config.save_settings({"window_title": "GoH", "other": 1})
    config.update_settings(window_title="New Title")
    assert config.load_settings() == {"window_title": "New Title", "other": 1}


def test_corrupt_settings_file_reads_as_empty_not_crash():
    config.SETTINGS_PATH.write_text("{not json at all", encoding="utf-8")
    assert config.load_settings() == {}


def test_non_dict_settings_file_reads_as_empty():
    config.SETTINGS_PATH.write_text("[1, 2, 3]", encoding="utf-8")
    assert config.load_settings() == {}


def test_saved_title_takes_priority_over_builtin_guesses():
    config.update_settings(window_title="My Exact Title")
    candidates = config.window_title_candidates()
    assert candidates[0] == "My Exact Title"
    # The built-in guesses stay as fallbacks.
    assert config.WINDOW_TITLE_CANDIDATES[0] in candidates


def test_builtin_guesses_used_when_nothing_saved():
    assert config.window_title_candidates() == config.WINDOW_TITLE_CANDIDATES


@pytest.mark.parametrize("value", ["", "   ", None, 42, [], {}])
def test_blank_or_wrong_typed_title_is_ignored(value):
    config.save_settings({"window_title": value})
    assert config.window_title_candidates() == config.WINDOW_TITLE_CANDIDATES
