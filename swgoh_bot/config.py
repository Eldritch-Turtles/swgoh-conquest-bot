"""Paths and tunable constants.

Kept deliberately boring: plain module-level constants, no config file yet.
When we need real configuration (account settings, farming targets) this grows
into a proper settings model.
"""

from __future__ import annotations

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
    "Galaxy of Heroes",
    "SWGOH",
    "Star Wars",
)
