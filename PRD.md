# PRD — Local Voice Dictation ("Whispr Flow" clone)

**Owner:** Sylvan · **Status:** Draft v1 · **Date:** 2026-07-19
**One-liner:** A push-to-talk voice dictation tool that runs entirely on my Mac, transcribes speech locally, cleans it up with a local LLM, and pastes the result at my cursor — with a menu-bar history so no text is ever lost.

---

## 1. Why / Problem

Whispr Flow is great but it's cloud-based and subscription-gated. I want the same core workflow — hold a key, talk, get clean text where my cursor is — running **100% locally** on my Mac, for personal use, at zero marginal cost per use.

This is also a deliberate practice project for orchestrating a build with Claude Code (Fable orchestrating, cheaper models doing the grunt work). See `WORKPLAN.md`.

## 2. Goals

- Push-to-talk dictation that feels **faster than typing** for a paragraph of text.
- Fully local: no audio or text ever leaves the machine at runtime.
- **Never lose text.** If the paste fails (cursor not focused), the text is always recoverable from a small menu-bar history without opening a full app.
- Low idle cost: negligible CPU/battery when not dictating.

## 3. Non-Goals (v1)

- Cross-platform (macOS only).
- Live on-screen preview of text as I speak (word-by-word). Note: overlapping record+transcribe internally for latency (§7.2) is allowed and encouraged — what's out of scope is *showing* a live partial transcript in the UI.
- Multi-user, accounts, sync, or any cloud component.
- Custom vocabulary, formatting modes, or per-app behavior (later, maybe).
- **Context-aware cleanup (deferred to v2).** Feeding surrounding context into the cleanup model to improve it is tempting but directly fights F3 — context tempts the model to answer/continue/rewrite rather than just clean. When pursued: start with the *narrow* version only (prior dictations or a proper-noun glossary as a spelling/terminology hint), keep the F3 constraint absolute, and add eval cases proving context never leaks into content (e.g. a transcript beside a question must still not answer it). Target-app/screen scraping is deferred indefinitely — it breaks the local/private pitch and adds latency + permissions.

## 4. Definition of Success

The build is a success if, in daily personal use:

1. **Latency (tiered by clip length):** short utterances must feel instant; long ones can take longer.
   - **Short (≤ ~3s of speech): ≤ 1.0s** end-to-end (key-release → text pasted). This is the make-or-break number — most dictations are short.
   - **Long (paragraph-length): ≤ ~2.5s**, scaling roughly with audio length. Hard ceiling 4s.
   - Cleanup runs concurrently where possible and must not push short clips over the 1s budget (fail-open on timeout, §7.3).
2. **Accuracy:** Transcription is good enough that I rarely have to hand-fix words. Measured as Word Error Rate (WER) on my own sample clips — target **< 8% WER** on clear speech.
3. **Recoverability:** In every case where the paste doesn't land, I can retrieve the exact text from the menu-bar icon in **≤ 2 clicks** (or one re-paste hotkey). Zero silent data loss.
4. **Cleanup helps, doesn't harm:** The LLM cleanup pass fixes filler/punctuation/casing **without changing my meaning or inventing words**.
5. **Idle footprint:** Negligible battery/CPU when I'm not actively dictating.

## 5. Definition of Failure ("here's what would make this fail")

Treat these as the top risks the build must actively design against:

| # | Failure mode | Why it kills the project | Mitigation |
|---|--------------|--------------------------|------------|
| F1 | **Latency too high** — feels slower than typing | The whole point is speed; if it's laggy I won't use it | Model-size eval (§7); keep model warm/loaded; measure every phase |
| F2 | **Lost text** — paste fails and text is gone | One data loss and I lose trust in it | Clipboard + persistent history is the source of truth, paste is best-effort on top |
| F3 | **Cleanup mangles meaning** — LLM rewrites/hallucinates | Silent corruption is worse than raw transcript | Constrained prompt; keep raw transcript in history; eval before/after diffs |
| F4 | **Permission / hotkey hell** — macOS Accessibility & mic perms, hotkey conflicts | Common blocker for mac dictation tools; can stall the build | Prove the OS-integration spike FIRST (see WORKPLAN Phase 0) |
| F5 | **Battery drain** — models pinned in memory or mic always hot | Laptop tool has to be background-cheap | Load-on-demand or idle-unload; measure with `powermetrics` |
| F6 | **Scope creep** — chasing full Whispr parity before core loop works | Runs out of build budget with nothing usable | MVP is push-to-talk + inject + history; everything else is later |

## 6. Users & Core Use Case

Single user (me). Primary flow, ~dozens of times/day:

1. Cursor is in some text field (Slack, editor, browser, notes).
2. I **hold** the push-to-talk hotkey and speak.
3. I **release** the key.
4. Within ~2s, cleaned text appears at my cursor.
5. If it didn't paste (I forgot to click into a field), I click the **menu-bar icon** and grab the last transcript, or hit the **re-paste hotkey**.

## 7. Functional Requirements

### 7.1 Capture
- Two hotkey modes, both configurable:
  - **Push-to-talk (hold):** record while the key is held; stop on release. Default for quick dictations.
  - **Latch (double-tap):** double-tap the key to start recording hands-free; single tap (or the same key) to stop. For longer dictations where I don't want to hold the key.
- **Visual recording indicator (required):** an unmistakable "you are being recorded" cue so I never speak into the void and waste time. Menu-bar icon changes state (idle → recording → transcribing). Strongly preferred: a small always-visible indicator (e.g. a colored dot / floating pill near the menu bar) that's obvious at a glance, plus optional subtle start/stop sound. If a floating overlay is too costly for v1, a clearly-colored menu-bar icon is the minimum.
- Records mic audio to a buffer and **persists it immediately on stop** (see §7.7 Durability) before any processing.

### 7.2 Transcription (local)
- Runs a **local speech-to-text model** on the captured audio. Model choice decided by the eval spike (§8) — candidates: `faster-whisper`, `whisper.cpp`, `distil-whisper`.
- Model stays warm between uses to hit the latency budget; unloads after an idle timeout to save battery.
- **Warm on key-down.** Trigger loading of the transcription model (and the cleanup model, §7.3) the moment recording *starts*, not on release — the load overlaps with the time spent speaking + transcribing, so it's nearly free. First dictation after an idle-unload pays a cold start; the eval (§8) quantifies it, and the fallback is warming on app-focus or a light keep-alive ping during active sessions.

**Overlap record + transcribe to cut latency (the main latency lever).** Rather than waiting for key-release to *start* transcribing, feed audio to the model **in chunks while recording is still happening** (chunked/streaming transcription). By the time I release the key, most of the audio is already transcribed, so the remaining work is just the final chunk — this is what makes long dictations feel fast (helps the ≤2.5s long-clip budget most; short clips are already near the model's floor). What *can't* be parallelized: cleanup needs the full transcript, and injection needs the cleaned text, so those stay sequential (`record‖transcribe → cleanup → inject`). Durability is unaffected: persist audio chunks as they arrive, so a crash mid-stream still leaves recoverable audio. This is an **optimization layered on the Phase 1 sequential pipeline** — build sequential first (simpler, provably correct), then add chunked transcription once the numbers say it's needed.

### 7.3 Cleanup (local LLM)
- Passes the raw transcript through a **small on-device model via Ollama** (default: `gemma4:e2b`, optimized for on-device use) to fix punctuation, capitalization, and remove filler ("um", "uh", false starts).
- **Constraint:** cleanup must not add information, change meaning, translate, or answer the text as if it were a prompt. Prompt is locked down and tested (F3).
- Cleanup is skippable via config (raw-transcript mode) and must **fail open**: if the LLM errors or is slow past a timeout, fall back to the raw transcript rather than blocking the paste.

### 7.4 Injection / Paste
- Default mechanism: place cleaned text on the **clipboard**, then synthesize **⌘V** into the focused app (via macOS Accessibility / key events).
- **Clipboard save/restore (required in v1).** Before writing the transcript to the clipboard, save the user's existing clipboard contents; after the paste, restore them. Dictating must never silently destroy what the user had copied. Restore on a short delay so the paste completes first.
- **Blocked-paste detection & fallback.** Some apps (password/secure fields, certain sandboxed/Electron apps) reject synthetic ⌘V. The app should not assume paste succeeded — on any failure it leaves the text in the durable history + on the clipboard and surfaces the re-paste path. (Do not treat "clipboard set" as "paste landed.")
- Because the text is on the clipboard first, a failed paste still leaves it pasteable manually. This is intentional and is the primary defense for F2.

### 7.5 Menu-bar UI + History (required for v1)
- Small **menu-bar icon** (top-right), not a full window.
- Clicking it shows a short **history list** of recent dictations. **Default view shows the cleaned text only.** The raw transcript is surfaced **only when cleanup didn't run** (LLM failed/timed out/disabled) — in that case the entry shows the raw text and is flagged as uncleaned. (Raw is still stored for every entry for durability/debugging, just not shown by default.)
- Each entry: **copy to clipboard** action. Most recent is one click away.
- **Re-paste hotkey**: re-injects the last dictation at the current cursor (mirrors Whispr's re-paste key) — covers the "I forgot to focus a field" case.
- Icon reflects state: idle / recording / transcribing.
- History persists across restarts (small local store, e.g. SQLite or JSON). Configurable cap (e.g. last 50).

### 7.6 Settings (minimal)
- Change hotkeys, pick transcription model + size, toggle cleanup, set history size, set idle-unload timeout. A simple config file is acceptable for v1 (no fancy UI required).

### 7.7 Durability & Retry (required)
The pipeline is a small, persisted **state machine** so that nothing is lost if a stage fails or the app crashes. A dictation is a **job** that moves through stages, each persisted as it completes:

`RECORDED → TRANSCRIBED → CLEANED → INJECTED (done)`

- **Save first, process second.** On record-stop, the audio is written to disk and a job row is created *before* transcription starts. So if transcription crashes, the audio is still there to re-run.
- **Per-stage persistence.** Each stage writes its output (transcript, cleaned text) and advances the job status. Nothing depends on in-memory state surviving.
- **Retry with backoff.** Transcription and cleanup failures retry automatically a few times (short backoff). Cleanup additionally **fails open** to the raw transcript so injection isn't blocked (§7.3).
- **Manual retry.** Any job stuck in an error state appears in the menu-bar history with a **Retry** action (re-transcribe / re-clean / re-inject as appropriate).
- **Crash recovery.** On startup, the app scans for incomplete jobs (e.g. `RECORDED` but not `INJECTED`) and offers to resume them — so a crash mid-transcription doesn't lose the recording.
- **Audio retention:** keep the audio for a job until it reaches a terminal state (done or explicitly discarded); prune old audio on a rolling basis to control disk use.

This is the deeper answer to "never lose text" (F2): the clipboard/history protects the *paste* step; the job state machine protects the *whole pipeline*.

## 8. Model Selection Spike (separate from app build)

Before committing the transcription engine, run a standalone **eval** (not app-integrated) comparing the three candidates so the choice is data-driven, not vibes:

- **Candidates:** `faster-whisper`, `whisper.cpp`, `distil-whisper` — each at a couple of model sizes (e.g. tiny/base/small equivalents).
- **Apple Silicon note:** target machine is Apple Silicon. `whisper.cpp` has **Metal/GPU acceleration** and is a strong default hypothesis here; `faster-whisper`'s CTranslate2 backend runs CPU-only on Mac (no Metal), so it may be slower/hotter on this hardware. `distil-whisper` (via a Metal-capable runtime) trades a little accuracy for speed and is the likely winner for the sub-1s short-clip budget. Let the numbers decide, but weight the eval toward Metal-capable paths.
- **Corpus:** ~15–20 of my own recorded clips covering realistic conditions: normal speech, fast speech, technical jargon/names, and some background noise. **Ground truth via pre-annotation:** Claude draft-transcribes each clip, Sylvan then corrects every draft by hand — the corrected file is the ground truth. (Proofread carefully; uncorrected model errors bias the comparison.) See `eval/README.md`.
- **Metrics per (engine × size):**
  - **Accuracy:** WER vs my ground-truth transcripts.
  - **Speed:** real-time factor (audio seconds ÷ processing seconds) and absolute latency for a 10s clip.
  - **Battery/thermal:** CPU-seconds and energy via `powermetrics` / `time` over a fixed batch.
  - **Memory:** peak RAM footprint per engine × size (so the chosen model fits in real RAM with margin — avoids swap-induced latency spikes that would blow F1). macOS won't OOM-crash on these small models; it compresses/swaps, which just gets slow — so pick sizes that fit comfortably rather than relying on a runtime memory guard.
- **Output:** a small results table + a recommendation. I make the final call on the accuracy/speed/battery tradeoff.

The cleanup model (Gemma size) gets a lighter version of the same treatment: does it improve readability without violating the F3 constraint?

## 9. Tech Stack (proposed — Fable to confirm in Phase 0)

- **Target hardware:** Apple Silicon Mac. Prefer Metal-capable model runtimes (favors `whisper.cpp`); factor unified memory into the keep-warm vs idle-unload decision (F5).
- **Language/runtime:** Python (fastest path to a local menu-bar MVP; easy to swap models). Native Swift is lower-latency/better battery but far more code — out of scope for v1, note as a possible v2 rewrite if latency (F1) can't be met.
- **Menu bar:** `rumps` (lightweight macOS menu-bar apps).
- **Hotkey + key synthesis:** `pynput` (global hotkey capture + ⌘V injection).
- **Audio capture:** `sounddevice` / `pyaudio`.
- **Transcription:** TBD by §8 eval (`whisper.cpp` w/ Metal is the default hypothesis on Apple Silicon).
- **Cleanup LLM:** local **Ollama**, default `gemma4:e2b` (on-device-optimized) — already installed. No API to "open up": when Ollama is running it serves a local REST API on `http://localhost:11434` (endpoints `/api/generate` and `/api/chat`), no auth, localhost-only. The app just POSTs to it. Requirements: Ollama running (`ollama serve`, which the desktop app does automatically) and the model pulled (`ollama pull gemma4:e2b`). App should health-check the endpoint on startup and fail open to raw transcript if it's down.
- **History store:** SQLite (or JSON for v1 simplicity).
- **Packaging:** run from a local venv for v1; `.app` bundling is a nice-to-have, not required.

**Runtime is 100% local — no Claude / Fable / cloud calls at runtime.** Cloud models are only used to *build* the app (see WORKPLAN).

## 10. Open Questions

- Which single hotkey feels best for push-to-talk without conflicting with system/app shortcuts? (Decide during Phase 0 testing.)
- Is JSON good enough for history or do we want SQLite from the start? (Lean JSON unless it gets messy.)
- Does keeping the whisper model warm blow the battery budget (F5)? Eval informs the idle-unload timeout.
