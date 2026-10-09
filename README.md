# swgoh-conquest-bot

An automation bot for Conquest in *Star Wars: Galaxy of Heroes*, targeting the
**official PC client** on Windows.

**Status: step 1 of 7.** Right now the bot can *see* the game. It cannot click
anything yet. That is deliberate — see [Roadmap](#roadmap).

---

## Read this first

Automating SWGOH is against EA's terms of service, and accounts have been
banned for it. Conquest is PvE so no other player is affected, but the risk to
your account is real. **Test on an account you can afford to lose.**

Two more things worth knowing before you invest time:

- **The bot will control your mouse.** DirectX games generally ignore synthetic
  click messages, so from step 3 onward the bot has to move the real cursor.
  While it runs, the computer is busy.
- **Conquest data is hand-maintained.** No API anywhere publishes Conquest
  feats, node layouts or disk modifiers, and they change every season.
  swgoh.gg and comlink give you *roster* data; ahnaldt101 and Reddit give you
  *strategy in prose*. Expect to update a season file by hand roughly monthly.

---

## Step 1 walkthrough

Nothing here clicks anything in the game or sends any data anywhere. Worst case
you get some PNGs in a folder.

### 1. Install Python

Download Python 3.11 or newer from [python.org/downloads](https://www.python.org/downloads/).

In the installer, **tick the "Add python.exe to PATH" checkbox** at the bottom
of the first screen before clicking Install. This is the single most common
thing to get wrong; without it, none of the commands below are found.

Verify it worked — press `Win+X`, choose **Terminal** (or **PowerShell**), and run:

```powershell
python --version
```

You want `Python 3.11.x` or higher. If you get an error or the Microsoft Store
opens, PATH wasn't ticked: re-run the installer, choose Modify, and add it.

### 2. Get the code

If you have Git installed:

```powershell
cd ~\Documents
git clone https://github.com/Eldritch-Turtles/swgoh-conquest-bot.git
cd swgoh-conquest-bot
git checkout claude/swgoh-conquest-bot-sm360w
```

No Git? Download the branch as a ZIP from GitHub (**Code → Download ZIP**),
extract it to `Documents`, then open a terminal in that folder: shift-right-click
the extracted folder and choose **Open in Terminal**.

Confirm you're in the right place — this should list `README.md` and `swgoh_bot`:

```powershell
dir
```

### 3. Create a virtual environment

A venv keeps this project's packages separate from the rest of your system.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`. If instead you get
*"running scripts is disabled on this system"*, run this once and retry:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

You need to run the `Activate.ps1` line again every time you open a new
terminal for this project.

### 4. Install the dependencies

```powershell
pip install -r requirements.txt
```

Takes a minute or two — OpenCV is a large download.

### 5. Check the install

```powershell
python -m swgoh_bot.cli doctor
```

Expected: `cv2`, `numpy`, `mss` and `pillow` all `[ok]`. `windows_capture` and
`pytesseract` showing `[missing]` is fine — they're optional and not needed
yet. Don't worry about the game window line yet.

### 6. Find the game window

**Start the SWGOH PC client** and get to the home screen. Leave it open and
visible — don't minimise it. Back in the terminal:

```powershell
python -m swgoh_bot.cli windows
```

You get a numbered list of every open window, largest first, like:

```
  #         size  title
---  -----------  --------------------------------------------------
  1   1920x1080   STAR WARS™: Galaxy of Heroes
  2   1280x720    EA app
  3    900x600    Windows PowerShell
```

Find the Galaxy of Heroes one — it's almost certainly one of the largest — and
lock it in by number:

```powershell
python -m swgoh_bot.cli set-window 1
```

That writes the title to `bot_settings.json`. You only do this once. Confirm:

```powershell
python -m swgoh_bot.cli doctor
```

The "game window" section should now print the window's size and position.

**If no window looks like the game:** the client may render into a child window
with an odd title. Look for one whose size matches the game area. You can also
set the title directly if you can read it from the title bar:

```powershell
python -m swgoh_bot.cli set-window "STAR WARS™: Galaxy of Heroes"
```

### 7. Take your first screenshot

With the game on its **home screen**:

```powershell
python -m swgoh_bot.cli grab --label home
```

It prints where it saved the PNG — inside `data\captures\`. **Open that file and
look at it.** You should see the game's home screen.

If it's black, empty or shows your desktop, see [Troubleshooting](#troubleshooting).

Now repeat on the other screens, navigating the game by hand between each:

```powershell
python -m swgoh_bot.cli grab --label conquest_map
python -m swgoh_bot.cli grab --label squad_select
python -m swgoh_bot.cli grab --label battle_result
```

### 8. Teach the bot one screen

```powershell
python -m swgoh_bot.cli learn home --image data\captures\home-20261009-143000.png
```

Use the actual filename that `grab` printed. A window opens showing your
screenshot.

**Drag a box around something unique to that screen** — a title, a button
label, a distinctive icon — then press **Enter**.

Pick something with detail in it. A plain patch of background gets rejected on
purpose: a featureless template matches *every* screen at full confidence,
which would poison the whole classifier. If you get a "featureless" error, just
run the command again and pick something with text or an icon in it.

### 9. Check that it works

Bring the game back to its home screen, then:

```powershell
python -m swgoh_bot.cli identify -v
```

You should see `screen: home (0.9xx)`. Navigate somewhere else in the game and
run it again — now it should say `unknown`.

### 10. Prove the anchor is actually discriminating

A screen matching itself at 0.99 proves the plumbing works. It does **not**
prove your anchor is specific to that screen. If you happened to crop
persistent UI chrome — a nav bar, a resource counter, anything present
everywhere — it will match `home` on every screen in the game, and `identify`
alone will never tell you.

`check` classifies every capture in `data/captures/` at once and grades the
results against the labels you gave `grab`:

```powershell
python -m swgoh_bot.cli check
```

```
file                              expected          detected           score  verdict
--------------------------------  ----------------  ----------------  ------  -------
conquest_map-20261009-143000.png  conquest_map      home               1.000  LEAK
home-20261009-143000.png          home              home               1.000  ok
squad_select-20261009-143000.png  squad_select      home               1.000  LEAK

1 correct, 2 wrong, 0 not yet taught

Non-discriminating anchors detected:
  'home' also matches: conquest_map, squad_select
```

`LEAK` means that anchor matched a screen it was never taught — the giveaway
for cropped chrome. Fix it by deleting `data/screens/<name>/` and re-running
`learn`, this time cropping something that appears *only* on that screen.

`(not taught)` is fine; it just means you haven't got to that screen yet.

Aim for every row reading `ok`:

```
3 correct, 0 wrong, 0 not yet taught
```

**That's step 1 complete.** The bot can see, and you can prove it.

Repeat steps 8 and 10 for each screen you captured, then tell me how it went —
the `check` table is what I need to tune thresholds for step 2.

There are around 13 screens on the path from launch to a finished battle, plus
eight or so popups that can interrupt it. See **[docs/screens.md](docs/screens.md)**
for the full inventory and a per-screen workflow.

If a screen animates — most do — use a burst so the motion gets masked out.
See [Animated screens](#animated-screens).

---

## Command reference

| command | what it does |
|---|---|
| `doctor` | check dependencies, window and known screens |
| `windows` | numbered list of open windows |
| `set-window <n\|title>` | remember which window is the game |
| `grab --label NAME` | screenshot the game into `data/captures/` |
| `learn NAME --image FILE` | teach the bot to recognise a screen |
| `grab --label NAME --burst 5` | capture a burst into a folder, to reveal animation |
| `stability --image DIR` | show what moves; suggest where to crop |
| `identify -v` | say which screen is showing, with scores |
| `check` | grade every capture in a folder; catches leaky anchors |
| `screens` | list the screens the bot knows |

Any command takes `--image PATH` to read a saved PNG instead of the live game,
which is how you work on this without the game running.

---

## Animated screens

Most SWGOH screens move — panning backdrops, drifting scenery, breathing
portraits, pulsing buttons. A template cropped from a moving region never
matches twice, so plain template matching is not enough on its own.

The fix is **burst capture plus masking**. Take several frames a fraction of a
second apart, diff them, and the moving pixels identify themselves. Those
pixels are then excluded from matching, so only the static part of an anchor —
the text, the icon — is ever compared.

```powershell
python -m swgoh_bot.cli grab --label sector_map --burst 5
python -m swgoh_bot.cli stability --image data\captures\sector_map-burst-<stamp>
```

`stability` reports what fraction of the screen is static, writes an overlay
PNG with the animated parts tinted red, and suggests concrete anchor regions
ranked by how much static detail they contain. Then:

```powershell
python -m swgoh_bot.cli learn sector_map --image data\captures\sector_map-burst-<stamp> --burst 5 --region 320,80,220,80
```

The `--burst` flag is what creates the mask.

### Why this matters, measured

On a test screen that is 99% animated, with antialiased title text over moving
scenery, matching the same region with and without a mask:

| | correct screen, unseen frame | wrong screen |
|---|---|---|
| **masked** | **0.998** | 0.065 |
| unmasked | 0.816 — *below threshold, a false negative* | 0.492 |

The unmasked anchor fails outright on a frame it has not seen. The masked one
is confident and has a wide margin.

### Where masking does not help

If every pixel that survives the mask is the *same value* — solid-fill text
over animation, say — there is nothing to correlate, and masked matching
returns 0.0 against everything. Such an anchor never fires. This is rejected at
save time with an explanation.

Note the asymmetry, since it drives two different guards:

- An **unmasked** flat template scores a perfect 1.0 against *every* screen. It
  fails dangerously, so it is rejected if its standard deviation is under 8.
- A **masked** template with uniform surviving pixels scores 0.0 against
  everything. It fails safely, so it only needs to clear a much lower floor.
  A masked anchor measured at standard deviation 6.4 still separated its screen
  from another at 0.998 versus 0.065 — rejecting it would have been wrong.

Every saved anchor is also matched against the frame it was cut from, so an
anchor that cannot recognise its own source is refused rather than written out.

## How it works

```
  game window  ──►  capture  ──►  working resolution  ──►  classifier
   (Windows)        backend         1600x900                 "home"
```

**`window.py`** finds the game window using raw `ctypes` calls to `user32`
(no pywin32 to install). It reports the *client* rectangle — the game's actual
pixels, excluding title bar and borders — and sets DPI awareness so display
scaling doesn't skew coordinates.

**`capture.py`** has three interchangeable backends:

| backend | what it does | when to use |
|---|---|---|
| `mss` | screenshots the desktop region the window covers | default; window must be visible |
| `wgc` | Windows Graphics Capture | works when the window is buried; needs `windows-capture` |
| `replay` | reads PNGs off disk | testing, and developing without the game |

The `replay` backend is why the test suite runs on any OS with no game
installed — and it's how you can hand screenshots to someone else to work from.

**Every frame is squashed to 1600x900 before any matching.** This is the key
decision in the vision layer: rather than writing resolution-independent
matching, we normalise everything — frames and reference templates alike — to
one canonical size. A screen taught at 1080p therefore still matches at 1440p
or in a resized window. Aspect ratio is deliberately not preserved; since both
sides get the same distortion, matching is unaffected.

**`vision.py`** does template matching, not machine learning. Each screen is a
folder under `data/screens/`:

```
data/screens/home/
    screen.json        name, threshold, where to look for each anchor
    anchor_home.png    the cropped template
    mask_home.png      which of those pixels to compare (if taught --burst)
    reference.png      the full screen, kept for debugging
```

A screen's score is its **weakest** anchor — every anchor must be present —
which keeps false positives down. The best screen clearing its threshold wins;
otherwise the answer is `unknown`, never a guess.

---

## Development

```bash
python -m pytest tests/ -q
```

78 tests, no Windows and no game required. They cover the settings layer, the
capture backends, the vision engine — including the cross-resolution claim and
the animation-masking figures above — and the leak detection in `check`.

---

## Troubleshooting

**`python` isn't recognised.** PATH wasn't ticked during install. Re-run the
Python installer, choose Modify, and tick "Add python.exe to PATH".

**`Activate.ps1 cannot be loaded`.** Run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then retry.

**`windows` shows nothing like the game.** The PC client may render into a child
window with a different title than the launcher. Look for one whose dimensions
match the game area rather than going by title.

**Screenshots come out black.** The `mss` backend captures the desktop, so it
fails against GPU-exclusive rendering. Try windowed or borderless mode in the
game's settings first. If that doesn't fix it, switch backend:

```powershell
pip install windows-capture
python -m swgoh_bot.cli grab --backend wgc --label home
```

**Screenshots show the wrong thing / my desktop.** The game window was behind
something, or minimised. `mss` grabs whatever pixels are on screen in that
region. Keep the game visible, or use the `wgc` backend.

**`identify` says `unknown` when it shouldn't.** Run with `-v` to see the score.
Close to the threshold means the anchor is too generic — re-run `learn` and pick
something more distinctive. Near zero means the anchor is being searched in the
wrong place; check `region` in that screen's `screen.json`.

**Everything matches everything.** An anchor is too plain, or it cropped UI
chrome present on every screen. Run `check` to confirm, then delete that
screen's folder under `data/screens/` and re-learn it from a region unique to
that screen.

---

## Roadmap

- [x] **Step 1 — See.** Find the window, capture frames, identify screens.
      Verified working against the real PC client (`Star Wars: Galaxy of
      Heroes`) with the `mss` backend.
- [x] **Step 1.5 — Survive animation.** Burst capture, stability masking,
      anchor suggestion.
- [ ] **Step 2 — Read.** OCR for stamina numbers, feat text, energy counts.
      Also the fallback for screens with no static anchor at all.
- [ ] **Step 3 — Act.** Move the cursor and click. Safety first: a global
      abort hotkey and a dry-run mode before anything taps for real.
- [ ] **Step 4 — Navigate.** A state machine that gets from launch to the
      Conquest map and recovers from popups, disconnects and patch prompts.
- [ ] **Step 5 — Know.** Roster data via [swgoh-comlink](https://github.com/swgoh-utils/swgoh-comlink),
      plus a hand-maintained `season.yaml` describing the current Conquest's
      feats and nodes.
- [ ] **Step 6 — Decide.** The agentic layer: given the feat list, the roster
      and stamina state, choose which node to run and which squad to send.
- [ ] **Step 7 — Report.** Progress updates on feats, crate tier and stamina.

Steps 1–4 are deterministic and cheap. The LLM only enters at step 6, where
judgement is actually needed. Navigation stays scripted because it needs to be
fast, free and debuggable.
