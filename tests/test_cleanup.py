"""Tests for src/cleanup.py and Phase 3 store additions (F2/F3).

Uses a stdlib http.server on a random localhost port — never hits real Ollama.
All tests run in <5s total.

Run: .venv/bin/python -m pytest tests/test_cleanup.py -q
"""

import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import cleanup
import store


# ---------------------------------------------------------------------------
# Shared: tiny HTTP server that serves a fixed response or error
# ---------------------------------------------------------------------------

def _make_server(handler_class):
    """Bind to an OS-assigned port; return (server, url)."""
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/api/generate"
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    return server, url, t


def _json_handler(response_body: dict, status: int = 200):
    """Return a handler class that replies with given JSON and status."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            # Drain the request body so the client doesn't get a broken-pipe.
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps(response_body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # suppress server noise in test output

    return Handler


def _slow_handler(delay: float):
    """Return a handler that sleeps before responding (for timeout tests)."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            time.sleep(delay)
            body = json.dumps({"response": "late reply"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


# ---------------------------------------------------------------------------
# cleanup.clean() tests
# ---------------------------------------------------------------------------

def test_clean_success(monkeypatch):
    """(a) Normal success: returns stripped response text."""
    server, url, _ = _make_server(_json_handler({"response": "  Cleaned text.  "}))
    monkeypatch.setattr(cleanup, "URL", url)
    result = cleanup.clean("raw input")
    assert result == "Cleaned text."
    server.server_close()


def test_clean_server_500(monkeypatch):
    """(b) Server returns 500 → None (fail open)."""
    server, url, _ = _make_server(_json_handler({"error": "boom"}, status=500))
    monkeypatch.setattr(cleanup, "URL", url)
    result = cleanup.clean("some text")
    assert result is None
    server.server_close()


def test_clean_connection_refused(monkeypatch):
    """(c) Connection refused (dead port) → None."""
    # Bind and immediately close to get a port guaranteed to be unused.
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(cleanup, "URL", f"http://127.0.0.1:{port}/api/generate")
    result = cleanup.clean("hello")
    assert result is None


def test_clean_timeout(monkeypatch):
    """(d) Response slower than timeout → None."""
    server, url, _ = _make_server(_slow_handler(1.0))
    monkeypatch.setattr(cleanup, "URL", url)
    # Pass a short timeout so the test is fast; the handler sleeps 1s.
    result = cleanup.clean("hello", timeout=0.3)
    assert result is None
    server.server_close()


def test_clean_empty_response(monkeypatch):
    """(e) Empty response string → None."""
    server, url, _ = _make_server(_json_handler({"response": "   "}))
    monkeypatch.setattr(cleanup, "URL", url)
    result = cleanup.clean("some text")
    assert result is None
    server.server_close()


def test_clean_rambling_response(monkeypatch):
    """(f) Response longer than 2*len(raw)+40 → None (model rambled)."""
    raw = "hi"  # len 2 → max allowed = 2*2+40 = 44 chars
    long_response = "x" * 200
    server, url, _ = _make_server(_json_handler({"response": long_response}))
    monkeypatch.setattr(cleanup, "URL", url)
    result = cleanup.clean(raw)
    assert result is None
    server.server_close()


# ---------------------------------------------------------------------------
# store: CLEANED status
# ---------------------------------------------------------------------------

def test_cleaned_in_incomplete_statuses():
    """CLEANED must be in INCOMPLETE_STATUSES (F2: durable but not terminal)."""
    assert store.CLEANED in store.INCOMPLETE_STATUSES


def test_update_job_persists_clean_field(tmp_path, monkeypatch):
    """update_job with clean= field is persisted to disk and readable back."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")
    monkeypatch.setattr(store, "_lock", __import__("threading").Lock())

    store.add_job("cj1", "audio/cj1.wav")
    store.update_job("cj1", status=store.CLEANED, clean="Cleaned version.")

    j = store.get_job("cj1")
    assert j["status"] == store.CLEANED
    assert j["clean"] == "Cleaned version."

    # Verify on-disk persistence (re-read raw JSON)
    raw = json.loads((tmp_path / "history.json").read_text())
    disk_job = next(x for x in raw["jobs"] if x["id"] == "cj1")
    assert disk_job["status"] == store.CLEANED
    assert disk_job["clean"] == "Cleaned version."


def test_warm_never_raises_on_dead_server(monkeypatch):
    """warm() is fire-and-forget: a dead Ollama must not raise."""
    monkeypatch.setattr(cleanup, "URL", "http://localhost:1/api/generate")
    cleanup.warm()  # no exception = pass


# ---------------------------------------------------------------------------
# _timeout_for: scaled timeout
# ---------------------------------------------------------------------------

def test_timeout_for_short():
    """Short transcript (~100 chars) gets base + small increment."""
    t = cleanup._timeout_for("a" * 100)
    assert t == pytest.approx(1.0 + 100 * 0.001)
    assert t < 2.0


def test_timeout_for_medium():
    """Medium transcript (~1000 chars) scales linearly."""
    t = cleanup._timeout_for("a" * 1000)
    assert t == pytest.approx(1.0 + 1000 * 0.001)
    assert t == pytest.approx(2.0)


def test_timeout_for_long_capped():
    """Very long transcript (>2500 chars) is capped at ceiling."""
    t = cleanup._timeout_for("a" * 10000)
    assert t == pytest.approx(cleanup._TIMEOUT_CEILING)


def test_timeout_for_empty():
    """Empty string uses the base timeout."""
    t = cleanup._timeout_for("")
    assert t == pytest.approx(cleanup._TIMEOUT_BASE)


def test_timeout_ceiling_under_hard_budget():
    """Ceiling must be below the 4s hard budget to leave room for transcription."""
    assert cleanup._TIMEOUT_CEILING < 4.0


def test_clean_uses_scaled_timeout(monkeypatch):
    """clean() with no explicit timeout uses _timeout_for, not a fixed constant."""
    captured = []

    original_urlopen = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        captured.append(timeout)
        return original_urlopen(req, timeout=timeout)

    # Use a server that responds immediately with a valid result.
    server, url, _ = _make_server(_json_handler({"response": "ok"}))
    monkeypatch.setattr(cleanup, "URL", url)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    raw = "a" * 500
    cleanup.clean(raw)
    assert len(captured) == 1
    expected = cleanup._timeout_for(raw)
    assert captured[0] == pytest.approx(expected)
    server.server_close()


# ---------------------------------------------------------------------------
# Truncation guard (F3: never paste a silently shortened dictation)
# ---------------------------------------------------------------------------

def test_looks_truncated_flags_dropped_clause():
    """The real gemma failure: everything after a punctuation command dropped."""
    raw = "we should ship it exclamation point that is bold huge end bold news"
    assert cleanup.looks_truncated(raw, "We should ship it!") is True


def test_looks_truncated_allows_filler_and_command_removal():
    """Removing fillers + command words is expected, not truncation."""
    raw = "um so the meeting is uh at three pm tomorrow period"
    assert cleanup.looks_truncated(raw, "So the meeting is at 3 pm tomorrow.") is False


def test_looks_truncated_ignores_short_input():
    """Under ~6 content words, normal rewording dominates — don't guess."""
    assert cleanup.looks_truncated("hello there", "Hi.") is False


def test_looks_truncated_allows_full_clean():
    raw = "the quarterly numbers look strong and the team is ahead of schedule"
    assert cleanup.looks_truncated(
        raw, "The quarterly numbers look strong and the team is ahead of schedule."
    ) is False


def test_looks_truncated_allows_heavy_stutter_removal():
    """2026-09-01 bug: several stutter pairs must not read as dropped content."""
    raw = "the the the meeting got moved to to Thursday"
    assert cleanup.looks_truncated(raw, "The meeting got moved to Thursday.") is False


def test_payload_word_count_collapses_stutters():
    assert cleanup._payload_word_count("the the the meeting got moved to to Thursday") == 6


def test_clean_returns_none_on_truncated_response(monkeypatch):
    """clean() fails open to raw when the model drops content."""
    raw = "we should ship it exclamation point that is bold huge end bold news"
    server, url, _ = _make_server(_json_handler({"response": "We should ship it!"}))
    monkeypatch.setattr(cleanup, "URL", url)
    assert cleanup.clean(raw) is None
    server.server_close()
