"""Scheduler date, Windows argv and ownership boundaries (no task mutations)."""

import ctypes
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import pytest

from ssu_lms_mcp import study_schedule as schedule

SID = "S-1-5-21-100-200-300-1001"
NS = {"t": schedule.TASK_NS}


@pytest.fixture
def task_spec(tmp_path):
    repo = tmp_path / "작업 공간 & 개인 자료"
    config_path = repo / ".omp" / "study" / "settings.json"
    config_path.parent.mkdir(parents=True)
    interpreter = repo / "LMS-MCP" / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    cloud = repo / "cloud.mjs"
    cloud.touch()
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "data_dir": str(tmp_path / "private-state"),
        "lms": {"data_dir": str(tmp_path / "lms-state"), "materials_dir": str(tmp_path / "materials")},
        "artifact": {"root": str(tmp_path / "artifacts"), "port": 8766},
        "course_ids": [],
        "cloud_command": [sys.executable, str(cloud)],
        "model": {"command": sys.executable, "model": "test-model", "thinking": "high", "timeout_seconds": 30, "pages_per_batch": 1},
        "schedule": {"time": "07:30", "timezone": "Asia/Seoul"},
    }), encoding="utf-8")
    return schedule._task_spec(config_path), config_path


@pytest.mark.parametrize(("now", "expected"), [
    ("2026-09-27T22:29:59+00:00", "2026-09-28T07:30:00+09:00"),
    ("2026-09-28T07:30:00+09:00", "2026-09-28T07:30:00+09:00"),
    ("2026-09-28T07:30:01+09:00", "2026-09-29T07:30:00+09:00"),
    ("2026-12-31T23:59:00+09:00", "2027-01-01T07:30:00+09:00"),
    ("2028-02-28T23:00:00+09:00", "2028-02-29T07:30:00+09:00"),
    ("2026-09-27T15:29:59-07:00", "2026-09-28T07:30:00+09:00"),
])
def test_next_seoul_boundary_is_timezone_and_calendar_correct(now, expected):
    boundary = datetime.fromisoformat(schedule._start_boundary(datetime.fromisoformat(now)))
    assert boundary == datetime.fromisoformat(expected)
    assert boundary.utcoffset() == timedelta(hours=9)
    assert boundary.hour == 7 and boundary.minute == 30


def test_naive_clock_cannot_silently_use_local_timezone():
    with pytest.raises(ValueError):
        schedule._start_boundary(datetime(2026, 9, 28, 7))


def _xml(spec, enabled=False):
    return schedule._build_task_xml(
        spec, SID, enabled=enabled, now=datetime(2026, 9, 28, tzinfo=UTC),
    )


def test_xml_enforces_interactive_unprivileged_offline_catchup(task_spec):
    spec, _ = task_spec
    root = ET.fromstring(_xml(spec))
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal.findtext("t:UserId", namespaces=NS) == SID
    assert principal.findtext("t:LogonType", namespaces=NS) == "InteractiveToken"
    assert principal.findtext("t:RunLevel", namespaces=NS) == "LeastPrivilege"
    settings = root.find("t:Settings", NS)
    assert settings.findtext("t:Enabled", namespaces=NS) == "false"
    assert settings.findtext("t:StartWhenAvailable", namespaces=NS) == "true"
    assert settings.findtext("t:WakeToRun", namespaces=NS) == "false"
    assert settings.findtext("t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert settings.findtext("t:DisallowStartIfOnBatteries", namespaces=NS) == "false"
    assert settings.findtext("t:RunOnlyIfNetworkAvailable", namespaces=NS) == "false"
    assert root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=NS) == SID
    assert root.findtext("t:Triggers/t:LogonTrigger/t:Enabled", namespaces=NS) == "true"
    assert root.findtext("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval", namespaces=NS) == "1"
    daily = datetime.fromisoformat(root.findtext("t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=NS))
    assert daily == datetime(2026, 9, 29, 7, 30, tzinfo=schedule.SEOUL)


@pytest.mark.skipif(os.name != "nt", reason="uses the actual Windows command-line parser")
def test_scheduler_argv_roundtrips_korean_spaces_and_shell_metacharacters(task_spec):
    spec, config_path = task_spec
    root = ET.fromstring(_xml(spec))
    command = root.findtext("t:Actions/t:Exec/t:Command", namespaces=NS)
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS)
    assert command == str(config_path.parent.parent.parent / "LMS-MCP" / ".venv" / "Scripts" / "python.exe")
    assert root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=NS) == str(config_path.parent.parent.parent)
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    count = ctypes.c_int()
    argv = parse('"' + command + '" ' + arguments, ctypes.byref(count))
    try:
        assert [argv[index] for index in range(count.value)] == [
            command, "-m", "ssu_lms_mcp.study_cli", "--config", str(config_path), "run", "--scheduled",
        ]
    finally:
        free(argv)


@pytest.mark.parametrize(("path", "replacement"), [
    ("t:RegistrationInfo/t:Source", "unrelated-product"),
    ("t:Principals/t:Principal/t:UserId", "S-1-5-18"),
    ("t:Principals/t:Principal/t:LogonType", "Password"),
    ("t:Principals/t:Principal/t:RunLevel", "HighestAvailable"),
    ("t:Actions/t:Exec/t:Command", r"C:\Windows\System32\cmd.exe"),
    ("t:Actions/t:Exec/t:Arguments", "/c unrelated-job"),
    ("t:Actions/t:Exec/t:WorkingDirectory", r"C:\other-workspace"),
])
def test_same_name_task_cannot_be_adopted_or_overwritten(task_spec, path, replacement):
    spec, _ = task_spec
    root = ET.fromstring(_xml(spec))
    root.find(path, NS).text = replacement
    snapshot = {"exists": True, "xml": ET.tostring(root, encoding="unicode"), "user_sid": SID}
    for enabling in (False, True):
        with pytest.raises(schedule.ScheduleError) as error:
            schedule._require_owned(snapshot, spec, enabling=enabling)
        assert error.value.code == "scheduler_name_conflict"


def test_windows_normalized_default_privilege_keeps_owned_registration(task_spec):
    spec, _ = task_spec
    root = ET.fromstring(_xml(spec))
    principal = root.find("t:Principals/t:Principal", NS)
    principal.remove(principal.find("t:RunLevel", NS))
    normalized = ET.tostring(root, encoding="unicode")
    assert schedule._owned_task(normalized, spec, SID)
    snapshot = {"exists": True, "xml": normalized, "user_sid": SID, "enabled": False}
    assert schedule._status_report(snapshot, spec, "Korea Standard Time")["owned"]


def test_matching_first_action_does_not_hide_an_unrelated_second_action(task_spec):
    spec, _ = task_spec
    root = ET.fromstring(_xml(spec))
    actions = root.find("t:Actions", NS)
    extra = ET.SubElement(actions, f"{{{schedule.TASK_NS}}}Exec")
    ET.SubElement(extra, f"{{{schedule.TASK_NS}}}Command").text = r"C:\unrelated.exe"
    assert not schedule._owned_task(ET.tostring(root, encoding="unicode"), spec, SID)


def test_enabling_requires_an_existing_owned_disabled_registration(task_spec):
    spec, _ = task_spec
    absent = {"exists": False, "user_sid": SID}
    with pytest.raises(schedule.ScheduleError) as error:
        schedule._require_owned(absent, spec, enabling=True)
    assert error.value.code == "scheduler_disable_first"
    registered = {"exists": True, "xml": _xml(spec), "user_sid": SID, "enabled": False}
    report = schedule._status_report(registered, spec, "Pacific Standard Time")
    assert report["owned"] is True and report["enabled"] is False
    assert report["timing"]["timezone"] == "Asia/Seoul"
    assert report["timing"]["time"] == "07:30"
    assert report["windows_timezone"] == "Pacific Standard Time"


def test_status_reports_changed_actual_timing_instead_of_configured_default(task_spec):
    spec, _ = task_spec
    root = ET.fromstring(_xml(spec))
    root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", NS).text = "2026-09-29T10:45:00-07:00"
    snapshot = {"exists": True, "xml": ET.tostring(root, encoding="unicode"), "user_sid": SID, "enabled": True}
    report = schedule._status_report(snapshot, spec, "Pacific Standard Time")
    assert report["enabled"] is True
    assert report["timing"]["time"] == "10:45"
    assert report["timing"]["timezone"] is None
    assert report["timing"]["explicit_offset"] == "-07:00"


@pytest.mark.parametrize(("trigger_state", "expected"), [
    ("omitted_enabled", True),
    ("disabled", False),
    ("absent", False),
])
def test_status_distinguishes_windows_normalized_enabled_and_missing_triggers(task_spec, trigger_state, expected):
    spec, _ = task_spec
    root = ET.fromstring(_xml(spec, enabled=True))
    triggers = root.find("t:Triggers", NS)
    for trigger in list(triggers):
        if trigger_state == "absent":
            triggers.remove(trigger)
        elif trigger_state == "disabled":
            trigger.find("t:Enabled", NS).text = "false"
        else:
            trigger.remove(trigger.find("t:Enabled", NS))
    snapshot = {"exists": True, "xml": ET.tostring(root, encoding="unicode"), "user_sid": SID, "enabled": True}
    report = schedule._status_report(snapshot, spec, "Korea Standard Time")
    assert report["timing"]["daily_enabled"] is expected
    assert report["timing"]["logon_catchup"] is expected


def test_unrelated_task_status_does_not_disclose_its_command_line(task_spec):
    spec, _ = task_spec
    snapshot = {"exists": True, "xml": "<private-argument-canary/>", "user_sid": SID, "enabled": True}
    report = schedule._status_report(snapshot, spec, "Korea Standard Time")
    assert report["status"] == "blocked"
    assert report["error_type"] == "scheduler_name_conflict"
    assert report["timing"] is None
    assert "private-argument-canary" not in json.dumps(report)
