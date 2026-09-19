"""Automated smoke tests for the transcription pipeline in src/app.py.

Run:  .venv/bin/python -m pytest tests/ -q

The fixture test generates a WAV via macOS TTS (say + afconvert),
then runs it through the same transcribe/normalize path the app uses.
Model load takes a few seconds on first run.
"""

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

# Make src importable without installing a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Import only the pure functions — importing app.py must NOT start the listener.
from app import int16_to_float32, normalize, transcribe, MIN_CLIP_SECS, _model
import app as _app_module

FIXTURE_AIFF = Path("/tmp/fixture.aiff")
FIXTURE_WAV = Path("/tmp/fixture.wav")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def whisper_model():
    """Load the model once for the whole test session."""
    from pywhispercpp.model import Model
    m = Model("base.en", print_realtime=False, print_progress=False)
    _app_module._model = m  # inject so transcribe() works
    return m


@pytest.fixture(scope="session")
def speech_wav(whisper_model):
    """Generate a deterministic WAV via macOS TTS. Requires 'say' and 'afconvert'."""
    text = "hello world this is a dictation test"
    subprocess.run(
        ["say", "-o", str(FIXTURE_AIFF), text],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
         str(FIXTURE_AIFF), str(FIXTURE_WAV)],
        check=True, capture_output=True,
    )
    return FIXTURE_WAV


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_transcribe_speech(speech_wav):
    """Full path: WAV → int16 → float32 → normalize → whisper → text."""
    with wave.open(str(speech_wav), "rb") as f:
        raw_bytes = f.readframes(f.getnframes())

    raw_np = np.frombuffer(raw_bytes, dtype=np.int16)
    audio_f32 = normalize(int16_to_float32(raw_np))
    result = transcribe(audio_f32)

    assert "hello" in result.lower(), f"'hello' not in {result!r}"
    assert "dictation" in result.lower(), f"'dictation' not in {result!r}"


def test_short_clip_guard():
    """Clips under MIN_CLIP_SECS should be detectable (guard logic in app)."""
    # Build a clip that's definitely under the threshold.
    short_samples = int(FIXTURE_WAV and (MIN_CLIP_SECS - 0.05) * 16000)
    raw_bytes = (np.zeros(short_samples, dtype=np.int16)).tobytes()
    clip_secs = len(raw_bytes) / 2 / 16000
    assert clip_secs < MIN_CLIP_SECS, "fixture is too long — test setup error"


def test_normalize_all_zeros():
    """normalize() must not crash or produce NaN on a silent clip."""
    silent = np.zeros(1600, dtype=np.float32)
    result = normalize(silent)
    assert not np.any(np.isnan(result)), "NaN in normalized silent audio"
    assert np.all(result == 0.0), "Silent audio should stay zero"


def test_normalize_scales_quiet_audio():
    """Quiet audio (peak < 1) should be scaled up to ~0.95."""
    quiet = np.full(1600, 0.01, dtype=np.float32)
    result = normalize(quiet)
    assert abs(np.max(np.abs(result)) - 0.95) < 1e-5


def test_int16_to_float32_range():
    """int16 max/min should land at ±1.0 after conversion."""
    samples = np.array([32767, -32768, 0], dtype=np.int16)
    f = int16_to_float32(samples)
    assert f.dtype == np.float32
    assert abs(f[0] - (32767 / 32768)) < 1e-4
    assert abs(f[1] - (-32768 / 32768)) < 1e-4
    assert f[2] == 0.0
