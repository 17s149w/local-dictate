"""Safety net: no test may ever touch the user's real data files.

Both store.STORE_PATH and stats.STATS_PATH default to repo-relative paths, so
a test that forgets to monkeypatch them writes into the REAL history.json /
stats.json. That actually happened: the F2 durability test wrote junk "f2test"
jobs into history.json, and because the store caps at 50 jobs it evicted real
dictations. Data loss from running the test suite is an F2 violation.

This autouse fixture repoints both at a per-test tmp dir. Tests that need to
inspect the file still monkeypatch to their own tmp_path as before; this only
guarantees the *default* can never be the real thing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True)
def _isolate_data_paths(tmp_path, monkeypatch):
    import stats
    import store

    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "history.json")
    monkeypatch.setattr(stats, "STATS_PATH", tmp_path / "stats.json")
