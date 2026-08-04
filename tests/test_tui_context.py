"""Tests for the TUI's context-window handling (CC v2.1.132+ semantics).

As of Claude Code v2.1.132 the hook's context_window.total_input_tokens /
total_output_tokens report *current context-window occupancy*, not cumulative
session totals. The monitor must therefore:
  * derive cumulative Input/Output from the JSONL transcript (merge-by-id), and
  * surface the hook's current-window numbers in the separate "Ctx" column.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path


def _load_monitor_module():
    root = Path(__file__).resolve().parent.parent
    module_path = root / "cc-session-monitor.py"
    spec = importlib.util.spec_from_file_location("cc_session_monitor", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _cells(table, col_index):
    """Plain-text contents of one rendered column."""
    out = []
    for cell in table.columns[col_index]._cells:
        out.append(cell.plain if hasattr(cell, "plain") else str(cell))
    return out


# ---------------------------------------------------------------------------
# _fmt_ctx
# ---------------------------------------------------------------------------

def test_fmt_ctx_shows_used_over_size_with_color():
    m = _load_monitor_module()
    txt = m._fmt_ctx(78_000, 200_000, 39.0)
    assert txt.plain == "78.0K/200.0K"
    assert str(txt.style) == "green"


def test_fmt_ctx_color_thresholds():
    m = _load_monitor_module()
    assert str(m._fmt_ctx(1, 200_000, 49.9).style) == "green"
    assert str(m._fmt_ctx(1, 200_000, 50.0).style) == "yellow"
    assert str(m._fmt_ctx(1, 200_000, 80.0).style) == "red"


def test_fmt_ctx_none_used_renders_dash():
    m = _load_monitor_module()
    txt = m._fmt_ctx(None, None, None)
    assert txt.plain == "—"
    assert str(txt.style) == "dim"


def test_fmt_ctx_without_size_shows_used_only():
    m = _load_monitor_module()
    txt = m._fmt_ctx(12_000, None, 10.0)
    assert txt.plain == "12.0K"


# ---------------------------------------------------------------------------
# _apply_snapshot captures context_window_size
# ---------------------------------------------------------------------------

def test_apply_snapshot_captures_context_window_size():
    m = _load_monitor_module()
    monitor = m.Monitor.__new__(m.Monitor)
    monitor.sessions = {}
    monitor._snapshot_mtimes = {}
    monitor._apply_snapshot({
        "session_id": "abc",
        "snapshot_ts": 1000.0,
        "context_window": {
            "used_percentage": 39.0,
            "total_input_tokens": 78_000,
            "total_output_tokens": 200,
            "context_window_size": 200_000,
        },
        "cost": {"total_cost_usd": 4.2},
    })
    state = monitor.sessions["abc"]
    assert state.hook_ctx_size == 200_000
    assert state.hook_ctx_input == 78_000


# ---------------------------------------------------------------------------
# build_table: Input/Output come from JSONL, Ctx from the hook
# ---------------------------------------------------------------------------

def test_build_table_input_output_use_jsonl_not_hook():
    m = _load_monitor_module()
    now = time.time()

    state = m.SessionState(session_id="deadbeefcafe", project="proj",
                           jsonl_path=Path("/tmp/x.jsonl"))
    # JSONL-derived cumulative usage.
    state.samples["req1"] = m.UsageSample(
        ts=now, input_tokens=1000, output_tokens=50,
        cache_creation=0, cache_read=300)
    state.last_ts = now
    # Hook current-context values that MUST NOT leak into Input/Output.
    state.hook_ts = now
    state.hook_cost_usd = 4.2
    state.hook_ctx_input = 78_000
    state.hook_ctx_output = 200
    state.hook_ctx_size = 200_000
    state.hook_ctx_pct = 39.0

    table = m.build_table("t", [state], now, velocity_window=30)

    # Column order: 0 Session,1 Project,2 Age,3 Input,4 Output,
    #               5 Cache R,6 Total,7 Ctx,8 t/s,9 out/s,10 Cost,11 $/h
    input_cells = _cells(table, 3)
    output_cells = _cells(table, 4)
    ctx_cells = _cells(table, 7)

    # First row is the session; last row is the TOTAL footer.
    assert input_cells[0] == "1.0K"          # JSONL 1000, NOT hook 78_000
    assert output_cells[0] == "50"           # JSONL 50,  NOT hook 200
    assert ctx_cells[0] == "78.0K/200.0K"    # hook current-window gauge


def test_build_table_ctx_dash_when_no_hook():
    m = _load_monitor_module()
    now = time.time()
    state = m.SessionState(session_id="nohookhere00", project="proj",
                           jsonl_path=Path("/tmp/y.jsonl"))
    state.samples["req1"] = m.UsageSample(ts=now, input_tokens=10, output_tokens=5)
    state.last_ts = now
    # No hook_ts / hook_ctx_* set.

    table = m.build_table("t", [state], now, velocity_window=30)
    assert _cells(table, 7)[0] == "—"


# ---------------------------------------------------------------------------
# SessionState.effort — captured from the JSONL during refresh
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _assistant(effort: str | None, ts: str, req_id: str, with_usage: bool = True) -> dict:
    entry: dict = {"type": "assistant", "timestamp": ts, "requestId": req_id}
    if effort is not None:
        entry["effort"] = effort
    if with_usage:
        entry["message"] = {
            "id": req_id,
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }
    return entry


def test_refresh_captures_effort_from_newest_assistant_line(tmp_path):
    m = _load_monitor_module()
    proj = tmp_path / "projects" / "-Users-x-myproj"
    proj.mkdir(parents=True)
    _write_jsonl(proj / "sess1234abcd.jsonl", [
        _assistant("medium", "2026-08-04T10:00:00Z", "r1"),
        _assistant("xhigh", "2026-08-04T10:05:00Z", "r2"),
    ])

    mon = m.Monitor(root=tmp_path / "projects", snapshot_dir=tmp_path / "snaps")
    mon.refresh()

    assert mon.sessions["sess1234abcd"].effort == "xhigh"


def test_refresh_captures_effort_from_entry_without_usage(tmp_path):
    """An assistant entry with no usage block must still contribute its effort.

    This pins the call site *before* the `if sample is None: continue` guard in
    the refresh loop.
    """
    m = _load_monitor_module()
    proj = tmp_path / "projects" / "-Users-x-myproj"
    proj.mkdir(parents=True)
    _write_jsonl(proj / "sess5678efgh.jsonl", [
        _assistant("medium", "2026-08-04T10:00:00Z", "r1"),
        _assistant("max", "2026-08-04T10:05:00Z", "r2", with_usage=False),
    ])

    mon = m.Monitor(root=tmp_path / "projects", snapshot_dir=tmp_path / "snaps")
    mon.refresh()

    assert mon.sessions["sess5678efgh"].effort == "max"


def test_refresh_leaves_effort_none_when_jsonl_has_no_effort(tmp_path):
    m = _load_monitor_module()
    proj = tmp_path / "projects" / "-Users-x-myproj"
    proj.mkdir(parents=True)
    _write_jsonl(proj / "sess9999zzzz.jsonl", [
        _assistant(None, "2026-08-04T10:00:00Z", "r1"),
    ])

    mon = m.Monitor(root=tmp_path / "projects", snapshot_dir=tmp_path / "snaps")
    mon.refresh()

    assert mon.sessions["sess9999zzzz"].effort is None
