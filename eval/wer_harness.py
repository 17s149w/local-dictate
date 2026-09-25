"""WER + hallucination harness for Local Dictate's transcription model.

Scores the SHIPPED whisper.cpp model (default base.en - the same ggml file the
app loads via pywhispercpp) against ground-truth transcripts, and probes for
hallucination on non-speech audio (silence/noise), where the correct output is
no fabricated words.

Usage:
  # Standard corpus: stratified subset of LibriSpeech test-clean
  #   (8 short <=4s, 8 medium 4-10s, 8 long >10s; seed 42)
  python3 eval/wer_harness.py --librispeech /path/to/LibriSpeech/test-clean

  # Own-voice corpus: every *.wav in a dir, ground truth in same-named .txt
  #   (record clips, draft-transcribe, correct BY HAND - eval/README.md step 4)
  python3 eval/wer_harness.py --clips eval/clips

  # Hallucination probe: any words emitted on non-speech audio are failures
  python3 eval/wer_harness.py --probes eval/probes
  #   generate probe audio with sox, e.g.:
  #   sox -n -r 16000 -c 1 silence10.wav trim 0.0 10.0
  #   sox -n -r 16000 -c 1 whitenoise5.wav synth 5 whitenoise vol 0.5

Requires: pip install pywhispercpp jiwer  (ffmpeg only for .flac input)
2026-09-24 baseline was produced with: base.en, n_threads=2, seed=42, n=24.
"""

import argparse, json, os, random, re, statistics, subprocess, sys, time
from pywhispercpp.model import Model
import jiwer


def norm(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def transcribe(model, wav):
    t0 = time.perf_counter()
    segs = model.transcribe(wav)
    return " ".join(s.text for s in segs).strip(), time.perf_counter() - t0


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path])
    return float(out.strip())


def to_wav(src, dst):
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-i", src,
                    "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", dst],
                   check=True)


def librispeech_subset(root, workdir, n_per_bucket=8, seed=42):
    random.seed(seed)
    refs = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith(".trans.txt"):
                for line in open(os.path.join(dirpath, f)):
                    uid, _, text = line.partition(" ")
                    refs[uid.strip()] = text.strip()
    flacs = []
    for dirpath, _, files in os.walk(root):
        flacs += [os.path.join(dirpath, f) for f in files if f.endswith(".flac")]
    random.shuffle(flacs)
    picked = {"short": [], "med": [], "long": []}
    items = []
    for p in flacs:
        if all(len(v) >= n_per_bucket for v in picked.values()):
            break
        d = duration(p)
        cat = "short" if d <= 4 else ("med" if d <= 10 else "long")
        if len(picked[cat]) >= n_per_bucket:
            continue
        uid = os.path.basename(p).replace(".flac", "")
        wav = os.path.join(workdir, uid + ".wav")
        to_wav(p, wav)
        picked[cat].append(uid)
        items.append({"uid": uid, "wav": wav, "dur": round(d, 2),
                      "ref": refs[uid], "cat": cat})
    return items


def clips_dir(path):
    items = []
    for f in sorted(os.listdir(path)):
        if not f.endswith(".wav"):
            continue
        ref_path = os.path.join(path, f[:-4] + ".txt")
        if not os.path.exists(ref_path):
            sys.exit(f"missing ground truth: {ref_path}")
        wav = os.path.join(path, f)
        items.append({"uid": f[:-4], "wav": wav, "dur": round(duration(wav), 2),
                      "ref": open(ref_path).read().strip(), "cat": "own"})
    return items


def run_wer(model, items):
    results = []
    for m in items:
        hyp, dt = transcribe(model, m["wav"])
        wer = jiwer.wer(norm(m["ref"]), norm(hyp))
        results.append({**m, "hyp": hyp, "wer": round(wer, 4),
                        "proc_s": round(dt, 2), "rtf": round(dt / m["dur"], 3)})
        print(f"{m['cat']:5s} {m['dur']:5.1f}s wer={wer:.3f}  {hyp[:70]}")
    ref_all = " ".join(norm(r["ref"]) for r in results)
    hyp_all = " ".join(norm(r["hyp"]) for r in results)
    print(f"\nAGGREGATE WER n={len(results)} "
          f"({sum(r['dur'] for r in results):.0f}s audio): "
          f"{jiwer.wer(ref_all, hyp_all):.4f}")
    for cat in sorted({r["cat"] for r in results}):
        rs = [r for r in results if r["cat"] == cat]
        print(f"  {cat}: {jiwer.wer(' '.join(norm(r['ref']) for r in rs), ' '.join(norm(r['hyp']) for r in rs)):.4f} (n={len(rs)})")
    print(f"RTF median {statistics.median(r['rtf'] for r in results):.2f} "
          f"(lower is faster; hardware-dependent)")
    return results


def run_probes(model, path):
    failures = 0
    for f in sorted(os.listdir(path)):
        if not f.endswith(".wav"):
            continue
        hyp, _ = transcribe(model, os.path.join(path, f))
        words = len(hyp.split()) if hyp else 0
        # [BLANK_AUDIO] and parenthesized sound descriptors are meta tokens,
        # not fabricated speech; anything else on non-speech audio is a
        # hallucination failure.
        fabricated = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", hyp).strip()
        status = "FAIL" if fabricated else "ok"
        if fabricated:
            failures += 1
        print(f"[{status}] {f}: words={words} out={hyp[:100]!r}")
    n = len([f for f in os.listdir(path) if f.endswith(".wav")])
    print(f"\n{n - failures}/{n} probes clean, {failures} hallucination failures")
    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--librispeech")
    ap.add_argument("--clips")
    ap.add_argument("--probes")
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--workdir", default="/tmp/wer-harness")
    ap.add_argument("--out", default="wer_results.json")
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    model = Model(args.model, n_threads=2)
    if args.librispeech:
        items = librispeech_subset(args.librispeech, args.workdir)
        json.dump(run_wer(model, items), open(args.out, "w"), indent=1)
    elif args.clips:
        json.dump(run_wer(model, clips_dir(args.clips)), open(args.out, "w"), indent=1)
    elif args.probes:
        sys.exit(1 if run_probes(model, args.probes) else 0)
    else:
        ap.error("one of --librispeech, --clips, --probes is required")


if __name__ == "__main__":
    main()
