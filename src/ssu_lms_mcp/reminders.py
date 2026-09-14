"""Deterministic, local LMS deadline and timetable reminders."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from filelock import FileLock

from ssu_lms_mcp.desk_events import journal_notification

SEOUL = timezone(timedelta(hours=9), "Asia/Seoul")
WINDOW, STALE_AFTER, STATE_RETENTION = (
    timedelta(minutes=10),
    timedelta(hours=24),
    timedelta(days=45),
)


def _tz(calendar):
    if calendar is None:
        return SEOUL
    if not isinstance(calendar, dict) or "timezone" not in calendar:
        return SEOUL
    if calendar.get("timezone") == "Asia/Seoul":
        return SEOUL
    name = calendar.get("timezone")
    if not isinstance(name, str):
        raise ValueError("calendar timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"calendar timezone is unavailable: {name}") from None


def _dt(value, tz):
    if not isinstance(value, str):
        return None
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)


def _now(value, tz):
    value = value or datetime.now(tz)
    return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)


def _clock(value):
    try:
        return time.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _class(event, begins, ends=None):
    return {
        "id": f"class:{event['id']}:{begins.date().isoformat()}",
        "source_id": str(event["id"]),
        "kind": "class",
        "title": str(event.get("title") or "Untitled class"),
        "starts_at": begins.isoformat(),
        "ends_at": ends.isoformat() if ends else None,
        "location": event.get("location") if isinstance(event.get("location"), str) else None,
    }


def expand_events(calendar, start, end):
    """Expand valid events in the half-open local range ``[start, end)``."""
    if not isinstance(calendar, dict) or not isinstance(calendar.get("events"), list):
        return []
    tz, start, end = _tz(calendar), _now(start, _tz(calendar)), _now(end, _tz(calendar))
    if end <= start:
        return []
    rows = []
    for event in calendar["events"]:
        if not isinstance(event, dict) or not event.get("id"):
            continue
        begins = _dt(event.get("starts_at"), tz)
        if begins:
            if start <= begins < end:
                rows.append(_class(event, begins, _dt(event.get("ends_at"), tz)))
            continue
        weekday, clock = event.get("weekday"), _clock(event.get("start"))
        if (
            isinstance(weekday, bool)
            or not isinstance(weekday, int)
            or not 0 <= weekday <= 6
            or not clock
        ):
            continue
        try:
            first = date.fromisoformat(event["valid_from"])
            last = date.fromisoformat(event["valid_until"])
        except (TypeError, ValueError):
            continue
        day = start.date() + timedelta(days=(weekday - start.weekday()) % 7)
        while day <= end.date():
            begins = datetime.combine(day, clock, tzinfo=tz)
            if (not first or day >= first) and (not last or day <= last) and start <= begins < end:
                closing = _clock(event.get("end"))
                rows.append(
                    _class(
                        event,
                        begins,
                        datetime.combine(day, closing, tzinfo=tz) if closing else None,
                    )
                )
            day += timedelta(days=7)
    return sorted(rows, key=lambda row: (row["starts_at"], row["id"]))


def _assignments(snapshot, tz):
    if not isinstance(snapshot, dict):
        return []
    raw_courses = snapshot.get("courses", [])
    raw_assignments = snapshot.get("assignments", [])
    if not isinstance(raw_courses, list) or not isinstance(raw_assignments, list):
        return []
    courses = {
        str(x.get("id")): str(x.get("name") or "Unknown course")
        for x in raw_courses
        if isinstance(x, dict)
    }
    rows = []
    for item in raw_assignments:
        if not isinstance(item, dict) or item.get("submitted") is True or item.get("id") is None:
            continue
        due = _dt(item.get("due_at"), tz)
        if due:
            rows.append(
                {
                    "id": f"deadline:{item.get('course_id')}:{item['id']}:{due.isoformat()}",
                    "source_id": str(item["id"]),
                    "kind": "deadline",
                    "title": str(item.get("title") or "Untitled assignment"),
                    "starts_at": due.isoformat(),
                    "due_at": due.isoformat(),
                    "course_id": item.get("course_id"),
                    "course": courses.get(str(item.get("course_id")), "Unknown course"),
                }
            )
    return rows


def _stale(snapshot, now):
    stamp = _dt(snapshot.get("generated_at"), now.tzinfo) if isinstance(snapshot, dict) else None
    return stamp is None or now - stamp > STALE_AFTER or snapshot.get("status") in {"partial", "error"}


def build_briefing(snapshot, calendar, now):
    tz, now = _tz(calendar), _now(now, _tz(calendar))
    tomorrow = now.date() + timedelta(days=1)
    start = datetime.combine(tomorrow, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    available = isinstance(calendar, dict) and isinstance(calendar.get("events"), list)
    stale = _stale(snapshot, now)
    return {
        "id": f"briefing:{tomorrow.isoformat()}",
        "mode": "briefing",
        "target_date": tomorrow.isoformat(),
        "timezone": "Asia/Seoul"
        if not isinstance(calendar, dict)
        else str(calendar.get("timezone", "Asia/Seoul")),
        "calendar_available": available,
        "classes": expand_events(calendar, start, end) if available else [],
        "deadlines": sorted(
            [x for x in _assignments(snapshot, tz) if start <= _dt(x["due_at"], tz) < end],
            key=lambda x: (x["due_at"], x["id"]),
        ),
        "snapshot_stale": stale,
        "warning": "LMS 수집 결과가 오래되었거나 일부 조회에 실패했습니다. 마감 정보를 다시 확인하세요."
        if stale
        else None,
    }


def due_reminders(snapshot, calendar, now):
    tz, now = _tz(calendar), _now(now, _tz(calendar))
    end = now + WINDOW
    classes = (
        [
            row
            for row in expand_events(calendar, now, end + timedelta(microseconds=1))
            if now < _dt(row["starts_at"], tz) <= end
        ]
        if isinstance(calendar, dict)
        else []
    )
    deadlines = [x for x in _assignments(snapshot, tz) if now < _dt(x["due_at"], tz) <= end]
    return sorted([*classes, *deadlines], key=lambda x: (x["starts_at"], x["id"]))


def notify(title, body):
    """Return true if Windows accepted a toast request; it may still suppress display."""
    if os.name != "nt":
        return False
    script = "& {$ErrorActionPreference='Stop';[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,ContentType=WindowsRuntime]>$null;$x=New-Object Windows.Data.Xml.Dom.XmlDocument;$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>'+[Security.SecurityElement]::Escape($env:SSU_REMINDER_TITLE)+'</text><text>'+[Security.SecurityElement]::Escape($env:SSU_REMINDER_BODY)+'</text></binding></visual></toast>');[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:SSU_REMINDER_AUMID).Show([Windows.UI.Notifications.ToastNotification]::new($x))}"
    env = os.environ.copy()
    env.update(
        SSU_REMINDER_TITLE=title,
        SSU_REMINDER_BODY=body,
        SSU_REMINDER_AUMID=env.get("SSU_REMINDER_AUMID", "Microsoft.Windows.PowerShell"),
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    try:
        return (
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    script,
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startup,
                timeout=8,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _read(path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def _write(path, value):
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _markdown(report):
    lines = [f"# LMS briefing — {report['target_date']}", ""]
    if not report["calendar_available"]:
        lines += ["시간표를 찾을 수 없습니다. 수업 시간을 추정하지 않았습니다.", ""]
    if report["warning"]:
        lines += [f"> {report['warning']}", ""]
    lines += ["## 수업"] + (
        [
            f"- {x['starts_at'][11:16]} {x['title']}"
            + (f" — {x['location']}" if x.get("location") else "")
            for x in report["classes"]
        ]
        or ["- 없음"]
    )
    lines += ["", "## 과제 마감"] + (
        [f"- {x['due_at'][11:16]} {x['title']} ({x['course']})" for x in report["deadlines"]]
        or ["- 없음"]
    )
    return "\n".join(lines) + "\n"


def run_reminders(data_dir, *, mode="upcoming", now=None, dry_run=False, context=None):
    if mode not in {"upcoming", "briefing"}:
        raise ValueError("mode must be 'upcoming' or 'briefing'")
    root = Path(data_dir) / "automation"
    snapshot = _read(root / "latest.json", {})
    health = _read(root / "collect-status.json", {})
    if health.get("status") in {"partial", "error"}:
        snapshot = {**snapshot, "status": health["status"]}
    calendar = _read(root / "calendar.json", {}) if (root / "calendar.json").exists() else None
    current = _now(now, _tz(calendar))
    if mode == "briefing":
        report = build_briefing(snapshot, calendar, current)
    else:
        stale = _stale(snapshot, current)
        report = {
            "mode": "upcoming",
            "calendar_available": isinstance(calendar, dict)
            and isinstance(calendar.get("events"), list),
            "snapshot_stale": stale,
            "warning": "LMS 수집 결과가 오래되었거나 일부 조회에 실패했습니다. 마감 정보를 다시 확인하세요."
            if stale
            else None,
            "reminders": due_reminders(snapshot, calendar, current),
        }
    if dry_run:
        return {**report, "dry_run": True, "sent": [], "skipped": []}
    root.mkdir(parents=True, exist_ok=True)
    if mode == "briefing":
        inbox = root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{report['target_date']}.md").write_text(_markdown(report), encoding="utf-8")
        candidates = [
            {
                "id": report["id"],
                "title": "내일 LMS 브리핑",
                "body": (
                    f"수업 {len(report['classes'])}개, 마감 {len(report['deadlines'])}개"
                    + (" · 수집 정보가 오래됨" if report["warning"] else "")
                ),
            }
        ]
    else:
        candidates = [
            {
                "id": x["id"],
                "title": "LMS 알림",
                "body": (
                    f"{x['title']} · {x['starts_at'][11:16]}"
                    + (" · 수집 정보가 오래됨" if report["warning"] else "")
                ),
            }
            for x in report["reminders"]
        ]
    state_path = root / "reminders-state.json"
    sent = []
    skipped = []
    failed = []
    handoffs = []
    with FileLock(str(state_path) + ".lock"):
        state = _read(state_path, {"sent": {}})
        prior = state.get("sent") if isinstance(state.get("sent"), dict) else {}
        prior = {
            key: value
            for key, value in prior.items()
            if _dt(value, current.tzinfo)
            and _dt(value, current.tzinfo) >= current - STATE_RETENTION
        }
        for candidate in candidates:
            if candidate["id"] in prior:
                skipped.append(candidate["id"])
            elif journal_notification(
                data_dir, candidate["title"], candidate["body"], mode=mode, context=context
            ):
                prior[candidate["id"]] = current.isoformat()
                sent.append(candidate["id"])
                handoffs.append(candidate["id"])
            elif notify(candidate["title"], candidate["body"]):
                prior[candidate["id"]] = current.isoformat()
                sent.append(candidate["id"])
            else:
                failed.append(candidate["id"])
        _write(state_path, {"sent": prior})
    return {
        **report,
        "dry_run": False,
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "desktop_handoffs": handoffs,
        "notification_note": "sent는 Windows가 로컬 알림 요청을 수락했다는 뜻이며, 집중 모드가 화면 표시를 숨길 수 있습니다."
        + (" 데스크톱 앱으로 전달된 알림이 있습니다." if handoffs else ""),
    }
