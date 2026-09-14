"""Offline time-boundary, recurrence and duplicate-reminder tests."""

import json
from datetime import datetime

from ssu_lms_mcp import reminders

CALENDAR = {
    "timezone": "Asia/Seoul",
    "events": [
        {
            "id": "m",
            "title": "Math",
            "weekday": 0,
            "start": "09:00",
            "end": "10:00",
            "valid_from": "2026-09-01",
            "valid_until": "2026-12-31",
        }
    ],
}


def test_expands_recurring_validity_and_boundaries():
    rows = reminders.expand_events(
        CALENDAR,
        datetime.fromisoformat("2026-09-14T08:00+09:00"),
        datetime.fromisoformat("2026-09-14T09:01+09:00"),
    )
    assert [x["id"] for x in rows] == ["class:m:2026-09-14"]
    assert not reminders.expand_events(
        CALENDAR,
        datetime.fromisoformat("2026-08-31T00:00+09:00"),
        datetime.fromisoformat("2026-09-01T00:00+09:00"),
    )


def test_due_class_boundaries_and_course_scoped_deadline_ids():
    now = datetime.fromisoformat("2026-09-14T09:00+09:00")
    calendar = {
        "timezone": "Asia/Seoul",
        "events": [
            {
                "id": "now",
                "title": "now",
                "starts_at": "2026-09-14T09:00+09:00",
            },
            {
                "id": "end",
                "title": "end",
                "starts_at": "2026-09-14T09:10+09:00",
            },
        ],
    }
    snapshot = {
        "assignments": [
            {"id": 7, "course_id": 1, "due_at": "2026-09-14T09:10+09:00"},
            {"id": 7, "course_id": 2, "due_at": "2026-09-14T09:10+09:00"},
        ]
    }
    rows = reminders.due_reminders(snapshot, calendar, now)
    assert {row["source_id"] for row in rows} == {"7", "end"}
    assert len({row["id"] for row in rows}) == 3


def test_briefing_and_missing_calendar():
    snapshot = {
        "generated_at": "2026-09-13T12:00:00Z",
        "courses": [{"id": 1, "name": "Math"}],
        "assignments": [
            {
                "id": 2,
                "course_id": 1,
                "title": "HW",
                "due_at": "2026-09-14T13:00:00+09:00",
                "submitted": False,
            }
        ],
    }
    result = reminders.build_briefing(
        snapshot, CALENDAR, datetime.fromisoformat("2026-09-13T20:00+09:00")
    )
    assert result["target_date"] == "2026-09-14" and result["deadlines"][0]["title"] == "HW"
    assert not reminders.build_briefing(
        snapshot, None, datetime.fromisoformat("2026-09-13T20:00+09:00")
    )["calendar_available"]


def test_aware_due_submitted_malformed_and_retry(tmp_path, monkeypatch):
    automation = tmp_path / "automation"
    automation.mkdir()
    (automation / "latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-09-14T00:00:00Z",
                "assignments": [
                    {
                        "id": 1,
                        "title": "due",
                        "due_at": "2026-09-14T09:09:00+09:00",
                        "submitted": False,
                    },
                    {
                        "id": 2,
                        "title": "done",
                        "due_at": "2026-09-14T09:05:00+09:00",
                        "submitted": True,
                    },
                ],
            }
        )
    )
    (automation / "calendar.json").write_text(
        json.dumps(
            {"timezone": "Asia/Seoul", "events": [{"id": "bad", "weekday": 9, "start": "no"}]}
        )
    )
    now = datetime.fromisoformat("2026-09-14T09:00+09:00")
    assert [
        x["source_id"]
        for x in reminders.due_reminders(reminders._read(automation / "latest.json", {}), {}, now)
    ] == ["1"]
    assert not reminders.due_reminders({"assignments": "malformed"}, None, now)
    monkeypatch.setattr(reminders, "notify", lambda *_: False)
    assert not reminders.run_reminders(tmp_path, now=now)["sent"]
    monkeypatch.setattr(reminders, "notify", lambda *_: True)
    assert reminders.run_reminders(tmp_path, now=now)["sent"]
    assert reminders.run_reminders(tmp_path, now=now)["skipped"]
    other = tmp_path / "other"
    assert (
        reminders.run_reminders(other, now=now, dry_run=True)["dry_run"]
        and not (other / "automation").exists()
    )


def test_notification_adapter_exit_failure(monkeypatch):
    class Startup:
        dwFlags = 0
        wShowWindow = 0

    class Result:
        returncode = 1

    monkeypatch.setattr(reminders.os, "name", "nt")
    monkeypatch.setattr(reminders.subprocess, "STARTUPINFO", Startup)
    monkeypatch.setattr(reminders.subprocess, "STARTF_USESHOWWINDOW", 1)
    monkeypatch.setattr(reminders.subprocess, "run", lambda *args, **kwargs: Result())
    assert not reminders.notify("title", "body")


def test_recent_partial_snapshot_is_reported_as_unreliable():
    now = datetime.fromisoformat("2026-09-14T09:00+09:00")
    snapshot = {"generated_at": now.isoformat(), "status": "partial", "assignments": []}
    report = reminders.build_briefing(snapshot, None, now)
    assert report["snapshot_stale"] and "일부 조회" in report["warning"]
