# swgoh-conquest-bot

An automation bot for Conquest in *Star Wars: Galaxy of Heroes*, targeting the
**official PC client** on Windows.

**Status: step 1 of many.** Right now the bot can *see* the game. It cannot
click anything yet. That is deliberate — see [Roadmap](#roadmap).

---

## Read this first

Automating SWGOH is against EA's terms of service, and accounts have been
banned for it. Conquest is PvE so no other player is affected, but the risk to
your account is real. **Test on an account you can afford to lose.**

Two more things worth knowing before you invest time:

- **The bot will control your mouse.** DirectX games generally ignore synthetic
  click messages, so from step 3 onward the bot has to move the real cursor.
  While it runs, the computer is busy.
- **Conquest data is hand-maintained.** There is no API anywhere that publishes
  Conquest feats, node layouts, or disk modifiers, and they change every
  season. swgoh.gg and comlink give you *roster* data; ahnaldt101 and Reddit
  give you *strategy in prose*. Neither gives you a machine-readable feat list.
  Expect to update a season file by hand roughly monthly.

---

## Setup (Windows)

You need Python 3.11 or newer. Get it from [python.org](https://www.python.org/downloads/)
and **tick "Add Python to PATH"** during install.

Open PowerShell in the folder where you want the project:

```powershell
git clone https://github.com/Eldritch-Turtles/swgoh-conquest-bot.git
cd swgoh-conquest-bot

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

If PowerShell refuses to run the activate script, run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Check everything landed:

```powershell
python -m swgoh_bot.cli doctor
```

---

## Step 1: teaching the bot to see

### 1. Find the game window

Start the SWGOH PC client, then:

```powershell
python -m swgoh_bot.cli windows
```

This lists every open window with its size and title. Find the Galaxy of Heroes
one. If its title isn't already matched, add it to `WINDOW_TITLE_CANDIDATES` in
`swgoh_bot/config.py`.

Confirm the bot can find it:

```powershell
python -m swgoh_bot.cli doctor
```

The "game window" line should now show its dimensions.

### 2. Capture some screens

With the game on its home screen:

```powershell
python -m swgoh_bot.cli grab --label home
```

The PNG lands in `data/captures/`. Repeat on each screen you care about
(`--label conquest_map`, `--label battle_result`, and so on). Open them and
check they show the game and not a black rectangle — if they're black, see
[Troubleshooting](#troubleshooting).

### 3. Teach it what it's looking at

```powershell
python -m swgoh_bot.cli learn home --image data/captures/home-20260905-143000.png
```

A window opens showing the screenshot. **Drag a box around something unique to
that screen** — a title, a button label, a distinctive icon — then press ENTER.

Pick something with detail in it. A flat patch of background is rejected on
purpose: a featureless template matches *every* screen at full confidence, which
would poison the whole classifier.

You can skip the interactive step if you already know the coordinates:

```powershell
python -m swgoh_bot.cli learn home --image data/captures/home.png --region 1180,380,340,240
```

### 4. Check it works

```powershell
python -m swgoh_bot.cli identify -v
```

With the game on the home screen this should print `screen: home (0.9xx)`.
Navigate elsewhere and run it again — it should say `unknown`, or name the other
screen if you've taught it.

`-v` shows every screen's score, which is what you want when tuning.

---

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

| backend  | what it does                                    | when to use |
|----------|-------------------------------------------------|-------------|
| `mss`    | screenshots the desktop region the window covers | default; window must be visible |
| `wgc`    | Windows Graphics Capture                         | works when the window is buried; needs `windows-capture` |
| `replay` | reads PNGs off disk                              | testing, and developing without the game running |

The `replay` backend is why the test suite runs on any OS with no game
installed — and it's how you can send screenshots to someone else to work from.

**Every frame is squashed to 1600x900 before any matching.** This is the key
decision in the vision layer: rather than writing resolution-independent
matching, we normalise everything — frames and reference templates alike — to
one canonical size. A screen taught at 1080p therefore still matches at 1440p or
in a resized window. Aspect ratio is deliberately not preserved; since both
sides get the same distortion, matching is unaffected.

**`vision.py`** does template matching, not machine learning. Each screen is a
folder under `data/screens/` containing a manifest and one or more cropped
"anchor" images:

```
data/screens/home/
    screen.json        name, threshold, where to look for each anchor
    anchor_home.png    the cropped template
    reference.png      the full screen, kept for debugging
```

A screen's score is its **weakest** anchor — every anchor must be present — which
keeps false positives down. The best screen clearing its threshold wins;
otherwise the answer is `unknown`, never a guess.

---

## Development

```bash
python -m pytest tests/ -q
```

29 tests, no Windows and no game required. They cover the capture backends and
the vision engine, including the cross-resolution claim above.

---

## Troubleshooting

**`windows` shows nothing / can't find the client.** The PC client may render
into a child window with a different title than the launcher. Run `windows`
with no filter and look for one whose dimensions match the game area.

**Screenshots come out black.** The `mss` backend captures the desktop, so it
fails on GPU-exclusive rendering. Try windowed or borderless mode, or switch to
the WGC backend:

```powershell
pip install windows-capture
python -m swgoh_bot.cli grab --backend wgc --label home
```

**`identify` says `unknown` when it shouldn't.** Run with `-v` to see the actual
score. If it's close to the threshold, the anchor is too generic — re-run
`learn` and pick something more distinctive. If it's near zero, the anchor is
probably being searched in the wrong place; check the `region` in `screen.json`.

**Everything matches everything.** An anchor is too plain. Delete that screen
folder and re-learn it with a region containing text or an icon.

---

## Roadmap

- [x] **Step 1 — See.** Find the window, capture frames, identify screens.
- [ ] **Step 2 — Read.** OCR for stamina numbers, feat text, energy counts.
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
