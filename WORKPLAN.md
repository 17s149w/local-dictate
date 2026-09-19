# WORKPLAN — Local Voice Dictation build

Companion to `PRD.md`. This doc is written to be handed to **Claude Code (Fable orchestrating)** in a fresh, clean project folder. It defines the build order, how each phase is tested/self-checked, and how to route work to cheaper models to conserve spend.

---

## 0. How to use this doc with Claude Code

- Start in a **new empty folder** (not my personal vault) so context stays clean.
- Drop `PRD.md` + `WORKPLAN.md` in the folder. First action: have Claude create a `CLAUDE.md` summarizing the project, the latency budget, and the failure modes (F1–F6) so every subagent inherits them.
- **Fable is the orchestrator**, not the coder-of-everything. It plans, reviews, and integrates. Cheap models do the bulk writing (see §4).
- Build **strictly in phase order** — Phase 0 de-risks the thing most likely to kill the project (OS integration). Do not start Phase 3+ features until Phase 0–1 pass their tests.
- After each phase, run that phase's **test/eval gate** and record pass/fail before moving on.

## 1. Guiding principles

1. **De-risk before you build.** Prove the scary OS-integration path (mic perms, global hotkey, ⌘V injection) end-to-end with dummy text before any ML.
2. **The clipboard + history is the source of truth.** Paste is best-effort layered on top. This is the design that kills F2 (lost text).
3. **Fail open.** Any slow/failed step (cleanup LLM, paste) falls back to a safe state (raw transcript, clipboard) rather than blocking or dropping text.
4. **Measure latency every phase.** F1 (too slow) is caught by numbers, not vibes.
5. **Cheap by default.** Runtime is 100% local. The build routes work to the cheapest model that can do it correctly (§4).

## 2. Phased build + test gates

Each phase has a **Deliverable**, an **Owner model** (who should do the bulk of it), and a **Test/Eval Gate** Claude must run and pass before continuing.

### Phase 0 — OS integration spike (highest risk first)
- **Deliverable:** A throwaway script that: registers a global push-to-talk hotkey, records mic while held, and on release puts a **fixed dummy string** on the clipboard and synthesizes ⌘V into the focused app. Plus written notes on the macOS **Accessibility + Microphone** permissions needed.
- **Owner:** Fable (this is the make-or-break integration; worth the good model).
- **Test/Eval Gate:**
  - Manual checklist (OS integration can't be unit-tested well): hotkey fires globally in 3+ different apps; dummy text lands at cursor; permissions prompts documented and resolved.
  - Confirm the hotkey doesn't collide with common system/app shortcuts.
  - **Gate = green only if dummy text reliably pastes into Slack, a browser field, and a text editor.**
- **If this phase is hard/blocked:** stop and reconsider stack (native Swift?) before investing further. This is the cheapest place to fail.

### Phase 1 — End-to-end skeleton (real audio → real text)
- **Deliverable:** Replace the dummy string with a real pipeline using **one** transcription engine (default hypothesis on Apple Silicon: `whisper.cpp` w/ Metal, small model) + cleanup off. Hold key → speak → transcript pastes at cursor. **Audio is persisted to disk on record-stop before transcription starts** (first half of durability, §7.7). **Clipboard save/restore is part of the inject step from day one** (save prior clipboard → set text → paste → restore prior clipboard).
- **Owner:** Fable orchestrates; Sonnet writes the pipeline glue.
- **Test/Eval Gate:**
  - End-to-end works on a live utterance.
  - **Latency logged** (release → paste), separately for a **short (≤3s)** and a **long (paragraph)** clip. Compare to tiered budget (short ≤1s, long ≤2.5s). Warm-model vs cold-model both measured.
  - Automated smoke test: feed a known WAV file through the transcription function, assert non-empty plausible output.
  - Kill the app right after record-stop; confirm the audio file survives on disk.
  - **Clipboard not clobbered:** copy something distinctive, dictate, confirm that after the paste the original clipboard contents are restored.

### Phase 2 — Menu-bar UI + history + durability (kills F2)
- **Deliverable:** `rumps` menu-bar icon with clear recording state + visual indicator; both hotkey modes (hold + double-tap latch); dictation history that **defaults to cleaned text, showing raw only when cleanup didn't run**; per-entry copy; **re-paste-last hotkey**; persistent store (JSON or SQLite). Plus the **durability state machine** (§7.7): job records `RECORDED→TRANSCRIBED→CLEANED→INJECTED`, automatic retry-with-backoff, per-entry manual **Retry**, and **crash recovery** (resume incomplete jobs on startup).
- **Owner:** Fable designs the state machine + reviews recoverability; Sonnet implements UI/store/retry.
- **Test/Eval Gate:**
  - **Simulate a failed paste** (no focused field): text is still in history, retrievable in ≤2 clicks, re-paste hotkey works.
  - **Simulate a transcription crash** mid-job: job persists in error state, manual Retry re-runs it from the saved audio.
  - **Crash recovery:** hard-kill app with a job in `RECORDED`; on restart it's detected and resumable.
  - History survives restart; unit tests on the store + state transitions (add/cap/persist/read/advance/retry).
  - **Gate = green only if there is no path where a dictation is lost** — for every stage there is a test proving the artifact is persisted before the next stage runs.

### Phase 3 — Local cleanup pass (Gemma via Ollama), safely
- **Deliverable:** Insert the cleanup step: raw transcript → local LLM → cleaned text. Locked-down prompt (fix punctuation/casing/filler only; never change meaning, translate, or answer). Timeout + **fail-open** to raw transcript.
- **Owner:** Fable writes/owns the prompt (F3 is subtle); Sonnet wires the Ollama HTTP call + timeout + retry + fail-open.
- **Test/Eval Gate (this is the important one for F3):**
  - Eval set of ~15 raw transcripts (incl. tricky ones: a transcript that *looks like a question/command*, one with numbers, one with a name). For each, diff raw vs cleaned and assert: no new facts, no meaning change, no "answering" behavior.
  - Timeout path tested: kill/slow Ollama, confirm it falls back to raw transcript and still pastes.
  - Measure added latency; confirm total still within budget (F1).

### Phase 4 — Model selection eval (PRD §8) + tuning
- **Deliverable:** Standalone eval harness comparing `faster-whisper` / `whisper.cpp` / `distil-whisper` across sizes on my recorded corpus: WER, real-time factor, and battery (`powermetrics`). Results table + recommendation. Then set the app's default to the winner.
- **Owner:** Sonnet builds the harness + runs it; Fable interprets results and recommends.
- **Test/Eval Gate:**
  - Harness produces a reproducible table (engine × size → WER, RTF, CPU-seconds/energy).
  - I review and pick the accuracy/speed/battery tradeoff; default is updated accordingly.

### Phase 5 — Latency + battery hardening (kills F1 tail & F5) + polish
- **Deliverable:**
  - **Chunked transcription** (§7.2): overlap record+transcribe so most audio is transcribed before key-release. Only build this if Phase 1/4 latency numbers show the sequential pipeline misses the long-clip budget. Persist audio chunks as they arrive so durability is preserved.
  - Idle-unload timeout for the transcription model; ensure mic isn't held open when idle; low background CPU. Minimal settings/config file. Optional `.app` packaging.
- **Owner:** Sonnet implements; Fable reviews the streaming correctness + resource behavior.
- **Test/Eval Gate:**
  - If chunked transcription added: long-clip latency re-measured against ≤2.5s; and a crash mid-stream still leaves recoverable audio (durability regression check).
  - `powermetrics` shows negligible energy when idle for 5 min.
  - Cold-start-after-unload latency measured and acceptable.

## 3. Cross-cutting "how we test / evaluate" (for Claude to check its own work)

Claude should self-verify against these throughout, not just at the end:

- **Latency ledger:** keep a running table of measured release→paste times per phase. Any regression above budget (F1) is a stop-and-fix.
- **No-lost-text invariant (F2):** for any code path that produces a transcript, there must be a test proving the text reaches the persistent history *before* the paste is attempted.
- **Cleanup safety (F3):** the raw vs cleaned diff eval must pass before cleanup is enabled by default.
- **Permissions doc:** keep `docs/PERMISSIONS.md` current so a fresh machine can be set up from scratch.
- **Manual checklist for OS integration:** maintain a short markdown checklist that a human (me) runs, since injection/hotkey behavior resists automated testing.
- **High-stakes review:** for the recoverability logic (Phase 2) and the cleanup prompt (Phase 3), run a **separate reviewer subagent** to adversarially check for lost-text paths and meaning-changing edits.

## 3.1 How an eval gate actually works (the loop)

Gates come in two kinds. Be explicit about which is which so it's clear when Claude can proceed alone vs when it needs me.

**Automated gates — Claude runs these itself, no human needed.** Unit tests, the transcription WER harness, latency logging, the cleanup diff eval. Claude writes them, runs them, and only advances a phase if they pass. It reports the numbers (e.g. "short-clip latency 0.8s, WER 6.2%") in its summary. If they fail, it fixes and re-runs — I don't touch anything.

**Manual gates — Claude prepares, I run, I report back.** The things that can't be automated: does dummy text actually paste into Slack/browser/editor (Phase 0)? Does the recording indicator read clearly? Which model *feels* right on the accuracy/speed/battery tradeoff (Phase 4)? For these, Claude produces a short **checklist** in `docs/MANUAL_CHECKLIST.md` with exact steps and expected results, then pauses and asks me to run it. I go through it, tell Claude what passed/failed (a sentence is enough), and it proceeds or fixes.

**The loop per phase:** Claude implements → runs automated gate → if a manual gate exists, hands me the checklist and waits → I test and report → Claude fixes or advances to the next phase. So it's not "Fable hands the whole thing back to me" — Claude self-verifies everything it can, and only pulls me in for the handful of things a human has to eyeball. Tell Claude up front: **do not mark a phase gate green on a manual item without my confirmation.**

## 4. Model delegation strategy (conserve spend)

Two independent levers — keep them straight:

### Lever A — Build-time (conserve Claude Code credits)

**Three tiers, not two.** Cheaper-per-token is not the same as cheaper-per-task — a weak model that needs three revisions plus a Fable review burns more than one good pass. Optimize for *fewest total tokens to correct code*, not lowest sticker price.

- **Fable (`claude-fable-5`) = orchestrator / architect / reviewer.** Planning, the OS-integration spike (Phase 0), the cleanup prompt design (Phase 3), the durability/state-machine design (Phase 2), integration, and adversarial reviews of F2/F3. The judgment-heavy, project-killing parts.
- **Sonnet (`claude-sonnet-5`) = default workhorse coder.** Almost all implementation: the pipeline, UI/store, retry logic, eval harness, tests. Strong enough to one-pass most tasks and follow a spec across files, which is what actually saves money. **This is where the bulk of build tokens should go — not Haiku.**
- **Haiku (`claude-haiku-4-5`) = mechanical only.** Narrow, unambiguous, low-reasoning tasks: formatting, simple find-replace edits, docstrings, filling in a function whose signature + test already exist, boilerplate config. Don't hand Haiku multi-file features or anything touching F1–F6 — the rework usually costs more than it saved.

**Rule of thumb:** pick the *cheapest model that can one-pass it correctly*. Clear spec + existing test → Sonnet (or Haiku if truly mechanical). Subtle, cross-cutting, or F1–F6-adjacent → Fable does or reviews it.

### Lever B — Runtime (keep the shipped app free)
- The app makes **zero cloud/LLM API calls at runtime.** All inference (whisper + Gemma) is local via the machine's own models / Ollama. This is a hard PRD requirement, not an optimization.

### "Running low on credits" fallback (most likely scenario)
Build in an order where you always have something usable if the budget runs out. Priority ladder — stop anywhere and still have value:

1. **Phase 0–1 done → you have a working dictate-and-paste tool.** (Minimum viable, already useful.)
2. **+ Phase 2 → you have safe recoverable dictation with history.** This is the real MVP; if credits die here, you have shipped the product's core promise.
3. **+ Phase 3 → cleaned-up text.** Nice, but raw transcript is fine to live with.
4. **+ Phase 4–5 → optimized engine + battery.** Pure polish.

If credits get tight mid-phase: have Fable write a **`HANDOFF.md`** capturing current state, what's done, the next concrete steps, and any gotchas — so the remaining work can be finished by Haiku alone, by local tooling, or by me by hand.

## 5. Suggested repo layout

```
voice-dictation/
  PRD.md
  WORKPLAN.md
  CLAUDE.md            # project brief + F1–F6 + latency budget (Claude writes first)
  docs/
    PERMISSIONS.md     # macOS Accessibility + mic setup
    MANUAL_CHECKLIST.md
    HANDOFF.md         # created if/when credits run low
  src/                 # app code
  eval/                # transcription + cleanup eval harness (Phase 4)
  tests/
```

## 6. First three actions for Claude Code

1. Read `PRD.md` + `WORKPLAN.md`; write `CLAUDE.md` (brief + F1–F6 + ≤2s budget).
2. **Phase 0 spike** (Fable): global hotkey → record → paste dummy text; document permissions. Run the Phase 0 gate.
3. Only if Phase 0 is green: start Phase 1 (Haiku glue, Fable review), logging latency.
