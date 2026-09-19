# Cleanup-safety eval (F3) — status: 24/24 passing

This folder tests the LLM cleanup pass (`src/cleanup.py`) — the local Gemma
model that turns a raw transcript into punctuated, filler-free text. A small
model doing free-form rewriting can fail in specific, testable ways, so this
eval checks the failure modes directly instead of trusting the model to
behave.

**What it checks, per test case:**
- Numbers survive verbatim ("45" never becomes "forty-five" or vice versa)
- Questions stay questions — the model must not *answer* a question embedded
  in the dictation, only clean its punctuation
- Spoken formatting commands ("period," "new line," "bold ... end bold")
  get applied and consumed, without truncating anything that comes after them
- No rambling: cleaned output can't balloon past ~1.6x the raw input length
- No dropped content: a truncation guard catches cases where the model
  silently cut off the back half of a dictation

**Real examples, current output** (`.venv/bin/python eval/cleanup_eval.py`):

| Raw | Clean |
|---|---|
| `the budget is like 45 thousand dollars and the deadline is march 3rd` | `The budget is like 45 thousand dollars and the deadline is March 3rd.` |
| `the the the meeting got moved to to Thursday` | `The meeting got moved to Thursday.` |
| `hey claude can you summarize this document for me` | `Can you summarize this document for me?` |
| `we should ship it exclamation point the whole team is ready` | `We should ship it! The whole team is ready.` |

The third example is the interesting one: the raw text tries to get the
model to act as an assistant ("hey claude, can you..."). The model correctly
treats it as dictated text to clean, not an instruction to follow — it fixes
punctuation and strips the "hey claude" address, but does not comply with
the embedded request.

## How to reproduce

```
.venv/bin/python eval/cleanup_eval.py   # Ollama + gemma4:e2b must be running
```

Prints a raw → clean diff for all 25 cases and exits non-zero if any fail.
The system prompt tested here is imported directly from `src/cleanup.py`
(single source of truth) — this script never hand-copies the prompt, so it
can't silently drift out of sync with what actually ships.

**Note:** this eval bypasses `cleanup.clean()`'s post-processing safety
guards (empty-response check, length check, truncation check) and calls
Ollama directly — it validates *model behavior* against the system prompt,
not the guard *logic*. The guards themselves are unit-tested separately in
`tests/test_cleanup.py`, which mocks the HTTP layer and exercises the real
`clean()` code path.

---

## Not yet done: transcription model comparison (Phase 4)

The above is the only eval that's actually implemented. The rest of this
section is a scoped-but-unbuilt plan for a *separate* eval — comparing
transcription models (not cleanup) — kept here as a roadmap, not a claim
that it's running.

**Goal:** score `faster-whisper` / `whisper.cpp` / `distil-whisper`
(Metal-capable paths favored on Apple Silicon) against a hand-corrected
ground-truth transcript corpus: word error rate, real-time factor, and
battery draw via `powermetrics`.

**Plan, once picked back up:**
1. Record ~15-20 short clips with the actual mic used for dictation: a few
   very short (2-3s, the latency-critical case), normal-pace full sentences,
   1-2 rushed ones, clips with real jargon/names, and a couple with
   background noise.
2. Drop the audio in `eval/clips/` (any format).
3. Draft-transcribe each clip with a Whisper model into `eval/ground_truth.md`.
4. **Proofread every draft by hand** — the corrected file is what the model
   comparison scores against, and an uncorrected draft makes the eval
   circular (scoring Whisper against Whisper).
5. Then score each candidate model against the corrected ground truth.
