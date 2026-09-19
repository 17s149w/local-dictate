# Morning verification — 2026-07-30

Everything from the bug list and wish list is built, tested, and committed.
What's left is the part I couldn't do: the checks that need your hands and your
laptop lid.

**Two things changed location.** The project now lives at **`~/Projects/local-dictate`**
(it was in `~/Documents`), and the app is running from
`~/Applications/LocalDictate.app` — launched the real way, via the bundle. The 🎤
should be in your menu bar right now.

Work top to bottom. Steps 1–4 are the bug fixes; 5–6 are the new features.
If something fails, the log is `logs/app.log` (now timestamped) and
`~/Library/Logs/LocalDictate/launch.log` for launch problems.

---

## 1. Re-grant two permissions (2 min) — no Full Disk Access needed

**Moving the repo was the actual fix for the Spotlight bug.** macOS blocks app
bundles from reading `~/Documents`, so Local Dictate couldn't read its own interpreter
and died before Python started. Outside a protected folder it needs no special
permission — Full Disk Access is off the table entirely.

**Already verified:** launching `~/Applications/LocalDictate.app` starts cleanly and
the menu-bar icon appears. That's how it's running now.

Rebuilding the bundle re-signs it, which invalidates TCC grants, so you need to
re-add two things once:

1. **System Settings → Privacy & Security → Input Monitoring** → "+" →
   `~/Applications/LocalDictate.app`
2. Same under **Accessibility**
3. Quit Local Dictate (menu-bar icon → Quit) and relaunch via ⌘Space → "Local Dictate"
4. The **Microphone** prompt appears on your first dictation — approve it

| Expected | If it fails |
|---|---|
| Hotkey records; text pastes at the cursor | These two fail *silently* with no prompt: no recording ⇒ Input Monitoring, records but never pastes ⇒ Accessibility |

Your terminal command changed too, if you use it:
`cd ~/Projects/local-dictate && .venv/bin/python src/app.py`

---

## 2. The lid-close bug (the one that killed the hotkey)

**What was wrong:** on wake, the audio device goes stale and opening the mic
throws. That throw happened *inside* the hotkey callback, which killed the
keyboard listener outright — so the hotkey was dead forever while the menu kept
working. That matches your report exactly.

Test it:

1. Close the lid, wait ~30 seconds, open it
2. Click into a text field and tap Right ⌥, speak, tap again

| Expected | Notes |
|---|---|
| Recording works normally on the first try | A wake observer resets the audio system before you press anything |
| If the first attempt hiccups, the second works | Fallback layers: PortAudio reinit + retry, then a watchdog restart |

Then check the log: `grep -E "\[wake\]|\[watchdog\]" logs/app.log`. You should
see `[wake] sleep/wake observer registered` at startup and a wake line after
opening the lid.

**Worth doing twice** — sleep/wake is exactly the kind of thing that works once
and fails the third time. This is the fix I'm least able to verify myself.

## 3. The stuck-in-recording bug

Same family as #2. Three layers now:

- The watchdog notices impossible states (icon says recording but no mic
  stream; stuck processing >60s) and returns to idle on its own
- **New "🔄 Reset" menu item** — the manual escape hatch, so you never have to
  quit and relaunch again
- If you hit Reset mid-recording, your audio is **saved and transcribed**, not
  discarded (F2)

To test Reset deliberately: start recording, click the menu icon → 🔄 Reset.
Expected: icon returns to 🎤, and the partial dictation still shows up in
history.

## 4. Sentence spacing + literal formatting words

Dictate this sentence, with a real pause where marked:

> "the first thing is done [pause] the second thing is next"

| Expected |
|---|
| `The first thing is done. The second thing is next.` — with a space |

Then test the formatting commands (this is the wish-list item):

| Say this | You should get |
|---|---|
| "that is a great idea exclamation point" | `That is a great idea!` |
| "i need milk comma eggs comma and bread period" | `I need milk, eggs, and bread.` |
| "heading project notes new line ship the fix" | `# Project notes` then the line |
| "bullet first point bullet second point" | `- first point` / `- second point` |
| "bold this matters end bold" | `**this matters**` |
| "the Cretaceous period ended" | keeps the *word* "period" — not a `.` |

That last row is the important one: the model has to tell a formatting command
from an ordinary word. All 24 eval cases pass, including three negative
controls like that one.

**One known weak spot:** if you combine a punctuation command *and* bold in the
same breath ("ship it exclamation point that is bold huge end bold news"),
gemma sometimes drops the tail. Rather than paste a silently shortened
dictation, the app now detects that and **falls back to your full raw text**.
So you'd see the literal command words that one time instead of losing half the
sentence. Losing words is the worse failure. If you hit this often, tell me and
I'll take another run at it.

## 5. Stats menu (wish-list item 1)

Menu bar → **📊 Stats**. Backfilled from your existing history, so it starts
with real numbers rather than zeros:

```
2,256 words dictated
42 dictations
Avg speaking speed: 106 WPM
Time saved vs typing: 23m
Wispr Flow avoided: $6.73 (10 days)
```

The savings line is prorated from your first dictation (2026-07-19) at $20/mo,
so it climbs on its own. Price lives in one constant in `src/stats.py` if Wispr
changes it. "Time saved" compares your actual speaking time against typing the
same words at 50 WPM.

## 6. Cleanup no longer times out

Your logs showed `[cleanup] error contacting Ollama: timed out` constantly —
meaning **you've silently been getting raw, uncleaned text on longer
dictations**. The timeout was a flat 2.5s regardless of length.

It now scales with transcript length (up to 3.5s). After a day of use:

```
grep -c "timed out" logs/app.log
```

Expected: near zero. If it's still climbing, tell me and I'll raise the ceiling
or make the cleanup async.

---

## Things I found that you didn't report

**Running the test suite was deleting your dictation history.** The durability
test wrote junk jobs into the real `history.json`, and since the store caps at
50 it evicted real entries to make room. Latent since Phase 2; it ate a few
tonight before I caught it. Fixed at the source (importing `app.py` no longer
touches real data paths) plus a `tests/conftest.py` that structurally prevents
any test from reaching your data. Junk purged; backup at `history.json.bak`,
delete it whenever.

**Two test files were silently disabling each other.** The new resilience tests
stubbed modules globally, which made the transcription test compare against an
empty string and every store test error out — while the workers that wrote them
reported those as "pre-existing failures." They weren't. Fixed; the suite is
96 passing with no skips.

---

## Status

| Item | State |
|---|---|
| Bug 1 — literal "exclamation point" | ✅ fixed, 24/24 eval |
| Bug 2 — menu-bar icon missing | ⚠️ **needs one Control Center change from you** — the icon is being placed behind the notch; see the addendum at the bottom. (My earlier guess that it was just bug 5 was wrong.) |
| Bug 3 — stuck in recording | ✅ fixed — **needs your hands** |
| Bug 4 — hotkey dead after lid close | ✅ fixed — **needs your lid** |
| Bug 5 — won't launch from Spotlight | ✅ fixed by moving the repo to `~/Projects/local-dictate`; verified launching from the bundle |
| Wish 1 — metrics menu | ✅ shipped, backfilled |
| Wish 2 — spoken formatting | ✅ shipped |
| Wish 3 — markdown for Notion | ✅ shipped (headings, bold, italic, bullets, numbered lists) |
| Phase 5 battery gate | ⬜ still unconfirmed from before — `docs/MANUAL_CHECKLIST.md` |

Once steps 1–4 pass, mark the "Bug-fix verification (2026-07-29)" section in
`docs/MANUAL_CHECKLIST.md` green and the project is where we planned to stop.

## If you want to keep going

Rough order of value, not started:

1. **Make cleanup async** — paste raw immediately, replace with cleaned text a
   beat later. Would make long dictations feel instant and retire the timeout
   question entirely
2. **Bold scoping** — the one formatting case gemma handles unreliably

---

## Addendum — why the menu-bar icon was invisible (2026-07-30)

Not a bug in the app, and not random. On notched Macs the menu bar is two
strips (here `0–663` and `848–1512`) with an unusable gap where the notch is.
macOS does **not** reflow status items around that gap: when the bar is full,
overflow items are positioned *under the notch* and are invisible — while still
reporting `isVisible=True` with a perfectly valid frame. That's why every
diagnostic looked healthy while Sylvan saw nothing.

Local Dictate was landing at x≈766–805, squarely inside the dead zone. Two other
status items were hidden the same way, so menu-bar icons were already being
lost before Local Dictate existed.

**Fix:** System Settings → Control Center → set unused items to "Don't Show in
Menu Bar" (Screen Mirroring, Bluetooth, Now Playing, Focus, Stage Manager are
typical dead weight, ~32–42px each). Freeing ~85px moves Local Dictate right of x=848.
Eleven system items currently occupy `973–1512`.

Restarting does not help — placement is deterministic for a given set of items.
It appeared to be intermittent because the set changes: on an external display
(no notch) it was always visible, and earlier runs landed at x=891/973, just
clear of the gap.

`app.py` now logs an explicit `[ui] WARNING: icon is BEHIND THE NOTCH` line at
startup when this happens, so it can never eat debugging time again.
