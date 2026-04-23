"""
cc_history — on-disk daily/monthly history for the claude-code-monitor project.

Writes one JSON file per calendar day under ~/.claude/session-monitor/history/daily/,
rolls days older than two days ago into monthly JSONL files, and keeps at
most the last 12 monthly files.

The live monitor (cc-session-monitor.py) owns the in-memory session state
and hands it to HistoryLogger at each write tick. For gaps when the monitor
was not running, reconstruct_missing_days scans Claude Code's JSONL
transcripts and writes reconstructed=true files with
session_cumulative_cost_usd=null.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
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


def extract_usage(entry: dict) -> tuple[str | None, "UsageSample | None"]:
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
    existing: "UsageSample | None", new: "UsageSample",
) -> "UsageSample":
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
    session_cumulative_cost_usd: float | None

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
            "session_cumulative_cost_usd": self.session_cumulative_cost_usd,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DailySessionEntry":
        cumulative_cost = d.get("session_cumulative_cost_usd")
        if cumulative_cost is None and "cost_usd" in d:
            cumulative_cost = d.get("cost_usd")
        return cls(
            project=d["project"],
            model=d.get("model"),
            first_ts=float(d["first_ts"]),
            last_ts=float(d["last_ts"]),
            input_tokens=int(d["input_tokens"]),
            output_tokens=int(d["output_tokens"]),
            cache_read_tokens=int(d["cache_read_tokens"]),
            cache_creation_tokens=int(d["cache_creation_tokens"]),
            session_cumulative_cost_usd=(
                float(cumulative_cost) if cumulative_cost is not None else None
            ),
        )


@dataclass
class DailyRecord:
    date: str                                          # YYYY-MM-DD (local)
    reconstructed: bool
    generated_at: float
    sessions: dict[str, DailySessionEntry] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        any_null = any(
            s.session_cumulative_cost_usd is None for s in self.sessions.values()
        )
        total_cost = (
            None if any_null
            else sum(
                s.session_cumulative_cost_usd or 0.0
                for s in self.sessions.values()
            )
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
                "session_cumulative_cost_usd": total_cost,
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
        tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(target)

    # -- retention ------------------------------------------------------

    # Keep today + previous 2 calendar days as standalone daily files.
    DAILY_KEEP_DAYS = 3
    MONTHLY_KEEP_COUNT = 12

    def run_retention(self, today) -> None:
        """Roll old daily JSONs into monthly JSONLs; dedup append.

        `today` is a datetime.date (local). Files whose date ≤
        today - DAILY_KEEP_DAYS are appended to their month's JSONL
        and then deleted. Monthly rollup is dedup-safe: an already-rolled
        date is not appended twice. After the daily-roll step, prunes
        monthly/ down to MONTHLY_KEEP_COUNT most recent files.
        """
        if not self.enabled:
            return

        if self.daily_dir.exists():
            cutoff = today - timedelta(days=self.DAILY_KEEP_DAYS - 1)
            # cutoff is the OLDEST day still kept. Roll anything < cutoff.

            for daily_file in sorted(self.daily_dir.glob("*.json")):
                try:
                    file_date = date.fromisoformat(daily_file.stem)
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

        # Prune monthly/ to the MONTHLY_KEEP_COUNT most recent.
        if self.monthly_dir.exists():
            monthlies = sorted(self.monthly_dir.glob("*.jsonl"))
            excess = len(monthlies) - self.MONTHLY_KEEP_COUNT
            if excess > 0:
                for old in monthlies[:excess]:
                    old.unlink(missing_ok=True)

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

        target_dates = {
            today - timedelta(days=i)
            for i in range(self.DAILY_KEEP_DAYS)
        }
        # Skip dates that already have a live OR previously-reconstructed file.
        missing = {
            d for d in target_dates
            if not (self.daily_dir / f"{d.isoformat()}.json").exists()
        }
        if not missing:
            return

        # date -> session_id -> {"project": str, "samples": dict[str, UsageSample]}
        by_day: dict[date, dict[str, dict]] = {d: {} for d in missing}

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

        if not any(by_day.values()):
            return  # no activity on any missing day → no files, no mkdir

        self.daily_dir.mkdir(parents=True, exist_ok=True)

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
                    session_cumulative_cost_usd=None,
                )
            rec = DailyRecord(
                date=d.isoformat(),
                reconstructed=True,
                generated_at=datetime.now().timestamp(),
                sessions=entries,
            )
            target = self.daily_dir / f"{d.isoformat()}.json"
            self._atomic_write_json(target, rec.to_dict())
