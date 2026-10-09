# Screen inventory

Every screen the bot has to recognise between launching the game and finishing
a Conquest battle. Tick them off as you teach them with `learn` and they pass
`check`.

**This list is my best guess and almost certainly wrong in places** — Conquest
gets reworked often. Correct it as you go; it drives everything in step 4
(navigation).

For each screen, the useful questions are: does it animate, and is there static
text on it? Anything with a static title is easy. Anything that moves
everywhere needs a burst (`learn --burst 5`) so the motion gets masked out.

## The main path

| # | screen | notes | taught |
|---|---|---|---|
| 1 | `splash` | EA / CG logos on launch. Timing varies wildly. | ☐ |
| 2 | `login` | Sometimes skipped entirely if the session is live. | ☐ |
| 3 | `home` | The hub. Heavily animated background. | ☐ |
| 4 | `conquest_lobby` | Conquest intro — crate tier, Normal/Hard, enter button. | ☐ |
| 5 | `difficulty_select` | Normal vs Hard. May be folded into the lobby. | ☐ |
| 6 | `sector_map` | The node graph for the current sector. Scrollable. | ☐ |
| 7 | `node_preview` | Tapping a node: modifiers, enemy preview, Battle button. | ☐ |
| 8 | `squad_select` | Unit picker with stamina values. The important one. | ☐ |
| 9 | `disk_prompt` | Data disk / consumable offer. Appears conditionally. | ☐ |
| 10 | `loading` | Between squad select and battle. | ☐ |
| 11 | `battle` | Combat itself. Animated by definition. | ☐ |
| 12 | `battle_result` | Victory / defeat. | ☐ |
| 13 | `rewards` | Loot summary, often several taps to clear. | ☐ |

## Interrupts

These are what actually kill a navigation bot. They can appear at almost any
point, so the state machine in step 4 has to check for them on every tick
rather than assuming the happy path.

| screen | notes | taught |
|---|---|---|
| `daily_login` | Reward popup on first launch of the day. | ☐ |
| `feat_complete` | Feat completion toast / popup. | ☐ |
| `energy_prompt` | Out of energy, offers a refill. **Must never auto-buy.** | ☐ |
| `keycard_prompt` | Keycard / consumable spend confirmation. | ☐ |
| `connection_lost` | Retry dialog. Frequent. | ☐ |
| `patch_prompt` | Update required on launch. | ☐ |
| `conquest_ended` | Conquest is not live — nothing to do. | ☐ |
| `generic_popup` | Catch-all. Anything with a close button. | ☐ |

## Workflow per screen

```powershell
# 1. Capture a burst so animation is visible
python -m swgoh_bot.cli grab --label sector_map --burst 5

# 2. See what holds still, and get a suggested region
python -m swgoh_bot.cli stability --image data\captures\sector_map-burst-<stamp> --label sector_map

# 3. Teach it, masking out whatever moves
python -m swgoh_bot.cli learn sector_map --image data\captures\sector_map-burst-<stamp> --burst 5 --region <from step 2>

# 4. Confirm nothing leaks into it
python -m swgoh_bot.cli check
```

## Screens that may resist template matching

Some screens have no reliable static text — `battle` most obviously, and
possibly `loading`. Two fallbacks, in order of preference:

1. **OCR the title text** (step 2). Reading the words "SELECT SQUAD" is far
   more robust than matching their pixels, and immune to background motion.
2. **Identify by exclusion.** If no screen matches and a known set of UI
   elements is absent, we are probably mid-battle. Crude but workable.

Run `stability` on any screen you suspect and paste the output — if it reports
no usable region, that screen goes on the OCR list.
