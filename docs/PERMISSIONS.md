# macOS permissions setup

The app (and the Phase 0 spike) needs three permissions, all granted to **the app
that launches the Python process** — i.e. your terminal (Terminal.app, iTerm2, or
the Claude Code desktop app if you run it from there). Grant once; they persist.

All three live in **System Settings → Privacy & Security**:

| Permission | Why we need it | When the prompt appears |
|---|---|---|
| **Microphone** | Record dictation audio | First time the spike opens the mic (first hotkey hold) |
| **Input Monitoring** | Global hotkey capture while other apps are focused | First time `pynput`'s keyboard listener starts (script launch) |
| **Accessibility** | Synthesize the ⌘V keystroke into the focused app | First time the script sends ⌘V (first key release) |

## Steps
1. Run `.venv/bin/python src/spike_phase0.py` from your terminal.
2. Approve each prompt as it appears. If a prompt doesn't appear but the feature
   silently fails, add your terminal manually under the matching pane
   (Privacy & Security → Microphone / Input Monitoring / Accessibility → "+").
3. **After granting Input Monitoring or Accessibility you must quit and relaunch
   the terminal app** — macOS applies these to new processes only.

## LocalDictate.app (Spotlight launch)

`scripts/make_app.sh` builds `~/Applications/LocalDictate.app` — a wrapper that runs
the same `src/app.py` from the repo venv. Permissions attach to **LocalDictate.app
itself** (not your terminal). Three permissions are required.

### Keep this repo OUT of ~/Documents (no Full Disk Access needed)

The project lives at `~/Projects/local-dictate` **deliberately**. macOS TCC blocks app
bundles from reading `~/Documents`, `~/Desktop` and `~/Downloads`, and this
bundle has to read its own interpreter (`.venv/`) and code (`src/app.py`) from
the repo. While the project sat in `~/Documents`, Spotlight launches died
before Python ever started and nothing reached `logs/app.log` — it looked like
the app simply never ran (2026-07-29 bug).

Granting Full Disk Access would also have fixed it, but that's a broad
permission this app doesn't otherwise need. An unprotected path needs none.

**So: if you ever move this repo, don't move it into a protected folder.** Then
re-run `scripts/make_app.sh` (it bakes the repo path into the launcher).

The launcher guards against a regression here: it does a real read of
`src/app.py` (a `[ -r ]` test would silently pass — `access(2)` is blind to
TCC) and shows a dialog naming the fix rather than failing silently. Every
launch attempt is logged to `~/Library/Logs/LocalDictate/launch.log`, which stays
writable regardless of TCC:
```
tail -f ~/Library/Logs/LocalDictate/launch.log
```

**Note:** re-running `scripts/make_app.sh` re-signs the bundle with a new
ad-hoc signature, which **invalidates existing TCC grants** (macOS ties them to
the codesign identity), so you must re-grant the three permissions below after
each rebuild. Code changes don't need a rebuild — just quit and relaunch.

### 1. Microphone
The **Microphone** prompt appears on your first dictation — approve it.

### 2. Input Monitoring
**Input Monitoring** usually fails silently instead of prompting: add Local Dictate
manually under System Settings → Privacy & Security → Input Monitoring → "+" →
~/Applications/LocalDictate.app.

### 3. Accessibility
Add Local Dictate manually under System Settings → Privacy & Security → Accessibility →
"+" → ~/Applications/LocalDictate.app.

### After granting permissions
Quit (menu-bar icon → Quit) and relaunch after granting Input Monitoring or
Accessibility — macOS applies these to new processes only.

### Notes
- No terminal window: output goes to `logs/app.log` (`tail -f logs/app.log`).
- The bundle runs code from the repo, so code changes need no rebuild — just
  quit and relaunch. Re-run `scripts/make_app.sh` only if the repo folder moves.
- The launcher refuses to start a second instance if Local Dictate is already running,
  using a PID file at `~/Library/Application Support/LocalDictate/localdictate.pid`.

### Dock icon note
The Dock icon is intentionally absent. Local Dictate uses `LSUIElement` + the
`NSApplicationActivationPolicyAccessory` activation policy, which is the correct
macOS pattern for menu-bar-only apps. This is not a bug.

## Gotchas
- Permissions attach to the *terminal*, not to Python. Switching terminals
  (Terminal.app → iTerm2) means granting again.
- If the hotkey listener runs but ⌘V never lands, Accessibility is the missing
  one (it fails silently — no prompt on some macOS versions).
- macOS shows an orange mic-in-use dot in the menu bar while recording — a free
  secondary recording indicator.
