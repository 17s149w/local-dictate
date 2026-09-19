"""Tests for src/store.py (Phase 2 durability / F2).

All tests use a tmp_path store to avoid touching real history.json.
Run: .venv/bin/python -m pytest tests/ -q
"""

import json
import subprocess
import sys
import threading
import wave
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import store

FIXTURE_AIFF = Path("/tmp/fixture.aiff")
FIXTURE_WAV = Path("/tmp/fixture.wav")


# ---------------------------------------------------------------------------
# Helper: point store at tmp_path for each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Redirect store.STORE_PATH to a temp file for each test."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")
    # Also reset the lock (it's module-level; a fresh instance avoids cross-test leaks)
    monkeypatch.setattr(store, "_lock", threading.Lock())


# ---------------------------------------------------------------------------
# Fixture WAV (reuses the say/afconvert approach from test_transcribe.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def speech_wav():
    """Generate a short WAV via macOS TTS. Cached for the whole session."""
    text = "hello world this is a dictation test"
    subprocess.run(["say", "-o", str(FIXTURE_AIFF), text], check=True, capture_output=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
         str(FIXTURE_AIFF), str(FIXTURE_WAV)],
        check=True, capture_output=True,
    )
    return FIXTURE_WAV


# ---------------------------------------------------------------------------
# 1. add / get / update / persist roundtrip
# ---------------------------------------------------------------------------

def test_add_get_update_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")
    job = store.add_job("job1", "audio/job1.wav")
    assert job["id"] == "job1"
    assert job["status"] == store.RECORDED

    # Re-read from disk to confirm persistence
    raw = json.loads((tmp_path / "history.json").read_text())
    assert raw["jobs"][0]["id"] == "job1"
    assert raw["jobs"][0]["status"] == store.RECORDED

    store.update_job("job1", status=store.TRANSCRIBED, raw="hello world")

    got = store.get_job("job1")
    assert got["status"] == store.TRANSCRIBED
    assert got["raw"] == "hello world"

    # Re-read from disk again
    raw2 = json.loads((tmp_path / "history.json").read_text())
    assert raw2["jobs"][0]["status"] == store.TRANSCRIBED
    assert raw2["jobs"][0]["raw"] == "hello world"


# ---------------------------------------------------------------------------
# 2. Cap eviction: only INJECTED jobs are evictable; non-INJECTED survive
# ---------------------------------------------------------------------------

def test_cap_evicts_injected_first(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    # oldest_injected: INJECTED — eligible for eviction; wav should be deleted
    oldest_wav = tmp_path / "oldest.wav"
    oldest_wav.write_bytes(b"fake wav")
    store.add_job("oldest_injected", str(oldest_wav))
    store.update_job("oldest_injected", status=store.INJECTED)

    # Fill to MAX_JOBS - 1 more INJECTED jobs (no real wavs needed)
    for i in range(1, store.MAX_JOBS):
        store.add_job(f"job{i}", f"audio/job{i}.wav")
        store.update_job(f"job{i}", status=store.INJECTED)

    assert len(store.jobs()) == 50

    # Add 51st — oldest INJECTED job should be evicted and its wav deleted
    store.add_job("newest", "audio/newest.wav")

    all_ids = [j["id"] for j in store.jobs()]
    assert "oldest_injected" not in all_ids
    assert len(all_ids) == 50
    assert not oldest_wav.exists(), "wav for evicted INJECTED job should be deleted"


def test_cap_keeps_non_injected_over_cap(tmp_path, monkeypatch):
    """A non-INJECTED job past the cap is retained even if count exceeds MAX_JOBS."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    # non_injected_wav: RECORDED — must NOT be evicted
    non_injected_wav = tmp_path / "keep.wav"
    non_injected_wav.write_bytes(b"fake wav")
    store.add_job("keep_recorded", str(non_injected_wav))
    # keep_recorded is RECORDED — stays in store regardless

    # Fill with INJECTED jobs to reach MAX_JOBS
    for i in range(1, store.MAX_JOBS):
        store.add_job(f"inj{i}", f"audio/inj{i}.wav")
        store.update_job(f"inj{i}", status=store.INJECTED)

    assert len(store.jobs()) == 50

    # Add 51st — the RECORDED job must survive; an INJECTED one is evicted instead
    store.add_job("newest51", "audio/newest51.wav")

    all_ids = [j["id"] for j in store.jobs()]
    assert "keep_recorded" in all_ids, "non-INJECTED job must not be evicted"
    assert non_injected_wav.exists(), "wav for non-INJECTED job must not be deleted"
    # newest51 is also present
    assert "newest51" in all_ids


# ---------------------------------------------------------------------------
# 3. Atomicity: temp file used + os.replace
# ---------------------------------------------------------------------------

def test_atomic_write_uses_tempfile_and_replace(tmp_path, monkeypatch):
    """Verify _save writes via a temp sibling and calls os.replace (not direct write).

    We patch store's own reference to os.replace so we can record calls without
    the mock recursively calling itself.
    """
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    replaced_pairs = []
    real_replace = os.replace  # grab the real function before patching

    def mock_replace(src, dst):
        replaced_pairs.append((Path(src), Path(dst)))
        real_replace(src, dst)  # delegate to the real implementation

    # Patch in store's module namespace so store._save sees our mock
    monkeypatch.setattr(store.os, "replace", mock_replace)

    store.add_job("atomictest", "audio/atomictest.wav")

    assert len(replaced_pairs) == 1
    src, dst = replaced_pairs[0]
    # dst must be our store path
    assert dst == tmp_path / "history.json"
    # src must be a sibling temp file in the same directory
    assert src.parent == tmp_path
    assert src != dst


# ---------------------------------------------------------------------------
# 4. State transitions: RECORDED→TRANSCRIBED→INJECTED; ERROR path
# ---------------------------------------------------------------------------

def test_state_transitions(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    store.add_job("t1", "audio/t1.wav")
    assert store.get_job("t1")["status"] == store.RECORDED

    store.update_job("t1", status=store.TRANSCRIBED, raw="hello")
    assert store.get_job("t1")["status"] == store.TRANSCRIBED
    assert store.get_job("t1")["raw"] == "hello"

    store.update_job("t1", status=store.INJECTED)
    assert store.get_job("t1")["status"] == store.INJECTED


def test_error_path(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    store.add_job("e1", "audio/e1.wav")
    store.update_job("e1", status=store.ERROR, error="boom", attempts=3)

    j = store.get_job("e1")
    assert j["status"] == store.ERROR
    assert j["error"] == "boom"
    assert j["attempts"] == 3


# ---------------------------------------------------------------------------
# 5. incomplete_jobs() returns RECORDED/TRANSCRIBED/ERROR but not INJECTED
# ---------------------------------------------------------------------------

def test_incomplete_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    store.add_job("r1", "audio/r1.wav")                             # RECORDED
    store.add_job("t1", "audio/t1.wav")
    store.update_job("t1", status=store.TRANSCRIBED, raw="hi")      # TRANSCRIBED
    store.add_job("e1", "audio/e1.wav")
    store.update_job("e1", status=store.ERROR, error="oops")        # ERROR
    store.add_job("i1", "audio/i1.wav")
    store.update_job("i1", status=store.INJECTED)                   # INJECTED (terminal)

    incomplete = store.incomplete_jobs()
    ids = {j["id"] for j in incomplete}
    assert "r1" in ids
    assert "t1" in ids
    assert "e1" in ids
    assert "i1" not in ids


# ---------------------------------------------------------------------------
# 6. F2 ordering test: raw text persisted to disk before inject is called
# ---------------------------------------------------------------------------

def test_f2_raw_persisted_before_inject(tmp_path, monkeypatch, speech_wav):
    """The F2 invariant: history.json shows TRANSCRIBED before inject() runs.

    add_job now lives in _stop_and_process, so we call it explicitly here to
    simulate the RECORDED entry existing before on_release_worker runs.
    """
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    import app as _app
    monkeypatch.setattr(_app, "_model", MagicMock())
    # Patch AUDIO_DIR so the worker uses tmp_path for its wav reference
    monkeypatch.setattr(_app, "AUDIO_DIR", tmp_path)

    # Fake transcribe returns deterministic text
    monkeypatch.setattr(_app, "transcribe", lambda audio: "hello world")

    status_at_inject_time = []

    def fake_inject(text):
        # Read history from disk at the moment inject is called
        raw = json.loads((tmp_path / "history.json").read_text())
        status_at_inject_time.append(raw["jobs"][0]["status"])

    monkeypatch.setattr(_app, "inject", fake_inject)

    # Build a minimal raw_bytes (0.5s of silence so it passes MIN_CLIP_SECS)
    samples = int(0.5 * 16000)
    raw_bytes = np.zeros(samples, dtype=np.int16).tobytes()
    job_id = "f2test"
    wav_path = tmp_path / f"{job_id}.wav"
    with wave.open(str(wav_path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(raw_bytes)

    # Simulate _stop_and_process: add_job is now called there, before spawning worker.
    store.add_job(job_id, str(wav_path))

    # Run the worker synchronously (not in a thread) so we can assert inline
    _app._busy = True
    _app.on_release_worker(job_id, raw_bytes, time.perf_counter())

    # The inject mock should have captured TRANSCRIBED status
    assert status_at_inject_time, "inject was never called"
    assert status_at_inject_time[0] == store.TRANSCRIBED, (
        f"Expected TRANSCRIBED at inject time, got {status_at_inject_time[0]}"
    )


# ---------------------------------------------------------------------------
# 7. Crash recovery: RECORDED→TRANSCRIBED; wav-missing→ERROR
# ---------------------------------------------------------------------------

def test_recovery_transcribes_recorded_job(tmp_path, monkeypatch, speech_wav):
    """Hand-written RECORDED job → recovery advances it to TRANSCRIBED."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    import app as _app
    monkeypatch.setattr(_app, "transcribe", lambda audio: "recovered text")

    job_id = "recovery1"
    # Write a real WAV so the recovery path can open it
    import shutil
    recovery_wav = tmp_path / f"{job_id}.wav"
    shutil.copy(speech_wav, recovery_wav)

    # Manually write a RECORDED job into history.json
    (tmp_path / "history.json").write_text(json.dumps({
        "jobs": [{
            "id": job_id,
            "created": "2025-01-01T00:00:00+00:00",
            "wav": str(recovery_wav),
            "status": store.RECORDED,
            "raw": None,
            "clean": None,
            "error": None,
            "attempts": 0,
        }]
    }))

    # Run recovery synchronously
    _app._recover_job(store.get_job(job_id))

    result = store.get_job(job_id)
    assert result["status"] == store.TRANSCRIBED
    assert result["raw"] == "recovered text"


def test_recovery_wav_missing_sets_error(tmp_path, monkeypatch):
    """RECORDED job with missing wav → ERROR 'audio file missing'."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    import app as _app

    job_id = "missing_audio"
    (tmp_path / "history.json").write_text(json.dumps({
        "jobs": [{
            "id": job_id,
            "created": "2025-01-01T00:00:00+00:00",
            "wav": str(tmp_path / "nonexistent.wav"),
            "status": store.RECORDED,
            "raw": None,
            "clean": None,
            "error": None,
            "attempts": 0,
        }]
    }))

    _app._recover_job(store.get_job(job_id))

    result = store.get_job(job_id)
    assert result["status"] == store.ERROR
    assert "missing" in result["error"]


import time
import os


# ---------------------------------------------------------------------------
# New tests for review findings
# ---------------------------------------------------------------------------

def test_corrupt_store_renamed_on_load(tmp_path, monkeypatch):
    """Garbage history.json gets renamed to .corrupt-* and store starts fresh."""
    store_path = tmp_path / "history.json"
    monkeypatch.setattr(store, "STORE_PATH", store_path)

    # Write garbage JSON
    store_path.write_text("THIS IS NOT JSON {{{{")

    # Any store operation should not crash and should start fresh
    job = store.add_job("aftercorrupt", "audio/aftercorrupt.wav")
    assert job["id"] == "aftercorrupt"

    # The corrupt file should have been renamed
    corrupt_files = list(tmp_path.glob("history.json.corrupt-*"))
    assert len(corrupt_files) == 1, f"Expected one .corrupt-* file, found: {corrupt_files}"

    # Fresh store should only contain the new job
    all_jobs = store.jobs()
    assert len(all_jobs) == 1
    assert all_jobs[0]["id"] == "aftercorrupt"


def test_orphan_wav_adoption(tmp_path, monkeypatch):
    """WAV files in AUDIO_DIR with no store entry are adopted as RECORDED jobs."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    import app as _app
    # Redirect AUDIO_DIR to tmp_path so adopt_orphan_wavs scans there
    monkeypatch.setattr(_app, "AUDIO_DIR", tmp_path)
    # Also redirect store.STORE_PATH in the store module (already done above)

    # Create an orphan WAV (no corresponding store entry)
    orphan_id = "20250101T120000000000Z"
    orphan_wav = tmp_path / f"{orphan_id}.wav"
    # Must exceed the tiny-orphan skip threshold (MIN_CLIP_SECS of PCM)
    orphan_wav.write_bytes(b"\x00" * 20000)

    # Ensure store is empty
    assert store.jobs() == []

    # Run adopt_orphan_wavs
    _app.adopt_orphan_wavs()

    # The orphan should now be in the store as RECORDED
    adopted = store.get_job(orphan_id)
    assert adopted is not None, "Orphan wav should have been adopted into store"
    assert adopted["status"] == store.RECORDED
    assert adopted["wav"] == str(orphan_wav)


def test_blank_transcription_stores_empty_raw(tmp_path, monkeypatch):
    """[BLANK_AUDIO] from Whisper stores raw='' not the literal marker string."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")

    import app as _app
    monkeypatch.setattr(_app, "_model", MagicMock())
    monkeypatch.setattr(_app, "AUDIO_DIR", tmp_path)
    # Return the blank audio marker Whisper emits on silence
    monkeypatch.setattr(_app, "transcribe", lambda audio: "[BLANK_AUDIO]")
    monkeypatch.setattr(_app, "inject", lambda text: None)

    samples = int(0.5 * 16000)
    raw_bytes = np.zeros(samples, dtype=np.int16).tobytes()
    job_id = "blanktest"
    wav_path = tmp_path / f"{job_id}.wav"
    with wave.open(str(wav_path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(raw_bytes)

    store.add_job(job_id, str(wav_path))
    _app._busy = True
    _app.on_release_worker(job_id, raw_bytes, time.perf_counter())

    j = store.get_job(job_id)
    assert j is not None
    # raw must be empty string, not the literal marker
    assert j.get("raw") == "", f"Expected raw='', got {j.get('raw')!r}"
    assert j["status"] == store.INJECTED


def test_delete_job_removes_entry_and_wav(tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x" * 100)
    store.add_job("a", str(wav))
    store.add_job("b", "nonexistent.wav")
    store.delete_job("a")
    assert store.get_job("a") is None
    assert store.get_job("b") is not None
    assert not wav.exists()
