"""Job store for Phase 2/3 durability (F2).

Pure stdlib — no rumps, whisper, or sounddevice — so it is always importable
and unit-testable in isolation.

Schema (history.json):
  {"jobs": [...]}   newest-first, capped at 50.

Job dict keys:
  id, created, wav, status, raw, clean, error, attempts

Statuses:
  RECORDED → TRANSCRIBED → CLEANED → INJECTED   (happy path with cleanup)
  RECORDED → TRANSCRIBED → INJECTED              (cleanup off or failed)
  ERROR                                          (any unrecoverable failure)

  CLEANED is skipped when cleanup is disabled or Ollama is unavailable (fail
  open).  It is included in INCOMPLETE_STATUSES so crash recovery can surface
  CLEANED jobs in the menu.

Atomic writes: every mutation rewrites the entire file via a sibling tmpfile +
os.replace so a crash mid-write never corrupts existing history.

All ops behind a single threading.Lock.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

# Statuses
RECORDED = "RECORDED"
TRANSCRIBED = "TRANSCRIBED"
CLEANED = "CLEANED"
INJECTED = "INJECTED"
ERROR = "ERROR"

TERMINAL_STATUSES = {INJECTED}
INCOMPLETE_STATUSES = {RECORDED, TRANSCRIBED, CLEANED, ERROR}

MAX_JOBS = 50

# Overrideable by tests (module-level variable, not hardcoded).
STORE_PATH = Path("history.json")

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> list[dict]:
    """Read history.json; return jobs list (newest first). Empty list if missing."""
    p = Path(STORE_PATH)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data.get("jobs", [])
    except json.JSONDecodeError:
        # Rename corrupt file so next save doesn't clobber it; start fresh.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        corrupt = p.with_name(f"history.json.corrupt-{ts}")
        try:
            p.rename(corrupt)
        except OSError:
            pass
        print(f"WARNING: history.json was corrupt — renamed to {corrupt.name}; starting fresh.")
        return []
    except OSError:
        return []


def _save(jobs: list[dict]) -> None:
    """Atomically write jobs to STORE_PATH via tempfile + os.replace."""
    p = Path(STORE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file so os.replace is atomic on the same filesystem.
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"jobs": jobs}, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        # Clean up temp file if write failed; leave existing store intact.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _enforce_cap(jobs: list[dict]) -> list[dict]:
    """Evict oldest INJECTED jobs until at or under MAX_JOBS cap.

    Non-INJECTED jobs are never evicted — losing audio that hasn't been
    successfully injected would violate F2 durability.
    """
    if len(jobs) <= MAX_JOBS:
        return jobs
    # Separate into keepers (non-INJECTED or within cap) vs candidates.
    kept = []
    evictable = []
    for job in jobs:
        if job.get("status") == INJECTED:
            evictable.append(job)
        else:
            kept.append(job)
    # Evict oldest INJECTED first (they appear last in the list = oldest).
    need_to_drop = len(jobs) - MAX_JOBS
    to_drop = evictable[-need_to_drop:] if need_to_drop <= len(evictable) else evictable
    drop_ids = {j["id"] for j in to_drop}
    for job in to_drop:
        wav = job.get("wav")
        if wav:
            try:
                Path(wav).unlink(missing_ok=True)
            except OSError:
                pass
    return [j for j in jobs if j["id"] not in drop_ids]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_job(job_id: str, wav_path: str) -> dict:
    """Create a new RECORDED job and persist it. Returns the new job dict."""
    job = {
        "id": job_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "wav": str(wav_path),
        "status": RECORDED,
        "raw": None,
        "clean": None,
        "error": None,
        "attempts": 0,
    }
    with _lock:
        jobs = _load()
        jobs.insert(0, job)  # newest first
        jobs = _enforce_cap(jobs)
        _save(jobs)
    return job


def update_job(job_id: str, **fields) -> None:
    """Merge fields into the job and persist atomically."""
    with _lock:
        jobs = _load()
        for j in jobs:
            if j["id"] == job_id:
                j.update(fields)
                break
        _save(jobs)


def delete_job(job_id: str) -> None:
    """Remove a job and its wav file (explicit user discard)."""
    with _lock:
        jobs = _load()
        keep = []
        for j in jobs:
            if j["id"] == job_id:
                if j.get("wav"):
                    Path(j["wav"]).unlink(missing_ok=True)
            else:
                keep.append(j)
        _save(keep)


def get_job(job_id: str) -> dict | None:
    """Return a job dict or None if not found."""
    with _lock:
        jobs = _load()
    for j in jobs:
        if j["id"] == job_id:
            return j
    return None


def jobs() -> list[dict]:
    """Return all jobs, newest first."""
    with _lock:
        return _load()


def incomplete_jobs() -> list[dict]:
    """Return jobs whose status is RECORDED, TRANSCRIBED, or ERROR (not INJECTED)."""
    return [j for j in jobs() if j.get("status") in INCOMPLETE_STATUSES]
