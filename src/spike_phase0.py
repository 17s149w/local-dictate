"""Phase 0 spike: global push-to-talk hotkey -> record mic -> paste dummy text.

Throwaway script proving the risky macOS integrations (F4) before any ML:
  1. Global hotkey capture while another app is focused (Input Monitoring perm).
  2. Mic capture while the key is held (Microphone perm).
  3. Synthesized Cmd+V into the focused app (Accessibility perm).

Setup: see docs/PERMISSIONS.md, then run:
    .venv/bin/python src/spike_phase0.py
Hold RIGHT OPTION to record, release to paste the dummy string at the cursor.
Ctrl+C in the terminal to quit.
"""

import subprocess
import time
import wave

import sounddevice as sd
from pynput import keyboard

# Right Option: reachable one-handed, almost never used as a shortcut on its own.
# If it collides for you, change it here (candidates: Key.f13, Key.cmd_r).
HOTKEY = keyboard.Key.alt_r
DUMMY_TEXT = "The quick brown fox -- dictation spike test."
SAMPLE_RATE = 16000  # what whisper expects later; good enough to verify capture
WAV_PATH = "spike_recording.wav"

chunks = []
stream = None
kb = keyboard.Controller()


def start_recording():
    global stream
    chunks.clear()
    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16",
        callback=lambda data, frames, t, status: chunks.append(bytes(data)),
    )
    stream.start()
    print("* recording...")


def stop_and_paste():
    global stream
    stream.stop()
    stream.close()
    stream = None

    audio = b"".join(chunks)
    with wave.open(WAV_PATH, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(audio)
    print(f"  stopped: {len(audio) / 2 / SAMPLE_RATE:.1f}s audio -> {WAV_PATH}")

    # Baseline for the F1 latency ledger: clipboard-set + keystroke overhead.
    t0 = time.perf_counter()
    subprocess.run("pbcopy", input=DUMMY_TEXT.encode())
    with kb.pressed(keyboard.Key.cmd):
        kb.press("v")
        kb.release("v")
    print(f"  Cmd+V sent ({(time.perf_counter() - t0) * 1000:.0f}ms paste overhead)")


def on_press(key):
    if key == HOTKEY and stream is None:
        start_recording()


def on_release(key):
    if key == HOTKEY and stream is not None:
        stop_and_paste()


print(f"Hold {HOTKEY} to record; release to paste dummy text. Ctrl+C to quit.")
with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()
