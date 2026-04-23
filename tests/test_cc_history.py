"""Tests for cc_history.HistoryLogger and DailyRecord."""
from __future__ import annotations

import json
from pathlib import Path

from cc_history import DailyRecord, DailySessionEntry, HistoryLogger
from datetime import date as date_cls


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
