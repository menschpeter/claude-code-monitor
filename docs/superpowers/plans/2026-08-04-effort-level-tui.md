# Effort Level in the TUI Footer — Implementation Plan

**Executed in `6625032..5c49594`.** All tasks below shipped; checkboxes are left unticked as the historical record of the plan as written, not as a to-do list.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the reasoning effort level of the most recently active session in the monitor's TUI footer, so a high `$/h` can be read together with the setting that drives it.

**Architecture:** Claude Code writes a flat `effort` string on `assistant` entries in its JSONL transcripts. A new parsing helper in `cc_history.py` reads it; `Monitor.refresh` — which already walks exactly those entries — stores the newest value on `SessionState.effort`. A pure formatting helper picks the session with the newest activity and renders one footer line, which `build_layout` appends to the existing legend block. No hook change, so no `--install-hook` and no Claude Code restart.

**Tech Stack:** Python 3.10+, stdlib only, `rich` for the TUI. Tests use `pytest` with the existing `tests/conftest.py` fixtures.

## Global Constraints

- Python 3.10+ syntax (`str | None` unions are already used throughout).
- No new dependencies. stdlib plus the already-required `rich`.
- Do **not** modify `cc-monitor-hook.sh`, `cc-monitor-hook.ps1`, or the snapshot JSON schema. The hook's `effort.level` payload field stays unread.
- Do **not** modify `cc_history.py`'s persistence schema, `DailySessionEntry`, `DailyRecord`, or anything under `~/.claude/session-monitor/history/`.
- Do **not** add a table column. The 12 existing columns stay exactly as they are.
- TUI-visible strings are **English**, matching the existing footer.
- Effort gets **no** red/yellow/green threshold colouring. Only `$/h` is a warning metric; effort is informational and uses the footer's existing `dim italic`.
- Effort must **not** become a field on `UsageSample`. `merge_sample` does a per-field MAX merge, which has no meaningful semantics for a string.
- All existing tests must stay green. Run the full suite, not just new tests.

**Spec:** `docs/superpowers/specs/2026-08-04-effort-level-tui-design.md`

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `cc_history.py` | Modify (add `extract_effort` after `extract_usage`, ~line 74) | Owns all JSONL field-name knowledge, per `CLAUDE.md`. Adds one parsing helper; no schema change. |
| `cc-session-monitor.py` | Modify (import block ~line 71-80; `SessionState` ~line 116-150; `Monitor.refresh` ~line 282-290; new helper near `_fmt_ctx` ~line 495; `build_layout` ~line 666-684) | Holds the session state, the formatting helper, and the layout wiring. |
| `tests/test_cc_history.py` | Modify (append) | Tests for `extract_effort`. |
| `tests/test_tui_context.py` | Modify (append) | Tests for the refresh wiring, `_fmt_effort_footer`, and the footer rendering. |
| `README.md` | Modify (~line 210) | User-facing documentation of the footer line and its caveat. |
| `CLAUDE.md` | Modify (append a paragraph to the Architecture section) | Records the data-source invariant so a future reader does not "fix" it toward the hook. |

`cc_history.py` does not consume the effort value itself. The helper lives there anyway because `CLAUDE.md` designates that module as the owner of the shared JSONL parsing helpers and forbids re-implementing them in the TUI.

---

### Task 1: `extract_effort` parsing helper

**Files:**
- Modify: `cc_history.py` (insert after `extract_usage`, which ends at line 74)
- Test: `tests/test_cc_history.py` (append at end of file)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `extract_effort(entry: dict) -> str | None` — returns the effort string for `assistant` entries that carry a non-empty string `effort` field, `None` otherwise. Task 2 imports this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cc_history.py`. Note the import at the top of that file is `from cc_history import DailyRecord, DailySessionEntry, HistoryLogger` — add `extract_effort` to it:

```python
def test_extract_effort_returns_level_from_assistant_entry():
    assert extract_effort({"type": "assistant", "effort": "xhigh"}) == "xhigh"


def test_extract_effort_none_when_field_absent():
    assert extract_effort({"type": "assistant", "message": {"usage": {}}}) is None


def test_extract_effort_none_for_non_assistant_types():
    for entry_type in ("user", "mode", "attachment", "file-history-snapshot"):
        entry = {"type": entry_type, "effort": "high"}
        assert extract_effort(entry) is None, entry_type


def test_extract_effort_none_for_empty_string():
    assert extract_effort({"type": "assistant", "effort": ""}) is None


def test_extract_effort_none_for_non_string_effort():
    # The statusLine payload nests this as {"level": "high"}. If the JSONL ever
    # adopts that shape we want None rather than a dict rendered into the footer.
    assert extract_effort({"type": "assistant", "effort": {"level": "high"}}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_cc_history.py -k effort -v`
Expected: collection error — `ImportError: cannot import name 'extract_effort' from 'cc_history'`.

- [ ] **Step 3: Write minimal implementation**

Insert into `cc_history.py` directly after `extract_usage` (after line 74, before `def merge_sample`):

```python
def extract_effort(entry: dict) -> str | None:
    """Pull the reasoning effort level out of one JSONL line, or None.

    Claude Code writes a flat `effort` string (e.g. "high", "xhigh") on
    `assistant` entries, reflecting the effort used for that request — so the
    newest assistant entry carries the session's current level.

    The isinstance guard is deliberate: the statusLine payload nests the same
    information as {"level": ...}, and if the JSONL ever switches to that shape
    we want to fail closed rather than render a dict into the UI.
    """
    if entry.get("type") != "assistant":
        return None
    effort = entry.get("effort")
    if isinstance(effort, str) and effort:
        return effort
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_cc_history.py -k effort -v`
Expected: 5 passed.

Then the full suite: `./.venv/bin/pytest tests/ -q`
Expected: all previously passing tests still pass (30 passed, 3 skipped before this task, so 35 passed, 3 skipped now).

- [ ] **Step 5: Commit**

```bash
git add cc_history.py tests/test_cc_history.py
git commit -m "feat: add extract_effort JSONL parsing helper

Reads the flat effort string Claude Code writes on assistant entries.
Lives beside extract_usage because CLAUDE.md designates cc_history as
the owner of JSONL field-name knowledge, even though the history logger
does not consume this value itself.

Guards on isinstance(str) so a future switch to the statusLine payload's
nested {\"level\": ...} shape fails closed instead of rendering a dict."
```

---

### Task 2: Capture effort on `SessionState` during refresh

**Files:**
- Modify: `cc-session-monitor.py` (import block lines 71-80; `SessionState` dataclass; `Monitor.refresh` loop around line 287)
- Test: `tests/test_tui_context.py` (append)

**Interfaces:**
- Consumes: `extract_effort` from Task 1, imported as `_extract_effort`.
- Produces: `SessionState.effort: str | None` — the effort of the newest `assistant` entry seen for that session, `None` if none seen. Task 3 reads this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui_context.py`. The file already imports `importlib.util`, `sys`, `time`, and `Path`; add `import json` to the imports at the top.

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_tui_context.py -k effort -v`
Expected: FAIL — `AttributeError: 'SessionState' object has no attribute 'effort'` (or `TypeError` from the dataclass) on the first two, and the third fails the same way.

- [ ] **Step 3: Write minimal implementation**

Three edits in `cc-session-monitor.py`.

**3a.** Add the import alias to the `from cc_history import (...)` block (lines 71-80), after the `extract_usage` line:

```python
    extract_usage as _extract_usage,
    extract_effort as _extract_effort,
```

**3b.** Add the field to `SessionState`. Put it immediately after the `hook_rl5_reset` line, at the end of the hook-derived block but clearly separated, since it is *not* hook-derived:

```python
    # ----- JSONL-derived session settings -----
    # Reasoning effort of the newest `assistant` entry. Deliberately NOT a
    # field on UsageSample: _merge_sample does a per-field MAX merge, and MAX
    # has no meaningful semantics for a string.
    effort: str | None = None
```

**3c.** In `Monitor.refresh`, insert the capture *before* the existing usage extraction. The loop body currently reads:

```python
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                req_id, sample = _extract_usage(entry)
                if sample is None:
                    continue
```

Change it to:

```python
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Before the usage guard below: an assistant entry without a
                # usage block still tells us the session's effort level.
                effort = _extract_effort(entry)
                if effort:
                    state.effort = effort

                req_id, sample = _extract_usage(entry)
                if sample is None:
                    continue
```

Lines are read in file order, so last-write-wins yields the newest value with no extra bookkeeping.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_tui_context.py -k effort -v`
Expected: 3 passed.

Then: `./.venv/bin/pytest tests/ -q`
Expected: 38 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add cc-session-monitor.py tests/test_tui_context.py
git commit -m "feat: capture per-session effort level during JSONL refresh

Stores the newest assistant entry's effort on SessionState. The call
sits before the usage guard so an assistant entry without a usage block
still contributes its level, which the second test pins.

Kept off UsageSample on purpose: merge_sample is a per-field MAX merge
and MAX means nothing for a string."
```

---

### Task 3: `_fmt_effort_footer` formatting helper

**Files:**
- Modify: `cc-session-monitor.py` (insert after `_fmt_ctx`, which ends at line 508, before `def build_table`)
- Test: `tests/test_tui_context.py` (append)

**Interfaces:**
- Consumes: `SessionState.effort` and `SessionState.last_ts` from Task 2.
- Produces: `_fmt_effort_footer(sessions: list[SessionState]) -> str | None` — the complete footer line including the session-ID annotation, or `None`. Task 4 calls this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui_context.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_tui_context.py -k effort_footer -v`
Expected: FAIL — `AttributeError: module 'cc_session_monitor' has no attribute '_fmt_effort_footer'`.

- [ ] **Step 3: Write minimal implementation**

Insert into `cc-session-monitor.py` after `_fmt_ctx` (after line 508), before `def build_table`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_tui_context.py -k effort_footer -v`
Expected: 5 passed.

Then: `./.venv/bin/pytest tests/ -q`
Expected: 43 passed, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add cc-session-monitor.py tests/test_tui_context.py
git commit -m "feat: add _fmt_effort_footer helper

Selects the session with the newest activity and returns the complete
footer line, annotation included, so selection and formatting are
covered by one test.

Returns None rather than falling through to an older session when the
newest one has no effort: the line names a specific session, so showing
another session's value under that label would be wrong."
```

---

### Task 4: Wire the line into `build_layout` and document it

**Files:**
- Modify: `cc-session-monitor.py` (`build_layout`, footer construction at lines 666-684)
- Modify: `README.md` (after line 210, "A **TOTAL** footer row sums…")
- Modify: `CLAUDE.md` (append a paragraph to the Architecture section)
- Test: `tests/test_tui_context.py` (append)

**Interfaces:**
- Consumes: `_fmt_effort_footer` from Task 3, and the `active` session list already computed at the top of `build_layout`.
- Consumes (test helper): `_state_with(m, session_id, last_ts, effort) -> SessionState`, added to `tests/test_tui_context.py` by Task 3. If Task 3's test additions are not present, define it here — it builds a `SessionState` with `first_ts`/`last_ts` set to `last_ts` and `effort` assigned.
- Produces: the user-visible footer line. Nothing later depends on it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui_context.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_tui_context.py -k build_layout_footer -v`
Expected: FAIL — the first asserts `"effort: xhigh" in ...` against the current 3-line legend and gets `AssertionError`; `layout["footer"].size` is 3, not 4.

- [ ] **Step 3: Write minimal implementation**

In `build_layout`, replace the existing footer construction (currently a single `Text(...)` with three embedded `\n`-joined segments, lines 666-676) with a list plus a derived size:

```python
    footer_lines = [
        "● hook installed (accurate Cost + live Ctx gauge)   "
        "○ JSONL-only (no Cost/Ctx yet)   "
        "install hook: --install-hook",
        "Input/Output/Total = JSONL cumulative (streaming placeholders, slight undercount)   "
        "Ctx = live context window used/size",
        "t/s = total throughput incl. cache   "
        "out/s = generation rate (output tokens only)   "
        "$/h = cost rate (red = burning money)",
    ]
    effort_line = _fmt_effort_footer(active)
    if effort_line:
        footer_lines.append(effort_line)

    footer = Text("\n".join(footer_lines), style="dim italic")
```

**Post-ship correction:** the final review found that appending the effort line last made it the row clipped by rich's line-wrapping below ~130 terminal columns (`size=len(footer_lines)` counts logical lines, but a wrapped legend line consumes extra rendered rows from the same fixed budget). The shipped code instead prepends the effort line — built first, then the three legend lines appended after it — so the highest-value line survives clipping. `size=len(footer_lines)` did not need to change. See `cc-session-monitor.py`'s footer construction in `build_layout` for the current code, and `tests/test_tui_context.py`'s width-120/width-200 render tests for the regression coverage.

And in the `layout.split_column(...)` call, derive the footer size from the line count instead of hard-coding `3`:

```python
        Layout(Align.center(footer), name="footer", size=len(footer_lines)),
```

Deriving the size means the pane cannot get out of sync with the content when the line is absent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_tui_context.py -k build_layout_footer -v`
Expected: 3 passed.

Then: `./.venv/bin/pytest tests/ -q`
Expected: 46 passed, 3 skipped.

- [ ] **Step 5: Document in README.md**

Replace line 210 (`A **TOTAL** footer row sums all sessions currently visible in that panel.`) with:

````markdown
A **TOTAL** footer row sums all sessions currently visible in that panel.

### Effort line

Below the legend at the bottom of the screen, the monitor shows the reasoning effort level of the most recently active session:

```
effort: xhigh  (2e2615e8, most recent)
```

It is scoped to one session — the one with the newest activity, named by its session-ID prefix — because effort is a per-session setting and a single unlabelled value spanning several sessions would be ambiguous. The line is omitted entirely when that session has no effort level, either because its model does not support one or because it has not produced an assistant turn yet.

The value is read from the JSONL transcript, so it reflects the effort **used for the last request** rather than the level currently configured. Change the level with no follow-up request and the line keeps showing the previous value until the next assistant turn. This also means it works for `○`-marked (JSONL-only) sessions, not just hook-backed ones.
````

**Post-ship correction:** the "Below the legend" placement above is what Step 5 originally asked for. The final review's reorder fix (see Step 3's correction note) moved the line to be *first* in the footer, ahead of the legend, so the shipped `README.md` wording says "ahead of the legend lines" instead. Current wording lives in `README.md`'s "Effort line" section.

- [ ] **Step 6: Document in CLAUDE.md**

Append this paragraph to the Architecture section, directly after the "**Where the payload's cache tokens live.**" paragraph:

```markdown
**Effort level comes from the JSONL, not the hook.** Claude Code exposes the reasoning effort twice: as `effort.level` in the statusLine payload (live setting, hook-only, present only when the model supports effort) and as a flat `effort` string on `assistant` entries in the JSONL (effort actually used for that request). The TUI reads the JSONL via `extract_effort` — no hook change, so no re-install, and `○` sessions are covered too. Two consequences are accepted by design: the value lags a `/effort` change until the next assistant turn, and "most recent session" is decided by JSONL `last_ts` rather than snapshot time. `SessionState.effort` is deliberately not a `UsageSample` field — `merge_sample` is a per-field MAX merge, meaningless for a string. `_fmt_effort_footer` returns `None` instead of falling through to an older session, because the rendered line names a specific session. If you ever route the hook's `effort.level` in as well, keep the precedence explicit and update `tests/test_tui_context.py`.
```

- [ ] **Step 7: Verify the docs render and the suite is green**

Run: `./.venv/bin/pytest tests/ -q`
Expected: 46 passed, 3 skipped.

Check the README's new fenced block is not nested inside another fence — the effort-line example uses a plain ``` fence inside a markdown section, so confirm the surrounding document still renders by viewing it (`grep -c '^```' README.md` must stay even).

- [ ] **Step 8: Commit**

```bash
git add cc-session-monitor.py README.md CLAUDE.md tests/test_tui_context.py
git commit -m "feat: show effort level in the TUI footer

Appends the line to the existing legend block and derives the footer
pane size from the line count, so the pane cannot get out of sync when
the line is absent.

Documents the data-source choice in CLAUDE.md, including the two
accepted consequences (value lags a /effort change until the next
assistant turn; most-recent is decided by JSONL timestamp), so it does
not later read as a bug to be fixed toward the hook."
```

---

## Manual Verification

After Task 4, run the TUI against live sessions:

```bash
./.venv/bin/python cc-session-monitor.py
```

Confirm:
1. The footer shows `effort: <level>  (<id>, most recent)` and the level matches the effort of the session you most recently used.
2. The three legend lines are still present and unchanged.
3. No table column was added — still 12 columns.

The value for this machine's current session should read `high`, matching the newest `assistant` entry in its transcript.
