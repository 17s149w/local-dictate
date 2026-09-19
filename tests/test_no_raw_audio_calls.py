"""Enforce the audio chokepoint rule by parsing app.py.

Every PortAudio/CoreAudio call can block forever on a mutex held inside Apple's
audio stack. A thread parked in C never returns to the interpreter loop, so it
honours no timeout and try/except cannot save it — a hang is not an exception.
The only way to bound such a call is to run it somewhere abandonable, which is
what app._bounded() does.

That rule was learned and re-learned three times, each time fixed only at the
call site that happened to bite:

    2026-08-31  Pa_CloseStream  wedged -> Reset silently did nothing
    2026-09-04  Pa_Terminate    wedged the main thread -> 12-day freeze
    2026-09-15  Pa_OpenStream   wedged the hotkey listener -> app bricked

Docstrings only help whoever reads them. This test is the version of that
knowledge the build can enforce, so the fourth occurrence fails CI instead of
shipping.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "src" / "app.py"

# sounddevice calls are legal only inside a closure handed to _bounded().
# By convention those closures are named _do (mutating calls) or _open
# (constructing a stream). Both are nested functions, never called directly.
APPROVED_WRAPPERS = {"_do", "_open"}


def _sd_calls_with_scope(tree):
    """Yield (attribute, enclosing-function-name) for every `sd.<x>` use."""
    found = []

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "sd"
            ):
                found.append((child.attr, scope))
            walk(child, scope)

    walk(tree, "<module>")
    return found


def test_all_sounddevice_calls_go_through_bounded():
    calls = _sd_calls_with_scope(ast.parse(APP.read_text()))
    assert calls, "found no sd.* calls at all — has the audio layer moved?"

    offenders = [
        f"sd.{attr} in {scope}()"
        for attr, scope in calls
        if scope not in APPROVED_WRAPPERS
    ]
    assert not offenders, (
        "Unbounded sounddevice call(s) found:\n  "
        + "\n  ".join(offenders)
        + "\n\nEvery PortAudio call can block forever on a wedged device and "
        "cannot be interrupted. Wrap it in a closure named _do/_open and run "
        "it through app._bounded(label, timeout, fn). See _bounded's docstring."
    )


def test_bounded_helper_still_exists():
    """The rule above is meaningless if the helper is renamed or deleted."""
    tree = ast.parse(APP.read_text())
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for required in ("_bounded", "_note_audio_wedge", "_request_restart"):
        assert required in names, f"{required}() is gone — audio hardening broken"
