# Estimated Cost Accounting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace misleading lifetime/billing cost claims with reset-safe estimated session totals and attributable local-day estimates.

**Architecture:** The POSIX and PowerShell hooks normalize Claude Code's raw client-side estimate into a monotonic observed total stored in each atomic snapshot. The monitor converts normalized-total changes into local-date deltas, restores today's baseline from daily history, and renders session or day scope without guessing unknown values.

**Tech Stack:** Bash + jq, Windows PowerShell 5.1, Python 3.10+, Rich, pytest

**Spec:** `docs/superpowers/specs/2026-08-24-estimated-cost-accounting-design.md`

## Global Constraints

- `cost.total_cost_usd` remains the only dollar source and is described as an estimated API list-price equivalent.
- No network calls, billing API integration, model price table, or JSONL-derived dollar calculation.
- Missing or unattributable cost is `None`/JSON `null`/`—`, never zero.
- Existing daily and monthly history files remain readable without migration.
- `session_cumulative_cost_usd` remains serialized as a deprecated legacy field.
- Both hooks preserve atomic snapshot replacement and Windows PowerShell 5.1 compatibility.
- The hook must remain fast enough for Claude Code's 300 ms status-line debounce/cancellation behavior.

---

### Task 1: Normalize raw estimates in both hooks

**Files:**
- Create: `tests/test_hook_cost.py`
- Modify: `cc-monitor-hook.sh:47-170`
- Modify: `cc-monitor-hook.ps1:55-205`

**Interfaces:**
- Consumes: Claude status-line JSON on stdin and an optional previous snapshot for the same `session_id`.
- Produces: snapshot fields `cost.total_cost_usd: float | null`, `cost.observed_total_cost_usd: float | null`, `cost.last_valid_raw_cost_usd: float | null`, and `cost.counter_resets: int`.
- Produces: optional `CC_MONITOR_SNAPSHOT_DIR` environment override used by tests; defaults remain unchanged.

- [ ] **Step 1: Write failing POSIX hook tests**

Add these subprocess helpers, which run the real hook with an isolated
snapshot directory:

```python
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


def _write_legacy_snapshot(tmp_path: Path, raw_cost: float) -> None:
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "cost-test.json").write_text(json.dumps({
        "session_id": "cost-test",
        "snapshot_ts": 1.0,
        "cost": {"total_cost_usd": raw_cost},
    }))


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
    assert "est —" in snap.status_line


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
```

- [ ] **Step 2: Run the hook tests and verify RED**

Run: `./.venv/bin/pytest tests/test_hook_cost.py -v`

Expected: FAIL because `CC_MONITOR_SNAPSHOT_DIR` is ignored, missing cost becomes `0`, and normalized fields do not exist.

- [ ] **Step 3: Implement minimal POSIX normalization**

Use the previous snapshot's `cost` object as jq input and implement this exact state transition:

```jq
if $raw == null then
  {
    raw: null,
    observed: null,
    last_valid: $prev_raw,
    resets: $prev_resets
  }
elif $prev_raw == null or $prev_observed == null then
  {
    raw: $raw,
    observed: $raw,
    last_valid: $raw,
    resets: $prev_resets
  }
elif $raw < $prev_raw then
  {
    raw: $raw,
    observed: ($prev_observed + $raw),
    last_valid: $raw,
    resets: ($prev_resets + 1)
  }
else
  {
    raw: $raw,
    observed: ($prev_observed + $raw - $prev_raw),
    last_valid: $raw,
    resets: $prev_resets
  }
end
```

The shell directory selection is:

```bash
SNAPSHOT_DIR="${CC_MONITOR_SNAPSHOT_DIR:-${HOME}/.claude/session-monitor/snapshots}"
```

Render `est —` for null raw cost and `est $X` for a numeric raw estimate.

- [ ] **Step 4: Add equivalent PowerShell implementation and platform tests**

Use nullable doubles and this transition:

```powershell
if ($null -eq $rawCost) {
    $observedCost = $null
} elseif ($null -eq $previousRaw -or $null -eq $previousObserved) {
    $observedCost = $rawCost
} elseif ($rawCost -lt $previousRaw) {
    $observedCost = $previousObserved + $rawCost
    $counterResets += 1
} else {
    $observedCost = $previousObserved + ($rawCost - $previousRaw)
}
```

On Windows, execute `cc-monitor-hook.ps1` via `powershell -File` using the same payload sequence. On non-Windows, skip execution but retain the existing parser check in CI.

- [ ] **Step 5: Run hook tests and syntax checks GREEN**

Run:

```bash
./.venv/bin/pytest tests/test_hook_cost.py tests/test_install_hook.py -v
bash -n cc-monitor-hook.sh run-monitor.sh
```

Expected: all applicable tests pass; Windows-only tests skip on macOS.

- [ ] **Step 6: Commit Task 1**

```bash
git add tests/test_hook_cost.py cc-monitor-hook.sh cc-monitor-hook.ps1
git commit -m "fix: normalize estimated cost counters"
```

---

### Task 2: Extend daily history with attributable estimate fields

**Files:**
- Modify: `cc_history.py:119-209`
- Modify: `cc_history.py:218-253`
- Modify: `tests/test_cc_history.py:11-129`

**Interfaces:**
- Produces: `DailySessionEntry.estimated_cost_usd`, `last_observed_total_cost_usd`, `last_raw_cost_usd`, `cost_counter_resets`, and `last_cost_ts`.
- Produces: `HistoryLogger.read_daily(date_str: str) -> DailyRecord | None`.
- Retains: `DailySessionEntry.session_cumulative_cost_usd` and its legacy `cost_usd` read fallback.

- [ ] **Step 1: Write failing history schema tests**

```python
def _daily_entry(**overrides):
    values = {
        "project": "proj",
        "model": "Opus",
        "first_ts": 1.0,
        "last_ts": 2.0,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 20,
        "cache_creation_tokens": 2,
        "session_cumulative_cost_usd": 1.0,
    }
    values.update(overrides)
    return DailySessionEntry(**values)


def test_daily_record_round_trip_preserves_estimated_cost_state():
    entry = _daily_entry(
        session_cumulative_cost_usd=1.5,
        estimated_cost_usd=4.25,
        last_observed_total_cost_usd=12.75,
        last_raw_cost_usd=1.5,
        cost_counter_resets=2,
        last_cost_ts=1787583000.0,
    )
    restored = DailyRecord.from_dict(
        DailyRecord("2026-08-24", False, 1787583000.0, {"s": entry}).to_dict()
    )
    assert restored.sessions["s"].estimated_cost_usd == 4.25
    assert restored.sessions["s"].last_observed_total_cost_usd == 12.75


def test_estimated_cost_total_is_null_if_any_session_unknown():
    record = DailyRecord(
        "2026-08-24", False, 1.0,
        {"known": _daily_entry(estimated_cost_usd=1.0),
         "unknown": _daily_entry(estimated_cost_usd=None)},
    )
    assert record.to_dict()["totals"]["estimated_cost_usd"] is None


def test_read_daily_returns_record_or_none(tmp_path):
    logger = HistoryLogger(tmp_path)
    assert logger.read_daily("2026-08-24") is None
    logger.write_today("2026-08-24", {"s": _daily_entry()}, 1.0)
    assert logger.read_daily("2026-08-24").date == "2026-08-24"


def test_legacy_daily_record_defaults_new_cost_fields_to_unknown():
    restored = DailyRecord.from_dict({
        "date": "2026-08-24",
        "reconstructed": False,
        "generated_at": 1.0,
        "sessions": {"s": {
            "project": "proj", "model": "Opus",
            "first_ts": 1.0, "last_ts": 2.0,
            "input_tokens": 10, "output_tokens": 5,
            "cache_read_tokens": 20, "cache_creation_tokens": 2,
            "session_cumulative_cost_usd": 1.0,
        }},
    })
    assert restored.sessions["s"].estimated_cost_usd is None
    assert restored.sessions["s"].last_observed_total_cost_usd is None
```

- [ ] **Step 2: Run targeted history tests RED**

Run: `./.venv/bin/pytest tests/test_cc_history.py -v`

Expected: FAIL for unknown constructor fields, absent total, and missing `read_daily`.

- [ ] **Step 3: Implement backward-compatible history fields**

Append optional dataclass fields after the existing required fields:

```python
estimated_cost_usd: float | None = None
last_observed_total_cost_usd: float | None = None
last_raw_cost_usd: float | None = None
cost_counter_resets: int = 0
last_cost_ts: float | None = None
```

Serialize each field explicitly. In `from_dict`, use `dict.get`, numeric
conversion only when non-null, and default reset count to zero. Add
`totals.estimated_cost_usd` with the same null-propagation rule as the design.
Extend the existing reconstruction and retention assertions so reconstructed
sessions have `estimated_cost_usd is None` and a rolled monthly line preserves
a non-null `estimated_cost_usd` unchanged.

Implement:

```python
def read_daily(self, date_str: str) -> DailyRecord | None:
    path = self.daily_dir / f"{date_str}.json"
    try:
        return DailyRecord.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run history tests GREEN**

Run: `./.venv/bin/pytest tests/test_cc_history.py -v`

Expected: all history tests pass, including legacy record tests.

- [ ] **Step 5: Commit Task 2**

```bash
git add cc_history.py tests/test_cc_history.py
git commit -m "feat: persist estimated daily cost state"
```

---

### Task 3: Attribute normalized cost changes to local dates

**Files:**
- Modify: `cc-session-monitor.py:118-214`
- Modify: `cc-session-monitor.py:214-410`
- Modify: `cc-session-monitor.py:432-464`
- Modify: `tests/test_tui_context.py`

**Interfaces:**
- Consumes: normalized snapshot cost fields from Task 1 and optional restored entries from Task 2.
- Produces: `SessionState.estimated_cost_by_date: dict[str, float | None]`.
- Produces: `SessionState.record_cost_observation(ts: float, observed: float, raw: float | None, resets: int) -> None`.
- Produces: `Monitor(root: Path = CLAUDE_PROJECTS_DIR, snapshot_dir: Path = SNAPSHOT_DIR, velocity_window: int = VELOCITY_WINDOW_SECONDS, restored_date: str | None = None, restored_cost_entries: dict[str, DailySessionEntry] | None = None)`.

- [ ] **Step 1: Write failing observation and restoration tests**

```python
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
    state.record_cost_observation(_local_ts(2026, 8, 24, 9, 5), 10.0, 10.0, 0)
    state.record_cost_observation(_local_ts(2026, 8, 24, 10, 0), 12.5, 0.5, 1)
    assert state.estimated_cost_by_date["2026-08-24"] == 12.5


def test_old_session_without_baseline_has_unknown_today_cost():
    m = _load_monitor_module()
    state = _state_started_at(m, _local_ts(2026, 8, 23, 9, 0))
    state.record_cost_observation(_local_ts(2026, 8, 24, 9, 0), 12.5, 0.5, 1)
    assert state.estimated_cost_by_date["2026-08-24"] is None


def test_restored_daily_baseline_adds_only_new_delta(tmp_path):
    m = _load_monitor_module()
    entry = m.DailySessionEntry(
        project="proj", model="Opus", first_ts=1.0, last_ts=2.0,
        input_tokens=10, output_tokens=5, cache_read_tokens=0,
        cache_creation_tokens=0, session_cumulative_cost_usd=0.5,
        estimated_cost_usd=4.0,
        last_observed_total_cost_usd=10.0,
        last_cost_ts=_local_ts(2026, 8, 24, 10, 0),
    )
    project = tmp_path / "projects" / "-tmp-proj"
    project.mkdir(parents=True)
    _write_jsonl(project / "s.jsonl", [
        _assistant("high", "2026-08-24T09:00:00Z", "r1"),
    ])
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "s.json").write_text(json.dumps({
        "session_id": "s",
        "snapshot_ts": _local_ts(2026, 8, 24, 11, 0),
        "cost": {
            "total_cost_usd": 1.0,
            "observed_total_cost_usd": 11.5,
            "counter_resets": 1,
        },
    }))
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
    state.record_cost_observation(_local_ts(2026, 8, 24, 9, 5), 10.0, 10.0, 0)
    state.record_cost_observation(_local_ts(2026, 8, 24, 9, 6), 2.0, 2.0, 0)
    assert state.estimated_cost_by_date["2026-08-24"] is None


def test_cost_observation_retains_previous_day_total_after_midnight():
    m = _load_monitor_module()
    state = _state_started_at(m, _local_ts(2026, 8, 23, 9, 0))
    state.record_cost_observation(_local_ts(2026, 8, 23, 23, 50), 10.0, 10.0, 0)
    state.record_cost_observation(_local_ts(2026, 8, 24, 0, 10), 11.5, 11.5, 0)
    assert state.estimated_cost_by_date["2026-08-23"] == 10.0
    assert state.estimated_cost_by_date["2026-08-24"] == 1.5
```

- [ ] **Step 2: Run targeted monitor tests RED**

Run: `./.venv/bin/pytest tests/test_tui_context.py -k 'cost_observation or baseline or normalized' -v`

Expected: FAIL because the new state fields and method do not exist.

- [ ] **Step 3: Implement date accounting and restore path**

Add explicit raw/observed fields and implement `record_cost_observation` with
this decision order:

```python
day_key = datetime.fromtimestamp(ts).date().isoformat()
if self.last_cost_observed_total is None:
    started_today = (
        self.first_ts is not None
        and datetime.fromtimestamp(self.first_ts).date().isoformat() == day_key
    )
    self.estimated_cost_by_date.setdefault(day_key, observed if started_today else None)
elif observed < self.last_cost_observed_total:
    self.estimated_cost_by_date[day_key] = None
else:
    delta = observed - self.last_cost_observed_total
    if day_key not in self.estimated_cost_by_date:
        self.estimated_cost_by_date[day_key] = delta
    elif self.estimated_cost_by_date[day_key] is not None:
        self.estimated_cost_by_date[day_key] += delta
```

Update `_apply_snapshot` so invalid/missing raw or observed values stay `None`
and do not append a cost point. Use observed totals for `cost_points`, replacing
the final point when timestamps are equal.

Restore the optional daily entry exactly once when a matching `SessionState`
is created, before applying its first new snapshot.

- [ ] **Step 4: Persist cost state from `entries_for_date`**

Populate all Task 2 fields from `SessionState`, using the target date's value
from `estimated_cost_by_date`. Continue writing the legacy raw field from
`hook_raw_cost_usd`.

- [ ] **Step 5: Run monitor and history tests GREEN**

Run:

```bash
./.venv/bin/pytest tests/test_tui_context.py tests/test_cc_history.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add cc-session-monitor.py tests/test_tui_context.py
git commit -m "feat: attribute estimated cost to local dates"
```

---

### Task 4: Render scoped estimates and wire startup restoration

**Files:**
- Modify: `cc-session-monitor.py:558-750`
- Modify: `cc-session-monitor.py:890-965`
- Modify: `tests/test_tui_context.py`

**Interfaces:**
- Consumes: `hook_observed_cost_usd` for Active and `estimated_cost_by_date` for Today.
- Produces: `build_table(title: str, sessions: list[SessionState], now: float, velocity_window: int, scope_cutoff: float | None = None, cost_scope_date: str | None = None) -> Table`.
- Consumes: `HistoryLogger.read_daily(date_str)` before the first monitor refresh.

- [ ] **Step 1: Write failing rendering tests**

```python
def _cost_state(m, now, session_estimate, today_estimate=None, sid="cost-state"):
    state = m.SessionState(sid, "proj", Path(f"/tmp/{sid}.jsonl"))
    state.first_ts = now
    state.last_ts = now
    state.hook_ts = now
    state.hook_observed_cost_usd = session_estimate
    state.estimated_cost_by_date["2026-08-24"] = today_estimate
    return state


def test_active_and_today_tables_use_different_cost_scopes():
    m = _load_monitor_module()
    now = _local_ts(2026, 8, 24, 12, 0)
    state = _cost_state(m, now, session_estimate=13.0, today_estimate=3.0)
    active = m.build_table("Active", [state], now, 30)
    today = m.build_table("Today", [state], now, 30,
                          scope_cutoff=m.local_midnight_ts(now),
                          cost_scope_date="2026-08-24")
    assert _cells(active, 10)[0] == "$13.00"
    assert _cells(today, 10)[0] == "$3.00"
    assert active.columns[10].header == "Est. session $"
    assert today.columns[10].header == "Est. today $"


def test_unknown_cost_makes_total_unknown():
    m = _load_monitor_module()
    now = _local_ts(2026, 8, 24, 12, 0)
    known = _cost_state(m, now, session_estimate=2.0, sid="known")
    unknown = _cost_state(m, now, session_estimate=None, sid="unknown")
    table = m.build_table("Active", [known, unknown], now, 30)
    assert _cells(table, 10)[-1] == "—"


def test_missing_snapshot_cost_renders_dash_not_zero():
    m = _load_monitor_module()
    now = _local_ts(2026, 8, 24, 12, 0)
    monitor = m.Monitor.__new__(m.Monitor)
    monitor.sessions = {}
    monitor._snapshot_mtimes = {}
    monitor._restored_date = None
    monitor._restored_cost_entries = {}
    monitor._apply_snapshot({
        "session_id": "missing",
        "snapshot_ts": now,
        "cost": {"total_cost_usd": None, "observed_total_cost_usd": None},
    })
    table = m.build_table("Active", list(monitor.sessions.values()), now, 30)
    assert _cells(table, 10)[0] == "—"
```

- [ ] **Step 2: Run rendering tests RED**

Run: `./.venv/bin/pytest tests/test_tui_context.py -k 'cost_scope or unknown_cost or missing_snapshot_cost' -v`

Expected: FAIL because the active/day cost distinction and complete-total rule are absent.

- [ ] **Step 3: Implement scoped rendering**

Select the row estimate with:

```python
row_cost = (
    s.estimated_cost_by_date.get(cost_scope_date)
    if cost_scope_date is not None
    else s.hook_observed_cost_usd
)
```

Set the column header according to scope. Track `all_costs_known`; render the
total only when it remains true, including a known total of `$0.00`.

Use normalized observed points for `$/h`. Update footer wording so `●` means
snapshot availability, cost can be `—`, and all dollar figures are estimates.

- [ ] **Step 4: Wire history restoration before Monitor construction**

In `main`, construct `HistoryLogger` first, read today's daily record, then
pass its sessions and date to `Monitor`. Preserve `--no-log`: when disabled,
do not read or write history and pass no restored entries.

- [ ] **Step 5: Run TUI, installer, and CLI tests GREEN**

Run:

```bash
./.venv/bin/pytest tests/test_tui_context.py tests/test_install_hook.py -v
./.venv/bin/python cc-session-monitor.py --help
```

Expected: tests pass and help exits zero.

- [ ] **Step 6: Commit Task 4**

```bash
git add cc-session-monitor.py tests/test_tui_context.py tests/test_install_hook.py
git commit -m "feat: render scoped estimated costs"
```

---

### Task 5: Align documentation and complete cross-platform verification

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `cc-session-monitor.py` module comments/footer copy
- Modify: `cc-monitor-hook.sh` header comments
- Modify: `cc-monitor-hook.ps1` header comments

**Interfaces:**
- Documents: raw estimate, observed session estimate, estimated daily cost, resets, unknown values, and authoritative billing sources.
- Preserves: installation commands and existing CLI options.

- [ ] **Step 1: Add a documentation regression test or exact-text audit**

Add a test in `tests/test_tui_context.py` for footer language and run an exact
repository scan that must return no user-facing claims of authoritative hook
cost:

```python
def test_footer_labels_dollar_values_as_estimates(tmp_path):
    layout = build_layout(Monitor(tmp_path / "p", tmp_path / "s"), 30)
    text = _footer_text(layout)
    assert "estimate" in text.lower()
    assert "accurate Cost" not in text
    assert "authoritative" not in text
```

Run:

```bash
./.venv/bin/pytest tests/test_tui_context.py::test_footer_labels_dollar_values_as_estimates -v
rg -n "accurate Cost|authoritative cost|only ACCURATE|accurate cumulative" README.md CLAUDE.md cc-session-monitor.py cc-monitor-hook.sh cc-monitor-hook.ps1
```

Expected: the test fails and the scan reports the stale claims before documentation changes.

- [ ] **Step 2: Update all user-facing documentation**

Document:

- `total_cost_usd` is a client-side standard-list-price estimate;
- Pro/Max included usage is not a per-session charge;
- Active shows observed estimated session cost;
- Today and history use attributable daily deltas or `null`/`—`;
- legacy `session_cumulative_cost_usd` is retained but deprecated;
- Claude Console, Usage and Cost API, or provider console is authoritative;
- the updated hook must be reinstalled after upgrading the repository.

- [ ] **Step 3: Run documentation test and stale-claim scan GREEN**

Run the Task 5 Step 1 commands again.

Expected: the test passes and `rg` exits 1 with no stale claims.

- [ ] **Step 4: Run the complete verification matrix**

Run:

```bash
./.venv/bin/pytest tests/ -v
./.venv/bin/python -m py_compile cc-session-monitor.py cc_history.py
./.venv/bin/python cc-session-monitor.py --help
bash -n cc-monitor-hook.sh run-monitor.sh
git diff --check
```

If `pwsh` is installed, also run:

```powershell
$tokens=$null
$errors=$null
[System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path 'cc-monitor-hook.ps1').Path,
    [ref]$tokens,
    [ref]$errors)
if ($errors.Count -gt 0) { $errors; exit 1 }
```

Expected: all applicable checks pass with only intentional platform skips.

- [ ] **Step 5: Commit Task 5**

```bash
git add README.md CLAUDE.md cc-session-monitor.py cc-monitor-hook.sh cc-monitor-hook.ps1 tests/test_tui_context.py
git commit -m "docs: clarify estimated cost semantics"
```

- [ ] **Step 6: Request final code review**

Review the complete diff from spec commit `0d78ba5` through `HEAD` against
`docs/superpowers/specs/2026-08-24-estimated-cost-accounting-design.md`.
Resolve every Critical or Important finding, add regression tests for fixes,
and rerun the complete verification matrix before handoff.
