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


def _collect_burst(args, count: int, interval: float):
    """Gather several frames of the same screen.

    From a folder of PNGs, every image in it is the burst - that's how a burst
    captured on the Windows box gets analysed anywhere else. From the live
    game, frames are taken `interval` seconds apart.
    """
    from swgoh_bot.capture import ReplayBackend, create_backend, grab_burst

    if args.image and Path(args.image).is_dir():
        backend = ReplayBackend(args.image, loop=True)
        if len(backend.paths) < 2:
            raise ValueError(
                f"{args.image} holds one image; a burst needs at least 2."
            )
        with backend:
            return [backend.grab() for _ in range(len(backend.paths))]

    with _open_backend(args) as backend:
        return grab_burst(backend, count=count, interval=interval)


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
    """Capture a frame, or a burst of them, and write to disk."""
    import cv2

    from swgoh_bot.capture import frames_are_identical, grab_burst

    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = args.label or "capture"

    def pixels(frame):
        return frame.to_working_size().image if args.working_size else frame.image

    if args.burst:
        # A burst goes in its own folder, which is what `stability` and
        # `learn --image <folder>` expect to be handed.
        folder = (
            Path(args.output)
            if args.output
            else CAPTURES_DIR / f"{label}-burst-{stamp}"
        )
        folder.mkdir(parents=True, exist_ok=True)

        with _open_backend(args) as backend:
            frames = grab_burst(backend, count=args.burst, interval=args.interval)

        for index, frame in enumerate(frames, start=1):
            cv2.imwrite(str(folder / f"{index:02d}.png"), pixels(frame))

        print(f"captured {len(frames)} frames from {frames[0].source}")
        print(f"saved    {folder}")
        if frames_are_identical(frames):
            print()
            print("Warning: every frame is identical. Either this screen is")
            print("completely static, or the game was not actually visible.")
        print()
        print("Next, see what moves:")
        print(
            f"    python -m swgoh_bot.cli stability --image {folder} "
            f"--label {label}"
        )
        return 0

    with _open_backend(args) as backend:
        frame = backend.grab()

    path = Path(args.output) if args.output else CAPTURES_DIR / f"{label}-{stamp}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), pixels(frame))
    print(f"captured {frame.size[0]}x{frame.size[1]} from {frame.source}")
    print(f"saved    {path}")
    return 0


def cmd_stability(args) -> int:
    """Find which parts of the current screen hold still.

    SWGOH screens animate - drifting starfields, breathing portraits, pulsing
    buttons. An anchor cropped from a moving region never matches twice. This
    captures a burst, works out what moved, and proposes anchor regions that
    are both static and distinctive.
    """
    import cv2
    import numpy as np

    from swgoh_bot.capture import frames_are_identical, to_working_size
    from swgoh_bot.vision import stability_mask, suggest_anchors

    try:
        frames = _collect_burst(args, args.burst, args.interval)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    print(f"{len(frames)} frames from {frames[0].source}")

    if frames_are_identical(frames):
        print()
        print("All frames are identical - nothing moved at all.")
        print("If this came from a single PNG, that is expected and the")
        print("analysis below is meaningless. Capture a real burst with:")
        print("    python -m swgoh_bot.cli grab --label home --burst 5")
        return 1

    mask = stability_mask(frames)
    stable_pct = 100.0 * float(mask.mean()) / 255.0
    print(f"static pixels: {stable_pct:.1f}%  (animated: {100 - stable_pct:.1f}%)")

    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = args.label or "stability"

    mean_frame = np.mean(
        np.stack([to_working_size(f.image).astype(np.float32) for f in frames]), axis=0
    ).astype(np.uint8)

    # Animated pixels tinted red over the averaged screen, so you can see at a
    # glance which parts of the UI are safe to anchor on.
    overlay = mean_frame.copy()
    moving = mask == 0
    overlay[moving] = (0.45 * overlay[moving] + 0.55 * np.array([0, 0, 255])).astype(
        np.uint8
    )

    mask_path = CAPTURES_DIR / f"{label}-mask-{stamp}.png"
    overlay_path = CAPTURES_DIR / f"{label}-overlay-{stamp}.png"
    cv2.imwrite(str(mask_path), mask)
    cv2.imwrite(str(overlay_path), overlay)
    print(f"mask:    {mask_path}")
    print(f"overlay: {overlay_path}   (red = animated, leave these alone)")

    size = (args.width, args.height)

    # Prefer a fully static region: those make the simplest anchors, needing
    # no mask at all. Only if none exists do we look for partly static ones,
    # which work but must be taught with a burst.
    suggestions = suggest_anchors(frames, count=args.count, size=size)
    needs_mask = False

    if not suggestions:
        suggestions = suggest_anchors(
            frames, count=args.count, size=size, min_stable=0.10
        )
        needs_mask = True

    print()
    if not suggestions:
        print("No region is both static and detailed enough to anchor on.")
        print("Open the overlay and look for any non-red area with text in it.")
        print("If essentially the whole screen moves, this screen needs OCR")
        print("rather than template matching - tell me and I'll do that next.")
        return 2

    name = args.label or "SCREEN_NAME"
    source = args.image or "data/captures/<your burst folder>"

    if needs_mask:
        print("No fully static region found, but these are partly static.")
        print("Taught with --burst, the moving pixels get masked out and")
        print("only the static ones are compared.")
    else:
        print(f"Best anchor regions ({len(suggestions)} found), strongest first:")
    print()

    for index, suggestion in enumerate(suggestions, start=1):
        print(
            f"  {index}. region {suggestion.cli_region:<20} "
            f"static {100 * suggestion.stable_fraction:.1f}%  "
            f"detail {suggestion.detail:.1f}"
        )
    print()
    print("Teach the screen using the top suggestion:")
    burst_flag = " --burst 5" if needs_mask else ""
    print(
        f"    python -m swgoh_bot.cli learn {name} --image {source}"
        f"{burst_flag} --region {suggestions[0].cli_region}"
    )
    return 0


def _largest_run(flags) -> tuple[int, int] | None:
    """Longest contiguous run of True values, as (start, length)."""
    best_start = best_length = 0
    start = None
    for index, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if index - start > best_length:
                best_start, best_length = start, index - start
            start = None
    return (best_start, best_length) if best_length else None


def cmd_hud(args) -> int:
    """Separate the fixed HUD from the scrolling content.

    Run this on a burst captured *while dragging the map*. Anything that holds
    still across a scroll is fixed furniture - header, sector label, energy bar
    - and that is the only safe place to anchor a scrollable screen's identity.
    Anything that moved is content, whose position means nothing.
    """
    import cv2
    import numpy as np

    from swgoh_bot.capture import frames_are_identical
    from swgoh_bot.vision import stability_mask, suggest_anchors

    try:
        frames = _collect_burst(args, args.burst, args.interval)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    print(f"{len(frames)} frames from {frames[0].source}")

    if frames_are_identical(frames):
        print()
        print("Nothing moved between these frames, so there is no scroll to")
        print("analyse. Capture again while dragging the map:")
        print("    python -m swgoh_bot.cli grab --label sector_map --burst 6")
        print("Drag steadily during the capture.")
        return 1

    mask = stability_mask(frames)
    row_static = mask.mean(axis=1) / 255.0

    moving = row_static < 0.5
    run = _largest_run(moving)
    if run is None:
        print()
        print("No band of this screen moved. Either the drag did not register,")
        print("or this screen does not scroll - in which case treat it as a")
        print("normal screen and use `stability` instead.")
        return 2

    start, length = run
    viewport = (0, start, WORK_WIDTH, length)

    print(f"static overall: {100 * mask.mean() / 255:.1f}%")
    print()
    print("Bands, top to bottom:")
    if start > 0:
        print(f"  y {0:>3}-{start:<3}  FIXED      header / HUD")
    print(f"  y {start:>3}-{start + length:<3}  SCROLLING  content")
    if start + length < WORK_HEIGHT:
        print(f"  y {start + length:>3}-{WORK_HEIGHT:<3}  FIXED      footer / HUD")
    print()
    print(f"Suggested viewport: {viewport[0]},{viewport[1]},{viewport[2]},{viewport[3]}")

    # Anchors must come from the fixed bands only.
    fixed_mask = mask.copy()
    fixed_mask[start : start + length, :] = 0
    suggestions = [
        candidate
        for candidate in suggest_anchors(
            frames, count=args.count * 3, size=(args.width, args.height)
        )
        if candidate.y + candidate.height <= start or candidate.y >= start + length
    ][: args.count]

    print()
    if not suggestions:
        print("No usable anchor found in the fixed bands. They may be too plain.")
        print("Open the overlay from `stability` and look for text in the header")
        print("or footer; pass --width/--height to try a different region size.")
        return 2

    print("Anchor regions in the FIXED bands, strongest first:")
    print()
    for index, candidate in enumerate(suggestions, start=1):
        band = "header" if candidate.y < start else "footer"
        print(
            f"  {index}. region {candidate.cli_region:<20} {band:<7} "
            f"static {100 * candidate.stable_fraction:.1f}%  "
            f"detail {candidate.detail:.1f}"
        )

    name = args.label or "SCREEN_NAME"
    source = args.image or "data/captures/<your burst folder>"
    print()
    print("Teach the screen from the fixed HUD, recording the viewport:")
    print(
        f"    python -m swgoh_bot.cli learn {name} --image {source} "
        f"--region {suggestions[0].cli_region} "
        f"--viewport {viewport[0]},{viewport[1]},{viewport[2]},{viewport[3]}"
    )
    return 0


def cmd_scroll_map(args) -> int:
    """Stitch a dragged sequence into one image of the whole scrollable area.

    Drag the map from one end to the other while capturing a burst, and this
    reassembles it. Every node is then locatable once, in coordinates that do
    not shift as the view scrolls.
    """
    import cv2

    from swgoh_bot.scroll import measure_shift, stitch
    from swgoh_bot.vision import ScreenClassifier

    viewport = args.viewport
    if viewport is None and args.screen:
        match = [
            screen
            for screen in ScreenClassifier.from_directory().screens
            if screen.name == args.screen
        ]
        if not match:
            print(f"No screen named {args.screen!r}. Run `screens` to list them.")
            return 1
        viewport = match[0].viewport
        if viewport is None:
            print(f"Screen {args.screen!r} has no viewport recorded.")
            print("Re-teach it with --viewport, or pass --viewport here.")
            return 1

    try:
        frames = _collect_burst(args, args.burst, args.interval)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    if viewport is None:
        print("Warning: no --viewport given, so the fixed HUD will be stitched")
        print("repeatedly and smear across the result. Run `hud` to find it.")
        print()

    shifts = [
        measure_shift(before, after, region=viewport)
        for before, after in zip(frames, frames[1:])
    ]
    weak = [s for s in shifts if not s.trustworthy]
    total = sum(-s.dx for s in shifts)

    print(f"{len(frames)} frames, {len(shifts)} transitions")
    print(f"scrolled {total:+.0f} px in total")
    if weak:
        print(f"{len(weak)} transition(s) too weak to measure - treated as still")
    if abs(total) < 20:
        print()
        print("Barely any movement detected. Did the drag register during capture?")
        return 2

    panorama = stitch(frames, region=viewport)
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = args.label or "scrollmap"
    path = CAPTURES_DIR / f"{label}-panorama-{stamp}.png"
    cv2.imwrite(str(path), panorama.image)

    print(f"panorama {panorama.size[0]}x{panorama.size[1]}")
    print(f"saved    {path}")
    print()
    print("Open it and check the content lines up with no visible seams or")
    print("repeated chunks. Seams mean the drag was too fast between frames -")
    print("capture again with more frames or a slower drag.")
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

    burst = None
    if args.image and Path(args.image).is_dir():
        burst = _collect_burst(args, args.burst or 5, args.interval)
        frame = burst[0]
        print(f"using {len(burst)} frames; animated pixels will be masked out")
    elif args.burst:
        burst = _collect_burst(args, args.burst, args.interval)
        frame = burst[0]
        print(f"captured {len(burst)} frames; animated pixels will be masked out")
    else:
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
            burst=burst,
            viewport=args.viewport,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    print(f"learned screen {args.name!r} -> {target}")
    print("Verify with: python -m swgoh_bot.cli identify -v")
    return 0


def expected_label(path: Path) -> str:
    """Infer which screen a captured file is meant to be, from its name.

    `grab --label home` writes "home-20261009-143000.png", so the label is
    everything before the two trailing numeric parts. A file named anything
    else falls back to its whole stem.
    """
    parts = path.stem.split("-")
    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
        return "-".join(parts[:-2])
    return path.stem


def cmd_check(args) -> int:
    """Classify a whole folder of captures at once and grade the results.

    This is the honesty check on your anchors. A single screen matching itself
    at 0.99 proves nothing - the question is whether it *also* matches every
    other screen. Running the full set catches an anchor that cropped
    persistent UI chrome rather than something screen-specific.
    """
    from swgoh_bot.capture import ReplayBackend
    from swgoh_bot.vision import ScreenClassifier

    directory = Path(args.dir) if args.dir else CAPTURES_DIR
    if not directory.is_dir():
        print(f"No such folder: {directory}")
        return 1

    classifier = ScreenClassifier.from_directory()
    if not classifier.screens:
        print("No screens taught yet. Use `learn` first.")
        return 1

    known = {screen.name for screen in classifier.screens}

    try:
        backend = ReplayBackend(directory)
    except FileNotFoundError as exc:
        print(f"{exc}")
        return 1

    rows = []
    for path in backend.paths:
        result = classifier.classify(ReplayBackend(path).grab())
        rows.append((path, expected_label(path), result))

    name_width = max(len(p.name) for p, _, _ in rows)
    name_width = max(name_width, 4)
    print(
        f"{'file':<{name_width}}  {'expected':<16}  {'detected':<16}  "
        f"{'score':>6}  verdict"
    )
    print(f"{'-' * name_width}  {'-' * 16}  {'-' * 16}  {'-' * 6}  -------")

    correct = wrong = untaught = 0
    leaked: dict[str, list[str]] = {}

    for path, expected, result in rows:
        detected = result.screen or "unknown"
        score = f"{result.score:.3f}" if result.recognised else "-"

        if expected not in known:
            # We never taught this screen. Anything matching it means some
            # other screen's anchor is not specific enough.
            if result.recognised:
                verdict = "LEAK"
                leaked.setdefault(detected, []).append(expected)
                wrong += 1
            else:
                verdict = "(not taught)"
                untaught += 1
        elif detected == expected:
            verdict = "ok"
            correct += 1
        else:
            verdict = "WRONG"
            wrong += 1

        print(
            f"{path.name:<{name_width}}  {expected:<16}  {detected:<16}  "
            f"{score:>6}  {verdict}"
        )

        if args.verbose:
            for name, value in sorted(result.scores.items(), key=lambda kv: -kv[1]):
                print(f"{'':<{name_width}}    {value:.3f}  {name}")

    print()
    print(f"{correct} correct, {wrong} wrong, {untaught} not yet taught")

    if leaked:
        print()
        print("Non-discriminating anchors detected:")
        for screen, others in leaked.items():
            targets = ", ".join(sorted(set(others)))
            print(f"  {screen!r} also matches: {targets}")
        print()
        print("That anchor is probably persistent UI chrome - a nav bar, a")
        print("resource counter, something on every screen. Delete the folder")
        print(f"data/screens/<name>/ and re-run `learn`, cropping something")
        print("that only appears on that one screen.")
        return 2

    if wrong:
        return 2
    if untaught:
        print("Teach the remaining screens with `learn` to finish the check.")
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
        if screen.viewport:
            x, y, w, h = screen.viewport
            print(f"  scrolls: viewport {x},{y},{w},{h}")
        for anchor in screen.anchors:
            h, w = anchor.template.shape[:2]
            masked = " masked" if anchor.mask is not None else ""
            print(
                f"  anchor {anchor.name!r} {w}x{h} region={anchor.region}{masked}"
            )
        stray = screen.anchors_in_viewport()
        if stray:
            names = ", ".join(repr(a.name) for a in stray)
            print(f"  WARNING: {names} sits in the scrolling area and will drift")
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
        "--burst",
        type=int,
        metavar="N",
        help="capture N frames into a folder, to reveal what animates",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="seconds between burst frames (default: 0.2)",
    )
    p.add_argument(
        "--working-size",
        action="store_true",
        help=f"save at {WORK_WIDTH}x{WORK_HEIGHT} instead of native resolution",
    )
    p.set_defaults(func=cmd_grab)

    p = sub.add_parser(
        "stability", help="find which parts of a screen hold still"
    )
    add_capture_options(p)
    p.add_argument("--label", help="screen name, used for output filenames")
    p.add_argument("--burst", type=int, default=5, help="frames to compare")
    p.add_argument("--interval", type=float, default=0.2, help="seconds between frames")
    p.add_argument("--count", type=int, default=5, help="how many regions to suggest")
    p.add_argument("--width", type=int, default=220, help="suggested region width")
    p.add_argument("--height", type=int, default=80, help="suggested region height")
    p.set_defaults(func=cmd_stability)

    p = sub.add_parser(
        "hud", help="split a scrollable screen into fixed HUD and content"
    )
    add_capture_options(p)
    p.add_argument("--label", help="screen name, used in the suggested command")
    p.add_argument("--burst", type=int, default=6, help="frames to compare")
    p.add_argument("--interval", type=float, default=0.2, help="seconds between frames")
    p.add_argument("--count", type=int, default=5, help="how many regions to suggest")
    p.add_argument("--width", type=int, default=220, help="suggested region width")
    p.add_argument("--height", type=int, default=80, help="suggested region height")
    p.set_defaults(func=cmd_hud)

    p = sub.add_parser(
        "scroll-map", help="stitch a dragged sequence into one wide image"
    )
    add_capture_options(p)
    p.add_argument("--label", help="name for the saved panorama")
    p.add_argument("--burst", type=int, default=12, help="frames to stitch")
    p.add_argument("--interval", type=float, default=0.3, help="seconds between frames")
    p.add_argument(
        "--viewport", type=_parse_region, help="scrolling area 'x,y,w,h'"
    )
    p.add_argument(
        "--screen", help="take the viewport from this already-taught screen"
    )
    p.set_defaults(func=cmd_scroll_map)

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
    p.add_argument(
        "--burst",
        type=int,
        metavar="N",
        help="compare N frames and mask out whatever animates",
    )
    p.add_argument(
        "--interval", type=float, default=0.2, help="seconds between burst frames"
    )
    p.add_argument(
        "--viewport",
        type=_parse_region,
        help="scrolling area 'x,y,w,h' on this screen; anchors must sit outside it",
    )
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser(
        "check", help="classify every capture in a folder and grade the results"
    )
    p.add_argument("--dir", help=f"folder of captures (default: {CAPTURES_DIR})")
    p.add_argument("-v", "--verbose", action="store_true", help="show every score")
    p.set_defaults(func=cmd_check)

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
