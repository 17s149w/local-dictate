"""Resilience tests for src/app.py — Bug A/B coverage.

Tests that the keyboard listener survives exceptions, the state machine
self-corrects from stuck states, and the Reset path preserves audio (F2).

Run: .venv/bin/python -m pytest tests/test_resilience.py -v

Follows the same stub-before-import pattern as tests/test_transcribe.py.
Heavy OS deps (sounddevice, pywhispercpp, rumps, pynput) are replaced with
minimal fakes so these tests run without hardware or GUI.
"""

import importlib.util
import sys
import types
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Stub every heavy import BEFORE importing app
# ---------------------------------------------------------------------------

def _install_stubs():
    # sounddevice
    sd = types.ModuleType("sounddevice")
    sd._terminate = MagicMock()
    sd._initialize = MagicMock()

    class FakeRawInputStream:
        def __init__(self, **kwargs):
            self.started = False
            self.closed = False
        def start(self):
            self.started = True
        def stop(self):
            pass
        def close(self):
            self.closed = True

    sd.RawInputStream = FakeRawInputStream
    sd.PortAudioError = Exception  # same base as real PortAudioError for tests
    sys.modules["sounddevice"] = sd

    # pywhispercpp
    pwc = types.ModuleType("pywhispercpp")
    pwc_model = types.ModuleType("pywhispercpp.model")
    class FakeModel:
        def __init__(self, *a, **kw): pass
        def transcribe(self, audio):
            return []
    pwc_model.Model = FakeModel
    sys.modules["pywhispercpp"] = pwc
    sys.modules["pywhispercpp.model"] = pwc_model

    # pynput
    pynput = types.ModuleType("pynput")
    pynput_kb = types.ModuleType("pynput.keyboard")

    class FakeKey:
        alt_r = object()
        cmd_r = object()
        cmd = object()

    class FakeController:
        def pressed(self, key):
            import contextlib
            return contextlib.nullcontext()
        def press(self, key): pass
        def release(self, key): pass

    class FakeListener:
        def __init__(self, on_press=None, on_release=None):
            self._on_press = on_press
            self._on_release = on_release
            self._alive = True
            self._thread = None
        def start(self):
            pass
        def stop(self):
            self._alive = False
        def is_alive(self):
            return self._alive

    pynput_kb.Key = FakeKey()
    pynput_kb.Controller = FakeController
    pynput_kb.Listener = FakeListener
    pynput.keyboard = pynput_kb
    sys.modules["pynput"] = pynput
    sys.modules["pynput.keyboard"] = pynput_kb

    # rumps
    rumps = types.ModuleType("rumps")
    class FakeApp:
        def __init__(self, *a, **kw):
            self.title = ""
            self.menu = MagicMock()
            self._nsapp = MagicMock()
        def run(self): pass
    rumps.App = FakeApp
    rumps.Timer = MagicMock(return_value=MagicMock())
    rumps.MenuItem = MagicMock(return_value=MagicMock())
    rumps.quit_application = MagicMock()
    sys.modules["rumps"] = rumps

    # store
    store = types.ModuleType("store")
    store.STORE_PATH = None
    store.RECORDED = "RECORDED"
    store.TRANSCRIBED = "TRANSCRIBED"
    store.CLEANED = "CLEANED"
    store.INJECTED = "INJECTED"
    store.ERROR = "ERROR"
    store.jobs = MagicMock(return_value=[])
    store.incomplete_jobs = MagicMock(return_value=[])
    store.add_job = MagicMock()
    store.update_job = MagicMock()
    store.delete_job = MagicMock()
    sys.modules["store"] = store

    # cleanup
    cleanup = types.ModuleType("cleanup")
    cleanup.health_check = MagicMock(return_value=False)
    cleanup.clean = MagicMock(side_effect=lambda t: t)
    cleanup.warm = MagicMock()
    sys.modules["cleanup"] = cleanup

    return sd, pynput_kb


_SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(_SRC))


def _load_app_with_stubs():
    """Load src/app.py against the fakes WITHOUT poisoning sys.modules.

    These tests need app bound to stubbed sounddevice/pynput/rumps/store, but
    test_transcribe.py needs the REAL ones and pytest runs every test module in
    one process. A plain `import app` after installing stubs leaves a faked-out
    `app` (plus faked `store`/`cleanup`) cached for everyone else — that
    silently made test_transcribe transcribe to '' and errored every
    test_store case. So: snapshot sys.modules, install stubs, exec app under a
    PRIVATE module name, then restore sys.modules exactly. The module object we
    return keeps its references to the fakes.
    """
    names = ("sounddevice", "pywhispercpp", "pywhispercpp.model", "pynput",
             "pynput.keyboard", "rumps", "store", "cleanup", "stats", "app")
    saved = {k: sys.modules.get(k) for k in names}
    sd_stub, kb_stub = _install_stubs()
    try:
        spec = importlib.util.spec_from_file_location(
            "app_under_test", _SRC / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    return module, sd_stub, kb_stub


app, _sd_stub, _kb_stub = _load_app_with_stubs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_app_state():
    """Reset all module-level state between tests."""
    app._stream = None
    app._busy = False
    app._chunks.clear()
    app._set_state("idle")
    app._cmd_r_down_time = None
    app._cmd_r_other_key_pressed = False
    if app._auto_stop_timer is not None:
        app._auto_stop_timer.cancel()
        app._auto_stop_timer = None
    # Reset stub call counts
    # app.store is the stub module (sys.modules holds the real one again).
    app.store.add_job.reset_mock()
    app.store.update_job.reset_mock()
    _sd_stub._terminate.reset_mock()
    _sd_stub._initialize.reset_mock()


# ---------------------------------------------------------------------------
# Bug A: on_press survives exceptions from start_recording
# ---------------------------------------------------------------------------

class TestListenerSurvivesExceptions:
    """_safe_on_press must swallow any exception so the listener thread lives."""

    def setup_method(self):
        _reset_app_state()

    def test_safe_on_press_does_not_propagate_exception(self):
        """If start_recording raises, _safe_on_press returns normally (no re-raise)."""
        with patch.object(app, "start_recording", side_effect=RuntimeError("PortAudio dead")):
            # Must not raise — if it raises, the listener thread would die.
            app._safe_on_press(app.HOTKEY)

    def test_safe_on_press_resets_state_after_start_recording_failure(self):
        """After start_recording failure, state is idle and _busy is False."""
        with patch.object(app, "start_recording", side_effect=RuntimeError("boom")):
            app._safe_on_press(app.HOTKEY)
        assert app._busy is False
        assert app._get_state() == "idle"

    def test_safe_on_press_resets_stream_after_start_recording_failure(self):
        """After start_recording failure, _stream is None (not left in a bad state)."""
        with patch.object(app, "start_recording", side_effect=RuntimeError("boom")):
            app._safe_on_press(app.HOTKEY)
        assert app._stream is None

    def test_safe_on_press_survives_arbitrary_exception_in_body(self):
        """Any exception inside on_press body is caught by the safe wrapper."""
        def _always_raises(key):
            raise ValueError("unexpected failure")
        with patch.object(app, "on_press", side_effect=_always_raises):
            # No raise means the listener is still alive.
            app._safe_on_press(app.HOTKEY)

    def test_safe_on_release_does_not_propagate_exception(self):
        """_safe_on_release swallows exceptions too."""
        def _raise(key):
            raise RuntimeError("release error")
        with patch.object(app, "on_release", side_effect=_raise):
            app._safe_on_release(app.HOTKEY)  # must not raise


# ---------------------------------------------------------------------------
# Bug A: PortAudio reinit on stream open failure
# ---------------------------------------------------------------------------

class TestPortAudioReinit:
    """start_recording reinitializes PortAudio on first failure and retries."""

    def setup_method(self):
        _reset_app_state()

    def test_reinit_called_on_stream_open_failure(self):
        """PortAudio _terminate/_initialize are called when the first stream open fails."""
        call_count = [0]

        class FailFirstRawInputStream:
            def __init__(self, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise Exception("PortAudio -9986")
                self.started = False
            def start(self):
                self.started = True
            def stop(self): pass
            def close(self): pass

        with patch.object(app.sd, "RawInputStream", FailFirstRawInputStream):
            app.start_recording()  # should succeed on retry

        assert _sd_stub._terminate.call_count == 1
        assert _sd_stub._initialize.call_count == 1
        assert app._stream is not None
        assert app._get_state() == "recording"

    def test_state_set_to_error_when_both_attempts_fail(self):
        """If both attempts fail, state is 'error' and _stream is None."""
        class AlwaysFail:
            def __init__(self, **kwargs):
                raise Exception("still broken after reinit")

        with patch.object(app.sd, "RawInputStream", AlwaysFail):
            with pytest.raises(Exception):
                app.start_recording()

        assert app._stream is None
        assert app._get_state() == "error"

    def test_reinit_portaudio_returns_false_on_sd_failure(self):
        """_reinit_portaudio returns False and doesn't propagate if sd._terminate raises."""
        with patch.object(_sd_stub, "_terminate", side_effect=Exception("sd broken")):
            result = app._reinit_portaudio()
        assert result is False


# ---------------------------------------------------------------------------
# Bug B: Stuck-state detector
# ---------------------------------------------------------------------------

class TestStuckStateDetector:
    """Watchdog must flip stuck states back to idle."""

    def setup_method(self):
        _reset_app_state()

    def _make_app(self):
        """Create a DictationApp instance without starting the rumps run loop."""
        a = app.DictationApp.__new__(app.DictationApp)
        a._ticks = 0
        a._last_state_entry = {}
        a._prev_state = "idle"
        return a

    def test_watchdog_resets_recording_with_no_stream(self):
        """state=recording but _stream=None → watchdog resets to idle."""
        a = self._make_app()
        app._set_state("recording")
        app._stream = None
        app._busy = True

        a._watchdog_check()

        assert app._get_state() == "idle"
        assert app._busy is False

    def test_watchdog_does_not_reset_recording_with_live_stream(self):
        """state=recording with a live stream → watchdog leaves it alone."""
        a = self._make_app()
        app._set_state("recording")
        app._stream = MagicMock()  # non-None stream

        a._watchdog_check()

        assert app._get_state() == "recording"
        app._stream = None  # cleanup

    def test_watchdog_resets_processing_after_60s(self):
        """state=processing for >60s → watchdog resets to idle."""
        a = self._make_app()
        app._set_state("processing")
        app._busy = True
        # Simulate an entry time 61 seconds ago.
        import time as _time
        a._last_state_entry["processing"] = _time.monotonic() - 61

        a._watchdog_check()

        assert app._get_state() == "idle"
        assert app._busy is False

    def test_watchdog_does_not_reset_recent_processing(self):
        """state=processing for < 60s → watchdog leaves it alone."""
        a = self._make_app()
        app._set_state("processing")
        app._busy = True
        import time as _time
        a._last_state_entry["processing"] = _time.monotonic() - 5

        a._watchdog_check()

        assert app._get_state() == "processing"
        # cleanup
        app._busy = False
        app._set_state("idle")

    def test_watchdog_restarts_dead_listener(self):
        """If the listener thread is dead, watchdog calls _restart_listener."""
        a = self._make_app()
        with patch.object(app, "_listener_alive", return_value=False), \
             patch.object(app, "_restart_listener") as mock_restart:
            a._watchdog_check()
        mock_restart.assert_called_once()


# ---------------------------------------------------------------------------
# Bug B: Reset path preserves in-progress audio (F2)
# ---------------------------------------------------------------------------

class TestResetPath:
    """_do_reset must not discard audio that meets the MIN_CLIP_SECS threshold."""

    def setup_method(self):
        _reset_app_state()

    def test_reset_clears_state_when_idle(self):
        """Reset with no active recording just clears state and restarts listener."""
        app._stream = None
        app._busy = False
        app._set_state("idle")

        with patch.object(app, "_restart_listener") as mock_rl:
            app._do_reset()

        assert app._get_state() == "idle"
        assert app._busy is False
        mock_rl.assert_called_once()

    def test_reset_clears_stuck_busy_state(self):
        """Reset with _busy=True and no stream clears _busy."""
        app._stream = None
        app._busy = True
        app._set_state("processing")

        with patch.object(app, "_restart_listener"):
            app._do_reset()

        assert app._busy is False
        assert app._get_state() == "idle"

    def test_reset_saves_audio_when_recording_above_threshold(self, tmp_path):
        """Reset during recording saves audio to disk and queues transcription (F2)."""
        # Simulate a recording in progress with enough audio.
        fake_stream = MagicMock()
        app._stream = fake_stream
        app._set_state("recording")

        # Fill _chunks with enough audio (> MIN_CLIP_SECS)
        samples = int(app.MIN_CLIP_SECS * app.SAMPLE_RATE * 1.5)
        import numpy as np
        audio = np.zeros(samples, dtype=np.int16)
        app._chunks.clear()
        app._chunks.append(audio.tobytes())

        worker_calls = []
        def fake_worker(job_id, raw_bytes, t_release):
            worker_calls.append((job_id, len(raw_bytes)))

        with patch.object(app, "AUDIO_DIR", tmp_path), \
             patch.object(app, "on_release_worker", side_effect=fake_worker), \
             patch.object(app, "_restart_listener"):
            app._do_reset()

        # Stream should be stopped and cleared.
        fake_stream.stop.assert_called_once()
        fake_stream.close.assert_called_once()
        assert app._stream is None

        # A WAV file should have been written.
        wavs = list(tmp_path.glob("*.wav"))
        assert len(wavs) == 1, f"Expected 1 WAV, got {len(wavs)}"

        # Worker should have been called with the audio.
        assert len(worker_calls) == 1

    def test_reset_discards_audio_below_threshold(self, tmp_path):
        """Reset during very short recording (< MIN_CLIP_SECS) discards audio."""
        fake_stream = MagicMock()
        app._stream = fake_stream
        app._set_state("recording")

        # Only a few samples — well below MIN_CLIP_SECS.
        app._chunks.clear()
        import numpy as np
        short_audio = np.zeros(100, dtype=np.int16)
        app._chunks.append(short_audio.tobytes())

        with patch.object(app, "AUDIO_DIR", tmp_path), \
             patch.object(app, "_restart_listener"):
            app._do_reset()

        wavs = list(tmp_path.glob("*.wav"))
        assert len(wavs) == 0, "No WAV should be saved for a clip too short to contain speech"
        assert app._busy is False
        assert app._get_state() == "idle"

    def test_reset_cancels_auto_stop_timer(self):
        """Reset cancels any pending auto-stop timer."""
        mock_timer = MagicMock()
        app._auto_stop_timer = mock_timer
        app._stream = None
        app._busy = False

        with patch.object(app, "_restart_listener"):
            app._do_reset()

        mock_timer.cancel.assert_called_once()
        assert app._auto_stop_timer is None


# ---------------------------------------------------------------------------
# Exception-safety of _stop_and_process
# ---------------------------------------------------------------------------

class TestStopAndProcessExceptionSafety:
    """_stop_and_process must null _stream even if stream.stop() raises."""

    def setup_method(self):
        _reset_app_state()

    def test_stream_nulled_even_if_stop_raises(self, tmp_path):
        """_stream is set to None in the finally block even if stream.stop() throws."""
        class BadStream:
            def stop(self):
                raise RuntimeError("stop failed")
            def close(self):
                pass

        app._stream = BadStream()
        app._busy = False

        # Provide enough audio to pass the MIN_CLIP_SECS check.
        samples = int(app.MIN_CLIP_SECS * app.SAMPLE_RATE * 2)
        import numpy as np
        app._chunks.clear()
        app._chunks.append(np.zeros(samples, dtype=np.int16).tobytes())

        with patch.object(app, "AUDIO_DIR", tmp_path), \
             patch.object(app, "on_release_worker"):
            app._stop_and_process()

        # Stream must be None regardless of the stop() exception.
        assert app._stream is None

    def test_stream_nulled_even_if_close_raises(self, tmp_path):
        """_stream is set to None even if stream.close() throws."""
        class BadStream:
            def stop(self): pass
            def close(self):
                raise RuntimeError("close failed")

        app._stream = BadStream()
        app._busy = False

        samples = int(app.MIN_CLIP_SECS * app.SAMPLE_RATE * 2)
        import numpy as np
        app._chunks.clear()
        app._chunks.append(np.zeros(samples, dtype=np.int16).tobytes())

        with patch.object(app, "AUDIO_DIR", tmp_path), \
             patch.object(app, "on_release_worker"):
            app._stop_and_process()

        assert app._stream is None


# ---------------------------------------------------------------------------
# Listener lifecycle helpers
# ---------------------------------------------------------------------------

class TestListenerLifecycle:
    """_listener_alive and _restart_listener behave correctly."""

    def setup_method(self):
        _reset_app_state()

    def test_listener_alive_true_when_running(self):
        fake = _kb_stub.Listener()
        fake._alive = True
        with app._listener_lock:
            app.listener = fake
        assert app._listener_alive() is True

    def test_listener_alive_false_when_stopped(self):
        fake = _kb_stub.Listener()
        fake._alive = False
        with app._listener_lock:
            app.listener = fake
        assert app._listener_alive() is False

    def test_listener_alive_false_when_none(self):
        with app._listener_lock:
            app.listener = None
        assert app._listener_alive() is False

    def test_restart_listener_replaces_dead_listener(self):
        old = _kb_stub.Listener()
        old._alive = False
        with app._listener_lock:
            app.listener = old

        app._restart_listener()

        assert app.listener is not old
        assert app.listener is not None
