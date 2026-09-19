"""Tests for src/stats.py — lifetime dictation metrics accumulator.

All tests use tmp_path so they never touch the real stats.json.
Run: .venv/bin/python -m pytest tests/ -q
"""

import json
import sys
import wave
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import stats


# ---------------------------------------------------------------------------
# Helper: point stats at tmp_path for each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_stats(tmp_path, monkeypatch):
    """Redirect stats.STATS_PATH to a temp file for each test."""
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")


# ---------------------------------------------------------------------------
# Helper: write a minimal valid WAV file
# ---------------------------------------------------------------------------

def make_wav(path: Path, duration_secs: float, framerate: int = 16000) -> Path:
    nframes = int(duration_secs * framerate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(b"\x00" * nframes * 2)
    return path


# ---------------------------------------------------------------------------
# Helper: freeze _now() to a fixed UTC datetime
# ---------------------------------------------------------------------------

def freeze_now(monkeypatch, dt: datetime):
    monkeypatch.setattr(stats, "_now", lambda: dt)


# ---------------------------------------------------------------------------
# 1. Accumulation across multiple records
# ---------------------------------------------------------------------------

def test_accumulates_across_records(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    stats.record(words=10, audio_secs=5.0)
    stats.record(words=20, audio_secs=10.0)
    stats.record(words=30, audio_secs=15.0)

    s = stats.summary()
    assert s["total_words"] == 60
    assert s["total_dictations"] == 3
    assert abs(s["total_audio_secs"] - 30.0) < 0.001


def test_record_persists_to_disk(tmp_path, monkeypatch):
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    stats.record(words=5, audio_secs=2.0)

    raw = json.loads(p.read_text())
    assert raw["total_words"] == 5
    assert raw["total_dictations"] == 1
    assert abs(raw["total_audio_secs"] - 2.0) < 0.001


# ---------------------------------------------------------------------------
# 2. Derived math — exact expected numbers for known inputs
# ---------------------------------------------------------------------------

def test_speaking_wpm_math(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    # 100 words spoken in 60 seconds → 100 WPM
    stats.record(words=100, audio_secs=60.0)
    s = stats.summary()
    assert abs(s["speaking_wpm"] - 100.0) < 0.1


def test_speaking_wpm_zero_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")
    # No records → no division by zero
    s = stats.summary()
    assert s["speaking_wpm"] == 0.0


def test_time_saved_math(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    # 500 words spoken in 3 minutes audio.
    # Typing at 50 WPM → 500/50 = 10 minutes typing.
    # Time saved = 10 - 3 = 7 minutes.
    stats.record(words=500, audio_secs=180.0)
    s = stats.summary()
    assert abs(s["time_saved_mins"] - 7.0) < 0.01


# ---------------------------------------------------------------------------
# 4. first_use_ts is set once and never overwritten
# ---------------------------------------------------------------------------

def test_first_use_ts_set_on_first_record(tmp_path, monkeypatch):
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    freeze_now(monkeypatch, t1)
    stats.record(words=5, audio_secs=2.0)

    raw = json.loads(p.read_text())
    assert "first_use_ts" in raw
    assert "2026-07-01" in raw["first_use_ts"]


def test_first_use_ts_never_overwritten(tmp_path, monkeypatch):
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 5, 10, 0, 0, tzinfo=timezone.utc)

    freeze_now(monkeypatch, t1)
    stats.record(words=5, audio_secs=2.0)

    freeze_now(monkeypatch, t2)
    stats.record(words=10, audio_secs=4.0)

    raw = json.loads(p.read_text())
    # first_use_ts must still be t1, not t2
    assert "2026-07-01" in raw["first_use_ts"]


# ---------------------------------------------------------------------------
# 5. Existing stats.json without first_use_ts upgrades gracefully (no reset)
# ---------------------------------------------------------------------------

def test_existing_stats_without_first_use_upgrades_gracefully(tmp_path, monkeypatch):
    """An old stats.json missing first_use_ts must NOT lose accumulated totals."""
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    # Write a stats.json in the old format (no first_use_ts key).
    old_data = {
        "total_words": 1234,
        "total_dictations": 42,
        "total_audio_secs": 600.0,
        "backfill_done": True,
    }
    p.write_text(json.dumps(old_data))

    s = stats.summary()
    # Totals must be preserved.
    assert s["total_words"] == 1234
    assert s["total_dictations"] == 42
    assert abs(s["total_audio_secs"] - 600.0) < 0.001
    # No first_use_ts → elapsed = 0 (no crash).
    assert s["days_elapsed"] == 0.0


def test_record_on_old_stats_sets_first_use(tmp_path, monkeypatch):
    """first record() after upgrade stamps first_use_ts without resetting totals."""
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    old_data = {
        "total_words": 100,
        "total_dictations": 5,
        "total_audio_secs": 120.0,
        "backfill_done": False,
    }
    p.write_text(json.dumps(old_data))

    t1 = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
    freeze_now(monkeypatch, t1)
    stats.record(words=10, audio_secs=5.0)

    raw = json.loads(p.read_text())
    assert raw["total_words"] == 110   # old 100 + new 10, not reset
    assert "first_use_ts" in raw


# ---------------------------------------------------------------------------
# 6. Formatting of human-readable menu_lines strings
# ---------------------------------------------------------------------------

def test_menu_lines_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    stats.record(words=12431, audio_secs=3600.0)
    lines = stats.menu_lines()

    assert isinstance(lines, list)
    assert len(lines) == 4
    # First line should contain formatted word count with comma separator
    assert "12,431" in lines[0]
    assert "words dictated" in lines[0]


def test_menu_lines_time_format_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    # 12000 words, 60 minutes audio → typing = 240 min → saved = 180 min = 3h 0m
    stats.record(words=12000, audio_secs=3600.0)
    lines = stats.menu_lines()
    time_line = [l for l in lines if "Time saved" in l][0]
    assert "h" in time_line and "m" in time_line


def test_menu_lines_time_format_minutes_only(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    # 100 words, 60s audio → typing = 2 min → saved = 2 - 1 = 1 min
    stats.record(words=100, audio_secs=60.0)
    lines = stats.menu_lines()
    time_line = [l for l in lines if "Time saved" in l][0]
    assert "m" in time_line
    assert "h" not in time_line


def test_menu_lines_zero_state(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")
    lines = stats.menu_lines()
    assert "0" in lines[0]
    # Time saved zero or negative → shows 0m
    time_line = [l for l in lines if "Time saved" in l][0]
    assert "0m" in time_line


def test_menu_lines_dictations_count(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")
    stats.record(words=10, audio_secs=5.0)
    stats.record(words=20, audio_secs=5.0)
    lines = stats.menu_lines()
    count_line = [l for l in lines if "dictations" in l][0]
    assert "2" in count_line


# ---------------------------------------------------------------------------
# 7. Corrupt-file recovery
# ---------------------------------------------------------------------------

def test_corrupt_stats_resets_to_zeros(tmp_path, monkeypatch):
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    p.write_text("NOT JSON {{{{")
    s = stats.summary()
    assert s["total_words"] == 0
    assert s["total_dictations"] == 0
    assert s["total_audio_secs"] == 0.0


def test_partial_stats_resets_to_zeros(tmp_path, monkeypatch):
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    # Missing required key → treated as corrupt
    p.write_text(json.dumps({"some_other_key": 999}))
    s = stats.summary()
    assert s["total_words"] == 0


def test_record_after_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    p.write_text("GARBAGE")
    # record() should recover gracefully and persist new data
    stats.record(words=5, audio_secs=3.0)
    s = stats.summary()
    assert s["total_words"] == 5


# ---------------------------------------------------------------------------
# 8. record() never raises
# ---------------------------------------------------------------------------

def test_record_never_raises_on_bad_input(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    # Negative values (clamped to 0)
    stats.record(words=-999, audio_secs=-5.0)
    s = stats.summary()
    assert s["total_words"] == 0
    assert s["total_audio_secs"] == 0.0


def test_record_never_raises_on_unwritable_path(tmp_path, monkeypatch):
    """record() must not raise even if the stats file can't be written."""
    bad_path = tmp_path / "nonexistent_dir" / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", bad_path)

    # Should not raise; just log a warning
    try:
        stats.record(words=5, audio_secs=2.0)
    except Exception as e:
        pytest.fail(f"record() raised an exception: {e}")


# ---------------------------------------------------------------------------
# 9. Backfill from history.json
# ---------------------------------------------------------------------------

def _make_history(tmp_path: Path, jobs: list[dict]) -> Path:
    p = tmp_path / "history.json"
    p.write_text(json.dumps({"jobs": jobs}))
    return p


def test_backfill_seeds_from_history(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    wav1 = make_wav(tmp_path / "job1.wav", duration_secs=3.0)
    wav2 = make_wav(tmp_path / "job2.wav", duration_secs=5.0)

    history = _make_history(tmp_path, [
        {"id": "job1", "clean": "hello world foo", "raw": None, "wav": str(wav1), "status": "INJECTED"},
        {"id": "job2", "clean": None, "raw": "bar baz qux quux", "wav": str(wav2), "status": "INJECTED"},
    ])

    processed = stats.backfill_from_history(history_path=history)
    assert processed == 2

    s = stats.summary()
    # job1: 3 words; job2: 4 words
    assert s["total_words"] == 7
    assert s["total_dictations"] == 2
    # audio: 3s + 5s = 8s
    assert abs(s["total_audio_secs"] - 8.0) < 0.1


def test_backfill_seeds_first_use_from_oldest_job(tmp_path, monkeypatch):
    """backfill seeds first_use_ts from the oldest parseable job timestamp."""
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    wav = make_wav(tmp_path / "j.wav", duration_secs=1.0)
    history = _make_history(tmp_path, [
        # Newer job first in list
        {"id": "20260722T173701123456Z", "clean": "hello world", "raw": None, "wav": str(wav)},
        # Older job second
        {"id": "20260719T213741884819Z", "clean": "foo bar", "raw": None, "wav": str(wav)},
        # Non-parseable id — should be ignored for timestamp purposes
        {"id": "f2test", "clean": "baz", "raw": None, "wav": str(wav)},
    ])

    stats.backfill_from_history(history_path=history)

    raw = json.loads((tmp_path / "stats.json").read_text())
    # first_use_ts must be the oldest parseable id: 2026-07-19
    assert "first_use_ts" in raw
    assert "2026-07-19" in raw["first_use_ts"]


def test_backfill_does_not_overwrite_existing_first_use(tmp_path, monkeypatch):
    """If first_use_ts already set (e.g. via record()), backfill must not overwrite it."""
    p = tmp_path / "stats.json"
    monkeypatch.setattr(stats, "STATS_PATH", p)

    # Pre-set a first_use_ts older than any job id in history.
    existing = {
        "total_words": 0, "total_dictations": 0, "total_audio_secs": 0.0,
        "backfill_done": False,
        "first_use_ts": "2026-07-01T00:00:00+00:00",
    }
    p.write_text(json.dumps(existing))

    wav = make_wav(tmp_path / "j.wav", duration_secs=1.0)
    history = _make_history(tmp_path, [
        {"id": "20260719T213741884819Z", "clean": "hello", "raw": None, "wav": str(wav)},
    ])
    stats.backfill_from_history(history_path=history)

    raw = json.loads(p.read_text())
    assert "2026-07-01" in raw["first_use_ts"]  # unchanged


def test_backfill_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    wav = make_wav(tmp_path / "job1.wav", duration_secs=2.0)
    history = _make_history(tmp_path, [
        {"id": "job1", "clean": "one two three", "raw": None, "wav": str(wav), "status": "INJECTED"},
    ])

    stats.backfill_from_history(history_path=history)
    count2 = stats.backfill_from_history(history_path=history)  # second call

    assert count2 == 0  # idempotent: nothing processed second time

    s = stats.summary()
    assert s["total_words"] == 3   # not doubled
    assert s["total_dictations"] == 1


def test_backfill_skips_missing_wav(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    history = _make_history(tmp_path, [
        {"id": "j1", "clean": "hello", "raw": None, "wav": str(tmp_path / "missing.wav"), "status": "INJECTED"},
    ])

    # Should not raise; words still counted even if wav missing (audio_secs=0)
    processed = stats.backfill_from_history(history_path=history)
    assert processed == 1
    s = stats.summary()
    assert s["total_words"] == 1
    assert s["total_audio_secs"] == 0.0


def test_backfill_skips_empty_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    history = _make_history(tmp_path, [
        {"id": "empty", "clean": None, "raw": None, "wav": None, "status": "INJECTED"},
        {"id": "blank", "clean": "", "raw": "", "wav": None, "status": "INJECTED"},
    ])

    processed = stats.backfill_from_history(history_path=history)
    assert processed == 0
    s = stats.summary()
    assert s["total_words"] == 0
    assert s["total_dictations"] == 0


def test_backfill_prefers_clean_over_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    wav = make_wav(tmp_path / "j.wav", duration_secs=1.0)
    # clean has 2 words, raw has 5 — should use clean
    history = _make_history(tmp_path, [
        {"id": "j", "clean": "two words", "raw": "one two three four five", "wav": str(wav), "status": "INJECTED"},
    ])

    stats.backfill_from_history(history_path=history)
    s = stats.summary()
    assert s["total_words"] == 2


def test_backfill_missing_history_file(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    processed = stats.backfill_from_history(history_path=tmp_path / "nonexistent.json")
    assert processed == 0
    # backfill_done should still be set so it doesn't retry
    raw = json.loads((tmp_path / "stats.json").read_text())
    assert raw.get("backfill_done") is True


def test_backfill_corrupt_history(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    bad_history = tmp_path / "history.json"
    bad_history.write_text("NOT JSON")

    processed = stats.backfill_from_history(history_path=bad_history)
    assert processed == 0
    raw = json.loads((tmp_path / "stats.json").read_text())
    assert raw.get("backfill_done") is True


# ---------------------------------------------------------------------------
# 10. Atomic write: verify tempfile + os.replace pattern
# ---------------------------------------------------------------------------

def test_atomic_write_uses_os_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")

    replaced_pairs = []
    import os as _os
    real_replace = _os.replace

    def mock_replace(src, dst):
        replaced_pairs.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(stats.os, "replace", mock_replace)
    stats.record(words=1, audio_secs=1.0)

    assert len(replaced_pairs) == 1
    src, dst = replaced_pairs[0]
    assert dst == tmp_path / "stats.json"
    assert src.parent == tmp_path
    assert src != dst
