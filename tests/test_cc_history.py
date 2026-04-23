"""Tests for cc_history.HistoryLogger and DailyRecord."""
from __future__ import annotations

import json
from pathlib import Path

from cc_history import DailyRecord, DailySessionEntry, HistoryLogger


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
