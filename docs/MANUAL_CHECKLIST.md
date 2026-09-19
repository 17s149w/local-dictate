# Manual test checklist

Claude prepares these; Sylvan runs them and reports pass/fail. A phase's manual
gate is green **only after Sylvan confirms**.

## Phase 0 gate — OS integration spike

Setup: complete `docs/PERMISSIONS.md`, then run
`.venv/bin/python src/spike_phase0.py` and leave it running in the terminal.

Hotkey = **hold Right Option (⌥)**, release to paste.

| # | Step | Expected | Pass? |
|---|---|---|---|
| 1 | Launch the script | Prints the "Hold Key.alt_r…" banner; permission prompts appear on first use (see PERMISSIONS.md) | |
| 2 | Click into a **Slack** message box, hold Right ⌥ ~2s while speaking, release | Terminal shows "recording…" then "stopped: ~2s audio"; dummy text appears in the Slack box | |
| 3 | Repeat in a **browser** text field (e.g. Google search box) | Dummy text lands at cursor | |
| 4 | Repeat in a **text editor** (Notes, TextEdit, VS Code…) | Dummy text lands at cursor | |
| 5 | Open `spike_recording.wav` (QuickTime) and play it | You hear your voice — mic capture is real | |
| 6 | While typing normally in any app, tap/use Right ⌥ as you'd use Option in daily work (e.g. ⌥-arrow word-jump uses *left* ⌥ — confirm your habits don't hit right ⌥) | No accidental recordings during normal typing | |
| 7 | Note the "paste overhead" ms printed on each release | Ideally < 100ms (baseline for the F1 latency ledger) | |

**Gate = green only if 2, 3, and 4 all pass reliably** (per WORKPLAN Phase 0).
If Right ⌥ collides with your habits (step 6), tell Claude which key to try instead.

## Phase 1 gate — end-to-end dictation pipeline

Setup: run `.venv/bin/python src/app.py` in a terminal. Wait for "Ready." banner.

Hotkey = **hold Right Option (⌥)**, speak, release. Transcript pastes at cursor.
Latency target: **short clip (≤3s speech) ≤ 1.0s · long clip (paragraph) ≤ 2.5s**.

| # | Step | Expected | Actual latency / Pass? |
|---|---|---|---|
| 1 | Launch the app | Whisper loads (may print model logs to stderr), then "Ready." | |
| 2 | **Short clip** — click into any text field, hold ⌥, say ~2s of speech ("hello world quick brown fox"), release | Terminal prints `[latency] clip=2.0s release->pasted=Xms`; transcript appears at cursor; X ≤ 1000ms | |
| 3 | **Long clip** — hold ⌥, speak a full paragraph (~8–10s), release | Transcript appears; latency ≤ 2500ms | |
| 4 | **Kill test (F2 durability)** — hold ⌥, speak a sentence, release, then immediately press Ctrl+C in the terminal | After kill: `ls audio/` shows a `.wav` file with today's timestamp; play it in QuickTime — your speech is there | |
| 5 | **Clipboard restore** — copy a distinctive string ("RESTORE_ME_123"), click into a text field, dictate a short sentence, paste (⌘V) after ~1s | Dictated text landed on the first paste (Cmd+V from the app); your "RESTORE_ME_123" is back on the clipboard when you paste again | |
| 6 | **Mic level** — open the newest `audio/*.wav` in QuickTime | Speech is clearly audible (not whisper-quiet). If barely audible, note it here — normalization should handle transcription but playback may still be quiet | |
| 7 | **Accidental tap guard** — tap Right ⌥ briefly (< 0.3s) without speaking | Terminal prints "accidental tap" skip message; nothing is pasted | |

**Gate = green only if steps 2–5 all pass** (per WORKPLAN Phase 1).
Note the printed latency for steps 2 and 3 here before confirming.

## Phase 2 gate — menu-bar UI and durability

Setup: run `.venv/bin/python src/app.py`. A mic emoji (🎤) appears in the menu bar.
The terminal will print a recovery summary line if any incomplete jobs were found.

Hotkey = tap **Right Option (⌥)** once to start recording, tap again to stop.
Re-paste hotkey = **clean tap of Right ⌘** (release with no other key, held < 0.5s).

| # | Step | Expected | Pass? |
|---|---|---|---|
| 1 | Launch the app | Menu-bar icon shows 🎤; terminal prints "Ready." | |
| 2 | **Icon state changes** — click into a text field, tap ⌥ to start recording | Menu bar switches 🎤→🔴 within ~0.15s | |
| 3 | Tap ⌥ again to stop | Icon switches 🔴→⏳ then back to 🎤 once pasted | |
| 4 | **Menu history** — click the menu-bar icon | Top section shows "Re-paste last"; below it a ● entry for the just-pasted transcript (40-char truncated first line) | |
| 5 | **Paste from history** — focus a text field, click the menu icon, click any history entry | The full transcript pastes at the cursor; your prior clipboard contents are untouched (copy something distinctive first to verify) | |
| 6 | **Failed-paste simulation** — make sure no text field is focused anywhere, tap ⌥, dictate a sentence, tap ⌥ again | Nothing is pasted (no focused field), but the entry still appears in the menu; focus a field and click the entry — text pastes, ≤2 clicks total | |
| 7 | **Re-paste via menu** — click "Re-paste last" with a text field focused | Last dictation text pastes at cursor | |
| 8 | **Re-paste hotkey** — focus a text field, tap Right ⌘ cleanly (press + release, nothing else held, < 0.5s) | Last dictation text pastes at cursor | |
| 9 | **Re-paste hotkey does NOT fire during ⌘C/⌘V** — hold Right ⌘ + press C to copy something | Normal copy; no re-paste | |
| 10 | **Hard-kill during processing** — start a long dictation (~5s), stop recording, then immediately run `pkill -9 -f "src/app.py"` from another terminal before the paste lands | On next launch: terminal prints "recovering 1 incomplete job(s)…"; the transcript appears in the menu under a ◉ (TRANSCRIBED) glyph; click it to copy | |
| 11 | **History survives restart** — quit normally (click Quit) and relaunch | Previous dictations still appear in the menu | |
| 12 | **Retry on ERROR job** — ERROR entries only exist when transcription itself fails, so force one: **quit the app**, then from the project root run:<br>`.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import store; j=store.jobs()[0]; store.update_job(j['id'], status='ERROR', error='forced for test')"`<br>then rename that job's wav in `audio/` (the newest file) and relaunch | Newest menu entry shows ⚠️ with a "↩ Retry" submenu item; clicking Retry keeps it ERROR (audio file missing) | |
| 13 | **Retry success** — restore the renamed wav to its original name, click "↩ Retry" again | Entry advances to ◉ TRANSCRIBED; focus a field and click it — text pastes | |

**Gate = green only if steps 2–8 and 10–11 all pass.**
Steps 9, 12, 13 are strongly recommended but not blocking if hardware makes them hard to test.

## Phase 3 gate — cleanup pass

Setup: ensure Ollama is running (`ollama serve`) and the model is pulled
(`ollama pull gemma4:e2b`). Then run `.venv/bin/python src/app.py`.

| # | Step | Expected | Pass? |
|---|---|---|---|
| 1 | Launch the app with Ollama running | Terminal prints "cleanup: gemma4:e2b ready" within ~20s of launch (model warm-up); hotkey works immediately even before this line appears | |
| 2 | Dictate "um so the meeting is uh at three pm tomorrow" | Pasted text has fillers (um, uh, so) removed and punctuation fixed, e.g. "The meeting is at 3 pm tomorrow." | |
| 3 | Dictate an enumerated list: "I need three things one a new keyboard two a USB hub three a monitor stand" | Pasted text is formatted as a numbered list, one item per line | |
| 4 | Dictate a question: "what time does the train leave on Friday" | Pasted text is a question with a question mark — NOT an answer from the model | |
| 5 | **Latency check** — short clip (≤3s speech): note the printed `release->pasted=Xms clean=Yms` | release→pasted ≤ 1000ms; for a long paragraph ≤ 2500ms | |
| 6 | **Fail-open** — quit the app, stop Ollama (`osascript -e 'quit app "Ollama"'` or `pkill ollama`), relaunch app | Terminal prints "cleanup: Ollama unavailable — pasting raw transcripts (start Ollama and relaunch to enable)"; dictation still works and raw transcript is pasted normally | |
| 7 | Restart Ollama after step 6 | No action needed until next app relaunch; confirm Ollama is running again for future use | |

**Gate = green only on Sylvan's confirmation.**

✅ **GREEN — confirmed by Sylvan 2026-07-19.** All 7 steps passed. Measured:
18.9s clip → 1122ms (clean 826ms) · 59.9s clip → 1371ms · 59s clip → 2206ms
(clean 1741ms) — long-clip budget met, so Phase 5 chunked transcription is
NOT needed per WORKPLAN. Note: gemma treats sentence-leading "so" as filler.

## Phase 5 gate — battery / idle behavior

Scope decisions (per WORKPLAN conditions + measured data):
- **Chunked transcription: skipped** — 59s clip pastes in 2.2s, within the 2.5s budget.
- **Whisper idle-unload: skipped** — the whole app idles at 0.1% CPU / ~126MB RAM
  (measured 2026-07-19); unloading a ~100MB model buys nothing and adds a reload stall.
- **Ollama**: already idle-unloads via `keep_alive: 10m` — its 7GB llama-server leaves
  RAM 10 min after the last dictation, nothing held open.
- Mic is only open while recording (stream created on hotkey start, closed on stop).

| # | Step | Expected | Pass? |
|---|---|---|---|
| 1 | With the app running but idle ≥5 min, run `sudo powermetrics -i 5000 -n 12 --samplers tasks \| grep -iE "python\|ollama"` | Python (the app) shows ~0% CPU / negligible energy impact across samples | |
| 2 | Confirm Ollama unload — after ≥10 min of no dictation, run `ollama ps` | Empty (gemma4:e2b no longer loaded) | |
| 3 | **Cold-start-after-unload** — with `ollama ps` empty, dictate a normal sentence (a few seconds of speech) | Pastes cleaned text within budget: a warm-up ping fires when recording *starts*, so gemma reloads while you speak. Only a very short clip (<~1s) right after unload may fall back to raw once | |
| 4 | Leave the app running through a normal workday on battery | No noticeable battery drain attributable to the app (menu bar → battery usage) | |

**Gate = green only on Sylvan's confirmation.**

## Bug-fix verification (2026-07-29)

These tests require real hardware and cannot be automated. Run after the
2026-07-29 bug-fix batch (Bug A: sleep/wake; Bug B: stuck state + Reset;
Bug C: Spotlight launch; Bug D: Dock icon).

### Lid-close / sleep-wake test (Bug A)

| # | Step | Expected | Pass? |
|---|---|---|---|
| 1 | Run app from terminal (`.venv/bin/python src/app.py`), confirm "Ready." | Menu-bar icon appears, hotkey works | |
| 2 | Click into a text field. Tap ⌥ once to start, tap again to stop | Text pastes — confirms baseline works | |
| 3 | Close the laptop lid and wait ≥10s, then open it | App is still running (icon visible) | |
| 4 | Terminal shows `[wake] system woke — reinitializing PortAudio` within ~1s of wake | Wake observer fired | |
| 5 | Immediately tap ⌥ in a text field and dictate a sentence | Text pastes normally — no "the option button doesn't work" failure | |
| 6 | Repeat steps 3–5 twice more | Hotkey works every time after wake; no permanent freeze | |
| 7 | With LocalDictate.app (Spotlight launch): repeat steps 3–5 | Same result via bundle | |

### Stuck-state / Reset test (Bug B)

| # | Step | Expected | Pass? |
|---|---|---|---|
| 1 | Launch app. Verify menu contains "🔄 Reset" item | Reset item visible | |
| 2 | Tap ⌥ to start a recording, then immediately tap ⌥ again to stop | Normal — confirms baseline | |
| 3 | Click menu-bar icon → "🔄 Reset" while idle | App stays on 🎤; log shows `[reset] force-reset requested` | |
| 4 | Tap ⌥, speak ~3s (don't stop). While recording: click "🔄 Reset" | 🔴 → 🎤; the in-progress audio is saved and appears in menu (F2 — not discarded); log shows `[reset] saving X.Xs of in-progress audio` | |
| 5 | After Reset, tap ⌥ and dictate a sentence | Hotkey works; text pastes | |
| 6 | Watchdog test: verify stuck-recording self-heal. Tap ⌥ to start. From another terminal: `kill -STOP $(pgrep -f "python.*app.py")`, wait 5s, `kill -CONT ...`. Then wait up to 65s. | Within ~2s of CONT, watchdog fires if stream is gone — `[watchdog] state=recording but stream is None` in log; icon returns to 🎤. (If stream is still alive, this test may not trigger; that is also fine.) | |

### Spotlight launch test (Bug C)

| # | Step | Expected | Pass? |
|---|---|---|---|
**Resolved by relocating the repo to `~/Projects/local-dictate`** — outside the
TCC-protected folders, so Full Disk Access is not needed at all. Steps marked ✅
were verified by Claude on 2026-07-30: pre-move, the guard fired the dialog and
named the cause in the launch log; post-move, the bundle launches clean.

| # | Step | Expected | Pass? |
|---|---|---|---|
| 1 | Run `bash scripts/make_app.sh` to rebuild | Prints "Built ~/Applications/LocalDictate.app" + TCC warning | ✅ |
| 2 | Unreadable-repo guard (verified pre-move) | Dialog: "Local Dictate needs Full Disk Access to start."; no silent exit | ✅ |
| 3 | Check `~/Library/Logs/LocalDictate/launch.log` (pre-move) | Contained `ERROR: cannot read .../src/app.py` | ✅ |
| 4 | Re-grant **Input Monitoring** + **Accessibility** for the rebuilt bundle (re-signing invalidates TCC grants) | Permissions granted | |
| 5 | Launch via Spotlight: ⌘Space → "Local Dictate" → Enter | Menu-bar icon (🎤) appears; no dialog | ✅ |
| 6 | Check `~/Library/Logs/LocalDictate/launch.log` | Contains `[launch] python child started as PID XXXXX` | ✅ |
| 7 | Check `logs/app.log` | Contains timestamped startup lines (e.g. `06:19:46.781 Ready.`) | ✅ |
| 8 | Dictate a sentence | Text pastes normally | |
| 9 | Quit and relaunch via Spotlight | Single instance: second launch aborts, log shows `already running as PID …` | |
