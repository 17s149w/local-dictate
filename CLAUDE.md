# CLAUDE.md — project brief for Claude Code

Read `PRD.md` and `WORKPLAN.md` in full before writing any code. This file is the short version every model working on the repo must keep in mind.

**If you were spawned as a subagent, you are a leaf: do the work yourself and never delegate.** The rest of this file — the read-everything instruction above, the token budget, the model-assignment table — is written for the orchestrating session, not for you. A worker reads only this file plus the files named in its own task, and hands back finished, tested work.

## Two prime directives (read before every decision)

**1. Spend as few tokens as possible by being right the first time.** Concise code that works on the first pass beats clever code that needs three revisions. Use the cheapest model that can one-pass the task (Fable→Sonnet→Haiku, see below). Checkpoint with git so a wrong turn is a `reset`, not a costly redo. Every avoidable rewrite is wasted budget — measure twice, cut once.

**2. Optimize for a product I'll actually use all day.** "Done" means it survives real use, not just a demo. The concrete usability bar is the failure modes: it's fast (F1 tiered latency), never loses text (F2 durability), never mangles meaning (F3), never clobbers my clipboard, and degrades gracefully when a paste is blocked. Before calling anything finished, ask: "would this annoy me on the 30th dictation of the day?" If yes, it's not done.

These two pull in tension sometimes (usability work costs tokens). Resolve it by shipping the smallest thing that's genuinely usable, in the WORKPLAN's risk-first order, and stopping there if credits run low.

## What we're building
A **local, macOS, personal** push-to-talk voice dictation tool, built as an offline alternative to cloud dictation tools like Whispr Flow. Hold (or double-tap to latch) a hotkey → speak → local Whisper transcribes → local Gemma (via Ollama) cleans up → text pastes at the cursor. A menu-bar icon shows recording state and keeps a recoverable history. **Everything runs locally. Zero cloud/LLM calls at runtime.**

## The numbers that define success
- **Short clip (≤3s speech): ≤ 1.0s** end-to-end (key-release → pasted). This is the make-or-break metric.
- **Long clip (paragraph): ≤ ~2.5s.** Hard ceiling 4s.
- Transcription **< 8% WER** on clear speech.
- **Zero lost text, ever.** Any dictation is recoverable from the menu-bar history in ≤2 clicks or one re-paste hotkey.
- Negligible idle CPU/battery.

## Failure modes to design against (do not regress these)
- **F1 Latency** — measure release→paste every phase; a regression past budget is a stop-and-fix.
- **F2 Lost text** — clipboard+history is the source of truth; paste is best-effort on top; a durable job state machine (`RECORDED→TRANSCRIBED→CLEANED→INJECTED`) protects the whole pipeline. For every stage there must be a test proving the artifact is persisted before the next stage runs.
- **F3 Cleanup mangles meaning** — the cleanup LLM fixes punctuation/casing/filler only; it must never change meaning, add facts, translate, or "answer" the text. Keep raw transcript stored. Diff-eval before enabling by default.
- **F4 Permissions/hotkey** — prove OS integration (Accessibility + mic + global hotkey + ⌘V) in Phase 0 before anything else.
- **F5 Battery** — load models on demand, idle-unload, don't hold the mic open when idle.
- **F6 Scope creep** — MVP is push-to-talk + inject + recoverable history. Nothing else until that's solid.

## Build order (see WORKPLAN for gates)
Phase 0 OS-integration spike → 1 end-to-end skeleton → 2 menu-bar UI + durability → 3 cleanup pass → 4 model eval → 5 battery/polish. **Do not start a phase until the previous phase's test gate is green.** Do not mark a *manual* gate green without Sylvan's confirmation.

## Who does what (conserve credits)
*This table assigns work to the orchestrating session. It is not an instruction to spawn subagents — if you are already a subagent, ignore it and just do the task.*
- **Fable (`claude-fable-5`): orchestrate, architect, review.** Phase 0 spike, cleanup prompt (F3), durability design (F2), integration, adversarial reviews.
- **Sonnet (`claude-sonnet-5`): default coder** for almost all implementation. Bulk of build tokens go here.
- **Haiku (`claude-haiku-4-5`): mechanical only** (formatting, docstrings, filling a function whose signature+test exist). Never multi-file features or F1–F6-adjacent work.
- Pick the cheapest model that can **one-pass it correctly** — rework costs more than the sticker-price saving.
- If credits run low, write `docs/HANDOFF.md` (state, what's done, next concrete steps, gotchas) so the rest can be finished cheaply or by hand.

## Ollama (runtime cleanup model)
Local REST API on `http://localhost:11434` (`/api/generate`, `/api/chat`), no auth, localhost-only. Nothing to "open." Requires Ollama running + `ollama pull gemma4:e2b` (on-device-optimized cleanup model; default). Health-check on startup; fail open to raw transcript if down or slow.

## Coding conventions — WRITE THE LEAST CODE THAT WORKS
Being terse here directly conserves tokens and keeps the app fast and debuggable.
- **Simplest thing first.** No frameworks, abstractions, or design patterns until a concrete need forces them. Prefer the standard library.
- **Small, single-purpose functions.** If it needs a paragraph to explain, split it.
- **No speculative generality.** Don't build config/plugins/hooks for features we don't have yet (F6).
- **Flat over clever.** Readable, boring code over clever one-liners.
- **Comment the *why*, not the *what*.** Code shows what; comments explain non-obvious decisions only.
- **Every new behavior ships with a test** (or a line in `docs/MANUAL_CHECKLIST.md` if it's OS integration that can't be unit-tested).
- **Fail open, log clearly.** Any slow/failed stage falls back to a safe state and logs enough to debug.
- **One file until it hurts.** Don't pre-split into many modules; split when a file is genuinely doing two jobs.
- When unsure between two approaches, pick the one with fewer lines and fewer dependencies, and note the tradeoff in a comment.

## Layout
`src/` app · `eval/` model benchmarks (Phase 4) · `tests/` · `docs/` (PERMISSIONS.md, MANUAL_CHECKLIST.md, HANDOFF.md if needed).

## First three actions
1. Read PRD + WORKPLAN; confirm this CLAUDE.md still matches (update if the docs changed).
2. Phase 0 spike (Fable): global hotkey → record → paste dummy text; write `docs/PERMISSIONS.md`. Run the Phase 0 gate.
3. Only if Phase 0 is green: start Phase 1 (Sonnet implements, Fable reviews), logging short- and long-clip latency.
