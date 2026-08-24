#!/usr/bin/env python3
"""
cc-session-monitor — live Terminal UI for tracking Claude Code token usage
and estimated API list-price cost per active session, with token/s velocity.
Part of the claude-code-monitor project.

Reads Claude Code's JSONL transcripts under ~/.claude/projects/<project>/*.jsonl
and renders two side-by-side panels:

  • ACTIVE (last 15 min) — sessions with activity in the last 15 minutes
  • TODAY (since 00:00)  — all sessions with activity since local midnight

For each session the monitor shows:
  • SessionID (shortened)   — the .jsonl UUID
  • Project                 — derived from the directory name
  • Last activity (age)
  • input / output / cache_read / cache_creation tokens (cumulative)
  • Total tokens
  • Ctx — live context-window occupancy (used/size) from the hook snapshot
  • Velocity: tokens/second over a rolling window (default 30s)

Known caveat (documented issue in Claude Code JSONL logs):
  input_tokens and output_tokens in the JSONL are streaming placeholders
  and undercount API usage (see gille.ai analysis). Cache fields are reliable.
  The tool deduplicates by requestId and uses
  per-requestId MAX to mitigate streaming duplicates, but absolute
  input/output numbers remain approximate. Velocity and relative trends
  between sessions are still meaningful.

Note (Claude Code v2.1.132+):
  The hook's context_window.total_input_tokens / total_output_tokens report
  *current context-window occupancy*, not cumulative session totals, so they
  feed the live "Ctx" gauge only — cumulative Input/Output come from JSONL.
  The hook supplies Claude Code's client-side standard-list-price estimate.
  It is useful for comparisons, but is not authoritative billing.

Usage:
    python cc-session-monitor.py
    python cc-session-monitor.py --refresh 1.0 --velocity-window 30

Requires: rich  (pip install rich)
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich.align import Align
except ImportError:
    sys.stderr.write(
        "This tool needs 'rich'. Install it with:\n    pip install rich\n"
    )
    sys.exit(1)

from cc_history import (
    DailyRecord,
    DailySessionEntry,
    HistoryLogger,
    UsageSample,
    parse_ts as _parse_ts,
    extract_usage as _extract_usage,
    extract_effort as _extract_effort,
    merge_sample as _merge_sample,
    humanize_project as _humanize_project,
)


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
SNAPSHOT_DIR = Path.home() / ".claude" / "session-monitor" / "snapshots"
ACTIVE_WINDOW_SECONDS = 15 * 60         # "active" = activity in last 15 min
VELOCITY_WINDOW_SECONDS = 30            # default rolling velocity window


def local_midnight_ts(now: float) -> float:
    """Unix timestamp of the most recent local-midnight on or before `now`."""
    return datetime.fromtimestamp(now).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

def _series_rate(points: deque, window_seconds: int, now: float) -> float:
    """Linear rate over the last `window_seconds` for a (ts, running_total)
    series. Falls back to the last two points if the window is too narrow."""
    if len(points) < 2:
        return 0.0
    cutoff = now - window_seconds
    pts = [p for p in points if p[0] >= cutoff]
    if len(pts) < 2:
        pts = list(points)[-2:]
    (t0, v0), (t1, v1) = pts[0], pts[-1]
    dt = t1 - t0
    if dt <= 0:
        return 0.0
    return max(0.0, (v1 - v0) / dt)


@dataclass
class SessionState:
    session_id: str                 # .jsonl filename stem (uuid)
    project: str                    # derived from directory name
    jsonl_path: Path
    file_size: int = 0              # last seen size, for tail-only reads

    # dedup: requestId -> best usage sample (MAX strategy for output_tokens)
    samples: dict[str, UsageSample] = field(default_factory=dict)

    # rolling velocity: (ts, total_tokens_at_ts) pairs
    velocity_points: deque = field(default_factory=lambda: deque(maxlen=2000))
    # parallel series for output-only tokens
    output_velocity_points: deque = field(default_factory=lambda: deque(maxlen=2000))

    first_ts: float | None = None
    last_ts: float | None = None

    # ----- hook-derived fields (None until the hook fires at least once) -----
    hook_ts: float | None = None            # when the last snapshot was written
    hook_raw_cost_usd: float | None = None  # latest raw list-price estimate
    hook_observed_cost_usd: float | None = None  # reset-safe session estimate
    hook_ctx_pct: float | None = None       # context_window.used_percentage (live)
    hook_ctx_input: int | None = None       # context_window.total_input_tokens (live)
    hook_ctx_output: int | None = None      # context_window.total_output_tokens (live)
    hook_ctx_size: int | None = None        # context_window.context_window_size
    hook_model: str | None = None
    hook_cwd: str | None = None
    hook_rl5_pct: float | None = None       # 5h rate-limit %
    hook_rl5_reset: int | None = None       # epoch seconds

    # ----- JSONL-derived session settings -----
    # Reasoning effort of the newest `assistant` entry. Deliberately NOT a
    # field on UsageSample: _merge_sample does a per-field MAX merge, and MAX
    # has no meaningful semantics for a string.
    effort: str | None = None

    # rolling history of (ts, cost_usd) for $/h velocity
    cost_points: deque = field(default_factory=lambda: deque(maxlen=500))
    estimated_cost_by_date: dict[str, float | None] = field(default_factory=dict)
    last_cost_observed_total: float | None = None
    last_cost_raw: float | None = None
    cost_counter_resets: int = 0
    last_cost_ts: float | None = None

    # ----- aggregates -----

    def totals(self) -> UsageSample:
        agg = UsageSample(ts=self.last_ts or 0.0)
        for s in self.samples.values():
            agg.input_tokens += s.input_tokens
            agg.output_tokens += s.output_tokens
            agg.cache_creation += s.cache_creation
            agg.cache_read += s.cache_read
        return agg

    def totals_since(self, cutoff_ts: float) -> UsageSample:
        agg = UsageSample(ts=self.last_ts or 0.0)
        for s in self.samples.values():
            if s.ts >= cutoff_ts:
                agg.input_tokens += s.input_tokens
                agg.output_tokens += s.output_tokens
                agg.cache_creation += s.cache_creation
                agg.cache_read += s.cache_read
        return agg

    def velocity(self, window_seconds: int, now: float) -> float:
        """Tokens per second over the last `window_seconds` (all token types)."""
        return _series_rate(self.velocity_points, window_seconds, now)

    def output_velocity(self, window_seconds: int, now: float) -> float:
        """Output tokens per second — the actual generation rate."""
        return _series_rate(self.output_velocity_points, window_seconds, now)

    def cost_velocity(self, window_seconds: int, now: float) -> float:
        """USD per hour over the last `window_seconds`. 0 if unknown."""
        if len(self.cost_points) < 2:
            return 0.0
        cutoff = now - window_seconds
        pts = [p for p in self.cost_points if p[0] >= cutoff]
        if len(pts) < 2:
            pts = list(self.cost_points)[-2:]
        (t0, c0), (t1, c1) = pts[0], pts[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return max(0.0, (c1 - c0) / dt * 3600.0)

    @property
    def hook_cost_usd(self) -> float | None:
        """Deprecated internal alias for the raw Claude estimate."""
        return self.hook_raw_cost_usd

    @hook_cost_usd.setter
    def hook_cost_usd(self, value: float | None) -> None:
        self.hook_raw_cost_usd = value

    def record_cost_observation(
        self,
        ts: float,
        observed: float,
        raw: float | None,
        resets: int,
    ) -> None:
        """Attribute one normalized session-total observation to a local day."""
        day = datetime.fromtimestamp(ts).date()
        day_key = day.isoformat()

        if self.last_cost_observed_total is None:
            started_today = (
                self.first_ts is not None
                and datetime.fromtimestamp(self.first_ts).date() == day
            )
            self.estimated_cost_by_date.setdefault(
                day_key, observed if started_today else None
            )
        elif observed < self.last_cost_observed_total:
            # A normalized total must be monotonic. If state was lost or
            # corrupted, retain tokens but stop claiming a dollar value today.
            self.estimated_cost_by_date[day_key] = None
        else:
            delta = observed - self.last_cost_observed_total
            if day_key not in self.estimated_cost_by_date:
                self.estimated_cost_by_date[day_key] = delta
            elif self.estimated_cost_by_date[day_key] is not None:
                self.estimated_cost_by_date[day_key] += delta

        self.last_cost_observed_total = observed
        if raw is not None:
            self.last_cost_raw = raw
        self.cost_counter_resets = max(0, resets)
        self.last_cost_ts = ts

        if self.cost_points and self.cost_points[-1][0] == ts:
            self.cost_points[-1] = (ts, observed)
        elif not self.cost_points or self.cost_points[-1][0] < ts:
            self.cost_points.append((ts, observed))

        # Only these two dates are needed for midnight rollover and persistence.
        keep = {day_key, (day - timedelta(days=1)).isoformat()}
        self.estimated_cost_by_date = {
            key: value
            for key, value in self.estimated_cost_by_date.items()
            if key in keep
        }

    def effective_last_ts(self) -> float | None:
        """Latest signal of activity — from JSONL or from the hook snapshot."""
        candidates = [t for t in (self.last_ts, self.hook_ts) if t is not None]
        return max(candidates) if candidates else None


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class Monitor:
    def __init__(
        self,
        root: Path = CLAUDE_PROJECTS_DIR,
        snapshot_dir: Path = SNAPSHOT_DIR,
        velocity_window: int = VELOCITY_WINDOW_SECONDS,
        restored_date: str | None = None,
        restored_cost_entries: dict[str, DailySessionEntry] | None = None,
    ) -> None:
        self.root = root
        self.snapshot_dir = snapshot_dir
        self.velocity_window = velocity_window
        self.sessions: dict[str, SessionState] = {}  # key = session_id
        self._snapshot_mtimes: dict[str, float] = {}  # session_id -> last mtime seen
        self._restored_date = restored_date
        self._restored_cost_entries = dict(restored_cost_entries or {})

    def _restore_cost_state(self, state: SessionState) -> None:
        """Restore one persisted cost baseline exactly once per session."""
        entries = getattr(self, "_restored_cost_entries", None)
        restored_date = getattr(self, "_restored_date", None)
        if not entries or not restored_date:
            return
        entry = entries.pop(state.session_id, None)
        if entry is None:
            return

        state.estimated_cost_by_date[restored_date] = entry.estimated_cost_usd
        state.last_cost_observed_total = entry.last_observed_total_cost_usd
        state.last_cost_raw = entry.last_raw_cost_usd
        state.cost_counter_resets = entry.cost_counter_resets
        state.last_cost_ts = entry.last_cost_ts
        state.hook_model = entry.model
        if entry.last_cost_ts is not None and entry.last_observed_total_cost_usd is not None:
            state.cost_points.append(
                (entry.last_cost_ts, entry.last_observed_total_cost_usd)
            )

    # ---- discovery ----

    def _iter_session_files(self) -> Iterable[tuple[str, Path]]:
        if not self.root.exists():
            return
        for project_dir in self.root.iterdir():
            if not project_dir.is_dir():
                continue
            project_name = _humanize_project(project_dir.name)
            for jsonl in project_dir.glob("*.jsonl"):
                yield project_name, jsonl

    def refresh(self) -> None:
        """Re-scan directory, tail any grown files."""
        for project, jsonl in self._iter_session_files():
            session_id = jsonl.stem
            try:
                size = jsonl.stat().st_size
            except FileNotFoundError:
                continue

            state = self.sessions.get(session_id)
            if state is None:
                state = SessionState(
                    session_id=session_id,
                    project=project,
                    jsonl_path=jsonl,
                )
                self._restore_cost_state(state)
                self.sessions[session_id] = state

            if size == state.file_size:
                continue  # nothing new

            # Read from last seen offset forward. If file shrank (log rotate
            # or edit), start from scratch.
            start = state.file_size if size > state.file_size else 0
            try:
                with jsonl.open("rb") as f:
                    f.seek(start)
                    data = f.read()
            except OSError:
                continue

            if start == 0:
                # full reset of the in-memory view
                state.samples.clear()
                state.velocity_points.clear()
                state.output_velocity_points.clear()
                state.first_ts = None
                state.last_ts = None
                state.effort = None

            state.file_size = size

            # split on newlines; last chunk may be partial if we caught it
            # mid-write, so skip trailing non-terminated line
            lines = data.split(b"\n")
            if data and not data.endswith(b"\n"):
                lines = lines[:-1]

            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Before the usage guard below: an assistant entry without a
                # usage block still tells us the session's effort level.
                #
                # The `if effort:` guard is load-bearing, not redundant: it
                # keeps non-assistant entries (which yield None) from
                # blanking a previously-seen value, and — per live-transcript
                # analysis — assigning unconditionally would make the footer
                # line flicker off on every `<synthetic>` interrupt entry.
                # Do not simplify this to an unconditional assignment.
                effort = _extract_effort(entry)
                if effort:
                    state.effort = effort

                req_id, sample = _extract_usage(entry)
                if sample is None:
                    continue

                # Dedup key: prefer requestId; fall back to message id or ts
                key = req_id or f"ts:{sample.ts}"
                state.samples[key] = _merge_sample(
                    state.samples.get(key), sample
                )

                if state.first_ts is None or sample.ts < state.first_ts:
                    state.first_ts = sample.ts
                if state.last_ts is None or sample.ts > state.last_ts:
                    state.last_ts = sample.ts

            # Rebuild velocity series: (ts, running_total) sorted by ts.
            # Cheap enough; sessions rarely exceed a few thousand samples.
            ordered = sorted(state.samples.values(), key=lambda s: s.ts)
            running = 0
            running_out = 0
            state.velocity_points.clear()
            state.output_velocity_points.clear()
            for s in ordered:
                running += s.total
                running_out += s.output_tokens
                state.velocity_points.append((s.ts, running))
                state.output_velocity_points.append((s.ts, running_out))

        # ----- Second pass: read any snapshot files dropped by the hook -----
        self._refresh_snapshots()

    def _refresh_snapshots(self) -> None:
        """Load hook-written snapshots. Files are tiny; we reread them only
        when mtime changes."""
        if not self.snapshot_dir.exists():
            return
        for snap in self.snapshot_dir.glob("*.json"):
            # Skip hidden tmp files and the raw fallback
            if snap.name.startswith((".", "_")):
                continue
            try:
                mtime = snap.stat().st_mtime
            except FileNotFoundError:
                continue
            prev_mtime = self._snapshot_mtimes.get(snap.stem)
            if prev_mtime is not None and mtime <= prev_mtime:
                continue
            try:
                with snap.open() as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            self._snapshot_mtimes[snap.stem] = mtime
            self._apply_snapshot(data)

    def _apply_snapshot(self, data: dict) -> None:
        session_id = data.get("session_id") or ""
        if not session_id:
            return
        state = self.sessions.get(session_id)
        if state is None:
            # Snapshot arrived before JSONL was discovered — e.g. a session
            # whose transcript is outside the projects dir. Stub it in so
            # it's still tracked. project name is derived from cwd.
            cwd = data.get("cwd") or ""
            project = Path(cwd).name if cwd else "?"
            jsonl_path = Path(data.get("transcript_path") or "")
            state = SessionState(
                session_id=session_id,
                project=project,
                jsonl_path=jsonl_path,
            )
            self._restore_cost_state(state)
            self.sessions[session_id] = state

        snap_ts = float(data.get("snapshot_ts") or 0.0)
        if snap_ts <= 0:
            return

        ctx = data.get("context_window") or {}
        cost = data.get("cost") or {}
        rl5 = (data.get("rate_limits") or {}).get("five_hour") or {}

        def optional_float(value) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            result = float(value)
            return result if math.isfinite(result) else None

        state.hook_ts = snap_ts
        state.hook_raw_cost_usd = optional_float(cost.get("total_cost_usd"))
        state.hook_observed_cost_usd = optional_float(
            cost.get("observed_total_cost_usd")
        )
        state.hook_ctx_pct = float(ctx.get("used_percentage") or 0.0)
        state.hook_ctx_input = int(ctx.get("total_input_tokens") or 0)
        state.hook_ctx_output = int(ctx.get("total_output_tokens") or 0)
        state.hook_ctx_size = int(ctx.get("context_window_size") or 0) or None
        state.hook_model = data.get("model") or state.hook_model
        state.hook_cwd = data.get("cwd") or state.hook_cwd
        state.hook_rl5_pct = float(rl5.get("used_percentage") or 0.0) or None
        reset = rl5.get("resets_at")
        state.hook_rl5_reset = int(reset) if reset else None

        resets_value = optional_float(cost.get("counter_resets"))
        resets = int(resets_value) if resets_value is not None else 0
        if state.hook_observed_cost_usd is not None:
            state.record_cost_observation(
                snap_ts,
                state.hook_observed_cost_usd,
                state.hook_raw_cost_usd,
                resets,
            )

    # ---- views ----

    def active_sessions(self, now: float) -> list[SessionState]:
        cutoff = now - ACTIVE_WINDOW_SECONDS
        return sorted(
            (
                s for s in self.sessions.values()
                if (ts := s.effective_last_ts()) is not None and ts >= cutoff
            ),
            key=lambda s: s.effective_last_ts() or 0,
            reverse=True,
        )

    def daily_window_sessions(self, now: float) -> list[SessionState]:
        cutoff = local_midnight_ts(now)
        return sorted(
            (
                s for s in self.sessions.values()
                if (ts := s.effective_last_ts()) is not None and ts >= cutoff
            ),
            key=lambda s: s.effective_last_ts() or 0,
            reverse=True,
        )

    def entries_for_date(self, target_date, now: float) -> dict[str, DailySessionEntry]:
        """Build a {session_id: DailySessionEntry} map for history logging.

        Includes only sessions with at least one usage sample whose local
        calendar date == `target_date`. Token fields are summed across
        samples within that day only.

        session_cumulative_cost_usd is retained as a deprecated compatibility
        field containing the latest raw hook counter. It is neither reset-safe
        nor date-attributable; consumers must use estimated_cost_usd instead.
        """
        out: dict[str, DailySessionEntry] = {}
        for state in self.sessions.values():
            day_samples = [
                s for s in state.samples.values()
                if datetime.fromtimestamp(s.ts).date() == target_date
            ]
            if not day_samples:
                continue
            out[state.session_id] = DailySessionEntry(
                project=state.project,
                model=state.hook_model,
                first_ts=min(s.ts for s in day_samples),
                last_ts=max(s.ts for s in day_samples),
                input_tokens=sum(s.input_tokens for s in day_samples),
                output_tokens=sum(s.output_tokens for s in day_samples),
                cache_read_tokens=sum(s.cache_read for s in day_samples),
                cache_creation_tokens=sum(s.cache_creation for s in day_samples),
                session_cumulative_cost_usd=state.hook_raw_cost_usd,
                estimated_cost_usd=state.estimated_cost_by_date.get(
                    target_date.isoformat()
                ),
                last_observed_total_cost_usd=state.last_cost_observed_total,
                last_raw_cost_usd=state.last_cost_raw,
                cost_counter_resets=state.cost_counter_resets,
                last_cost_ts=state.last_cost_ts,
            )
        return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m{int(seconds)%60:02d}s"
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    return f"{h}h{m:02d}m"


def _fmt_velocity(tps: float) -> Text:
    """Throughput across all token types — informational, not a warning.

    Cache-reads dominate this number, so it tracks data flow rather than
    cost or 'real work'. See `_fmt_output_velocity` for generation rate
    and the `$/h` formatter (inline in build_table) for the warning metric.
    """
    if tps <= 0:
        return Text("idle", style="dim")
    if tps >= 1000:
        return Text(f"{tps/1000:.2f}K t/s", style="cyan")
    if tps >= 100:
        return Text(f"{tps:.0f} t/s", style="cyan")
    return Text(f"{tps:.1f} t/s", style="cyan")


def _fmt_output_velocity(tps: float) -> Text:
    """Generation rate (output tokens only) — the 'real work' signal."""
    if tps <= 0:
        return Text("idle", style="dim")
    if tps >= 1000:
        return Text(f"{tps/1000:.2f}K o/s", style="bold cyan")
    if tps >= 100:
        return Text(f"{tps:.0f} o/s", style="bold cyan")
    return Text(f"{tps:.1f} o/s", style="bold cyan")


def _fmt_ctx(used: int | None, size: int | None, pct: float | None) -> Text:
    """Live context-window occupancy from the hook snapshot.

    Renders the *current* window load (not cumulative usage) as `used/size`,
    coloured like the status bar's context gauge: green <50%, yellow <80%,
    red otherwise. Shows "—" when no hook snapshot is available yet.
    """
    if used is None:
        return Text("—", style="dim")
    pct_val = pct or 0.0
    color = "green" if pct_val < 50 else "yellow" if pct_val < 80 else "red"
    if size:
        return Text(f"{_fmt_tokens(used)}/{_fmt_tokens(size)}", style=color)
    return Text(_fmt_tokens(used), style=color)


def _fmt_effort_footer(sessions: list[SessionState]) -> str | None:
    """Footer line naming the reasoning effort of the most recent session.

    Scoped to a single session on purpose: effort is a per-session setting, so
    one unlabelled value spanning differing sessions would be ambiguous — hence
    the session-ID annotation.

    Returns None when the newest session has no effort level (its model does not
    support one, or it has not produced an assistant turn yet). Falling through
    to an older session would print a value that does not belong to the session
    the line names.

    Effort is informational, so the caller renders it in the footer's neutral
    dim italic — no threshold colouring, which is reserved for `$/h`.
    """
    newest: SessionState | None = None
    for state in sessions:
        if state.last_ts is None:
            continue
        if newest is None or state.last_ts > newest.last_ts:
            newest = state
    if newest is None or not newest.effort:
        return None
    return f"effort: {newest.effort}  ({newest.session_id[:8]}, most recent)"


def build_table(
    title: str,
    sessions: list[SessionState],
    now: float,
    velocity_window: int,
    scope_cutoff: float | None = None,
    cost_scope_date: str | None = None,
) -> Table:
    """
    scope_cutoff: if given, only tokens accumulated at/after this ts are
    summed (used for the calendar-day "today" view to reflect only what
    counts in the current window).
    """
    table = Table(
        title=title,
        expand=True,
        header_style="bold cyan",
        border_style="dim",
        pad_edge=False,
    )
    table.add_column("Session", style="bold", no_wrap=True)
    table.add_column("Project", style="magenta", no_wrap=True)
    table.add_column("Age", justify="right", no_wrap=True)
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cache R", justify="right", style="dim")
    table.add_column("Total", justify="right", style="bold")
    table.add_column("Ctx", justify="right")
    table.add_column("t/s", justify="right")
    table.add_column("out/s", justify="right")
    cost_header = "Est. today $" if cost_scope_date else "Est. session $"
    table.add_column(cost_header, justify="right", style="green")
    table.add_column("$/h", justify="right")

    if not sessions:
        table.add_row(
            Text("—", style="dim"),
            Text("no sessions in window (hook not installed? start a "
                 "Claude Code session, or run `--install-hook`)",
                 style="dim italic"),
            "", "", "", "", "", "", "", "", "", "",
        )
        return table

    grand_total_tokens = 0
    grand_total_cost = 0.0
    all_costs_known = True
    for s in sessions:
        totals = s.totals_since(scope_cutoff) if scope_cutoff else s.totals()
        age = now - (s.effective_last_ts() or now)
        vel = s.velocity(velocity_window, now)
        out_vel = s.output_velocity(velocity_window, now)

        # Cumulative Input/Output come from the JSONL transcript (merge-by-
        # requestId). These undercount API output slightly (the JSONL
        # fields are streaming placeholders) but they are monotonic and
        # cumulative. The hook's context_window totals can NOT be used here:
        # since CC v2.1.132 they report current context occupancy, not session
        # totals — they're surfaced separately in the "Ctx" column below.
        input_tok = totals.input_tokens
        output_tok = totals.output_tokens

        # All four components are JSONL-cumulative, so the sum is coherent.
        row_total = input_tok + output_tok + totals.cache_read + totals.cache_creation
        grand_total_tokens += row_total

        ctx_txt = _fmt_ctx(s.hook_ctx_input, s.hook_ctx_size, s.hook_ctx_pct)

        row_cost = (
            s.estimated_cost_by_date.get(cost_scope_date)
            if cost_scope_date is not None
            else s.hook_observed_cost_usd
        )
        cost_txt = (
            Text(
                f"${row_cost:.2f}" if row_cost >= 1 else f"${row_cost:.3f}",
                style="green",
            )
            if row_cost is not None else Text("—", style="dim")
        )
        if row_cost is None:
            all_costs_known = False
        else:
            grand_total_cost += row_cost

        cvel = s.cost_velocity(velocity_window, now)
        cvel_txt = (
            Text(f"${cvel:.2f}/h",
                 style=("bold red" if cvel >= 5 else
                        "yellow" if cvel >= 1 else "green"))
            if cvel > 0 else Text("—", style="dim")
        )

        # Mark rows with a live hook snapshot (●). An individual estimate can
        # still be unknown; the marker only promises snapshot-backed live data.
        # Input/Output/Cache/Total are JSONL-derived for both.
        marker = "●" if s.hook_ts is not None else "○"
        marker_color = "green" if s.hook_ts is not None else "yellow"

        table.add_row(
            Text(f"{marker} ", style=marker_color).append(
                s.session_id[:8], style="bold"),
            s.project,
            _fmt_age(age),
            _fmt_tokens(input_tok),
            _fmt_tokens(output_tok),
            _fmt_tokens(totals.cache_read),
            _fmt_tokens(row_total),
            ctx_txt,
            _fmt_velocity(vel),
            _fmt_output_velocity(out_vel),
            cost_txt,
            cvel_txt,
        )

    # Summary footer
    table.add_section()
    cost_cell = (
        Text(f"${grand_total_cost:.2f}", style="bold white on green")
        if all_costs_known else Text("—", style="dim")
    )
    table.add_row(
        Text("TOTAL", style="bold"),
        Text(f"{len(sessions)} session(s)", style="dim"),
        "", "", "", "",
        Text(_fmt_tokens(grand_total_tokens), style="bold white on dark_cyan"),
        "", "", "",
        cost_cell,
        "",
    )
    return table


def build_layout(
    monitor: Monitor,
    velocity_window: int,
) -> Layout:
    now = time.time()
    daily_cutoff = local_midnight_ts(now)

    active = monitor.active_sessions(now)
    daily = monitor.daily_window_sessions(now)

    active_tbl = build_table(
        f"🔥 Active sessions (last {ACTIVE_WINDOW_SECONDS // 60} min)",
        active,
        now,
        velocity_window,
    )
    daily_tbl = build_table(
        "📊 Today (since 00:00)",
        daily,
        now,
        velocity_window,
        scope_cutoff=daily_cutoff,
        cost_scope_date=datetime.fromtimestamp(now).date().isoformat(),
    )

    header = Text.assemble(
        ("claude-code-monitor", "bold white"),
        ("   │   ", "dim"),
        (f"refresh {time.strftime('%H:%M:%S')}", "cyan"),
        ("   │   ", "dim"),
        (f"velocity window: {velocity_window}s", "cyan"),
        ("   │   ", "dim"),
        ("Ctrl-C to quit", "dim"),
    )

    # Effort goes first: `size=len(footer_lines)` below counts logical lines,
    # but rich wraps the long legend lines at narrower terminal widths, which
    # eats into the footer pane's fixed row budget and clips whichever line
    # is last. Putting the highest-value line first means it survives that
    # clipping instead of the legend.
    footer_lines = []
    effort_line = _fmt_effort_footer(active)
    if effort_line:
        footer_lines.append(effort_line)
    footer_lines.extend([
        "● snapshot available (estimate may be —; live Ctx gauge)   "
        "○ JSONL-only (no estimate/Ctx yet)   "
        "install hook: --install-hook",
        "Input/Output/Total = JSONL cumulative (streaming placeholders, slight undercount)   "
        "Ctx = live context window used/size",
        "t/s = total throughput incl. cache   "
        "out/s = generation rate (output tokens only)   "
        "$/h = estimated list-price cost rate (red = high)",
    ])

    footer = Text("\n".join(footer_lines), style="dim italic")

    layout = Layout()
    layout.split_column(
        Layout(Align.center(header), name="header", size=1),
        Layout(name="active"),
        Layout(name="daily"),
        Layout(Align.center(footer), name="footer", size=len(footer_lines)),
    )
    layout["active"].update(Panel(active_tbl, border_style="green"))
    layout["daily"].update(Panel(daily_tbl, border_style="blue"))
    return layout


# ---------------------------------------------------------------------------
# Hook installer
# ---------------------------------------------------------------------------

def _hook_src_and_dest(here: Path) -> tuple[Path, Path, str]:
    """Return (src_path, dest_path, command_string) for the current platform.

    On Windows the PowerShell hook is used; on all other platforms the bash
    hook is used.  The command string is what gets written into settings.json.
    """
    claude_dir = Path.home() / ".claude"
    if os.name == "nt":
        src = here / "cc-monitor-hook.ps1"
        dest = claude_dir / "cc-monitor-hook.ps1"
        # Use the full path so Claude Code can find it regardless of cwd.
        command = f"powershell -NoProfile -NonInteractive -File \"{dest}\""
    else:
        src = here / "cc-monitor-hook.sh"
        dest = claude_dir / "cc-monitor-hook.sh"
        command = str(dest)
    return src, dest, command


def install_hook() -> int:
    """Copy the hook script next to ~/.claude/ and patch settings.json."""
    here = Path(__file__).resolve().parent
    src, dest, command = _hook_src_and_dest(here)

    if not src.exists():
        sys.stderr.write(
            f"Hook script not found at {src}.\n"
            "It should sit next to cc-session-monitor.py. "
            "Re-download the bundle.\n"
        )
        return 2

    claude_dir = dest.parent  # already computed by _hook_src_and_dest
    claude_dir.mkdir(exist_ok=True)

    dest.write_text(src.read_text())
    if os.name != "nt":
        dest.chmod(0o755)

    settings_path = claude_dir / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.stderr.write(
                f"⚠  {settings_path} exists but is not valid JSON. "
                "Refusing to overwrite.\n"
                "Add this entry manually:\n\n"
                '  "statusLine": {\n'
                '    "type": "command",\n'
                f'    "command": "{command}",\n'
                '    "padding": 0\n'
                '  }\n'
            )
            return 3
        if not isinstance(settings, dict):
            sys.stderr.write(
                f"⚠  {settings_path} must contain a JSON object at the top level, "
                f"not {type(settings).__name__}.\n"
                "Refusing to overwrite.\n"
            )
            return 3

    existing = settings.get("statusLine")
    if existing and existing.get("command") != command:
        print(
            f"⚠  settings.json already has a statusLine: "
            f"{existing.get('command')!r}"
        )
        ans = input("Replace it? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted. No changes made.")
            return 0

    settings["statusLine"] = {
        "type": "command",
        "command": command,
        "padding": 0,
    }
    # ensure_ascii=False so unrelated non-ASCII settings entries (e.g. a hook's
    # German statusMessage) survive the round-trip verbatim instead of being
    # rewritten as \uXXXX escapes. Explicit utf-8 because Python would otherwise
    # use the locale encoding on Windows and blow up on non-cp1252 characters.
    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"✓ Installed hook to  {dest}")
    print(f"✓ Updated            {settings_path}")
    print(f"✓ Snapshots will go to  {SNAPSHOT_DIR}")
    print()
    print("Restart any running Claude Code sessions to pick up the hook.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument(
        "--refresh", type=float, default=1.0,
        help="refresh interval in seconds (default: 1.0)",
    )
    ap.add_argument(
        "--velocity-window", type=int, default=VELOCITY_WINDOW_SECONDS,
        help=f"rolling velocity window in seconds "
             f"(default: {VELOCITY_WINDOW_SECONDS})",
    )
    ap.add_argument(
        "--projects-dir", type=Path, default=CLAUDE_PROJECTS_DIR,
        help=f"path to Claude Code projects dir "
             f"(default: {CLAUDE_PROJECTS_DIR})",
    )
    ap.add_argument(
        "--snapshot-dir", type=Path, default=SNAPSHOT_DIR,
        help=f"path where the hook writes session snapshots "
             f"(default: {SNAPSHOT_DIR})",
    )
    ap.add_argument(
        "--install-hook", action="store_true",
        help="install the statusLine hook into ~/.claude/ and exit",
    )
    ap.add_argument(
        "--no-log", action="store_true",
        help="disable persistent daily history logging",
    )
    ap.add_argument(
        "--history-dir", type=Path,
        default=Path.home() / ".claude" / "session-monitor" / "history",
        help="where to write daily/monthly history files "
             "(default: ~/.claude/session-monitor/history)",
    )
    args = ap.parse_args()

    if args.install_hook:
        return install_hook()

    if not args.projects_dir.exists():
        sys.stderr.write(
            f"Claude projects dir not found: {args.projects_dir}\n"
            "Has Claude Code ever been run on this machine?\n"
        )
        return 2

    logger = HistoryLogger(
        history_dir=args.history_dir,
        enabled=not args.no_log,
    )

    restored_record: DailyRecord | None = None
    if logger.enabled:
        startup_today = date.today()
        logger.run_retention(today=startup_today)
        logger.reconstruct_missing_days(
            today=startup_today, projects_dir=args.projects_dir,
        )
        restored_record = logger.read_daily(startup_today.isoformat())

    monitor = Monitor(
        root=args.projects_dir,
        snapshot_dir=args.snapshot_dir,
        velocity_window=args.velocity_window,
        restored_date=(restored_record.date if restored_record else None),
        restored_cost_entries=(
            restored_record.sessions if restored_record else None
        ),
    )
    console = Console()

    last_log_write = 0.0
    last_log_date: date | None = None
    LOG_INTERVAL = 60.0

    def _final_log_write():
        if not logger.enabled:
            return
        try:
            today = date.today()
            # If we crossed midnight since the last tick, flush yesterday
            # one more time so its last ~60s of activity isn't lost.
            if last_log_date is not None and last_log_date != today:
                yesterday_entries = monitor.entries_for_date(last_log_date, time.time())
                logger.write_today(
                    date=last_log_date.isoformat(),
                    entries=yesterday_entries,
                    now_ts=time.time(),
                )
            entries = monitor.entries_for_date(today, time.time())
            logger.write_today(
                date=today.isoformat(),
                entries=entries,
                now_ts=time.time(),
            )
        except Exception:
            # Exiting — never raise from a shutdown hook.
            pass

    atexit.register(_final_log_write)

    try:
        with Live(
            build_layout(monitor, args.velocity_window),
            console=console,
            refresh_per_second=max(1.0, 1.0 / args.refresh),
            screen=True,
        ) as live:
            while True:
                monitor.refresh()
                live.update(build_layout(monitor, args.velocity_window))

                now = time.time()
                if logger.enabled and (now - last_log_write) >= LOG_INTERVAL:
                    today = date.today()
                    # Midnight rollover: flush yesterday one final time
                    # with the last samples we observed before the date
                    # changed, so nothing between the last tick and 00:00
                    # goes missing.
                    if last_log_date is not None and last_log_date != today:
                        yesterday_entries = monitor.entries_for_date(last_log_date, now)
                        logger.write_today(
                            date=last_log_date.isoformat(),
                            entries=yesterday_entries,
                            now_ts=now,
                        )
                    # Cheap retention check in case the monitor spans midnight.
                    logger.run_retention(today=today)
                    entries = monitor.entries_for_date(today, now)
                    logger.write_today(
                        date=today.isoformat(),
                        entries=entries,
                        now_ts=now,
                    )
                    last_log_write = now
                    last_log_date = today

                time.sleep(args.refresh)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
