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


def test_install_hook_rejects_non_object_settings(fake_home, monkeypatch, capsys):
    monitor = _load_monitor_module()
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("[]\n")

    rc = monitor.install_hook()

    assert rc == 3
    err = capsys.readouterr().err
    assert "must contain a JSON object" in err


def test_install_hook_rejects_invalid_json_settings(fake_home, monkeypatch, capsys):
    monitor = _load_monitor_module()
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not-json}\n")

    rc = monitor.install_hook()

    assert rc == 3
    err = capsys.readouterr().err
    assert "is not valid JSON" in err


def test_install_hook_writes_hook_and_settings(fake_home, monkeypatch):
    monitor = _load_monitor_module()

    rc = monitor.install_hook()

    assert rc == 0
    hook_name = "cc-monitor-hook.ps1" if os.name == "nt" else "cc-monitor-hook.sh"
    hook_path = fake_home / ".claude" / hook_name
    settings_path = fake_home / ".claude" / "settings.json"
    assert hook_path.exists()
    assert settings_path.exists()
    assert '"command": "' in settings_path.read_text()


# ---------------------------------------------------------------------------
# Platform-specific install behaviour
# ---------------------------------------------------------------------------

def test_install_hook_missing_source_returns_2(fake_home, monkeypatch):
    """install_hook returns 2 when the hook source file is absent (any platform)."""
    monitor = _load_monitor_module()
    absent_src = fake_home / "src" / "cc-monitor-hook.sh"  # never created
    fake_dest = fake_home / ".claude" / "cc-monitor-hook.sh"
    monkeypatch.setattr(monitor, "_hook_src_and_dest", lambda here: (absent_src, fake_dest, str(fake_dest)))

    rc = monitor.install_hook()

    assert rc == 2


@pytest.mark.skipif(sys.platform == "win32", reason="tests POSIX path on POSIX only")
def test_hook_src_and_dest_posix(fake_home, monkeypatch):
    """On POSIX, _hook_src_and_dest returns the .sh hook and a bare command path."""
    monitor = _load_monitor_module()
    src, dest, command = monitor._hook_src_and_dest(Path(monitor.__file__).parent)
    assert src.name == "cc-monitor-hook.sh"
    assert dest.name == "cc-monitor-hook.sh"
    assert "powershell" not in command
    assert command == str(dest)


@pytest.mark.skipif(sys.platform != "win32", reason="tests Windows path on Windows only")
def test_hook_src_and_dest_windows(fake_home, monkeypatch):
    """On Windows, _hook_src_and_dest returns the .ps1 hook and a powershell command."""
    monitor = _load_monitor_module()
    src, dest, command = monitor._hook_src_and_dest(Path(monitor.__file__).parent)
    assert src.name == "cc-monitor-hook.ps1"
    assert dest.name == "cc-monitor-hook.ps1"
    assert "powershell" in command.lower()
    assert str(dest) in command


@pytest.mark.skipif(sys.platform != "win32", reason="tests Windows install on Windows only")
def test_install_hook_windows_installs_ps1(fake_home, monkeypatch):
    """On Windows, install_hook copies the .ps1 file and writes a powershell command."""
    monitor = _load_monitor_module()
    rc = monitor.install_hook()

    assert rc == 0
    ps1_path = fake_home / ".claude" / "cc-monitor-hook.ps1"
    settings_path = fake_home / ".claude" / "settings.json"
    assert ps1_path.exists(), "cc-monitor-hook.ps1 should be installed"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    command = settings["statusLine"]["command"]
    assert "powershell" in command.lower()
    assert str(ps1_path) in command


@pytest.mark.skipif(sys.platform != "win32", reason="tests Windows missing ps1 on Windows only")
def test_install_hook_windows_missing_ps1_returns_2(fake_home, monkeypatch):
    """install_hook returns 2 when the .ps1 source file is absent (Windows)."""
    monitor = _load_monitor_module()
    # Return a .ps1 src path that does not exist so install_hook hits the rc==2 branch.
    absent_src = fake_home / "src" / "cc-monitor-hook.ps1"  # never created
    fake_dest = fake_home / ".claude" / "cc-monitor-hook.ps1"
    fake_cmd = f'powershell -NoProfile -NonInteractive -File "{fake_dest}"'
    monkeypatch.setattr(monitor, "_hook_src_and_dest", lambda here: (absent_src, fake_dest, fake_cmd))

    rc = monitor.install_hook()

    assert rc == 2
