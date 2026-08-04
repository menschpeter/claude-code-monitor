# Effort Level in the TUI Footer — Design

**Goal:** Surface the reasoning effort level of the currently active session in the monitor's TUI, so a high `$/h` can be read together with the setting that drives it.

**Scope:** One new footer line. No new table column, no change to the snapshot format, no change to history persistence.

---

## Decisions

Four choices were settled during brainstorming. Each is recorded with its rationale, because the cheaper-looking alternatives are the ones a future reader would reach for.

| Decision | Choice | Why |
|---|---|---|
| Placement | Footer line, not a table column | The table already carries 12 columns; effort is a per-session setting that changes rarely, so it does not earn permanent column width. |
| Value when sessions differ | Effort of the session with the newest activity, annotated with its session ID | A single unlabelled value across differing sessions would be ambiguous. The annotation makes the scope explicit. |
| History persistence | Not persisted | Keeps the daily-file schema and the "old files lack the field" migration question out of scope. Can be added later without reworking this design. |
| Data source | JSONL transcript, not the hook snapshot | See below. |

### Why the JSONL, not the hook

The statusLine payload does carry `effort.level` (present only when the model supports effort). Using it would mean editing both hooks, re-running `--install-hook`, and restarting Claude Code — for one footer line, and only for `●` sessions.

The JSONL transcript already carries the value as a flat `effort` string on `assistant` entries. Verified against live transcripts: 2762 occurrences across this machine's projects, and the current session's newest `assistant` entry reads `effort: "high"` on `claude-opus-5` with a timestamp seconds old. So the JSONL value is both current and free to read — `Monitor.refresh` already walks exactly these entries.

Consequences of this choice, both accepted:

- "Newest activity" is determined by the JSONL timestamp (`last_ts`) rather than by the hook snapshot time. This is a superset: it also covers `○` (JSONL-only) sessions.
- The value reflects the effort **used for the last request**, not the live setting. If the user changes effort and sends no request, the footer stays on the previous value until the next assistant turn. Before the first assistant turn there is no value at all.

---

## Components

### 1. `extract_effort` in `cc_history.py`

```python
def extract_effort(entry: dict) -> str | None:
    """Pull the effort level out of one JSONL line, or None."""
```

Returns the `effort` string for `assistant` entries, `None` for every other entry type and when the field is absent.

It lives in `cc_history.py` beside `extract_usage` because `CLAUDE.md` designates that module as the owner of the shared JSONL parsing helpers, and explicitly forbids re-implementing them in the TUI. `cc_history` itself does not consume the value — the placement is about keeping knowledge of JSONL field names in one module, not about the caller.

`cc-session-monitor.py` imports it as `_extract_effort`, matching the existing `_`-prefixed alias convention.

### 2. `SessionState.effort: str | None`

New field, default `None`. Holds the effort of the most recent `assistant` entry seen for that session.

Deliberately **not** a field on `UsageSample`: `_merge_sample` performs a per-field MAX merge keyed by `requestId`, and MAX has no meaningful semantics for a string. The implementation carries a comment to this effect, so the omission does not read as an oversight.

### 3. `Monitor.refresh` integration

Call `_extract_effort(entry)` and assign to `state.effort` when it returns a value. The call sits **before** the existing `if sample is None: continue` guard, so an `assistant` entry that carries no `usage` block still contributes its effort.

Lines are read in file order, so last-write-wins yields the most recent value. No extra bookkeeping.

### 4. `_fmt_effort_footer(sessions) -> str | None`

Pure function, modelled on the already-tested `_fmt_ctx` helper.

- Input: the Active-window session list — the same list the Active panel renders.
- Selects the session with the largest `last_ts`.
- Returns the **complete formatted line** as a plain string, including the session-ID annotation — e.g. `"effort: high  (2e2615e8, most recent)"` — so that formatting and selection are tested together and `build_layout` only has to append it.
- Returns `None` if the selected session has no effort.

Returning `None` when the newest session lacks a value is intentional: falling through to another session would display a value under the label "most recent" that does not belong to the most recent session. An absent line is honest; a mislabelled one is not.

Empty session list returns `None`.

### 5. Footer rendering in `build_layout`

The footer grows from `size=3` to `size=4`. The new line joins the existing legend block in the same `dim italic` style:

```
effort: high  (2e2615e8, most recent)
```

The session ID is the same 8-character prefix the Session column already shows. English, consistent with the rest of the footer.

When `_fmt_effort_footer` returns `None` the line is omitted and the footer stays at `size=3`.

**No threshold colouring.** `CLAUDE.md` records that only `$/h` uses red/yellow/green, because cost is the only dimension where "high" actually means "bad". Effort is informational and stays neutral.

---

## Non-Goals

- No `Effort` or `Model` table column.
- No change to `cc-monitor-hook.sh` or `cc-monitor-hook.ps1`, and therefore no re-install and no Claude Code restart to pick this up.
- No change to the snapshot JSON schema.
- No change to `cc_history.py`'s persistence schema, `DailySessionEntry`, or the files under `~/.claude/session-monitor/history/`.
- The hook's `effort.level` payload field stays unread.

---

## Testing

**`tests/test_cc_history.py`**

- `extract_effort` returns the value for an `assistant` entry that has the field.
- Returns `None` for an `assistant` entry without the field.
- Returns `None` for non-`assistant` entry types (`user`, `mode`, `attachment`).

**`tests/test_tui_context.py`**

- `_fmt_effort_footer` returns a line carrying the effort **and** the ID prefix of the session with the newest `last_ts`, when sessions carry different levels.
- Returns `None` when the newest session has no effort, even though an older session does.
- Returns `None` for an empty session list.
- `Monitor.refresh` sets `SessionState.effort` from the newest `assistant` line, including when that line carries no `usage` block.

Existing tests must stay green — in particular the `build_table` and `_fmt_ctx` tests, which this change does not touch.

## Verification

- `./.venv/bin/pytest tests/ -v`
- Run the TUI against live sessions and confirm the footer line appears with the expected value, and that it disappears rather than showing a stale or mislabelled value when the newest active session has no effort.
