from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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
