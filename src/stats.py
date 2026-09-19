"""Lifetime dictation metrics accumulator.

Stores running totals in stats.json (same directory as history.json).
Writes are atomic (same tempfile+os.replace pattern as store.py).

Public API:
  record(words, audio_secs)  — call once per completed dictation
  summary()                  -> dict of derived metrics
  menu_lines()               -> list[str] for menu-bar display
  backfill_from_history()    — seed from existing history.json; idempotent
"""

import json
import os
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Typing speed baseline for "time saved" calculation.
_TYPING_WPM = 50

# Overrideable by tests (same pattern as store.STATS_PATH).
STATS_PATH = Path("stats.json")


# ---------------------------------------------------------------------------
# Injectable clock — monkeypatch _now in tests for determinism
# ---------------------------------------------------------------------------

def _now() -> datetime:
    """Return current UTC time. Monkeypatch this in tests."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    """Read stats.json; return accumulator dict. Zeros if missing/corrupt."""
    p = Path(STATS_PATH)
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text())
        # Validate expected keys; corrupt/partial files reset to zeros.
        if not isinstance(data, dict) or "total_words" not in data:
            return _empty()
        return data
    except (json.JSONDecodeError, OSError):
        return _empty()


def _empty() -> dict:
    return {
        "total_words": 0,
        "total_dictations": 0,
        "total_audio_secs": 0.0,
        "backfill_done": False,
        # "first_use_ts" is absent until first record() or backfill.
    }


def _save(data: dict) -> None:
    """Atomically write data to STATS_PATH via tempfile + os.replace."""
    p = Path(STATS_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_job_ts(job_id: str) -> datetime | None:
    """Parse a job id of the form 20260722T173701123456Z into a UTC datetime.

    Returns None if the id doesn't match that pattern.
    """
    try:
        # IDs are YYYYMMDDTHHMMSSxxxxxxZ; trim microseconds and Z, then parse.
        if len(job_id) < 16 or not job_id.endswith("Z"):
            return None
        # Take first 15 chars: YYYYMMDDTHHMMSS
        return datetime.strptime(job_id[:15], "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record(words: int, audio_secs: float) -> None:
    """Accumulate one completed dictation. Crash-safe: never raises."""
    try:
        data = _load()
        data["total_words"] += max(0, int(words))
        data["total_dictations"] += 1
        data["total_audio_secs"] += max(0.0, float(audio_secs))
        # Set first-use timestamp on the very first real record; never overwrite.
        if "first_use_ts" not in data:
            data["first_use_ts"] = _now().isoformat()
        _save(data)
    except Exception as e:
        # Fail open — dictation must succeed even if stats are broken.
        print(f"WARNING: stats.record() failed (stats not updated): {e}")


def summary() -> dict:
    """Return derived metrics as a plain dict."""
    data = _load()
    words = data["total_words"]
    dictations = data["total_dictations"]
    audio_secs = data["total_audio_secs"]
    audio_mins = audio_secs / 60.0

    # Speaking WPM: words per minute of recorded audio (not wall-clock time).
    speaking_wpm = (words / audio_mins) if audio_mins > 0 else 0.0

    # Time saved vs typing at _TYPING_WPM: typing time minus speaking time.
    typing_mins = words / _TYPING_WPM if _TYPING_WPM > 0 else 0.0
    time_saved_mins = typing_mins - audio_mins  # can be negative for slow speakers

    # Days since first use — reported on its own, and used for nothing else.
    first_use_ts = data.get("first_use_ts")
    if first_use_ts:
        try:
            first_use = datetime.fromisoformat(first_use_ts)
            # Make offset-aware if needed.
            if first_use.tzinfo is None:
                first_use = first_use.replace(tzinfo=timezone.utc)
            elapsed_days = max(0.0, (_now() - first_use).total_seconds() / 86400)
        except (ValueError, TypeError):
            elapsed_days = 0.0
    else:
        elapsed_days = 0.0

    return {
        "total_words": words,
        "total_dictations": dictations,
        "total_audio_secs": audio_secs,
        "speaking_wpm": round(speaking_wpm, 1),
        "time_saved_mins": round(time_saved_mins, 2),
        "days_elapsed": round(elapsed_days, 2),
    }


def menu_lines() -> list[str]:
    """Return preformatted strings for the menu-bar stats section."""
    s = summary()

    # Format time_saved_mins humanely.
    tsm = s["time_saved_mins"]
    if tsm <= 0:
        time_str = "0m"
    else:
        h = int(tsm // 60)
        m = int(tsm % 60)
        time_str = f"{h}h {m}m" if h > 0 else f"{m}m"

    wpm = s["speaking_wpm"]

    return [
        f"{s['total_words']:,} words dictated",
        f"{s['total_dictations']:,} dictations",
        f"Avg speaking speed: {wpm:.0f} WPM",
        f"Time saved vs typing: {time_str}",
    ]


# ---------------------------------------------------------------------------
# One-shot backfill from history.json
# ---------------------------------------------------------------------------

def backfill_from_history(history_path: Path | None = None) -> int:
    """Seed accumulator from existing history.json jobs. Idempotent.

    Reads word counts from job['clean'] or job['raw'], and audio duration
    from the job's .wav file via the stdlib wave module. Skips jobs with
    missing wav or no text. Returns number of jobs processed.

    Also seeds first_use_ts from the oldest job id timestamp so the savings
    counter reflects actual use history rather than today.

    Records in stats.json that backfill ran so this can never double-count.
    """
    data = _load()
    if data.get("backfill_done"):
        return 0  # already ran; skip

    if history_path is None:
        # Resolve history.json relative to STATS_PATH's directory.
        history_path = Path(STATS_PATH).parent / "history.json"

    history_path = Path(history_path)
    if not history_path.exists():
        data["backfill_done"] = True
        _save(data)
        return 0

    try:
        jobs = json.loads(history_path.read_text()).get("jobs", [])
    except (json.JSONDecodeError, OSError):
        data["backfill_done"] = True
        _save(data)
        return 0

    # Find the oldest parseable job timestamp to seed first_use_ts.
    oldest_dt: datetime | None = None
    for job in jobs:
        dt = _parse_job_ts(job.get("id", ""))
        if dt is not None:
            if oldest_dt is None or dt < oldest_dt:
                oldest_dt = dt

    if oldest_dt is not None and "first_use_ts" not in data:
        data["first_use_ts"] = oldest_dt.isoformat()

    processed = 0
    for job in jobs:
        text = job.get("clean") or job.get("raw") or ""
        words = len(text.split()) if text.strip() else 0

        wav_path = job.get("wav")
        audio_secs = 0.0
        if wav_path:
            try:
                with wave.open(str(wav_path), "rb") as wf:
                    audio_secs = wf.getnframes() / wf.getframerate()
            except Exception:
                pass  # skip unreadable wav; still count words

        if words == 0 and audio_secs == 0.0:
            continue

        data["total_words"] += words
        data["total_dictations"] += 1
        data["total_audio_secs"] += audio_secs
        processed += 1

    data["backfill_done"] = True
    _save(data)
    return processed
