"""Command line entry point.

    python -m swgoh_bot.cli doctor            check the environment
    python -m swgoh_bot.cli windows           list open windows (Windows only)
    python -m swgoh_bot.cli grab              save a screenshot of the game
    python -m swgoh_bot.cli identify          say which screen we're on
    python -m swgoh_bot.cli learn NAME ...    teach the bot a new screen
    python -m swgoh_bot.cli screens           list screens the bot knows
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from swgoh_bot import __version__
from swgoh_bot.config import (
    CAPTURES_DIR,
    SCREENS_DIR,
    SETTINGS_PATH,
    WORK_HEIGHT,
    WORK_WIDTH,
    load_settings,
    update_settings,
    window_title_candidates,
)


def _open_backend(args):
    """Build the capture backend the user asked for."""
    from swgoh_bot.capture import create_backend

    if args.image:
        return create_backend("replay", path=args.image)
    return create_backend(args.backend)


def cmd_doctor(args) -> int:
    """Report what is and isn't available on this machine."""
    import platform

    print(f"swgoh-conquest-bot {__version__}")
    print(f"python   {sys.version.split()[0]}  ({platform.platform()})")
    print(f"working resolution  {WORK_WIDTH}x{WORK_HEIGHT}")
    print()

    print("dependencies")
    for module, purpose in [
        ("cv2", "image matching"),
        ("numpy", "arrays"),
        ("mss", "screen capture"),
        ("windows_capture", "background window capture (optional)"),
        ("pytesseract", "text reading (needed later, not yet)"),
    ]:
        try:
            __import__(module)
            print(f"  [ok]      {module:<18} {purpose}")
        except ImportError:
            print(f"  [missing] {module:<18} {purpose}")

    print()
    print("game window")
    from swgoh_bot.window import IS_WINDOWS, WindowNotFoundError, find_game_window

    saved = load_settings().get("window_title")
    if saved:
        print(f"  configured title: {saved!r}")
    else:
        print(f"  no title set yet; guessing from {list(window_title_candidates())}")

    if not IS_WINDOWS:
        print("  [skip]    not on Windows - use --image to work from saved captures")
    else:
        try:
            window = find_game_window()
            print(f"  [ok]      {window}")
        except WindowNotFoundError:
            print("  [missing] could not find it. Is the game running?")
            print("            run: python -m swgoh_bot.cli windows")

    print()
    from swgoh_bot.vision import ScreenClassifier

    known = ScreenClassifier.from_directory().screens
    print(f"known screens: {len(known)}")
    for screen in known:
        print(f"  - {screen.name}")
    if not known:
        print("  (none yet - capture some with `grab`, then teach them with `learn`)")
    return 0


def cmd_windows(args) -> int:
    """Dump every titled window, numbered, so we can identify the game client.

    The listing is saved to bot_settings.json so `set-window <number>` can
    refer back to it. That spares the user from retyping a long title.
    """
    from swgoh_bot.window import IS_WINDOWS, enumerate_windows

    if not IS_WINDOWS:
        print("Window listing only works on Windows.")
        return 1

    windows = enumerate_windows()
    if not windows:
        print("No titled windows found. Is anything actually open?")
        return 1

    needle = (args.filter or "").lower()
    shown = [w for w in windows if not needle or needle in w.title.lower()]
    if not shown:
        print(f"No window title contains {args.filter!r}.")
        return 1

    # Biggest windows first: the game is almost certainly one of the largest.
    shown.sort(key=lambda w: w.width * w.height, reverse=True)

    print(f"{'#':>3}  {'size':>11}  title")
    print(f"{'-' * 3}  {'-' * 11}  {'-' * 50}")
    for index, window in enumerate(shown, start=1):
        print(f"{index:>3}  {window.width:>5}x{window.height:<5}  {window.title}")

    update_settings(last_listing=[w.title for w in shown])

    print()
    print("Find the Galaxy of Heroes client above (likely one of the largest),")
    print("then lock it in with its number, e.g.:")
    print("    python -m swgoh_bot.cli set-window 1")
    return 0


def cmd_set_window(args) -> int:
    """Remember which window is the game, by list number or by title text."""
    target = args.window.strip()

    if target.isdigit():
        listing = load_settings().get("last_listing") or []
        index = int(target)
        if not listing:
            print("No saved window listing. Run this first:")
            print("    python -m swgoh_bot.cli windows")
            return 1
        if not 1 <= index <= len(listing):
            print(f"Pick a number between 1 and {len(listing)}; got {index}.")
            return 1
        title = listing[index - 1]
    else:
        title = target

    update_settings(window_title=title)
    print(f"Saved window title: {title!r}")
    print(f"  -> {SETTINGS_PATH}")

    from swgoh_bot.window import IS_WINDOWS, WindowNotFoundError, find_game_window

    if IS_WINDOWS:
        try:
            print(f"Found it: {find_game_window()}")
        except WindowNotFoundError:
            print("Warning: saved, but no window with that title is open now.")
            return 1

    print("\nNext: python -m swgoh_bot.cli grab --label home")
    return 0


def cmd_grab(args) -> int:
    """Capture one frame and write it to disk."""
    import cv2

    with _open_backend(args) as backend:
        frame = backend.grab()

    image = frame.to_working_size().image if args.working_size else frame.image

    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = args.label or "capture"
    path = Path(args.output) if args.output else CAPTURES_DIR / f"{label}-{stamp}.png"
    path.parent.mkdir(parents=True, exist_ok=True)

    cv2.imwrite(str(path), image)
    print(f"captured {frame.size[0]}x{frame.size[1]} from {frame.source}")
    print(f"saved    {path}")
    return 0


def cmd_identify(args) -> int:
    """Say which known screen the current frame is."""
    from swgoh_bot.vision import ScreenClassifier

    classifier = ScreenClassifier.from_directory()
    if not classifier.screens:
        print("The bot doesn't know any screens yet.")
        print("Capture one with `grab`, then teach it with `learn`.")
        return 1

    with _open_backend(args) as backend:
        frame = backend.grab()

    result = classifier.classify(frame)
    print(f"source: {frame.source}")
    print(f"screen: {result}")

    if args.verbose:
        print("\nall scores:")
        for name, score in sorted(result.scores.items(), key=lambda kv: -kv[1]):
            print(f"  {score:.3f}  {name}")
        for match in result.matches:
            print(f"  anchor {match.anchor!r} at {match.center} ({match.score:.3f})")

    return 0 if result.recognised else 2


def _parse_region(text: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"Region must be 'x,y,w,h' at {WORK_WIDTH}x{WORK_HEIGHT}, got {text!r}"
        )
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Region values must be integers: {text!r}")
    return x, y, w, h


def cmd_learn(args) -> int:
    """Teach the bot to recognise a screen from a captured image."""
    import cv2

    from swgoh_bot.capture import to_working_size
    from swgoh_bot.vision import save_screen_definition

    with _open_backend(args) as backend:
        frame = backend.grab()

    region = args.region
    if region is None:
        # Let the user drag a box around the identifying element.
        work = to_working_size(frame.image)
        print("Drag a box around something unique to this screen, then press ENTER.")
        print("Pick text or an icon - a plain background patch will be rejected.")
        try:
            box = cv2.selectROI("select anchor", work, showCrosshair=False)
            cv2.destroyAllWindows()
        except cv2.error as exc:
            print(f"Interactive selection unavailable ({exc}).")
            print("Pass --region x,y,w,h instead.")
            return 1
        if not box or box[2] == 0 or box[3] == 0:
            print("Nothing selected.")
            return 1
        region = tuple(int(v) for v in box)
        print(f"selected region: {region[0]},{region[1]},{region[2]},{region[3]}")

    try:
        target = save_screen_definition(
            args.name,
            frame,
            region,
            description=args.description,
            threshold=args.threshold,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    print(f"learned screen {args.name!r} -> {target}")
    print("Verify with: python -m swgoh_bot.cli identify -v")
    return 0


def cmd_screens(args) -> int:
    """List the screens the bot currently recognises."""
    from swgoh_bot.vision import ScreenClassifier

    screens = ScreenClassifier.from_directory().screens
    if not screens:
        print(f"No screens defined in {SCREENS_DIR}")
        return 1
    for screen in screens:
        print(f"{screen.name}  (threshold {screen.threshold})")
        if screen.description:
            print(f"  {screen.description}")
        for anchor in screen.anchors:
            h, w = anchor.template.shape[:2]
            print(f"  anchor {anchor.name!r} {w}x{h} region={anchor.region}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swgoh_bot",
        description="SWGOH Conquest bot - step 1: seeing the game.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_capture_options(sp):
        sp.add_argument(
            "--backend",
            default="auto",
            choices=["auto", "mss", "wgc"],
            help="how to capture the live game (default: auto)",
        )
        sp.add_argument(
            "--image",
            help="read from this PNG or folder instead of the live game",
        )

    p = sub.add_parser("doctor", help="check the environment")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("windows", help="list open windows to find the game client")
    p.add_argument("--filter", help="only show titles containing this text")
    p.set_defaults(func=cmd_windows)

    p = sub.add_parser("set-window", help="remember which window is the game")
    p.add_argument(
        "window",
        help="a number from the `windows` listing, or the window title itself",
    )
    p.set_defaults(func=cmd_set_window)

    p = sub.add_parser("grab", help="capture a screenshot")
    add_capture_options(p)
    p.add_argument("--label", help="name to include in the saved filename")
    p.add_argument("--output", help="write to this exact path")
    p.add_argument(
        "--working-size",
        action="store_true",
        help=f"save at {WORK_WIDTH}x{WORK_HEIGHT} instead of native resolution",
    )
    p.set_defaults(func=cmd_grab)

    p = sub.add_parser("identify", help="say which screen is showing")
    add_capture_options(p)
    p.add_argument("-v", "--verbose", action="store_true", help="show every score")
    p.set_defaults(func=cmd_identify)

    p = sub.add_parser("learn", help="teach the bot a new screen")
    add_capture_options(p)
    p.add_argument("name", help="screen name, e.g. home or conquest_map")
    p.add_argument(
        "--region",
        type=_parse_region,
        help=f"anchor box 'x,y,w,h' at {WORK_WIDTH}x{WORK_HEIGHT}; "
        "omit to select it interactively",
    )
    p.add_argument("--description", default="", help="human note about this screen")
    p.add_argument("--threshold", type=float, default=0.85, help="match threshold")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("screens", help="list known screens")
    p.set_defaults(func=cmd_screens)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
