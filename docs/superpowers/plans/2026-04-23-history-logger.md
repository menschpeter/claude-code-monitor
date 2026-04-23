# History Logger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist per-day, per-session usage/cost snapshots to disk so the user has long-term history that survives monitor restarts and JSONL cleanup.

**Architecture:** A new `HistoryLogger` class periodically (every 60 s and on shutdown) writes a JSON file per calendar day to `~/.claude/session-monitor/history/daily/YYYY-MM-DD.json`. Each file contains a per-`session_id` snapshot of cumulative token/cost state observed while that date was the current local date. Daily files older than 2 days are appended to a monthly JSONL file (`history/monthly/YYYY-MM.jsonl`) and then deleted; monthly files beyond the last 12 are deleted. On startup, any daily files missing within the last 3 days are reconstructed from Claude Code's JSONL transcripts (with `reconstructed: true` and `cost_usd: null` to flag that cost data is unreliable in that path). A `--no-log` CLI flag disables the whole subsystem.

**Tech Stack:** Python 3.10+, stdlib only (`json`, `pathlib`, `datetime`, `signal`, `atexit`). Tests use `pytest` (to be added). The logger is a separate module (`cc_history.py`) imported by `cc-session-monitor.py`.

---

## Data Contract

All tasks implement and read the same shape. Defined here once; later tasks refer back.

**`DailyRecord` JSON format** (daily file and monthly JSONL lines use the same shape, minus `generated_at` in monthly):

```json
{
  "date": "2026-04-23",
  "reconstructed": false,
  "generated_at": 1745403600.0,
  "sessions": {
    "abcd1234-...-fullUuid": {
      "project": "foo",
      "model": "Opus",
      "first_ts": 1745382000.0,
      "last_ts": 1745400000.0,
      "input_tokens": 12345,
      "output_tokens": 6789,
      "cache_read_tokens": 98765,
      "cache_creation_tokens": 4321,
      "cost_usd": 2.34
    }
  },
  "totals": {
    "sessions": 1,
    "input_tokens": 12345,
    "output_tokens": 6789,
    "cache_read_tokens": 98765,
    "cache_creation_tokens": 4321,
    "cost_usd": 2.34
  }
}
```

Rules:
- `date`: local-time date string `YYYY-MM-DD`.
- `reconstructed: false` means a live monitor wrote it. `true` means the file was rebuilt from JSONL post-hoc.
- `first_ts` / `last_ts`: unix seconds, earliest/latest usage-entry timestamp **within that date's window** for this session.
- Token fields: **sum of usage samples whose timestamps fall within that date**, not cumulative session totals.
- `cost_usd`: hook-derived. `null` when `reconstructed: true` (we can't attribute a cumulative snapshot to one day after the fact), and `null` when no hook data was observed.
- `totals.cost_usd`: `null` if ANY session's `cost_usd` is `null`, else sum.
- `totals.sessions`: count of distinct session_ids.

**Monthly JSONL** (`history/monthly/YYYY-MM.jsonl`): each line is one full `DailyRecord` JSON object minus the `generated_at` key. Lines are sorted by date ascending.

---

## Files

- Create: `cc_history.py` — the `HistoryLogger` class, `DailyRecord` dataclass, reconstruction logic.
- Create: `tests/__init__.py` — empty.
- Create: `tests/conftest.py` — pytest fixture shared across test files.
- Create: `tests/test_cc_history.py` — unit tests.
- Modify: `cc-session-monitor.py` — import and wire up `HistoryLogger`, add CLI flags, install signal/atexit handlers.
- Modify: `README.md` — document the new history feature and files.
- Modify: `CLAUDE.md` — note the logger responsibility split.

---

## Task 0: Test infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Install pytest into the local venv**

Run:
```bash
./.venv/bin/pip install pytest
```
Expected: `Successfully installed pytest-...` (or "already satisfied").

- [ ] **Step 2: Create `tests/__init__.py`**

Empty file:
```python
```

- [ ] **Step 3: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures for cc-session-monitor tests."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable so tests can `import cc_history`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [ ] **Step 4: Verify pytest can collect (will find zero tests — that's fine)**

Run:
```bash
./.venv/bin/pytest tests/ -q
```
Expected: `no tests ran in 0.XXs` exit code 5 (no tests collected). That's the correct signal; next tasks add tests.

- [ ] **Step 5: Commit**

```bash
git add tests/__init__.py tests/conftest.py
git commit -m "test: add pytest scaffolding for history logger"
```

---

## Task 1: `DailyRecord` dataclass and serialization

**Files:**
- Create: `cc_history.py`
- Create: `tests/test_cc_history.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cc_history.py`:

```python
"""Tests for cc_history.HistoryLogger and DailyRecord."""
from __future__ import annotations

import json

from cc_history import DailyRecord, DailySessionEntry


def test_daily_record_round_trip():
    rec = DailyRecord(
        date="2026-04-23",
        reconstructed=False,
        generated_at=1745403600.0,
        sessions={
            "sess-1": DailySessionEntry(
                project="foo",
                model="Opus",
                first_ts=1745382000.0,
                last_ts=1745400000.0,
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=500,
                cache_creation_tokens=10,
                cost_usd=1.23,
            ),
        },
    )
    as_dict = rec.to_dict()
    # totals computed on serialize
    assert as_dict["totals"] == {
        "sessions": 1,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 500,
        "cache_creation_tokens": 10,
        "cost_usd": 1.23,
    }
    # round-trip through JSON
    restored = DailyRecord.from_dict(json.loads(json.dumps(as_dict)))
    assert restored.date == rec.date
    assert restored.reconstructed is False
    assert restored.sessions["sess-1"].input_tokens == 100
    assert restored.sessions["sess-1"].cost_usd == 1.23


def test_totals_cost_null_if_any_session_null():
    rec = DailyRecord(
        date="2026-04-23",
        reconstructed=True,
        generated_at=0.0,
        sessions={
            "a": DailySessionEntry(
                project="p", model=None, first_ts=0, last_ts=0,
                input_tokens=1, output_tokens=1,
                cache_read_tokens=0, cache_creation_tokens=0,
                cost_usd=None,
            ),
            "b": DailySessionEntry(
                project="p", model=None, first_ts=0, last_ts=0,
                input_tokens=1, output_tokens=1,
                cache_read_tokens=0, cache_creation_tokens=0,
                cost_usd=0.5,
            ),
        },
    )
    assert rec.to_dict()["totals"]["cost_usd"] is None
```

- [ ] **Step 2: Run tests and verify they fail**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: `ModuleNotFoundError: No module named 'cc_history'` or ImportError.

- [ ] **Step 3: Create `cc_history.py` with `DailyRecord` and `DailySessionEntry`**

```python
"""
cc_history — on-disk daily/monthly history for cc-session-monitor.

Writes one JSON file per calendar day under ~/.claude/session-monitor/history/daily/,
rolls days older than two days ago into monthly JSONL files, and keeps at
most the last 12 monthly files.

The live monitor (cc-session-monitor.py) owns the in-memory session state
and hands it to HistoryLogger at each write tick. For gaps when the monitor
was not running, reconstruct_missing_days scans Claude Code's JSONL
transcripts and writes reconstructed=true files with cost_usd=null.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DailySessionEntry:
    project: str
    model: str | None
    first_ts: float
    last_ts: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "model": self.model,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DailySessionEntry":
        return cls(
            project=d["project"],
            model=d.get("model"),
            first_ts=float(d["first_ts"]),
            last_ts=float(d["last_ts"]),
            input_tokens=int(d["input_tokens"]),
            output_tokens=int(d["output_tokens"]),
            cache_read_tokens=int(d["cache_read_tokens"]),
            cache_creation_tokens=int(d["cache_creation_tokens"]),
            cost_usd=(float(d["cost_usd"]) if d.get("cost_usd") is not None else None),
        )


@dataclass
class DailyRecord:
    date: str                                          # YYYY-MM-DD (local)
    reconstructed: bool
    generated_at: float
    sessions: dict[str, DailySessionEntry] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        any_null = any(s.cost_usd is None for s in self.sessions.values())
        total_cost = (
            None if any_null
            else sum(s.cost_usd or 0.0 for s in self.sessions.values())
        )
        return {
            "date": self.date,
            "reconstructed": self.reconstructed,
            "generated_at": self.generated_at,
            "sessions": {sid: s.to_dict() for sid, s in self.sessions.items()},
            "totals": {
                "sessions": len(self.sessions),
                "input_tokens": sum(s.input_tokens for s in self.sessions.values()),
                "output_tokens": sum(s.output_tokens for s in self.sessions.values()),
                "cache_read_tokens": sum(s.cache_read_tokens for s in self.sessions.values()),
                "cache_creation_tokens": sum(s.cache_creation_tokens for s in self.sessions.values()),
                "cost_usd": total_cost,
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DailyRecord":
        return cls(
            date=d["date"],
            reconstructed=bool(d.get("reconstructed", False)),
            generated_at=float(d.get("generated_at", 0.0)),
            sessions={
                sid: DailySessionEntry.from_dict(entry)
                for sid, entry in (d.get("sessions") or {}).items()
            },
        )
```

- [ ] **Step 4: Run tests and verify they pass**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cc_history.py tests/test_cc_history.py
git commit -m "feat(history): add DailyRecord + DailySessionEntry dataclasses"
```

---

## Task 2: `HistoryLogger.write_today` — atomic write of today's file

**Files:**
- Modify: `cc_history.py`
- Modify: `tests/test_cc_history.py`

**Design note:** The logger receives a plain iterable of `DailySessionEntry` objects keyed by session_id for "today". Later tasks will build that iterable from the live `Monitor`; keeping the interface data-only here makes the logger testable without importing `cc-session-monitor.py`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cc_history.py`:

```python
from cc_history import HistoryLogger


def test_write_today_creates_atomic_daily_file(tmp_path):
    logger = HistoryLogger(history_dir=tmp_path, enabled=True)
    entries = {
        "sess-1": DailySessionEntry(
            project="foo", model="Opus",
            first_ts=1745382000.0, last_ts=1745400000.0,
            input_tokens=100, output_tokens=50,
            cache_read_tokens=500, cache_creation_tokens=10,
            cost_usd=1.23,
        ),
    }
    logger.write_today(
        date="2026-04-23",
        entries=entries,
        now_ts=1745403600.0,
    )
    daily_file = tmp_path / "daily" / "2026-04-23.json"
    assert daily_file.exists()
    data = json.loads(daily_file.read_text())
    assert data["date"] == "2026-04-23"
    assert data["reconstructed"] is False
    assert "sess-1" in data["sessions"]
    assert data["totals"]["input_tokens"] == 100
    # no leftover tmp files
    assert not list((tmp_path / "daily").glob(".*.tmp*"))


def test_write_today_disabled_is_noop(tmp_path):
    logger = HistoryLogger(history_dir=tmp_path, enabled=False)
    logger.write_today(date="2026-04-23", entries={}, now_ts=0.0)
    assert not (tmp_path / "daily").exists()
```

- [ ] **Step 2: Run tests and verify they fail**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: ImportError for `HistoryLogger`, or the two new tests fail.

- [ ] **Step 3: Add `HistoryLogger` class to `cc_history.py`**

Append to `cc_history.py`:

```python
class HistoryLogger:
    """
    Writes per-day JSON snapshots of session usage to `history/daily/`
    and rolls old days into `history/monthly/`. See module docstring.

    Atomic writes: every file goes via `<name>.tmp.<pid>` + rename, so a
    crash mid-write never leaves a half-written daily file in place.
    """

    def __init__(self, history_dir: Path, enabled: bool = True) -> None:
        self.history_dir = Path(history_dir)
        self.enabled = enabled
        self.daily_dir = self.history_dir / "daily"
        self.monthly_dir = self.history_dir / "monthly"

    # -- write path -----------------------------------------------------

    def write_today(
        self,
        date: str,
        entries: dict[str, DailySessionEntry],
        now_ts: float,
    ) -> None:
        """Serialize a DailyRecord for `date` and write it atomically.

        Called both periodically and at shutdown. Safe to call when
        `enabled` is False — becomes a no-op.
        """
        if not self.enabled:
            return
        self.daily_dir.mkdir(parents=True, exist_ok=True)

        rec = DailyRecord(
            date=date,
            reconstructed=False,
            generated_at=now_ts,
            sessions=entries,
        )
        target = self.daily_dir / f"{date}.json"
        self._atomic_write_json(target, rec.to_dict())

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
        import os
        tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(target)
```

- [ ] **Step 4: Run tests and verify they pass**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: all four tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cc_history.py tests/test_cc_history.py
git commit -m "feat(history): HistoryLogger.write_today with atomic writes"
```

---

## Task 3: Retention — daily → monthly roll-up

**Files:**
- Modify: `cc_history.py`
- Modify: `tests/test_cc_history.py`

**Policy:** Keep today, yesterday, and the day before yesterday as standalone daily JSON files. Any daily file with a date ≤ (today - 3 days) is appended as one line to `monthly/YYYY-MM.jsonl` (matching the daily file's own month) and then deleted.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cc_history.py`:

```python
from datetime import date as date_cls


def _write_daily_fixture(
    daily_dir: Path,
    date_str: str,
    sessions: dict[str, DailySessionEntry] | None = None,
) -> None:
    daily_dir.mkdir(parents=True, exist_ok=True)
    rec = DailyRecord(
        date=date_str,
        reconstructed=False,
        generated_at=0.0,
        sessions=sessions or {},
    )
    (daily_dir / f"{date_str}.json").write_text(json.dumps(rec.to_dict()))


def test_retention_rolls_old_daily_into_monthly(tmp_path):
    daily_dir = tmp_path / "daily"
    # today = 2026-04-23, so keep 04-21, 04-22, 04-23. Roll older.
    _write_daily_fixture(daily_dir, "2026-04-18")
    _write_daily_fixture(daily_dir, "2026-04-19")
    _write_daily_fixture(daily_dir, "2026-04-20")   # boundary: should roll
    _write_daily_fixture(daily_dir, "2026-04-21")
    _write_daily_fixture(daily_dir, "2026-04-22")
    _write_daily_fixture(daily_dir, "2026-04-23")

    logger = HistoryLogger(history_dir=tmp_path, enabled=True)
    logger.run_retention(today=date_cls(2026, 4, 23))

    # Kept
    assert (daily_dir / "2026-04-21.json").exists()
    assert (daily_dir / "2026-04-22.json").exists()
    assert (daily_dir / "2026-04-23.json").exists()
    # Rolled + removed
    assert not (daily_dir / "2026-04-18.json").exists()
    assert not (daily_dir / "2026-04-19.json").exists()
    assert not (daily_dir / "2026-04-20.json").exists()

    monthly_file = tmp_path / "monthly" / "2026-04.jsonl"
    assert monthly_file.exists()
    lines = [json.loads(l) for l in monthly_file.read_text().splitlines() if l.strip()]
    assert [l["date"] for l in lines] == ["2026-04-18", "2026-04-19", "2026-04-20"]
    # monthly lines drop generated_at
    assert "generated_at" not in lines[0]


def test_retention_across_month_boundary(tmp_path):
    daily_dir = tmp_path / "daily"
    # today = 2026-05-02 → keep 04-30, 05-01, 05-02. Roll 04-25, 04-26.
    _write_daily_fixture(daily_dir, "2026-04-25")
    _write_daily_fixture(daily_dir, "2026-04-26")
    _write_daily_fixture(daily_dir, "2026-04-30")
    _write_daily_fixture(daily_dir, "2026-05-01")
    _write_daily_fixture(daily_dir, "2026-05-02")

    logger = HistoryLogger(history_dir=tmp_path, enabled=True)
    logger.run_retention(today=date_cls(2026, 5, 2))

    apr_file = tmp_path / "monthly" / "2026-04.jsonl"
    assert apr_file.exists()
    dates = [json.loads(l)["date"] for l in apr_file.read_text().splitlines() if l.strip()]
    assert dates == ["2026-04-25", "2026-04-26"]
    # 2026-04-30 stayed as daily (within keep window)
    assert (daily_dir / "2026-04-30.json").exists()


def test_retention_is_idempotent(tmp_path):
    daily_dir = tmp_path / "daily"
    _write_daily_fixture(daily_dir, "2026-04-18")
    _write_daily_fixture(daily_dir, "2026-04-23")
    logger = HistoryLogger(history_dir=tmp_path, enabled=True)
    logger.run_retention(today=date_cls(2026, 4, 23))
    logger.run_retention(today=date_cls(2026, 4, 23))  # second run no-op
    monthly_file = tmp_path / "monthly" / "2026-04.jsonl"
    dates = [json.loads(l)["date"] for l in monthly_file.read_text().splitlines() if l.strip()]
    assert dates == ["2026-04-18"]   # not duplicated
```

- [ ] **Step 2: Run tests and verify they fail**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: `AttributeError: 'HistoryLogger' object has no attribute 'run_retention'`.

- [ ] **Step 3: Implement retention**

Add to `cc_history.py` (inside `HistoryLogger` class):

```python
    # -- retention ------------------------------------------------------

    # Keep today + previous 2 calendar days as standalone daily files.
    DAILY_KEEP_DAYS = 3

    def run_retention(self, today) -> None:
        """Roll old daily JSONs into monthly JSONLs; dedup append.

        `today` is a datetime.date (local). Files whose date ≤
        today - DAILY_KEEP_DAYS are appended to their month's JSONL
        and then deleted. Monthly rollup is dedup-safe: an already-rolled
        date is not appended twice.
        """
        if not self.enabled or not self.daily_dir.exists():
            return

        from datetime import date as date_cls, timedelta
        cutoff = today - timedelta(days=self.DAILY_KEEP_DAYS - 1)
        # cutoff is the OLDEST day still kept. Roll anything < cutoff.

        for daily_file in sorted(self.daily_dir.glob("*.json")):
            try:
                file_date = date_cls.fromisoformat(daily_file.stem)
            except ValueError:
                continue  # ignore unrelated files
            if file_date >= cutoff:
                continue

            try:
                payload = json.loads(daily_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            self._append_to_monthly(file_date, payload)
            daily_file.unlink(missing_ok=True)

    def _append_to_monthly(self, file_date, payload: dict[str, Any]) -> None:
        self.monthly_dir.mkdir(parents=True, exist_ok=True)
        monthly = self.monthly_dir / f"{file_date.strftime('%Y-%m')}.jsonl"

        # Dedup: skip if this date is already present in the file.
        if monthly.exists():
            for line in monthly.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("date") == payload.get("date"):
                    return  # already rolled

        payload = dict(payload)
        payload.pop("generated_at", None)  # monthly lines drop it
        with monthly.open("a") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
```

- [ ] **Step 4: Run tests and verify they pass**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cc_history.py tests/test_cc_history.py
git commit -m "feat(history): daily → monthly retention roll-up"
```

---

## Task 4: Retention — prune monthly files beyond 12

**Files:**
- Modify: `cc_history.py`
- Modify: `tests/test_cc_history.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cc_history.py`:

```python
def test_retention_keeps_at_most_12_monthly_files(tmp_path):
    monthly_dir = tmp_path / "monthly"
    monthly_dir.mkdir(parents=True)
    # 14 months of files — expect the oldest 2 to be deleted.
    for y, m in [
        (2025, 1), (2025, 2), (2025, 3), (2025, 4),
        (2025, 5), (2025, 6), (2025, 7), (2025, 8),
        (2025, 9), (2025, 10), (2025, 11), (2025, 12),
        (2026, 1), (2026, 2),
    ]:
        (monthly_dir / f"{y:04d}-{m:02d}.jsonl").write_text("{}\n")

    logger = HistoryLogger(history_dir=tmp_path, enabled=True)
    logger.run_retention(today=date_cls(2026, 3, 1))

    remaining = sorted(p.name for p in monthly_dir.glob("*.jsonl"))
    assert len(remaining) == 12
    assert "2025-01.jsonl" not in remaining
    assert "2025-02.jsonl" not in remaining
    assert "2025-03.jsonl" in remaining
    assert "2026-02.jsonl" in remaining
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py::test_retention_keeps_at_most_12_monthly_files -v
```
Expected: FAIL — monthly pruning not implemented, 14 files remain.

- [ ] **Step 3: Extend `run_retention` in `cc_history.py`**

Add inside `HistoryLogger`:

```python
    MONTHLY_KEEP_COUNT = 12
```

And at the **end** of `run_retention`, after the daily loop:

```python
        # Prune monthly/ to the MONTHLY_KEEP_COUNT most recent.
        if self.monthly_dir.exists():
            monthlies = sorted(self.monthly_dir.glob("*.jsonl"))
            excess = len(monthlies) - self.MONTHLY_KEEP_COUNT
            if excess > 0:
                for old in monthlies[:excess]:
                    old.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests and verify they pass**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add cc_history.py tests/test_cc_history.py
git commit -m "feat(history): prune monthly files beyond the last 12"
```

---

## Task 5: Reconstruction from JSONL transcripts

**Files:**
- Modify: `cc_history.py`
- Modify: `tests/test_cc_history.py`

**Design note:** `reconstruct_missing_days` is called on startup. It scans `~/.claude/projects/*/*.jsonl`, groups `assistant`-type usage entries by local calendar date, and writes a `DailyRecord` for each missing daily file in the last `DAILY_KEEP_DAYS`. Reconstructed records have `reconstructed=true` and `cost_usd=None` on every session (we cannot attribute a cumulative snapshot cost to one specific day reliably after the fact).

The function reuses `_parse_ts`, `_extract_usage`, `_merge_sample`, and `_humanize_project` from `cc-session-monitor.py` to keep parsing rules consistent. Extract those four helpers into `cc_history.py` and import them from there in `cc-session-monitor.py` in a later task.

- [ ] **Step 1: Move the parsing helpers from `cc-session-monitor.py` into `cc_history.py`**

At the top of `cc_history.py` (after the imports block), add:

```python
from dataclasses import dataclass as _dataclass
from datetime import datetime, timezone


@_dataclass
class UsageSample:
    """One deduplicated usage observation tied to a requestId."""
    ts: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens + self.output_tokens
            + self.cache_creation + self.cache_read
        )


def parse_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def extract_usage(entry: dict) -> tuple[str | None, UsageSample | None]:
    """Pull (requestId, UsageSample) out of one JSONL line, or (None, None)."""
    if entry.get("type") != "assistant":
        return None, None
    msg = entry.get("message") or {}
    usage = msg.get("usage") or {}
    if not usage:
        return None, None
    req_id = (
        entry.get("requestId")
        or msg.get("id")
        or msg.get("request_id")
    )
    ts = parse_ts(entry.get("timestamp"))
    if ts is None:
        return None, None
    return req_id, UsageSample(
        ts=ts,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
    )


def merge_sample(
    existing: UsageSample | None, new: UsageSample,
) -> UsageSample:
    """MAX merge: streaming duplicates hold placeholder values."""
    if existing is None:
        return new
    return UsageSample(
        ts=max(existing.ts, new.ts),
        input_tokens=max(existing.input_tokens, new.input_tokens),
        output_tokens=max(existing.output_tokens, new.output_tokens),
        cache_creation=max(existing.cache_creation, new.cache_creation),
        cache_read=max(existing.cache_read, new.cache_read),
    )


def humanize_project(encoded: str) -> str:
    if encoded.startswith("-"):
        encoded = encoded[1:]
    parts = encoded.split("-")
    return parts[-1] if parts else encoded
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_cc_history.py`:

```python
def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _iso(year, month, day, hour=12, minute=0, second=0) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def test_reconstruct_missing_days_from_jsonl(tmp_path):
    projects = tmp_path / "projects"
    history = tmp_path / "history"

    session_uuid = "abcd1234-abcd-abcd-abcd-abcd12340001"
    project_dir = projects / "-Users-peter-code-myproj"
    _write_jsonl(
        project_dir / f"{session_uuid}.jsonl",
        [
            {
                "type": "assistant",
                "requestId": "r1",
                "timestamp": _iso(2026, 4, 22, 10, 0, 0),
                "message": {
                    "usage": {
                        "input_tokens": 100, "output_tokens": 50,
                        "cache_read_input_tokens": 500,
                        "cache_creation_input_tokens": 10,
                    },
                },
            },
            # same day, second request
            {
                "type": "assistant",
                "requestId": "r2",
                "timestamp": _iso(2026, 4, 22, 11, 30, 0),
                "message": {
                    "usage": {
                        "input_tokens": 200, "output_tokens": 80,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
            # next day, should land in a different file
            {
                "type": "assistant",
                "requestId": "r3",
                "timestamp": _iso(2026, 4, 23, 9, 0, 0),
                "message": {
                    "usage": {
                        "input_tokens": 1, "output_tokens": 1,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
        ],
    )

    logger = HistoryLogger(history_dir=history, enabled=True)
    logger.reconstruct_missing_days(
        today=date_cls(2026, 4, 23),
        projects_dir=projects,
    )

    f22 = history / "daily" / "2026-04-22.json"
    f23 = history / "daily" / "2026-04-23.json"
    assert f22.exists()
    assert f23.exists()

    rec22 = json.loads(f22.read_text())
    assert rec22["reconstructed"] is True
    assert session_uuid in rec22["sessions"]
    s = rec22["sessions"][session_uuid]
    assert s["input_tokens"] == 300
    assert s["output_tokens"] == 130
    assert s["cache_read_tokens"] == 600
    assert s["cache_creation_tokens"] == 10
    assert s["cost_usd"] is None
    assert s["project"] == "myproj"
    assert rec22["totals"]["cost_usd"] is None


def test_reconstruct_does_not_overwrite_live_file(tmp_path):
    projects = tmp_path / "projects"
    history = tmp_path / "history"
    (history / "daily").mkdir(parents=True)
    # Existing LIVE file for 2026-04-22
    live = DailyRecord(
        date="2026-04-22", reconstructed=False, generated_at=123.0,
        sessions={},
    )
    (history / "daily" / "2026-04-22.json").write_text(json.dumps(live.to_dict()))

    # Even if JSONL data exists for that date, reconstruct must skip it.
    session_uuid = "ffff0000-0000-0000-0000-000000000001"
    _write_jsonl(
        projects / "-proj" / f"{session_uuid}.jsonl",
        [{
            "type": "assistant", "requestId": "r1",
            "timestamp": _iso(2026, 4, 22, 10, 0, 0),
            "message": {"usage": {"input_tokens": 999, "output_tokens": 999}},
        }],
    )

    logger = HistoryLogger(history_dir=history, enabled=True)
    logger.reconstruct_missing_days(
        today=date_cls(2026, 4, 23),
        projects_dir=projects,
    )

    rec = json.loads((history / "daily" / "2026-04-22.json").read_text())
    assert rec["reconstructed"] is False
    assert rec["generated_at"] == 123.0
    assert rec["sessions"] == {}
```

- [ ] **Step 3: Run tests to verify they fail**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: `AttributeError: 'HistoryLogger' object has no attribute 'reconstruct_missing_days'`.

- [ ] **Step 4: Implement `reconstruct_missing_days`**

Add inside `HistoryLogger` (use `datetime.fromtimestamp` with local TZ to match the live writer's day semantics):

```python
    # -- reconstruction -------------------------------------------------

    def reconstruct_missing_days(
        self,
        today,
        projects_dir: Path,
    ) -> None:
        """For each of the last DAILY_KEEP_DAYS calendar days (inclusive),
        if no daily file exists AND JSONL data shows activity on that day,
        write a reconstructed DailyRecord. Never overwrites a live file.
        """
        if not self.enabled or not projects_dir.exists():
            return

        from datetime import date as date_cls, datetime, timedelta

        self.daily_dir.mkdir(parents=True, exist_ok=True)

        target_dates = {
            today - timedelta(days=i)
            for i in range(self.DAILY_KEEP_DAYS)
        }
        # Skip dates that already have a live file.
        missing = {
            d for d in target_dates
            if not (self.daily_dir / f"{d.isoformat()}.json").exists()
        }
        if not missing:
            return

        # date -> session_id -> (project, samples dict by requestId)
        by_day: dict[date_cls, dict[str, dict]] = {d: {} for d in missing}

        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            project = humanize_project(project_dir.name)
            for jsonl in project_dir.glob("*.jsonl"):
                session_id = jsonl.stem
                try:
                    with jsonl.open() as f:
                        for raw in f:
                            if not raw.strip():
                                continue
                            try:
                                entry = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            req_id, sample = extract_usage(entry)
                            if sample is None:
                                continue
                            d = datetime.fromtimestamp(sample.ts).date()
                            if d not in missing:
                                continue
                            bucket = by_day[d].setdefault(
                                session_id,
                                {"project": project, "samples": {}},
                            )
                            key = req_id or f"ts:{sample.ts}"
                            bucket["samples"][key] = merge_sample(
                                bucket["samples"].get(key), sample,
                            )
                except OSError:
                    continue

        for d, sessions_map in by_day.items():
            if not sessions_map:
                continue  # no activity that day → no file written
            entries: dict[str, DailySessionEntry] = {}
            for sid, bucket in sessions_map.items():
                samples = list(bucket["samples"].values())
                if not samples:
                    continue
                first_ts = min(s.ts for s in samples)
                last_ts = max(s.ts for s in samples)
                entries[sid] = DailySessionEntry(
                    project=bucket["project"],
                    model=None,
                    first_ts=first_ts,
                    last_ts=last_ts,
                    input_tokens=sum(s.input_tokens for s in samples),
                    output_tokens=sum(s.output_tokens for s in samples),
                    cache_read_tokens=sum(s.cache_read for s in samples),
                    cache_creation_tokens=sum(s.cache_creation for s in samples),
                    cost_usd=None,
                )
            rec = DailyRecord(
                date=d.isoformat(),
                reconstructed=True,
                generated_at=datetime.now().timestamp(),
                sessions=entries,
            )
            target = self.daily_dir / f"{d.isoformat()}.json"
            self._atomic_write_json(target, rec.to_dict())
```

- [ ] **Step 5: Run tests and verify they pass**

Run:
```bash
./.venv/bin/pytest tests/test_cc_history.py -v
```
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add cc_history.py tests/test_cc_history.py
git commit -m "feat(history): reconstruct missing days from JSONL transcripts"
```

---

## Task 6: Wire up `HistoryLogger` in `cc-session-monitor.py`

**Files:**
- Modify: `cc-session-monitor.py`

**Strategy:**
1. Import the shared parsing helpers from `cc_history` (replace local definitions with re-exports so nothing else breaks).
2. Add CLI flags `--no-log` and `--history-dir`.
3. Build an `entries_for_today(now)` helper on `Monitor` that produces the `dict[session_id, DailySessionEntry]` structure the logger expects.
4. In `main()`: instantiate `HistoryLogger`, run retention + reconstruction, then inside the main loop call `write_today` at most once every 60 s, and register an `atexit` handler (also triggered from the `KeyboardInterrupt` branch) to do a final `write_today`.

- [ ] **Step 1: Replace local parsing helpers with imports from `cc_history`**

In `cc-session-monitor.py`, near the other imports (after the `from rich...` block), add:

```python
from cc_history import (
    DailyRecord,
    DailySessionEntry,
    HistoryLogger,
    UsageSample,
    parse_ts as _parse_ts,
    extract_usage as _extract_usage,
    merge_sample as _merge_sample,
    humanize_project as _humanize_project,
)
```

Delete the now-duplicated local definitions of `UsageSample`, `_parse_ts`, `_extract_usage`, `_merge_sample`, and `_humanize_project` from `cc-session-monitor.py`. Keep everything else unchanged.

- [ ] **Step 2: Verify the script still runs**

Run:
```bash
./.venv/bin/python -c "import ast; ast.parse(open('cc-session-monitor.py').read()); print('syntax OK')"
./.venv/bin/python cc-session-monitor.py --help
```
Expected: `syntax OK` and the argparse help output.

- [ ] **Step 3: Add `Monitor.entries_for_today` method**

Below the existing `daily_window_sessions` method, add:

```python
    def entries_for_today(self, today, now: float) -> dict[str, DailySessionEntry]:
        """Build a {session_id: DailySessionEntry} map for live history logging.

        Includes only sessions with at least one usage sample whose local
        calendar date == `today`. Token fields are summed across samples
        within that day only; cost_usd is taken from hook_cost_usd (cumulative
        across session lifetime, the best we have for an open session).
        """
        from datetime import datetime
        out: dict[str, DailySessionEntry] = {}
        for state in self.sessions.values():
            today_samples = [
                s for s in state.samples.values()
                if datetime.fromtimestamp(s.ts).date() == today
            ]
            if not today_samples:
                continue
            out[state.session_id] = DailySessionEntry(
                project=state.project,
                model=state.hook_model,
                first_ts=min(s.ts for s in today_samples),
                last_ts=max(s.ts for s in today_samples),
                input_tokens=sum(s.input_tokens for s in today_samples),
                output_tokens=sum(s.output_tokens for s in today_samples),
                cache_read_tokens=sum(s.cache_read for s in today_samples),
                cache_creation_tokens=sum(s.cache_creation for s in today_samples),
                cost_usd=state.hook_cost_usd,
            )
        return out
```

- [ ] **Step 4: Add CLI flags**

In `main()`, inside the argparse block, add:

```python
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
```

- [ ] **Step 5: Wire up the logger in `main()`**

Replace the final block in `main()` (the `monitor = Monitor(...)` through the `except KeyboardInterrupt` block) with:

```python
    monitor = Monitor(
        root=args.projects_dir,
        snapshot_dir=args.snapshot_dir,
        velocity_window=args.velocity_window,
    )
    console = Console()

    logger = HistoryLogger(
        history_dir=args.history_dir,
        enabled=not args.no_log,
    )

    from datetime import date as _date_cls
    if logger.enabled:
        today = _date_cls.today()
        logger.run_retention(today=today)
        logger.reconstruct_missing_days(
            today=today, projects_dir=args.projects_dir,
        )

    last_log_write = 0.0
    LOG_INTERVAL = 60.0

    def _final_log_write():
        if not logger.enabled:
            return
        try:
            monitor.refresh()
            today = _date_cls.today()
            entries = monitor.entries_for_today(today, time.time())
            logger.write_today(
                date=today.isoformat(),
                entries=entries,
                now_ts=time.time(),
            )
        except Exception:
            # Exiting — never raise from a shutdown hook.
            pass

    import atexit
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
                    today = _date_cls.today()
                    # Cheap retention check in case the monitor spans midnight.
                    logger.run_retention(today=today)
                    entries = monitor.entries_for_today(today, now)
                    logger.write_today(
                        date=today.isoformat(),
                        entries=entries,
                        now_ts=now,
                    )
                    last_log_write = now

                time.sleep(args.refresh)
    except KeyboardInterrupt:
        return 0
```

- [ ] **Step 6: Smoke-test end-to-end against a throwaway history dir**

Run:
```bash
TMP=$(mktemp -d)
./.venv/bin/python cc-session-monitor.py --history-dir "$TMP" --refresh 0.5 &
MON_PID=$!
sleep 2
kill -INT $MON_PID || true
wait $MON_PID 2>/dev/null || true
ls -la "$TMP" "$TMP/daily" 2>/dev/null || true
```
Expected: `$TMP/daily/` contains today's `YYYY-MM-DD.json` file (if any sessions were active) and no `.tmp.*` leftovers.

- [ ] **Step 7: Verify `--no-log` disables everything**

Run:
```bash
TMP=$(mktemp -d)
./.venv/bin/python cc-session-monitor.py --history-dir "$TMP" --no-log --refresh 0.5 &
MON_PID=$!
sleep 2
kill -INT $MON_PID || true
wait $MON_PID 2>/dev/null || true
ls -la "$TMP" 2>/dev/null
```
Expected: `$TMP` is empty (no `daily/` or `monthly/` created).

- [ ] **Step 8: Commit**

```bash
git add cc-session-monitor.py cc_history.py
git commit -m "feat(history): wire HistoryLogger into monitor with periodic + shutdown writes"
```

---

## Task 7: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a "History" section to `README.md`**

Insert after the "How it works" section (before any future sections), a new second-level section:

```markdown
## History

The monitor writes a JSON snapshot per calendar day to `~/.claude/session-monitor/history/daily/YYYY-MM-DD.json`. Every 60 seconds and on Ctrl-C shutdown the file is refreshed with the cumulative-today state of every session that had any activity on that local date.

### Retention

- **Daily files**: today + the previous 2 days (3 total).
- **Older days**: appended as one line to `history/monthly/YYYY-MM.jsonl` and the daily file is deleted. Dedup-safe: re-running retention never duplicates a day.
- **Monthly files**: the 12 most recent are kept, older ones are deleted. Max ≈ 12 monthly + 3 daily files on disk.

### Reconstruction

If the monitor was not running yesterday (or two days ago), it reconstructs those daily files from Claude Code's JSONL transcripts on the next startup. Reconstructed files have `"reconstructed": true` and `"cost_usd": null` on every session, because the cumulative cost snapshot in the hook's data cannot be reliably attributed to one specific day after the fact. Token counts are still accurate (modulo the known JSONL placeholder issue).

### CLI

```
--no-log           disable history logging entirely
--history-dir P    alternate location (default: ~/.claude/session-monitor/history)
```

### File format

Daily JSON (and one JSONL line in the monthly file) is:

```json
{
  "date": "2026-04-23",
  "reconstructed": false,
  "generated_at": 1745403600.0,
  "sessions": {
    "<session_uuid>": {
      "project": "foo",
      "model": "Opus",
      "first_ts": 1745382000.0,
      "last_ts": 1745400000.0,
      "input_tokens": 12345,
      "output_tokens": 6789,
      "cache_read_tokens": 98765,
      "cache_creation_tokens": 4321,
      "cost_usd": 2.34
    }
  },
  "totals": { "... same fields summed ...": 0 }
}
```

`sessions` is keyed by session UUID so external tools can join/diff across days.
```

- [ ] **Step 2: Update the "Files" section of `README.md`**

Replace the existing file tree with:

```markdown
```
.
├── cc-session-monitor.py   # the TUI
├── cc_history.py           # persistent per-day logger + retention
├── cc-monitor-hook.sh      # the statusLine hook
├── run-monitor.sh          # convenience wrapper (uses ./.venv)
├── install_cc-monitor.md   # short install note (DE)
├── tests/                  # pytest unit tests for cc_history
└── CLAUDE.md               # guidance for Claude Code working in this repo
```
```

- [ ] **Step 3: Update `CLAUDE.md`**

Under the "## Commands" section, append:

```markdown

Tests (only cover `cc_history.py`):

```bash
./.venv/bin/pytest tests/ -v
```
```

Under the "## Architecture" section, after the existing paragraphs, add:

```markdown

**History logger split.** `cc_history.py` owns daily/monthly persistence plus the shared JSONL parsing helpers (`parse_ts`, `extract_usage`, `merge_sample`, `humanize_project`). `cc-session-monitor.py` imports them; do not re-implement these in the monitor. The logger is intentionally stateless across runs — every startup re-runs retention and fills missing-day gaps by scanning JSONL. Reconstructed files set `cost_usd: null`; the cumulative hook snapshot is not safely attributable to one post-hoc day. Write cadence is every 60 s in the main loop plus an `atexit` flush; both calls also trigger retention so a monitor spanning midnight rolls yesterday into monthly at the first post-midnight tick.
```

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document history logger, retention, and reconstruction"
```

---

## Self-Review Summary

- **Spec coverage:** Task 0 (test infra), 1 (data model), 2 (write path), 3 (daily→monthly roll), 4 (monthly prune), 5 (JSONL reconstruction), 6 (integration + CLI + shutdown), 7 (docs). Every user decision from the spec round is covered.
- **Types consistent:** `DailyRecord` / `DailySessionEntry` defined in Task 1 are used identically in Tasks 2, 3, 5, 6. `run_retention(today=...)` signature matches in Tasks 3, 4, 6. `reconstruct_missing_days(today=..., projects_dir=...)` matches in Tasks 5 and 6.
- **Shared helpers:** Task 5 moves `parse_ts`, `extract_usage`, `merge_sample`, `humanize_project`, and `UsageSample` into `cc_history.py`. Task 6 replaces the monitor's local copies with imports — no drift possible.
- **No placeholders:** every step has either exact code or an exact command with expected output.
