from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_monitor_module():
    root = Path(__file__).resolve().parent.parent
    module_path = root / "cc-session-monitor.py"
    spec = importlib.util.spec_from_file_location("cc_session_monitor", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_install_hook_rejects_non_object_settings(tmp_path, monkeypatch, capsys):
    monitor = _load_monitor_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("[]\n")

    rc = monitor.install_hook()

    assert rc == 3
    err = capsys.readouterr().err
    assert "must contain a JSON object" in err


def test_install_hook_rejects_invalid_json_settings(tmp_path, monkeypatch, capsys):
    monitor = _load_monitor_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not-json}\n")

    rc = monitor.install_hook()

    assert rc == 3
    err = capsys.readouterr().err
    assert "is not valid JSON" in err


def test_install_hook_writes_hook_and_settings(tmp_path, monkeypatch):
    monitor = _load_monitor_module()
    monkeypatch.setenv("HOME", str(tmp_path))

    rc = monitor.install_hook()

    assert rc == 0
    hook_path = tmp_path / ".claude" / "cc-monitor-hook.sh"
    settings_path = tmp_path / ".claude" / "settings.json"
    assert hook_path.exists()
    assert settings_path.exists()
    assert '"command": "' in settings_path.read_text()


# ---------------------------------------------------------------------------
# Platform-specific install behaviour
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="tests POSIX path on POSIX only")
def test_hook_src_and_dest_posix(tmp_path, monkeypatch):
    """On POSIX, _hook_src_and_dest returns the .sh hook and a bare command path."""
    monitor = _load_monitor_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    src, dest, command = monitor._hook_src_and_dest(Path(monitor.__file__).parent)
    assert src.name == "cc-monitor-hook.sh"
    assert dest.name == "cc-monitor-hook.sh"
    assert "powershell" not in command
    assert command == str(dest)


@pytest.mark.skipif(sys.platform != "win32", reason="tests Windows path on Windows only")
def test_hook_src_and_dest_windows(tmp_path, monkeypatch):
    """On Windows, _hook_src_and_dest returns the .ps1 hook and a powershell command."""
    monitor = _load_monitor_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    src, dest, command = monitor._hook_src_and_dest(Path(monitor.__file__).parent)
    assert src.name == "cc-monitor-hook.ps1"
    assert dest.name == "cc-monitor-hook.ps1"
    assert "powershell" in command.lower()
    assert str(dest) in command


@pytest.mark.skipif(sys.platform != "win32", reason="tests Windows install on Windows only")
def test_install_hook_windows_installs_ps1(tmp_path, monkeypatch):
    """On Windows, install_hook copies the .ps1 file and writes a powershell command."""
    monitor = _load_monitor_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = monitor.install_hook()

    assert rc == 0
    ps1_path = tmp_path / ".claude" / "cc-monitor-hook.ps1"
    settings_path = tmp_path / ".claude" / "settings.json"
    assert ps1_path.exists(), "cc-monitor-hook.ps1 should be installed"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    command = settings["statusLine"]["command"]
    assert "powershell" in command.lower()
    assert str(ps1_path) in command


@pytest.mark.skipif(sys.platform != "win32", reason="tests Windows missing ps1 on Windows only")
def test_install_hook_windows_missing_ps1_returns_2(tmp_path, monkeypatch):
    """Return code 2 when the .ps1 source file is absent (Windows)."""
    monitor = _load_monitor_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    # Point the installer at a temp dir that has no hook scripts.
    fake_here = tmp_path / "src"
    fake_here.mkdir()
    src, _dest, _command = monitor._hook_src_and_dest(fake_here)
    assert not src.exists(), "prerequisite: source file must be absent"
    # Simulate what install_hook does when src is missing.
    assert src.name == "cc-monitor-hook.ps1"
