"""Paths and tunable constants.

Kept deliberately boring: plain module-level constants, no config file yet.
When we need real configuration (account settings, farming targets) this grows
into a proper settings model.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# Reference screenshots + their manifests. Committed to git: they are the
# bot's eyes, and they need to travel with the code.
SCREENS_DIR = DATA_DIR / "screens"

# Ad-hoc captures taken while debugging. Git-ignored: they pile up fast and
# may contain your account name.
CAPTURES_DIR = DATA_DIR / "captures"

# Every frame is scaled to this size before any vision work happens.
#
# This is the single most important decision in the vision layer. The PC client
# can run at 1080p, 1440p, ultrawide, windowed, whatever. Rather than writing
# resolution-independent matching (hard), we squash every incoming frame to one
# canonical size and do all matching there. Reference templates are stored at
# this size too, so they always line up.
WORK_WIDTH = 1600
WORK_HEIGHT = 900

# Substrings we will try when hunting for the game window, best guess first.
# The PC client's real window title is unconfirmed - `python -m swgoh_bot.cli
# windows` prints every window so we can find out and pin it down.
WINDOW_TITLE_CANDIDATES = (
    # Confirmed title of the official PC client, as reported by the client
    # itself on a real install.
    "Star Wars: Galaxy of Heroes",
    "Galaxy of Heroes",
    "SWGOH",
    "Star Wars",
)


# Machine-specific settings, written by the CLI rather than edited by hand.
# Git-ignored: the window title and layout are particular to your PC.
SETTINGS_PATH = REPO_ROOT / "bot_settings.json"


def load_settings() -> dict:
    """Read bot_settings.json, or return {} if it's absent or corrupt."""
    if not SETTINGS_PATH.is_file():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(settings: dict) -> None:
    """Write bot_settings.json."""
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8"
    )


def update_settings(**changes) -> dict:
    """Merge changes into bot_settings.json and return the result."""
    settings = load_settings()
    settings.update(changes)
    save_settings(settings)
    return settings


def window_title_candidates() -> tuple[str, ...]:
    """Titles to try when hunting for the game window, best first.

    A title saved via `cli set-window` takes priority over the built-in
    guesses, so nobody has to edit this file to get going.
    """
    saved = load_settings().get("window_title")
    if isinstance(saved, str) and saved.strip():
        return (saved,) + WINDOW_TITLE_CANDIDATES
    return WINDOW_TITLE_CANDIDATES
