"""Shared pytest fixtures for claude-code-monitor tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable so tests can `import cc_history`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to tmp_path on every platform.

    Path.home() reads $HOME on POSIX but %USERPROFILE% (falling back to
    %HOMEDRIVE%\\%HOMEPATH%) on Windows.  Setting only HOME is not sufficient
    for cross-platform test isolation.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", tmp_path.drive or "")
    monkeypatch.setenv("HOMEPATH", str(tmp_path)[len(tmp_path.drive):])
    return tmp_path
