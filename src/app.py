"""Phase 2: push-to-talk dictation with menu-bar UI and durable job store (F2).

Tap Right Option (⌥) → records mic → tap again → Whisper transcribes →
pastes at cursor.  Right ⌘ clean-tap → re-pastes the last result.

Audio is written to disk + job persisted BEFORE transcription (F2 durability).
Crash recovery on startup re-transcribes any RECORDED jobs found in history.
Latency logged to logs/latency.log.

Run:  .venv/bin/python src/app.py
"""

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
from pywhispercpp.model import Model
from pynput import keyboard
import rumps

import cleanup
import stats
import store

ROOT = Path(__file__).resolve().parent.parent
# NOTE: store.STORE_PATH and stats.STATS_PATH are bound in __main__, NOT here.
# Binding at import time meant `import app` from a test silently repointed the
# store at the REAL history.json and wrote junk jobs into it — which, with the
# 50-job cap, evicted real dictations (an F2 violation). Importing this module
# must have no side effects on user data.

HOTKEY = keyboard.Key.alt_r
SAMPLE_RATE = 16000
AUDIO_DIR = ROOT / "audio"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "latency.log"
MIN_CLIP_SECS = 0.3
MAX_RECORD_SECS = 120

# How long to wait for a PortAudio reinit before declaring the device wedged
# and abandoning the attempt. Matches _teardown_stream's 1.5s precedent, with
# a little headroom since reinit does more work than a stream close.
REINIT_TIMEOUT_SECS = 2.0

# Bound for any single PortAudio/CoreAudio call made through _bounded().
AUDIO_CALL_TIMEOUT_SECS = 2.0

# Circuit breaker. Every bounded audio call that times out counts as a "wedge";
# a successful stream open clears the count. At this threshold the device is
# considered unrecoverable in-process and we restart (see _request_restart).
#
# 2 rather than 1 deliberately, from observed behaviour on 2026-09-15: a lone
# teardown wedge at 23:08:26 was followed by a dictation that transcribed and
# pasted normally. One wedge is survivable. It was the SECOND wedge — the
# stream open — that consumed the hotkey listener and bricked the app.
_AUDIO_WEDGE_RESTART_THRESHOLD = 2
_audio_wedge_count = 0

# Restart budget. Must live on disk: a restart destroys memory, so an in-memory
# counter would reset every time and happily loop forever.
APP_BUNDLE = Path.home() / "Applications" / "LocalDictate.app"
RESTART_HISTORY = (Path.home() / "Library" / "Application Support" / "LocalDictate"
                   / "restart-history.json")
_RESTART_BUDGET = 2       # at most N self-restarts...
_RESTART_WINDOW_SECS = 900  # ...within this rolling window (15 min)

# Transcription retry config
MAX_ATTEMPTS = 3
RETRY_BACKOFFS = [0.5, 1.0]  # seconds before attempt 2 and 3

_chunks: list[bytes] = []
_stream = None
_busy = False
_last_audio_cb_ts = 0.0  # monotonic time of the most recent audio callback
_kb = keyboard.Controller()
_model: Model | None = None

# Set True after health_check() confirms Ollama + gemma4:e2b are reachable.
# First dictation during warm-up just pastes raw — acceptable.
_cleanup_enabled = False

# Menu-bar state (worker threads write; rumps timer reads on main thread)
_state = "idle"   # "idle" | "recording" | "processing" | "error"
_state_lock = threading.Lock()
_dirty = False    # set True when _state or history changes; timer clears it

_whisper_lock = threading.Lock()
_auto_stop_timer: threading.Timer | None = None

# Right-⌘ clean-tap detection
_cmd_r_down_time: float | None = None
_cmd_r_other_key_pressed = False

# Keyboard listener (module-level so watchdog can restart it)
listener: keyboard.Listener | None = None
_listener_lock = threading.Lock()

# Wake-from-sleep observer — keep a reference so it isn't garbage collected.
_sleep_observer = None

# Watchdog: check listener health every ~2s from _tick (runs every 0.15s).
_watchdog_ticks = 0
_WATCHDOG_INTERVAL_TICKS = 13  # ~13 × 0.15s ≈ 2s

# Proactive keep-warm: re-ping Ollama on a timer well inside its 10min
# keep_alive window, so gemma should never actually go cold mid-session
# (2026-09-01 bug — see cleanup.py's _TIMEOUT_CEILING comment).
_last_warm_ts = 0.0
_WARM_INTERVAL_SECS = 480  # 8 minutes

# Main-thread heartbeat. _tick() refreshes this every 0.15s; _heartbeat_watchdog()
# reads it from a plain daemon thread.
#
# Why a separate thread at all: _watchdog_check() is driven by rumps.Timer, which
# fires on the main thread's run loop — the exact thread that deadlocked for 12
# days on 2026-09-04. A watchdog that shares fate with the thing it watches can
# never report that thing dying. This one runs independently, so it keeps logging
# even when the main thread is wedged inside CoreAudio.
#
# It cannot FIX a main-thread deadlock (the held lock lives inside Apple's audio
# stack, not ours). Its job is narrower and still worth it: turn a silent freeze
# into a diagnosed one, with the stack capture attached.
_last_tick_ts = 0.0
_HEARTBEAT_POLL_SECS = 2.0
_HEARTBEAT_STALL_SECS = 10.0   # ~66 missed ticks — far beyond any normal hitch
_HEARTBEAT_RELOG_SECS = 300.0  # re-log every 5min while still stalled


# ---------------------------------------------------------------------------
# Timestamped logging — wraps stdout so every print() gets a timestamp prefix.
# This keeps existing print() calls unchanged and doesn't touch latency.log.
# ---------------------------------------------------------------------------

class _TimestampedStdout:
    """Proxy stdout to prefix each line with a timestamp (HH:MM:SS.mmm)."""
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def write(self, s):
        if s and s != "\n":
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            # Avoid double-stamping multiline strings by inserting ts after each \n
            stamped = s.replace("\n", f"\n{ts} ")
            # Trim trailing dangling timestamp from a trailing newline
            if stamped.endswith(f"\n{ts} "):
                stamped = stamped[:-(len(ts) + 2)] + "\n"
            self._wrapped.write(f"{ts} {stamped}")
        else:
            self._wrapped.write(s)

    def flush(self):
        self._wrapped.flush()

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


# Install once; harmless to re-install in tests because tests don't call __main__.
def _install_timestamp_logging():
    sys.stdout = _TimestampedStdout(sys.stdout)


# ---------------------------------------------------------------------------
# Audio helpers (pure — unit testable)
# ---------------------------------------------------------------------------

def normalize(audio_f32: np.ndarray) -> np.ndarray:
    """Scale to ±0.95; guard against all-zeros to avoid divide-by-zero."""
    peak = np.max(np.abs(audio_f32))
    if peak < 1e-6:
        return audio_f32
    return audio_f32 / peak * 0.95


def int16_to_float32(raw: np.ndarray) -> np.ndarray:
    """Convert int16 PCM → float32 in [-1, 1]."""
    return raw.astype(np.float32) / 32768.0


def transcribe(audio_f32: np.ndarray) -> str:
    """Run Whisper; return stripped text. Serialized — model isn't thread-safe."""
    with _whisper_lock:
        segs = _model.transcribe(audio_f32)
        # Segments don't reliably carry leading spaces (esp. after pauses) —
        # naive concat produced "now.Let's". Strip each and join with spaces.
        return " ".join(t for t in (s.text.strip() for s in segs) if t)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _set_state(s: str) -> None:
    global _state, _dirty
    with _state_lock:
        _state = s
        _dirty = True


def _get_state() -> str:
    with _state_lock:
        return _state


# ---------------------------------------------------------------------------
# Inject (clipboard save/restore + Cmd+V)
# ---------------------------------------------------------------------------

def inject(text: str) -> None:
    """Put text on clipboard and send Cmd+V; restore prior clipboard after 1.0s."""
    try:
        # Images/files aren't restorable via pbpaste — text-only restore is a known v1 limitation.
        prior = subprocess.run("pbpaste", capture_output=True, timeout=1).stdout
    except Exception:
        prior = b""

    try:
        subprocess.run("pbcopy", input=text.encode(), timeout=1, check=True)
    except Exception as e:
        print(f"  pbcopy failed: {e} — text is in history but not pasted")
        return  # don't send Cmd+V; pasting stale clipboard is worse than not pasting

    with _kb.pressed(keyboard.Key.cmd):
        _kb.press("v")
        _kb.release("v")

    injected = text.encode()

    def _restore():
        # Only restore prior if clipboard still holds the injected text;
        # a concurrent dictation may have already replaced it.
        try:
            current = subprocess.run("pbpaste", capture_output=True, timeout=1).stdout
            if prior and current == injected:
                subprocess.run("pbcopy", input=prior, timeout=1)
        except Exception:
            pass

    threading.Timer(1.0, _restore).start()


# ---------------------------------------------------------------------------
# Transcription with retry (persists attempts to store on each try)
# ---------------------------------------------------------------------------

def _transcribe_with_retry(job_id: str, audio_f32: np.ndarray) -> str | None:
    """Attempt transcription up to MAX_ATTEMPTS times. Returns text or None on exhaustion."""
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        store.update_job(job_id, attempts=attempt)
        try:
            text = transcribe(audio_f32)
            return text
        except Exception as e:
            last_err = e
            print(f"  transcription attempt {attempt} failed: {e}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFFS[attempt - 1])

    # All attempts exhausted
    store.update_job(job_id, status=store.ERROR, error=str(last_err))
    _set_state("error")
    print(f"  transcription failed after {MAX_ATTEMPTS} attempts — audio safe on disk")
    return None


# ---------------------------------------------------------------------------
# Worker (runs in background thread after recording stops)
# ---------------------------------------------------------------------------

def on_release_worker(job_id: str, raw_bytes: bytes, t_release: float) -> None:
    """F2 pipeline: add_job already done in _stop_and_process; transcribe then inject."""
    global _busy
    try:
        _set_state("processing")

        # WAV written + job persisted as RECORDED before this thread started (F2).
        # 1. Transcribe with retry
        raw_np = np.frombuffer(raw_bytes, dtype=np.int16)
        audio_f32 = normalize(int16_to_float32(raw_np))
        text = _transcribe_with_retry(job_id, audio_f32)

        if text is None:
            return  # error path already handled in _transcribe_with_retry

        # Persist TRANSCRIBED — text is safe before any paste attempt (F2 invariant).
        store.update_job(job_id, status=store.TRANSCRIBED, raw=text)
        global _dirty
        with _state_lock:
            _dirty = True

        if not text or text in ("[BLANK_AUDIO]",):
            # Store empty string — avoids literal "[BLANK_AUDIO]" being re-pasteable.
            if text in ("[BLANK_AUDIO]",):
                store.update_job(job_id, status=store.TRANSCRIBED, raw="")
            store.update_job(job_id, status=store.INJECTED)
            print("  empty transcription, nothing pasted")
            _set_state("idle")
            return

        print(f"  transcript: {text!r}")

        # Cleanup pass (Phase 3): if Ollama is available, clean the raw text.
        # F2: persist CLEANED status + clean field BEFORE calling inject.
        paste_text = text
        t_clean_start = time.perf_counter()
        clean_ms_str = ""
        if _cleanup_enabled and text:
            cleaned = cleanup.clean(text)
            clean_ms = int((time.perf_counter() - t_clean_start) * 1000)
            clean_ms_str = f" clean={clean_ms}ms"
            if cleaned:
                store.update_job(job_id, status=store.CLEANED, clean=cleaned)
                with _state_lock:
                    _dirty = True
                paste_text = cleaned
            else:
                print("  [cleanup] failed — pasting raw")

        # 4. Inject — paste can silently fail (no focused field) which is fine;
        #    text is already in history and on the clipboard.
        inject(paste_text)
        store.update_job(job_id, status=store.INJECTED)
        with _state_lock:
            _dirty = True

        # Latency log (includes cleanup time when cleanup ran)
        t_pasted = time.perf_counter()
        clip_secs = len(raw_bytes) / 2 / SAMPLE_RATE
        # Lifetime metrics. record() never raises — a stats failure must not
        # affect a dictation that has already been pasted and persisted.
        stats.record(len(paste_text.split()), clip_secs)
        elapsed_ms = int((t_pasted - t_release) * 1000)
        line = f"[latency] clip={clip_secs:.1f}s release->pasted={elapsed_ms}ms{clean_ms_str}"
        print(f"  {line}")
        LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_FILE, "a") as lf:
            lf.write(line + "\n")

        _set_state("idle")

    finally:
        _busy = False


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _audio_callback(data, frames, t, status):
    global _last_audio_cb_ts
    _chunks.append(bytes(data))
    _last_audio_cb_ts = time.monotonic()
    if status:
        # Non-empty status = PortAudio flagged something (overflow, device
        # change, etc.) on this buffer. Doesn't mean the stream is dead — just
        # log it so a mic-steal event leaves a trace instead of vanishing.
        print(f"  [audio] callback status flag: {status}")


def _bounded(label: str, timeout: float, fn, *args, **kwargs) -> tuple[bool, object]:
    """Run a call that may never return, and return anyway. THE audio chokepoint.

    Every PortAudio/CoreAudio call in this file must go through here. Enforced
    by tests/test_no_raw_audio_calls.py, which fails the build if `sd.` appears
    outside the approved wrappers — because this rule has now been rediscovered
    the hard way three separate times:

      2026-08-31  Pa_CloseStream   wedged -> Reset appeared to do nothing
      2026-09-04  Pa_Terminate     wedged on the main thread -> 12-day freeze
      2026-09-15  Pa_OpenStream    wedged the hotkey listener -> app bricked

    Each was fixed individually at its call site, so the next unprotected call
    simply became the next bug. The knowledge lived in docstrings, which only
    help someone who happens to read them. It now lives in a test.

    WHY A THREAD: these calls cross the FFI boundary into Apple's audio stack
    and can block on a mutex held inside CoreAudio. A thread parked in C never
    returns to the interpreter loop, so it checks no signals and honours no
    timeout — it is not slow, it is unreachable. try/except cannot help: a hang
    is not an exception. When a library offers a timeout parameter (as urllib
    does in cleanup.py) use that instead; PortAudio offers none, so we impose
    one from outside, and the only way to bound a call from outside in Python
    is to run it somewhere we are willing to abandon.

    Returns (True, result) on success, (False, None) on timeout. Real
    exceptions are re-raised so callers keep their existing error handling
    (start_recording depends on catching PortAudioError -9986 to trigger a
    reinit-and-retry). Distinguishing "hung" from "raised" is the point: only a
    hang means the device is wedged, and only a wedge trips the breaker.
    """
    box: dict = {}

    def _do():
        try:
            box["value"] = fn(*args, **kwargs)
            box["ok"] = True
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller
            box["error"] = e
            box["ok"] = False

    t = threading.Thread(target=_do, daemon=True, name=f"bounded-{label}")
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Unkillable: a thread stuck in C cannot be terminated from Python,
        # only abandoned. Costs ~1MB of parked stack and zero CPU. Only a
        # process exit ever reclaims it — which is what the breaker is for.
        print(f"  [audio] WARNING: {label} did not finish within {timeout}s "
              f"(device likely wedged) — abandoning it in the background")
        _note_audio_wedge(label)
        return False, None

    if box.get("ok"):
        return True, box.get("value")
    raise box["error"]


def _note_audio_ok() -> None:
    """Clear the wedge count after a call that actually worked."""
    global _audio_wedge_count
    if _audio_wedge_count:
        print(f"  [audio] audio recovered — clearing wedge count "
              f"({_audio_wedge_count} -> 0)")
    _audio_wedge_count = 0


def _note_audio_wedge(label: str) -> None:
    """Count a wedged audio call; restart the process once they pile up.

    A timeout makes each individual call survivable. It does NOT make the app
    work: the device stays wedged, so every retry spawns another abandoned
    thread and fails again. Without this breaker the app is responsive and
    permanently broken, cheerfully retrying something that cannot succeed and
    leaking a thread per keypress.
    """
    global _audio_wedge_count
    _audio_wedge_count += 1
    print(f"  [audio] wedge {_audio_wedge_count}/{_AUDIO_WEDGE_RESTART_THRESHOLD} ({label})")
    if _audio_wedge_count >= _AUDIO_WEDGE_RESTART_THRESHOLD:
        _request_restart(f"audio wedged after {_audio_wedge_count} timed-out calls")


def _restart_allowed() -> bool:
    """True if we are under budget. Reads/writes history on disk, not memory."""
    now = time.time()
    try:
        RESTART_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        hist = json.loads(RESTART_HISTORY.read_text()) if RESTART_HISTORY.exists() else []
    except Exception:
        hist = []
    hist = [t for t in hist if isinstance(t, (int, float)) and now - t < _RESTART_WINDOW_SECS]
    if len(hist) >= _RESTART_BUDGET:
        return False
    hist.append(now)
    try:
        RESTART_HISTORY.write_text(json.dumps(hist))
    except Exception as e:
        # Can't record it -> can't bound it. Refuse rather than risk a loop.
        print(f"  [restart] cannot write restart history ({e}) — refusing restart")
        return False
    return True


def _request_restart(reason: str, manual: bool = False) -> None:
    """Relaunch Local Dictate, because process death is the only cure for a wedge.

    Abandoned threads and a wedged CoreAudio client connection are per-process
    state that nothing in-process can reclaim. Pa_Terminate cannot fix it — it
    blocks on the very lock it would need (2026-09-04). A fresh process gets a
    fresh CoreAudio connection, and crash recovery replays any RECORDED job
    from history.json, so nothing dictated is lost.

    Relaunches through LocalDictate.app, never sys.executable: macOS attributes
    Accessibility and Input Monitoring grants to the BUNDLE. Re-exec'ing the
    Python binary directly would come back as an unrecognised app with a dead
    hotkey and no error message.

    manual=True marks a restart the user asked for from the menu. It skips the
    budget check and exits 0, because the budget guards against the app looping
    on itself, not against a person making a decision.
    """
    if not APP_BUNDLE.exists():
        print(f"  [restart] {APP_BUNDLE} not found (running from a terminal?) — "
              "staying up in error state instead")
        _set_state("error")
        return

    # The budget exists to stop the app restarting ITSELF in a loop. A person
    # clicking a menu item is not a loop — it is the way out of one, so a manual
    # restart is never refused. It is also never *recorded*: spending budget on
    # deliberate restarts would quietly disarm automatic recovery afterwards.
    if not manual and not _restart_allowed():
        print(f"  [restart] restart budget exhausted "
              f"({_RESTART_BUDGET} per {_RESTART_WINDOW_SECS // 60}min) — "
              "the device may be wedged at the hardware level. Staying up in "
              "error state; unplug/replug the mic, then quit and relaunch.")
        _set_state("error")
        return

    print(f"[restart] {reason} — restarting Local Dictate")

    # Detached relauncher. Waits for THIS pid to disappear before reopening,
    # because the bundle's launcher holds a single-instance PID file and only
    # removes it after its `wait` on us returns. Reopening too early hits that
    # guard and exits silently, leaving no Local Dictate running at all.
    # start_new_session detaches it from our process group so our death does
    # not take it with us.
    try:
        subprocess.Popen(
            ["/bin/sh", "-c",
             f'while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.5; done; '
             f'sleep 2; open -a "{APP_BUNDLE}"'],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"  [restart] could not spawn relauncher ({e}) — staying up")
        _set_state("error")
        return

    # os._exit, not sys.exit: sys.exit raises SystemExit, which unwinds the
    # stack and runs cleanup handlers — and any of those touching PortAudio
    # would block on the same wedged lock, hanging us during our own shutdown.
    # We want immediate death. stdout is unbuffered (-u) so the log is flushed.
    os._exit(0 if manual else 1)


def _reinit_portaudio(timeout: float = REINIT_TIMEOUT_SECS) -> bool:
    """Reinitialize PortAudio after sleep/wake or device state staleness.

    Uses private sounddevice API (_terminate/_initialize) — the only way to
    reset PortAudio's internal state without restarting the process.

    Runs on a throwaway daemon thread with a bounded wait, for the same reason
    _teardown_stream does: sd._terminate() calls Pa_Terminate(), which blocks
    on CoreAudio's device lock. If the audio device is wedged that lock is
    never released and the call NEVER RETURNS. A try/except cannot save you
    here — it catches exceptions, and a hang is not an exception.

    This is not hypothetical (2026-09-04): a wedged mic left an abandoned
    teardown thread queued on that lock; 13 minutes later a wake-from-sleep
    called this function on the MAIN thread, joined the same queue, and froze
    the entire app for 12 days — menu bar, hotkey, and every click dead. Even
    Activity Monitor's Quit did nothing: it sends an Apple Event the app must
    process in its event loop, and the event loop was exactly what was stuck.
    Only Force Quit (SIGKILL, handled by the kernel) could end it.

    Returns True ONLY if the reinit actually completed. False means the audio
    layer is wedged or errored; callers must surface that to the user rather
    than assume audio is ready.
    """
    def _do():
        sd._terminate()
        sd._initialize()

    try:
        ok, _ = _bounded("PortAudio reinit", timeout, _do)
    except Exception as e:
        print(f"  [audio] PortAudio reinit failed: {e}")
        return False

    if not ok:
        return False
    print("  [audio] PortAudio reinitialized OK")
    return True


def _teardown_stream(stream) -> None:
    """Stop/close a PortAudio stream without ever blocking the caller.

    If another app reclaims the mic mid-recording (or a Bluetooth/USB device
    drops), stream.stop()/close() can wedge indefinitely inside CoreAudio —
    that's what made Reset appear to do nothing (2026-08-31 bug report): the
    reset thread hung forever inside this exact call, and _stream was never
    cleared, so a second click just hung the same way again. Run teardown on
    its own thread and give up waiting after a short timeout; the caller has
    already detached `_stream` before calling this, so a hang here just leaks
    one daemon thread instead of freezing the app.
    """
    def _do():
        stream.stop()
        stream.close()

    try:
        _bounded("stream teardown", 1.5, _do)
    except Exception as e:
        print(f"  [audio] stream teardown error (ignored): {e}")


def start_recording() -> None:
    global _stream
    _chunks.clear()

    def _open():
        return sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            callback=_audio_callback,
        )

    # Runs on the pynput listener thread. That thread handles EVERY key event,
    # so blocking here does not just fail this dictation — it consumes the
    # hotkey permanently and every later press queues behind a callback that
    # never returns. That is exactly how the app bricked on 2026-09-15:
    # Pa_OpenStream joined a CoreAudio lock queue already occupied by an
    # abandoned teardown thread. Nothing below may block unboundedly.
    #
    # First attempt; if PortAudio's device list is stale (e.g. after sleep/wake)
    # this raises PortAudioError -9986. We catch it, reinit, and retry once.
    try:
        ok, _stream = _bounded("stream open", AUDIO_CALL_TIMEOUT_SECS, _open)
    except Exception as e:
        print(f"  [audio] stream open failed ({e}), reinitializing PortAudio and retrying…")
        if not _reinit_portaudio():
            _set_state("error")
            raise  # propagate so on_press can catch and stay alive
        try:
            ok, _stream = _bounded("stream open (retry)", AUDIO_CALL_TIMEOUT_SECS, _open)
        except Exception as e2:
            print(f"  [audio] stream open failed after reinit ({e2}) — resetting to idle")
            _stream = None
            _set_state("error")
            raise  # propagate so on_press can catch and stay alive

    if not ok:
        # Timed out rather than raised: the device is wedged. _bounded has
        # already counted it, which may have tripped the restart breaker.
        _stream = None
        _set_state("error")
        raise RuntimeError("audio device wedged — stream open timed out")

    # Pa_StartStream takes the same CoreAudio lock as open/close/terminate.
    ok, _ = _bounded("stream start", AUDIO_CALL_TIMEOUT_SECS, _stream.start)
    if not ok:
        _stream = None
        _set_state("error")
        raise RuntimeError("audio device wedged — stream start timed out")

    _note_audio_ok()
    _set_state("recording")
    # Reload gemma while the user speaks so a post-idle-unload dictation
    # doesn't hit the cleanup timeout and fall back to raw (F5 + F1).
    if _cleanup_enabled:
        threading.Thread(target=cleanup.warm, daemon=True).start()
    print("* recording…")


def _stop_and_process() -> None:
    global _busy, _stream, _auto_stop_timer
    if _stream is None:
        return  # guard: timer/hotkey race — nothing to stop
    if _auto_stop_timer is not None:
        _auto_stop_timer.cancel()
        _auto_stop_timer = None

    # Detach _stream immediately (before attempting teardown) so state never
    # waits on a possibly-wedged PortAudio call.
    stream_to_close = _stream
    _stream = None
    _teardown_stream(stream_to_close)

    _busy = True

    raw_bytes = b"".join(_chunks)
    clip_secs = len(raw_bytes) / 2 / SAMPLE_RATE

    if clip_secs < MIN_CLIP_SECS:
        print(f"  clip {clip_secs:.2f}s < {MIN_CLIP_SECS}s — skipping (accidental tap)")
        _busy = False
        _set_state("idle")
        return

    # Write WAV to disk FIRST (F2: audio is durable before job record exists).
    AUDIO_DIR.mkdir(exist_ok=True)
    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    wav_path = AUDIO_DIR / f"{job_id}.wav"
    try:
        with wave.open(str(wav_path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(SAMPLE_RATE)
            f.writeframes(raw_bytes)
        print(f"  audio saved → {wav_path}")
    except Exception as e:
        print(f"  [audio] WAV write failed: {e} — processing in-memory")
        wav_path = None

    if wav_path is not None:
        store.add_job(job_id, str(wav_path))
        print(f"  job {job_id} persisted (RECORDED)")
    else:
        # Still process in-memory even if disk write failed (F2 best-effort).
        job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    t_release = time.perf_counter()
    threading.Thread(
        target=on_release_worker, args=(job_id, raw_bytes, t_release), daemon=True
    ).start()


def _auto_stop() -> None:
    if _stream is not None:
        print(f"  auto-stop: hit {MAX_RECORD_SECS}s cap")
        _stop_and_process()


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

def _recover_job(job: dict) -> None:
    """Re-transcribe a RECORDED job found at startup. Does NOT auto-paste.

    CLEANED jobs are already durable (text is in the clean field) — surface
    them in the menu like TRANSCRIBED so the user can re-paste manually.
    """
    job_id = job["id"]
    status = job.get("status")

    # CLEANED: text already durable, nothing more to do — surface in menu.
    if status == store.CLEANED:
        print(f"  recovery: {job_id} already CLEANED — surfaced in menu")
        return

    wav_path = Path(job["wav"])
    if not wav_path.exists():
        store.update_job(job_id, status=store.ERROR, error="audio file missing")
        print(f"  recovery: {job_id} → ERROR (audio file missing)")
        return

    print(f"  recovery: transcribing {job_id}…")
    try:
        with wave.open(str(wav_path), "rb") as f:
            raw_bytes = f.readframes(f.getnframes())
        raw_np = np.frombuffer(raw_bytes, dtype=np.int16)
        audio_f32 = normalize(int16_to_float32(raw_np))
        text = _transcribe_with_retry(job_id, audio_f32)
        if text is not None:
            if not text or text == "[BLANK_AUDIO]":
                # Nothing recoverable — close the job out instead of leaving a
                # junk entry stuck in the incomplete list forever.
                store.update_job(job_id, status=store.INJECTED, raw="")
                print(f"  recovery: {job_id} → empty transcription, closed out")
            else:
                store.update_job(job_id, status=store.TRANSCRIBED, raw=text)
                print(f"  recovery: {job_id} → TRANSCRIBED ({len(text)} chars)")
    except Exception as e:
        store.update_job(job_id, status=store.ERROR, error=str(e))
        print(f"  recovery: {job_id} → ERROR ({e})")


def adopt_orphan_wavs() -> None:
    """Adopt WAV files in AUDIO_DIR not referenced by any store job.

    A crash between wav write and add_job leaves audio with no history entry.
    We adopt each orphan as a new RECORDED job so recovery can transcribe it.
    """
    if not AUDIO_DIR.exists():
        return
    known_wavs = {j["wav"] for j in store.jobs()}
    # Anything shorter than MIN_CLIP_SECS of PCM can't contain speech.
    min_bytes = 44 + int(MIN_CLIP_SECS * SAMPLE_RATE * 2)
    for wav_path in sorted(AUDIO_DIR.glob("*.wav")):
        if str(wav_path) not in known_wavs:
            if wav_path.stat().st_size < min_bytes:
                print(f"  skipping tiny orphan wav (no speech possible): {wav_path.name}")
                continue
            job_id = wav_path.stem  # filename without extension = job_id
            print(f"  adopting orphan wav: {wav_path.name}")
            store.add_job(job_id, str(wav_path))


def run_crash_recovery() -> None:
    """Check for RECORDED jobs and re-transcribe them in a background thread."""
    def _do_recovery():
        adopt_orphan_wavs()
        # Self-heal: close out any TRANSCRIBED/CLEANED job with no text (older
        # junk from empty recoveries) so it stops lingering in the menu.
        for j in store.incomplete_jobs():
            if j["status"] in (store.TRANSCRIBED, store.CLEANED) and not (
                j.get("raw") or j.get("clean")
            ):
                store.update_job(j["id"], status=store.INJECTED)
        recorded = [j for j in store.incomplete_jobs() if j["status"] == store.RECORDED]
        if not recorded:
            return
        print(f"recovering {len(recorded)} incomplete job(s)…")
        for job in recorded:
            _recover_job(job)
        global _dirty
        with _state_lock:
            _dirty = True
        print(f"recovery complete")

    threading.Thread(target=_do_recovery, daemon=True).start()


# ---------------------------------------------------------------------------
# Keyboard listener lifecycle
# ---------------------------------------------------------------------------

def _start_listener() -> keyboard.Listener:
    """Create, start, and return a new keyboard listener."""
    l = keyboard.Listener(on_press=_safe_on_press, on_release=_safe_on_release)
    l.start()
    return l


def _restart_listener() -> None:
    """Stop any existing listener and start a fresh one. Logs every restart."""
    global listener
    with _listener_lock:
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        listener = _start_listener()
    print("[hotkey] listener restarted")


def _listener_alive() -> bool:
    """Return True if the listener thread is running."""
    with _listener_lock:
        return listener is not None and listener.is_alive()


# ---------------------------------------------------------------------------
# Hotkey callbacks (pynput listener thread)
# NOTE: These are wrapped by _safe_on_press/_safe_on_release below.
#       NOTHING in on_press/on_release may ever propagate an exception —
#       an unhandled exception kills the pynput listener thread permanently.
# ---------------------------------------------------------------------------

def on_press(key):
    global _busy, _stream, _cmd_r_other_key_pressed, _cmd_r_down_time

    # Track non-cmd_r keys while cmd_r is held (for clean-tap detection).
    if _cmd_r_down_time is not None and key != keyboard.Key.cmd_r:
        _cmd_r_other_key_pressed = True

    if key == keyboard.Key.cmd_r:
        _cmd_r_down_time = time.perf_counter()
        _cmd_r_other_key_pressed = False
        return

    if key != HOTKEY:
        return

    if _stream is None:
        if not _busy:
            try:
                start_recording()
            except Exception as e:
                # start_recording already set state to error and logged.
                # Ensure stream is cleared and state is idle so next press can retry.
                print(f"  [hotkey] start_recording failed: {e} — resetting to idle")
                _stream = None
                _busy = False
                _set_state("idle")
                return
            global _auto_stop_timer
            _auto_stop_timer = threading.Timer(MAX_RECORD_SECS, _auto_stop)
            _auto_stop_timer.start()
        return

    _stop_and_process()


def on_release(key):
    global _cmd_r_down_time, _cmd_r_other_key_pressed

    if key == keyboard.Key.cmd_r and _cmd_r_down_time is not None:
        held = time.perf_counter() - _cmd_r_down_time
        _cmd_r_down_time = None
        # Clean tap: no other key pressed while held, held < 0.5s
        if not _cmd_r_other_key_pressed and held < 0.5:
            threading.Thread(target=_repaste_last, daemon=True).start()
        _cmd_r_other_key_pressed = False


def _safe_on_press(key):
    """Wrapper: catches ALL exceptions so the listener thread never dies."""
    try:
        on_press(key)
    except Exception:
        print(f"  [hotkey] on_press exception (listener protected):\n{traceback.format_exc()}")
        # Ensure state is sane after an unexpected error.
        global _busy, _stream
        _busy = False
        _stream = None
        _set_state("idle")


def _safe_on_release(key):
    """Wrapper: catches ALL exceptions so the listener thread never dies."""
    try:
        on_release(key)
    except Exception:
        print(f"  [hotkey] on_release exception (listener protected):\n{traceback.format_exc()}")


def _repaste_last() -> None:
    """Re-paste the newest job's text via clipboard + Cmd+V."""
    all_jobs = store.jobs()
    for job in all_jobs:
        text = job.get("clean") or job.get("raw")
        if text:
            inject(text)
            print("  re-pasted last dictation")
            return
    print("  re-paste: no previous dictation found")


# ---------------------------------------------------------------------------
# Reset: force-stop any recording/processing and return to idle.
# F2: if a recording was in progress, save the audio rather than discarding.
# ---------------------------------------------------------------------------

def _do_reset() -> None:
    """Force-reset all state to idle. Safe to call from any thread."""
    global _busy, _stream, _auto_stop_timer
    print("[reset] force-reset requested")

    # Cancel the auto-stop timer first so it doesn't race with us.
    if _auto_stop_timer is not None:
        _auto_stop_timer.cancel()
        _auto_stop_timer = None

    current_stream = _stream
    _stream = None  # detach immediately — teardown below may hang, this must not
    if current_stream is not None:
        # Save audio before clearing state (F2: never lose text).
        raw_bytes = b"".join(_chunks)
        clip_secs = len(raw_bytes) / 2 / SAMPLE_RATE

        _teardown_stream(current_stream)

        if clip_secs >= MIN_CLIP_SECS:
            # Save and transcribe asynchronously — don't block the reset.
            print(f"  [reset] saving {clip_secs:.1f}s of in-progress audio (F2)")
            AUDIO_DIR.mkdir(exist_ok=True)
            job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            wav_path = AUDIO_DIR / f"{job_id}.wav"
            try:
                with wave.open(str(wav_path), "wb") as f:
                    f.setnchannels(1)
                    f.setsampwidth(2)
                    f.setframerate(SAMPLE_RATE)
                    f.writeframes(raw_bytes)
                store.add_job(job_id, str(wav_path))
                t_release = time.perf_counter()
                threading.Thread(
                    target=on_release_worker, args=(job_id, raw_bytes, t_release), daemon=True
                ).start()
                print(f"  [reset] job {job_id} queued for transcription")
            except Exception as e:
                print(f"  [reset] failed to save audio: {e}")
                _busy = False
                _set_state("idle")
                return
        else:
            print(f"  [reset] discarding {clip_secs:.2f}s (too short, no speech)")
            _busy = False
            _set_state("idle")
    else:
        # No active recording — just clear stuck processing state.
        _busy = False
        _set_state("idle")

    # Restart the listener regardless, in case it died.
    _restart_listener()
    print("[reset] done — state is idle, listener restarted")


def _do_manual_restart() -> None:
    """Menu-bar Restart. Resets first so no audio dies with the process.

    Reset is not decoration here. A recording in progress lives only in the
    _chunks list in memory; _do_reset is what writes those bytes to a wav and
    registers the job, and os._exit gives no handler a chance to do it later.
    Restarting without resetting first would silently discard whatever the user
    was in the middle of saying.

    Everything already on disk is safe without our help: a job stays RECORDED
    until transcription finishes, so a kill mid-transcribe loses only the
    compute, and run_crash_recovery re-transcribes it on the way back up.

    _do_reset is bounded (its teardown abandons a wedged device after ~2s), so
    a wedged mic delays this by seconds rather than blocking it — which matters,
    because a wedged mic is the main reason to press the button at all.
    """
    print("[restart] manual restart requested from menu")
    try:
        _do_reset()
    except Exception as e:
        # Never let a failed save block the restart — the restart is the point.
        print(f"  [restart] reset before restart failed ({e}) — restarting anyway")
    _request_restart("user requested restart from menu", manual=True)


# ---------------------------------------------------------------------------
# Sleep/wake observer — proactively reinit PortAudio on wake.
# ---------------------------------------------------------------------------

def _on_wake_from_sleep() -> None:
    """Called ON THE MAIN THREAD when macOS wakes from sleep.

    Does no work here — starts a thread and returns immediately.

    macOS delivers this notification by calling us from inside its own event
    loop on the main thread, so the menu bar, every click, and even the Quit
    command are frozen until this function returns. Wake work touches
    PortAudio, which can block forever on a wedged audio device, so doing it
    inline froze the whole app for 12 days (2026-09-04). Nothing that can
    block belongs on this thread.
    """
    threading.Thread(target=_wake_work, daemon=True).start()


def _wake_work() -> None:
    """The actual wake work, on a throwaway background thread.

    Reinitializes PortAudio so the first post-wake hotkey press doesn't fail
    with a stale device list (Bug A root cause). If the audio device is wedged
    this thread is simply abandoned — one parked thread, zero CPU, app stays
    fully responsive.

    _restart_listener() is safe off the main thread: _do_reset() already calls
    it from background threads (see its "Safe to call from any thread" docstring).
    """
    print("[wake] system woke — reinitializing PortAudio")
    if not _reinit_portaudio():
        # Surviving isn't the same as working. If audio is wedged, say so in
        # the menu bar — otherwise the app looks idle and healthy while every
        # dictation silently fails, which is worse UX than an obvious freeze.
        print("[wake] audio did not come back — flagging error state")
        _set_state("error")

    if not _listener_alive():
        print("[wake] listener was dead after wake — restarting")
        _restart_listener()
    else:
        print("[wake] listener alive after wake")

    # Sleep unloads the Ollama model same as a >10min idle gap would. Warm it
    # in the background now, so the first post-wake dictation doesn't race a
    # ~4s cold load against clean()'s timeout (2026-09-01 bug).
    if _cleanup_enabled:
        threading.Thread(target=cleanup.warm, daemon=True).start()


def _register_sleep_observer() -> None:
    """Register an NSWorkspace wake notification observer on the main thread.

    The observer object is stored in _sleep_observer (module-level) to prevent
    garbage collection — Objective-C observer callbacks need a live Python ref.
    """
    global _sleep_observer
    try:
        from AppKit import NSWorkspace
        from Foundation import NSObject, NSNotificationCenter

        # Define a minimal ObjC-compatible observer class.
        class _WakeObserver(NSObject):
            def wakeFromSleep_(self, notification):
                # Dispatch to our handler; errors here must not propagate.
                try:
                    _on_wake_from_sleep()
                except Exception as e:
                    print(f"[wake] observer error: {e}")

        obs = _WakeObserver.alloc().init()
        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        nc.addObserver_selector_name_object_(
            obs,
            "wakeFromSleep:",
            "NSWorkspaceDidWakeNotification",
            None,
        )
        _sleep_observer = obs  # keep alive
        print("[wake] sleep/wake observer registered")
    except Exception as e:
        print(f"[wake] could not register sleep observer: {e}")


# ---------------------------------------------------------------------------
# rumps menu-bar app
# ---------------------------------------------------------------------------

_STATE_ICON = {
    "idle": "🎤",
    "recording": "🔴",
    "processing": "⏳",
    "error": "⚠️",
}

_STATUS_GLYPH = {
    store.RECORDED: "○",
    store.TRANSCRIBED: "◉",
    store.CLEANED: "◎",
    store.INJECTED: "●",
    store.ERROR: "⚠️",
}


def _job_title(job: dict) -> str:
    text = job.get("clean") or job.get("raw") or ""
    # First non-empty line, truncated to 40 chars
    first_line = (text.splitlines() or [""])[0][:40]
    glyph = _STATUS_GLYPH.get(job["status"], "?")
    if job["status"] == store.ERROR:
        # Surface the failure reason so Retry outcomes are visible in the menu.
        first_line = f"[{(job.get('error') or 'error')[:34]}]"
    elif not first_line:
        first_line = f"[{job['status']}]"
    return f"{glyph} {first_line}"


def _warn_if_behind_notch(frame) -> None:
    """Log loudly if our status item was placed in the notch's dead zone.

    On notched Macs the menu bar is two strips with an unusable gap between
    them. macOS does NOT reflow status items around that gap: when the bar is
    full, overflow items get positioned *under* the notch and are simply
    invisible — while still reporting isVisible=True with a correct frame.
    That cost real debugging time (2026-07-30), so say it out loud instead.
    """
    try:
        from AppKit import NSScreen
        screen = NSScreen.mainScreen()
        left, right = screen.auxiliaryTopLeftArea(), screen.auxiliaryTopRightArea()
        if left is None or right is None:
            return  # no notch on this display
        gap_start = left.origin.x + left.size.width
        gap_end = right.origin.x
        x0, x1 = frame.origin.x, frame.origin.x + frame.size.width
        if x0 < gap_end and x1 > gap_start:
            print(
                f"[ui] WARNING: icon is BEHIND THE NOTCH (x {x0:.0f}–{x1:.0f} "
                f"overlaps dead zone {gap_start:.0f}–{gap_end:.0f}) — it exists "
                "but cannot be seen. Free up menu-bar space: System Settings → "
                "Control Center → set unused items to 'Don't Show in Menu Bar'."
            )
    except Exception as e:
        print(f"[ui] notch check skipped: {e!r}")


class DictationApp(rumps.App):
    def __init__(self):
        super().__init__("🎤", quit_button=None)
        # Mark dirty so the first tick builds the menu — otherwise it's empty
        # until the first dictation.
        global _dirty
        with _state_lock:
            _dirty = True
        self._timer = rumps.Timer(self._tick, 0.15)
        self._timer.start()
        # Track when we last checked processing timeout
        self._last_state_entry: dict[str, float] = {}
        self._prev_state = "idle"

    def _tick(self, _sender):
        """Called every 0.15s on main thread; updates title and menu when dirty."""
        global _dirty, _watchdog_ticks, _last_tick_ts
        # Heartbeat first, before any other work in this function — so a stall
        # introduced anywhere below still registers as a missed beat.
        _last_tick_ts = time.monotonic()
        # One-shot status-item self-report ~2s in: diagnoses the icon silently
        # not appearing under LocalDictate.app launches, and un-hides an item whose
        # visibility was persisted off (e.g. once ⌘-dragged out of the menu bar).
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks == 14:
            try:
                from AppKit import NSApplication
                it = self._nsapp.nsstatusitem
                if not it.isVisible():
                    it.setVisible_(True)
                    print("[ui] status item was hidden — forced visible")
                win = it.button().window() if it.button() else None
                print(f"[ui] policy={NSApplication.sharedApplication().activationPolicy()} "
                      f"visible={bool(it.isVisible())} title={it.title()!r} "
                      f"frame={win.frame() if win else None}")
                if win:
                    _warn_if_behind_notch(win.frame())
            except Exception as e:
                print(f"[ui] status item report failed: {e!r}")

        # --- Watchdog: check listener + stuck-state every ~2s ---
        _watchdog_ticks += 1
        if _watchdog_ticks >= _WATCHDOG_INTERVAL_TICKS:
            _watchdog_ticks = 0
            self._watchdog_check()

        with _state_lock:
            if not _dirty:
                return
            _dirty = False
            current_state = _state

        # Track state entry time for stuck-processing detection.
        if current_state != self._prev_state:
            self._last_state_entry[current_state] = time.monotonic()
            self._prev_state = current_state

        self.title = _STATE_ICON.get(current_state, "🎤")

        # Rebuild menu: Re-paste + Reset + last 10 jobs
        menu_items = [rumps.MenuItem("Re-paste last", callback=self._repaste)]
        menu_items.append(rumps.MenuItem("🔁 Restart Local Dictate", callback=self._restart))
        menu_items.append(None)  # separator

        all_jobs = store.jobs()
        for idx, job in enumerate(all_jobs[:10], start=1):
            title = f"{idx}. {_job_title(job)}"
            item = rumps.MenuItem(title, callback=self._make_paste_cb(job))
            if job["status"] == store.ERROR:
                retry = rumps.MenuItem("↩ Retry", callback=self._make_retry_cb(job))
                discard = rumps.MenuItem("🗑 Discard", callback=self._make_discard_cb(job))
                item.update([retry, discard])
            menu_items.append(item)

        menu_items.append(None)  # separator before stats + Quit
        # Stats submenu: display-only, so the entries take no callback.
        stats_item = rumps.MenuItem("📊 Stats")
        stats_item.update([rumps.MenuItem(line) for line in stats.menu_lines()])
        menu_items.append(stats_item)

        menu_items.append(rumps.MenuItem("Quit", callback=rumps.quit_application))

        self.menu.clear()
        self.menu.update(menu_items)

    def _watchdog_check(self):
        """Run every ~2s from _tick. Cheap checks — never blocks."""
        global _busy, _stream

        # 1. Listener liveness — restart if thread has died.
        if not _listener_alive():
            print("[watchdog] listener thread dead — restarting")
            _restart_listener()

        # 2. Stuck-state: "recording" but the stream is dead or has gone
        #    silent (mic reclaimed by another app, device disconnected, etc.).
        #    A dead stream doesn't always raise — it can just stop delivering
        #    callbacks — so a "stream is None" check alone misses that case.
        #    _do_reset() itself is now hang-proof (2026-08-31 fix), so it's
        #    safe to call unconditionally here.
        current = _get_state()
        if current == "recording":
            if _stream is None:
                # No stream object at all — nothing to tear down, reset inline.
                print("[watchdog] state=recording but stream is None — resetting to idle")
                _busy = False
                _set_state("idle")
            else:
                recording_started = self._last_state_entry.get("recording", time.monotonic())
                last_activity = max(_last_audio_cb_ts, recording_started)
                if (time.monotonic() - last_activity) > 5.0:
                    # A live stream object that's gone silent — teardown may
                    # hang (device wedged), so run the hang-proof reset path
                    # off this thread rather than blocking the watchdog tick.
                    print("[watchdog] recording stream stalled — forcing reset")
                    threading.Thread(target=_do_reset, daemon=True).start()

        # 3. Stuck-state: "processing" for > 60s → reset to idle.
        #    (Transcription never takes that long; something must have thrown.)
        if current == "processing":
            entry_time = self._last_state_entry.get("processing", time.monotonic())
            if time.monotonic() - entry_time > 60:
                print("[watchdog] state=processing for >60s — resetting to idle")
                _busy = False
                _set_state("idle")

        # 4. Keep gemma warm — re-ping Ollama well inside its 10min keep_alive
        #    window so the model shouldn't go cold mid-session regardless of
        #    dictation frequency (2026-09-01 bug, see cleanup._TIMEOUT_CEILING).
        global _last_warm_ts
        if _cleanup_enabled and (time.monotonic() - _last_warm_ts) > _WARM_INTERVAL_SECS:
            _last_warm_ts = time.monotonic()
            threading.Thread(target=cleanup.warm, daemon=True).start()

    def _repaste(self, _sender):
        threading.Thread(target=_repaste_last, daemon=True).start()

    def _restart(self, _sender):
        """Menu-bar Restart button — replaces the process, not just the state.

        Reset cannot fix a wedged CoreAudio client connection; that state is
        owned by the process, so only a new process clears it. Threaded like
        _reset so this callback returns to the event loop immediately.
        """
        threading.Thread(target=_do_manual_restart, daemon=True).start()

    def _make_paste_cb(self, job: dict):
        text = job.get("clean") or job.get("raw") or ""

        def _paste(_sender):
            if not text:
                return

            def _do():
                # Let the menu close and key focus return to the previous app
                # before synthesizing Cmd+V, or the paste lands nowhere.
                time.sleep(0.2)
                inject(text)

            threading.Thread(target=_do, daemon=True).start()

        return _paste

    def _make_discard_cb(self, job: dict):
        job_id = job["id"]

        def _discard(_sender):
            store.delete_job(job_id)
            print(f"  discarded job {job_id}")
            _set_state("idle")

        return _discard

    def _make_retry_cb(self, job: dict):
        job_id = job["id"]
        wav_path = Path(job["wav"])

        def _retry(_sender):
            def _do():
                if not wav_path.exists():
                    store.update_job(job_id, status=store.ERROR, error="audio file missing")
                    _set_state("error")
                    return
                _set_state("processing")
                try:
                    with wave.open(str(wav_path), "rb") as f:
                        raw_bytes = f.readframes(f.getnframes())
                    raw_np = np.frombuffer(raw_bytes, dtype=np.int16)
                    audio_f32 = normalize(int16_to_float32(raw_np))
                    text = _transcribe_with_retry(job_id, audio_f32)
                    if text is not None:
                        store.update_job(job_id, status=store.TRANSCRIBED, raw=text)
                        _set_state("idle")
                        global _dirty
                        with _state_lock:
                            _dirty = True
                except Exception as e:
                    store.update_job(job_id, status=store.ERROR, error=str(e))
                    _set_state("error")

            threading.Thread(target=_do, daemon=True).start()

        return _retry


# ---------------------------------------------------------------------------
# Main-thread heartbeat watchdog
# ---------------------------------------------------------------------------

def _capture_self_sample(reason: str) -> None:
    """Run macOS `sample` against our own process and save the stacks to disk.

    This is the whole point of the heartbeat watchdog. Diagnosing the 2026-09-04
    freeze required a live `sample` of the wedged process — evidence that only
    exists while the process is still running, and that nobody thinks to collect
    until days later. Capturing it automatically, at the moment of the stall,
    means the next hang arrives pre-diagnosed.

    `sample` reads another process's stacks without stopping it, so it works fine
    on a deadlocked target (verified on PID 83217). Bounded and fail-open: a
    failure here must never take down the watchdog.
    """
    try:
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        out = LOG_DIR / f"stall-sample-{ts}.txt"
        subprocess.run(
            ["sample", str(os.getpid()), "3", "-file", str(out)],
            capture_output=True,
            timeout=30,
        )
        print(f"[heartbeat] stack sample written to {out} ({reason})")
    except Exception as e:
        print(f"[heartbeat] could not capture stack sample: {e!r}")


def _heartbeat_watchdog() -> None:
    """Daemon thread: log loudly when the main thread stops ticking.

    Detect-and-record only — no recovery is attempted, because there isn't one.
    A main thread blocked in Pa_Terminate is queued on a CoreAudio mutex held
    inside Apple's code; nothing this process can do releases it, and a thread
    stuck in C cannot be killed from Python. Force Quit is the only exit.

    Never raises: this thread outliving a bug is more valuable than it being
    strictly correct.
    """
    stalled_since: float | None = None
    last_logged: float = 0.0

    while True:
        before = time.monotonic()
        time.sleep(_HEARTBEAT_POLL_SECS)
        now = time.monotonic()

        # If our own sleep overshot wildly, the whole machine was suspended (or
        # the CPU was badly oversubscribed). The main thread is "late" for a
        # reason that has nothing to do with it. Reset and re-measure rather
        # than accuse it — a watchdog that false-positives on every lid close
        # gets ignored, and an ignored watchdog is worse than none.
        if now - before > _HEARTBEAT_POLL_SECS * 3:
            stalled_since = None
            continue

        last_tick = _last_tick_ts
        if last_tick == 0.0:
            continue  # rumps.Timer hasn't fired yet; nothing to compare against

        gap = now - last_tick

        if gap > _HEARTBEAT_STALL_SECS:
            if stalled_since is None:
                stalled_since = now
                last_logged = now
                print(
                    f"[heartbeat] MAIN THREAD STALLED — no tick for {gap:.1f}s. "
                    "Menu bar, hotkey and Quit are frozen; Force Quit is the only "
                    "way out (plain Quit sends an Apple Event the wedged event "
                    "loop can never process). Capturing stacks…"
                )
                _capture_self_sample(f"main thread stalled {gap:.1f}s")
            elif now - last_logged > _HEARTBEAT_RELOG_SECS:
                last_logged = now
                print(
                    f"[heartbeat] still stalled — {now - stalled_since:.0f}s "
                    "since the main thread last ticked"
                )
        elif stalled_since is not None:
            # Recovery is possible when the block was long-but-finite rather
            # than a true deadlock — worth recording, since it distinguishes
            # "slow" from "wedged" in the log after the fact.
            print(
                f"[heartbeat] main thread recovered after "
                f"{now - stalled_since:.0f}s"
            )
            stalled_since = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_cleanup_health_check() -> None:
    """Background: warm up Ollama and set _cleanup_enabled. Non-blocking for startup."""
    global _cleanup_enabled, _last_warm_ts
    ok = cleanup.health_check()
    _cleanup_enabled = ok
    _last_warm_ts = time.monotonic()
    if ok:
        print("cleanup: gemma4:e2b ready")
    else:
        print(
            "cleanup: Ollama unavailable — pasting raw transcripts "
            "(start Ollama and relaunch to enable)"
        )


if __name__ == "__main__":
    _install_timestamp_logging()

    # Bind data paths here (not at import) so tests can never touch real data.
    store.STORE_PATH = ROOT / "history.json"
    stats.STATS_PATH = ROOT / "stats.json"
    # Seed lifetime metrics from existing history on first run only (idempotent).
    stats.backfill_from_history()

    # When launched via LocalDictate.app the effective activation policy can resolve
    # to Prohibited (bundle plist vs Python.app mismatch) and macOS then never
    # attaches the NSStatusItem — no menu-bar icon. Force Accessory: menu-bar
    # app, no Dock icon. Harmless when run from a terminal.
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )

    print("Loading Whisper base.en…")
    _model = Model("base.en", print_realtime=False, print_progress=False)
    print("Whisper ready.")

    run_crash_recovery()
    # Health check runs in background so it doesn't delay the "Ready." banner.
    # First dictation during warm-up falls back to raw — acceptable.
    threading.Thread(target=_run_cleanup_health_check, daemon=True).start()

    # Register sleep/wake observer BEFORE starting the listener — must be on main thread.
    _register_sleep_observer()

    # Start pynput listener BEFORE app.run() — rumps owns the main thread.
    with _listener_lock:
        listener = _start_listener()

    # Independent of the rumps run loop on purpose — it must survive the main
    # thread deadlocking. Started last so a stall during startup isn't reported.
    threading.Thread(target=_heartbeat_watchdog, daemon=True).start()

    print(f"Ready. Tap {HOTKEY} to record. Right ⌘ to re-paste. Check menu bar.")
    DictationApp().run()
