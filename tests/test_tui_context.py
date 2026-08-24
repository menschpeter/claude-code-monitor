"""Tests for the TUI's context-window handling (CC v2.1.132+ semantics) and
the effort-level footer line.

As of Claude Code v2.1.132 the hook's context_window.total_input_tokens /
total_output_tokens report *current context-window occupancy*, not cumulative
session totals. The monitor must therefore:
  * derive cumulative Input/Output from the JSONL transcript (merge-by-id), and
  * surface the hook's current-window numbers in the separate "Ctx" column.

This file also covers `SessionState.effort`, `_fmt_effort_footer`, and the
footer's rendered layout, including at fixed terminal widths (see the
render-through-a-Console tests near the bottom).
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

from rich.console import Console


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


def _local_ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute).timestamp()


def _state_started_at(m, ts):
    state = m.SessionState("s", "proj", Path("/tmp/s.jsonl"))
    state.first_ts = ts
    state.last_ts = ts
    return state


def test_cost_observations_accumulate_by_local_date():
    m = _load_monitor_module()
    state = _state_started_at(m, _local_ts(2026, 8, 24, 9, 0))

    state.record_cost_observation(
        _local_ts(2026, 8, 24, 9, 5), 10.0, 10.0, 0
    )
    state.record_cost_observation(
        _local_ts(2026, 8, 24, 10, 0), 12.5, 0.5, 1
    )

    assert state.estimated_cost_by_date["2026-08-24"] == 12.5


def test_old_session_without_baseline_has_unknown_today_cost():
    m = _load_monitor_module()
    state = _state_started_at(m, _local_ts(2026, 8, 23, 9, 0))

    state.record_cost_observation(
        _local_ts(2026, 8, 24, 9, 0), 12.5, 0.5, 1
    )

    assert state.estimated_cost_by_date["2026-08-24"] is None


def test_restored_daily_baseline_adds_only_new_delta(tmp_path):
    m = _load_monitor_module()
    entry = m.DailySessionEntry(
        project="proj",
        model="Opus",
        first_ts=1.0,
        last_ts=2.0,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        session_cumulative_cost_usd=0.5,
        estimated_cost_usd=4.0,
        last_observed_total_cost_usd=10.0,
        last_cost_ts=_local_ts(2026, 8, 24, 10, 0),
    )
    project = tmp_path / "projects" / "-tmp-proj"
    project.mkdir(parents=True)
    _write_jsonl(
        project / "s.jsonl",
        [_assistant("high", "2026-08-24T09:00:00Z", "r1")],
    )
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "s.json").write_text(
        json.dumps(
            {
                "session_id": "s",
                "snapshot_ts": _local_ts(2026, 8, 24, 11, 0),
                "cost": {
                    "total_cost_usd": 1.0,
                    "observed_total_cost_usd": 11.5,
                    "counter_resets": 1,
                },
            }
        )
    )
    monitor = m.Monitor(
        root=tmp_path / "projects",
        snapshot_dir=snapshots,
        restored_date="2026-08-24",
        restored_cost_entries={"s": entry},
    )

    monitor.refresh()

    assert monitor.sessions["s"].estimated_cost_by_date["2026-08-24"] == 5.5


def test_decreasing_normalized_total_invalidates_day():
    m = _load_monitor_module()
    state = _state_started_at(m, _local_ts(2026, 8, 24, 9, 0))
    state.record_cost_observation(
        _local_ts(2026, 8, 24, 9, 5), 10.0, 10.0, 0
    )

    state.record_cost_observation(
        _local_ts(2026, 8, 24, 9, 6), 2.0, 2.0, 0
    )

    assert state.estimated_cost_by_date["2026-08-24"] is None


def test_cost_observation_retains_previous_day_total_after_midnight():
    m = _load_monitor_module()
    state = _state_started_at(m, _local_ts(2026, 8, 23, 9, 0))

    state.record_cost_observation(
        _local_ts(2026, 8, 23, 23, 50), 10.0, 10.0, 0
    )
    state.record_cost_observation(
        _local_ts(2026, 8, 24, 0, 10), 11.5, 11.5, 0
    )

    assert state.estimated_cost_by_date["2026-08-23"] == 10.0
    assert state.estimated_cost_by_date["2026-08-24"] == 1.5


def test_entries_for_date_persists_attributable_cost_state(tmp_path):
    m = _load_monitor_module()
    ts = _local_ts(2026, 8, 24, 9, 0)
    monitor = m.Monitor(
        root=tmp_path / "projects", snapshot_dir=tmp_path / "snapshots"
    )
    state = _state_started_at(m, ts)
    state.samples["r1"] = m.UsageSample(ts=ts, input_tokens=10, output_tokens=5)
    state.record_cost_observation(ts + 60, 3.0, 0.5, 1)
    monitor.sessions[state.session_id] = state

    entry = monitor.entries_for_date(date(2026, 8, 24), ts + 60)["s"]

    assert entry.estimated_cost_usd == 3.0
    assert entry.last_observed_total_cost_usd == 3.0
    assert entry.last_raw_cost_usd == 0.5
    assert entry.cost_counter_resets == 1
    assert entry.last_cost_ts == ts + 60


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


# ---------------------------------------------------------------------------
# _fmt_effort_footer
# ---------------------------------------------------------------------------

def _state_with(m, session_id: str, last_ts: float, effort: str | None):
    state = m.SessionState(session_id=session_id, project="proj",
                           jsonl_path=Path(f"/tmp/{session_id}.jsonl"))
    state.first_ts = last_ts
    state.last_ts = last_ts
    state.effort = effort
    return state


def test_fmt_effort_footer_picks_newest_session():
    m = _load_monitor_module()
    older = _state_with(m, "aaaaaaaa1111", 100.0, "medium")
    newer = _state_with(m, "bbbbbbbb2222", 200.0, "xhigh")

    line = m._fmt_effort_footer([older, newer])

    assert line is not None
    assert "xhigh" in line
    assert "bbbbbbbb" in line
    assert "medium" not in line
    assert "aaaaaaaa" not in line


def test_fmt_effort_footer_none_when_newest_lacks_effort():
    """No fall-through: the line names one session, so it must be that one."""
    m = _load_monitor_module()
    older = _state_with(m, "aaaaaaaa1111", 100.0, "high")
    newer = _state_with(m, "bbbbbbbb2222", 200.0, None)

    assert m._fmt_effort_footer([older, newer]) is None


def test_fmt_effort_footer_none_for_empty_list():
    m = _load_monitor_module()
    assert m._fmt_effort_footer([]) is None


def test_fmt_effort_footer_skips_sessions_without_last_ts():
    m = _load_monitor_module()
    no_ts = _state_with(m, "cccccccc3333", 0.0, "max")
    no_ts.last_ts = None
    real = _state_with(m, "dddddddd4444", 50.0, "low")

    line = m._fmt_effort_footer([no_ts, real])

    assert line is not None
    assert "low" in line
    assert "dddddddd" in line


def test_fmt_effort_footer_uses_eight_char_session_prefix():
    m = _load_monitor_module()
    state = _state_with(m, "0123456789abcdef", 10.0, "high")

    line = m._fmt_effort_footer([state])

    assert "01234567" in line
    assert "89abcdef" not in line


# ---------------------------------------------------------------------------
# build_layout: footer carries the effort line
# ---------------------------------------------------------------------------

def _footer_text(layout) -> str:
    """Plain text of the footer pane (Layout -> Align -> Text)."""
    return layout["footer"].renderable.renderable.plain


def test_build_layout_footer_includes_effort_line(tmp_path):
    m = _load_monitor_module()
    mon = m.Monitor(root=tmp_path / "projects", snapshot_dir=tmp_path / "snaps")
    state = _state_with(m, "cccccccc3333", time.time(), "xhigh")
    mon.sessions[state.session_id] = state

    layout = m.build_layout(mon, velocity_window=30)

    assert "effort: xhigh" in _footer_text(layout)
    assert "cccccccc" in _footer_text(layout)
    assert layout["footer"].size == 4


def test_build_layout_footer_omits_effort_line_without_effort(tmp_path):
    m = _load_monitor_module()
    mon = m.Monitor(root=tmp_path / "projects", snapshot_dir=tmp_path / "snaps")
    state = _state_with(m, "eeeeeeee5555", time.time(), None)
    mon.sessions[state.session_id] = state

    layout = m.build_layout(mon, velocity_window=30)

    assert "effort:" not in _footer_text(layout)
    assert layout["footer"].size == 3


def test_build_layout_footer_keeps_existing_legend(tmp_path):
    """The effort line is appended, not a replacement for the legend."""
    m = _load_monitor_module()
    mon = m.Monitor(root=tmp_path / "projects", snapshot_dir=tmp_path / "snaps")
    state = _state_with(m, "ffffffff6666", time.time(), "high")
    mon.sessions[state.session_id] = state

    text = _footer_text(m.build_layout(mon, velocity_window=30))

    assert "hook installed" in text
    assert "$/h = cost rate" in text
    assert "effort: high" in text


# ---------------------------------------------------------------------------
# footer at fixed terminal widths — regression for the clipping bug
#
# `layout.split_column(..., size=len(footer_lines))` counts *logical* lines,
# but rich wraps long ones. The legend lines are long enough (134 / 133 chars)
# to wrap at narrower widths, which consumes the footer pane's fixed row
# budget and clips whatever line is *last*. Inspecting the `Text` object (as
# the other footer tests above do) can't catch this — it only shows up once
# the layout is actually rendered through a Console at a real width.
# ---------------------------------------------------------------------------

def _render_layout(layout, width: int) -> str:
    """Plain text of a Layout rendered through a fixed-width Console."""
    console = Console(file=io.StringIO(), width=width, height=50, no_color=True)
    console.print(layout)
    return console.file.getvalue()


def test_footer_effort_line_survives_narrow_width(tmp_path):
    """At width=120 the legend wraps; the effort line must still render.

    This is the width the final review measured as broken (line clipped)
    before the fix that puts the effort line first in `footer_lines`.
    """
    m = _load_monitor_module()
    mon = m.Monitor(root=tmp_path / "projects", snapshot_dir=tmp_path / "snaps")
    state = _state_with(m, "cccccccc3333", time.time(), "xhigh")
    mon.sessions[state.session_id] = state

    rendered = _render_layout(m.build_layout(mon, velocity_window=30), width=120)

    assert "effort: xhigh" in rendered


def test_footer_effort_line_survives_wide_width(tmp_path):
    """At a wide width (200) nothing wraps, so this must pass regardless of
    ordering — it pins the non-clipped case alongside the narrow one above."""
    m = _load_monitor_module()
    mon = m.Monitor(root=tmp_path / "projects", snapshot_dir=tmp_path / "snaps")
    state = _state_with(m, "cccccccc3333", time.time(), "xhigh")
    mon.sessions[state.session_id] = state

    rendered = _render_layout(m.build_layout(mon, velocity_window=30), width=200)

    assert "effort: xhigh" in rendered
