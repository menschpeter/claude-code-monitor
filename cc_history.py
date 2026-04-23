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
