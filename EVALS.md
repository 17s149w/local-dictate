# Evals - Local Dictate

One row per config change. **One change at a time, recorded.** Every number in
the table comes from a measured run with its script and corpus named - never an
estimate. If a cell can't be measured yet, it says so.

## Results

| Date | Config change | WER (LibriSpeech) | WER (own clips) | Hallucination probes | Latency short (<=3s) med / p90 | Latency long (>3s) med / p90 | Cleanup cases | Cost / dictation | Human-review queue |
|---|---|---|---|---|---|---|---|---|---|
| 2026-09-24 | baseline: `whisper.cpp` `base.en` + `gemma4:e2b` cleanup | **6.7%** (n=24 clips, 230s) | not yet recorded | **0 fabricated / 5** | **184ms / 1.04s** (n=71) | **1.75s / 3.34s** (n=356) | 24/24 passing | $0 | none outstanding |

PRD targets for reference: WER < 8% on clear speech; short clips <= 1.0s
key-release-to-pasted; long clips <= 2.5s, hard ceiling 4s.

## How each number was measured (2026-09-24 baseline)

- **WER (LibriSpeech):** `eval/wer_harness.py --librispeech` against a
  stratified subset of LibriSpeech test-clean (8 short / 8 medium / 8 long,
  seed 42), run on the exact shipped model file (`ggml-base.en.bin` via
  pywhispercpp/whisper.cpp), CPU-only sandbox. Meets the <8% target. Errors
  concentrate on proper nouns - the known tradeoff from README decision 3.
  Caveat: audiobook narration is cleaner than real dictation; this is a floor
  on accuracy, not a measurement of daily use.
- **WER (own clips):** pending the corpus in `eval/README.md` (~15-20 clips on
  the real mic, hand-corrected transcripts). Run with
  `eval/wer_harness.py --clips eval/clips` once the clips exist. This is the
  number that matches daily use (mic, voice, jargon).
- **Hallucination probes:** `eval/wer_harness.py --probes` on 5 non-speech
  clips (silence 3s/10s, white noise 5s, pink noise 10s, brown-noise "cafe"
  15s). The model emitted only meta tokens (`[BLANK_AUDIO]`, `(wind blowing)`,
  `(engine revving)`) - zero fabricated sentences. Watch item: those
  parenthesized descriptors flow into the cleanup pass, so the cleanup eval
  should gain a case proving they get stripped rather than polished into text.
- **Latency:** `scripts/latency_stats.py` over `logs/latency.log` (n=427
  production samples, Apple Silicon, warm models). Short clips: 8/71 over the
  1.0s target. Long clips: 92/356 over the 2.5s target and **15/356 over the
  4.0s hard ceiling** (max 6.2s). The README's "inside ceiling" claim covers
  the median case; the ceiling breaches in the tail are the open latency
  problem.
- **Cleanup cases:** `eval/cleanup_eval.py` (needs Ollama + `gemma4:e2b`
  running): `.venv/bin/python eval/cleanup_eval.py`. Repo-reported 24/24 at PR
  time; rerun after any prompt or model change.
- **Cost:** $0 marginal per dictation - all inference is local. (Electricity
  is real but sub-cent per day at this duty cycle; not separately metered.)
- **Human-review queue:** the count of guard fallbacks (cleanup rejected, raw
  transcript pasted) is not currently instrumented anywhere readable -
  `logs/app.log` is gitignored by design. Add a counter before tuning the
  guard thresholds. Until then, the raw-to-clean diff printed by
  `eval/cleanup_eval.py` is the manual review artifact.

## Rules for future rows

1. One config change per row; fill every cell from a fresh run of its script.
2. Latency always comes from the production log, never from the eval machine.
3. Accuracy always names corpus + model + date.
4. A regression in any column is recorded, not silently re-baselined.
