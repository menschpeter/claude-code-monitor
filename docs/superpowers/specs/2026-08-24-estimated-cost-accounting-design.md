# Estimated Cost Accounting Design

**Date:** 2026-08-24

**Status:** Approved in chat; awaiting review of this written specification

## Context

Claude Code 2.1.241 still sends `cost.total_cost_usd` to a configured
`statusLine` command. Anthropic now documents that value as an estimated
session cost calculated locally at standard API list prices. It can differ
from an API bill, does not include negotiated discounts or promotional
pricing, and is not a billing amount for subscription usage included in a
Pro, Max, Team, or Enterprise seat.

The monitor currently describes this value as accurate or authoritative.
It also shows lifetime session estimates in a panel titled "Today" while
scoping the token columns to local midnight. The daily history explicitly
stores the same lifetime counter on every day a session is active, so its
cost total is not spend attributable to that calendar day.

The upstream counter is not guaranteed to remain monotonic. It resets when
`/clear` starts a new session, and Claude Code has had status-line bugs that
reset it within an existing session. The current monitor replaces its stored
value when this happens, causing displayed cost to fall and making `$/h`
temporarily disappear.

## Goals

1. Describe every dollar value as an estimated API list-price equivalent,
   never as authoritative billing data.
2. Preserve a monotonic estimated session total when the raw Claude counter
   resets under the same session ID.
3. Show estimated cost attributable to the current local calendar day in the
   "Today" panel, rather than the raw lifetime counter.
4. Preserve day attribution across ordinary monitor restarts.
5. Represent unavailable or unattributable cost as unknown, never as zero.
6. Keep existing history files readable and retain the legacy cumulative
   field for downstream consumers.
7. Keep the status-line hook local, fast, and free of network calls or price
   tables.

## Non-goals

- Reproducing an Anthropic invoice locally.
- Calling the Usage and Cost API, Claude Console, or a cloud-provider billing
  API from the hook or TUI.
- Converting Pro, Max, Team, or Enterprise plan allowance into dollars.
- Reconstructing costs for dates where no usable hook observation exists.
- Deriving cost from JSONL token placeholders or adding a model price table to
  this repository.
- Rewriting existing monthly history files in place.

## Terminology

- **Raw estimate:** Claude Code's current `cost.total_cost_usd` value.
- **Observed session estimate:** A monotonic total constructed from raw
  estimates seen for one session ID. It includes new epochs after a raw
  counter reset.
- **Estimated daily cost:** The positive change in the observed session
  estimate attributed to one local calendar date.
- **Known daily cost:** A daily estimate with a valid baseline. Unknown values
  are serialized as JSON `null` and rendered as `—`.

## Architecture

The hook remains the only source of dollar estimates. In addition to copying
Claude's raw estimate, it uses the previous atomic snapshot for the same
session to maintain a monotonic observed session estimate. The monitor reads
that normalized value, accumulates date-scoped deltas, renders either the
session or daily scope, and persists enough state in the daily record to
continue after a monitor restart.

```text
Claude statusLine payload
        │ raw total_cost_usd
        ▼
Hook snapshot normalizer
        │ raw + monotonic observed total + reset count
        ▼
Monitor SessionState
        │ active scope / local-date deltas
        ├──────────────► TUI
        ▼
DailyRecord
        │ daily estimate + last observed baseline
        ▼
Restart restoration / monthly retention
```

## Snapshot schema and hook behavior

The existing `cost` object remains, with three additions:

```json
{
  "cost": {
    "total_cost_usd": 1.25,
    "observed_total_cost_usd": 3.75,
    "last_valid_raw_cost_usd": 1.25,
    "counter_resets": 2,
    "total_duration_ms": 45000,
    "total_api_duration_ms": 2300,
    "total_lines_added": 10,
    "total_lines_removed": 2
  }
}
```

`total_cost_usd` is the raw current estimate and is `null` when the incoming
payload omits it or supplies `null`. It must not default to zero.

For a valid raw estimate `new_raw`, the hook reads the previous atomic
snapshot for the same session and derives the new observed total:

```text
no previous valid raw:
    observed_total = new_raw

new_raw >= previous_raw:
    observed_total = previous_observed_total + (new_raw - previous_raw)

new_raw < previous_raw:
    observed_total = previous_observed_total + new_raw
    counter_resets += 1
```

An older snapshot without `observed_total_cost_usd` is upgraded by treating
its raw `total_cost_usd` as both the previous raw and previous observed total.
This keeps hook upgrades continuous without rewriting old files.

If the new payload lacks a valid raw estimate, the snapshot exposes
`total_cost_usd: null` and `observed_total_cost_usd: null` so the UI reports
unknown. `last_valid_raw_cost_usd` and `counter_resets` retain their previous
values internally, allowing a later valid payload to continue normalization.

Both POSIX and PowerShell hooks implement the same algorithm. Snapshot writes
remain temp-file-plus-rename operations. No journal, network access, price
lookup, or unbounded file is added.

## In-memory cost accounting

`SessionState` keeps these distinct concepts:

- `hook_raw_cost_usd`: latest valid raw Claude estimate or `None`.
- `hook_observed_cost_usd`: latest monotonic observed total or `None`.
- `hook_cost_reset_count`: reset count supplied by the hook.
- `cost_points`: `(timestamp, observed_total)` points for `$/h`.
- `estimated_cost_by_date`: local ISO date to estimated daily cost or `None`.
- `last_cost_observation`: timestamp and observed total used for day deltas.

On each valid snapshot, the monitor computes the non-negative difference
between the new and previous observed total. Because reset normalization
happens in the hook, a decrease at this layer means the normalization state
was lost or the snapshot is inconsistent. In that case the current day's
estimate becomes unknown instead of guessing.

The delta is assigned to the local date of `snapshot_ts`. An API response that
crosses midnight is therefore attributed to the day on which Claude Code
emits the completed status-line update.

When the first observation for a session has no restored baseline:

- If the session's first JSONL usage timestamp is on the same local date, its
  observed total is attributable to that date.
- If the session predates the date, daily cost is unknown until a reliable
  baseline exists. The monitor does not assign an old lifetime estimate to
  today.

The per-date map retains at least the current and previous local dates so the
post-midnight flush can write yesterday's final value.

## Restart restoration

Each daily session entry persists the last normalized baseline used for that
date. At startup, `HistoryLogger` loads today's existing daily record, if any,
before the monitor consumes hook snapshots. The monitor restores the saved
daily estimate, last observed total, last raw value, reset count, and last
cost timestamp for matching session IDs.

The first new snapshot then contributes only its positive normalized delta.
If the restored observed total exceeds the new observed total, continuity
cannot be proven; today's cost becomes unknown.

If no daily record exists, existing reconstruction behavior remains honest:
token fields may be reconstructed from JSONL, while cost fields remain
`null`. A newly started session whose first usage is today can still establish
a known daily estimate from its first observed total.

## Daily history schema

`DailySessionEntry` gains the following fields:

```json
{
  "estimated_cost_usd": 4.25,
  "last_observed_total_cost_usd": 12.75,
  "last_raw_cost_usd": 1.50,
  "cost_counter_resets": 2,
  "last_cost_ts": 1787583000.0,
  "session_cumulative_cost_usd": 1.50
}
```

`estimated_cost_usd` is the date-attributable estimate. The baseline fields
exist to resume accounting and are not independently summed.

`session_cumulative_cost_usd` remains as the legacy raw Claude counter for
backward compatibility. It is deprecated, may reset, and must no longer be
presented as a daily total.

`DailyRecord.totals` gains `estimated_cost_usd`. Its value is `null` if any
included session has unknown estimated daily cost; otherwise it is the sum of
the per-session daily estimates. The legacy
`totals.session_cumulative_cost_usd` remains unchanged so old consumers do not
break.

`from_dict` treats all new fields as optional. Old daily and monthly records
therefore continue to deserialize. Retention copies the new fields without
special handling because it already rolls complete JSON records into monthly
JSONL.

## TUI behavior

The active panel uses the normalized observed session estimate and labels the
column `Est. session $`. The Today panel uses `estimated_cost_by_date[today]`
and labels the column `Est. today $`.

Rows render `—` when their relevant estimate is unknown. A total is rendered
only when every visible row has a known value; otherwise the total is `—` to
avoid presenting a partial sum as complete.

`$/h` is calculated from normalized observed-total points. Counter resets no
longer create a negative rate. It remains an extrapolated short-window
estimate and is labeled accordingly in the footer and README.

The hook marker continues to mean that a hook snapshot exists, not that every
field is available. The legend explicitly separates hook availability from
cost availability.

The status bar uses `est $X` for a valid raw estimate and `est —` when the
field is missing. It does not claim billing accuracy.

## Error handling

- Missing or null raw cost: preserve `None`; do not create a zero sample.
- Non-numeric raw cost: treat as unavailable and keep the status line alive.
- Invalid previous snapshot: ignore its normalization state and start from
  the current raw estimate; daily attribution remains subject to the first
  observation rules.
- Normalized total decreases in the TUI: mark the affected date unknown.
- JSONL-only session: cost remains unknown.
- Reconstructed day: cost remains unknown unless a future explicit source can
  prove attribution.
- Missing `jq`: retain the existing degraded status-line message. The TUI does
  not claim cost availability from `_last-raw.json`.

## Documentation changes

The README, module comments, hook headers, TUI footer, and column descriptions
will use these terms consistently:

- "estimated API list-price equivalent"
- "not authoritative billing data"
- "not a subscription charge for included plan usage"
- "use Claude Console, the Usage and Cost API, or the cloud-provider billing
  console for billing"

Statements that hook-backed cost is "accurate" or `$/h` is "authoritative"
will be removed. The history section will distinguish the new daily estimate
from the retained legacy cumulative field.

## Testing strategy

All behavioral changes follow red-green-refactor TDD.

Hook tests cover:

- first valid raw estimate;
- increasing raw estimates;
- reset to a lower raw estimate;
- missing raw estimate;
- upgrade from a legacy snapshot;
- parity of the serialized POSIX and PowerShell schemas where the platform can
  execute the hook.

Monitor tests cover:

- missing cost remains `None` and renders `—`;
- active rows use observed session estimate;
- Today rows use date-scoped estimate;
- multi-day sessions do not leak lifetime cost into Today;
- normalized resets keep `$/h` non-negative and continuous;
- an old session without a baseline produces unknown daily cost;
- a session first observed and started today can establish a known daily cost;
- a restored daily baseline continues with only the new delta;
- a decreasing normalized total invalidates daily attribution.

History tests cover:

- new-field round trip;
- legacy record compatibility;
- daily total becomes `null` if one session is unknown;
- current and previous date values survive midnight flush behavior;
- reconstructed records keep estimated cost `null`;
- monthly retention preserves new fields.

The full existing test suite, Python syntax checks, Bash syntax checks, CLI
help smoke test, and PowerShell parser check where `pwsh` is available remain
the completion gate.

## Acceptance criteria

1. Claude Code 2.1.241 payloads continue to produce valid snapshots and status
   lines on POSIX and Windows.
2. Missing cost never appears as `$0` or as a known estimate.
3. A raw sequence `10.00 → 12.00 → 0.50 → 1.00` yields observed session
   estimates `10.00 → 12.00 → 12.50 → 13.00` and one detected reset.
4. A session with yesterday's cost does not contribute that amount to today's
   cost without a valid baseline and positive delta.
5. Restarting the monitor with today's daily record continues from the stored
   baseline without double-counting.
6. Existing history files deserialize without migration.
7. The TUI and README never describe local estimates as actual or authoritative
   billing amounts.
8. No price table or network call is introduced.
