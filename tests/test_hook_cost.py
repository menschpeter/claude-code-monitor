from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class HookRun:
    snapshot: dict
    status_line: str

    def __getitem__(self, key):
        return self.snapshot[key]


def _payload(cost: float) -> dict:
    return {
        "session_id": "cost-test",
        "workspace": {"current_dir": "/tmp/project"},
        "model": {"display_name": "Opus"},
        "cost": {"total_cost_usd": cost},
        "context_window": {},
    }


def _payload_without_cost() -> dict:
    payload = _payload(0.0)
    payload["cost"] = {}
    return payload


def _run_posix_hook(tmp_path: Path, payload: dict) -> HookRun:
    if os.name == "nt":
        pytest.skip("POSIX hook test")
    snapshot_dir = tmp_path / "snapshots"
    result = subprocess.run(
        [str(ROOT / "cc-monitor-hook.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "CC_MONITOR_SNAPSHOT_DIR": str(snapshot_dir)},
    )
    snapshot = json.loads((snapshot_dir / "cost-test.json").read_text())
    return HookRun(snapshot=snapshot, status_line=result.stdout)


def _run_windows_hook(tmp_path: Path, payload: dict) -> HookRun:
    if os.name != "nt":
        pytest.skip("Windows PowerShell hook test")
    snapshot_dir = tmp_path / "snapshots"
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "cc-monitor-hook.ps1"),
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "CC_MONITOR_SNAPSHOT_DIR": str(snapshot_dir)},
    )
    snapshot = json.loads(
        (snapshot_dir / "cost-test.json").read_text(encoding="utf-8-sig")
    )
    return HookRun(snapshot=snapshot, status_line=result.stdout)


def _write_legacy_snapshot(tmp_path: Path, raw_cost: float) -> None:
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "cost-test.json").write_text(
        json.dumps(
            {
                "session_id": "cost-test",
                "snapshot_ts": 1.0,
                "cost": {"total_cost_usd": raw_cost},
            }
        )
    )


def test_posix_hook_normalizes_counter_reset(tmp_path):
    first = _run_posix_hook(tmp_path, _payload(10.0))
    second = _run_posix_hook(tmp_path, _payload(12.0))
    reset = _run_posix_hook(tmp_path, _payload(0.5))
    final = _run_posix_hook(tmp_path, _payload(1.0))

    assert [
        first["cost"]["observed_total_cost_usd"],
        second["cost"]["observed_total_cost_usd"],
        reset["cost"]["observed_total_cost_usd"],
        final["cost"]["observed_total_cost_usd"],
    ] == [10.0, 12.0, 12.5, 13.0]
    assert final["cost"]["counter_resets"] == 1


def test_posix_hook_preserves_missing_cost_as_null(tmp_path):
    snap = _run_posix_hook(tmp_path, _payload_without_cost())
    assert snap["cost"]["total_cost_usd"] is None
    assert snap["cost"]["observed_total_cost_usd"] is None
    assert "est \N{EM DASH}" in snap.status_line


def test_posix_hook_resumes_from_last_valid_state_after_missing_cost(tmp_path):
    _run_posix_hook(tmp_path, _payload(7.0))
    missing = _run_posix_hook(tmp_path, _payload_without_cost())
    resumed = _run_posix_hook(tmp_path, _payload(0.5))

    assert missing["cost"]["last_valid_raw_cost_usd"] == 7.0
    assert resumed["cost"]["observed_total_cost_usd"] == 7.5
    assert resumed["cost"]["counter_resets"] == 1


def test_posix_hook_upgrades_legacy_snapshot(tmp_path):
    _write_legacy_snapshot(tmp_path, raw_cost=4.0)
    snap = _run_posix_hook(tmp_path, _payload(5.5))
    assert snap["cost"]["observed_total_cost_usd"] == 5.5


def test_posix_hook_ignores_corrupt_previous_snapshot(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "cost-test.json").write_text("not-json")
    snap = _run_posix_hook(tmp_path, _payload(2.0))
    assert snap["cost"]["observed_total_cost_usd"] == 2.0


def test_posix_hook_rejects_non_numeric_cost(tmp_path):
    payload = _payload(0.0)
    payload["cost"]["total_cost_usd"] = "unknown"
    snap = _run_posix_hook(tmp_path, payload)
    assert snap["cost"]["total_cost_usd"] is None
    assert snap["cost"]["observed_total_cost_usd"] is None


def test_windows_hook_normalizes_counter_reset(tmp_path):
    first = _run_windows_hook(tmp_path, _payload(10.0))
    second = _run_windows_hook(tmp_path, _payload(12.0))
    reset = _run_windows_hook(tmp_path, _payload(0.5))
    final = _run_windows_hook(tmp_path, _payload(1.0))

    assert [
        first["cost"]["observed_total_cost_usd"],
        second["cost"]["observed_total_cost_usd"],
        reset["cost"]["observed_total_cost_usd"],
        final["cost"]["observed_total_cost_usd"],
    ] == [10.0, 12.0, 12.5, 13.0]
    assert final["cost"]["counter_resets"] == 1


def test_windows_hook_preserves_missing_cost_as_null(tmp_path):
    snap = _run_windows_hook(tmp_path, _payload_without_cost())
    assert snap["cost"]["total_cost_usd"] is None
    assert snap["cost"]["observed_total_cost_usd"] is None
    assert "est \N{EM DASH}" in snap.status_line
