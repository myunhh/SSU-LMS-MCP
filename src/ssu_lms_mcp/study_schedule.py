"""Owned, password-free Windows Task Scheduler registration for SSU study."""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

TASK_NAME = "SSU Study Daily"
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SEOUL = timezone(timedelta(hours=9), "Asia/Seoul")
_NS = {"t": TASK_NS}


class ScheduleError(RuntimeError):
    """Fixed failure code; never includes PowerShell stderr or task XML."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# Only this fixed local script is executed. Paths and XML travel as JSON on stdin,
# never through PowerShell interpolation, command text, shell=True or credentials.
_SCHEDULER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$failure = 'scheduler_unavailable'
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')
    function Find-StudyTask {
        try { return $folder.GetTask('SSU Study Daily') }
        catch {
            $cause = $_.Exception
            while ($null -ne $cause) {
                if ($cause.HResult -eq -2147024894) { return $null }
                $cause = $cause.InnerException
            }
            throw
        }
    }
    $failure = 'scheduler_query_failed'
    $task = Find-StudyTask
    if ($request.operation -eq 'configure') {
        $failure = 'scheduler_identity_changed'
        if ($request.user_sid -ne $sid) { throw 'identity changed' }
        $failure = 'scheduler_task_changed'
        if ($null -ne $task) {
            if (-not $request.expected_xml -or $task.Xml -cne $request.expected_xml) {
                throw 'task changed'
            }
            $flags = 4
        } else {
            if ($request.expected_xml) { throw 'task disappeared' }
            $flags = 2
        }
        $failure = 'scheduler_disable_first'
        if ($request.enabled -and $null -eq $task) { throw 'disable first' }
        $failure = 'scheduler_registration_failed'
        $xml = [System.IO.File]::ReadAllText($request.xml_path)
        # TASK_CREATE or TASK_UPDATE; TASK_LOGON_INTERACTIVE_TOKEN (3).
        # No password, highest privileges, remote server or forced overwrite.
        $task = $folder.RegisterTask('SSU Study Daily', $xml, $flags, $sid, $null, 3, $null)
    }
    $failure = 'scheduler_query_failed'
    if ($null -eq $task) {
        @{exists=$false; user_sid=$sid} | ConvertTo-Json -Compress
    } else {
        $last = $null
        $next = $null
        if ($task.LastRunTime.Year -gt 1900) { $last = $task.LastRunTime.ToString('s') }
        if ($task.NextRunTime.Year -gt 1900) { $next = $task.NextRunTime.ToString('s') }
        @{
            exists=$true; user_sid=$sid; xml=$task.Xml; enabled=[bool]$task.Enabled
            state=[int]$task.State; last_result=[long]$task.LastTaskResult
            last_run_local=$last; next_run_local=$next
        } | ConvertTo-Json -Compress -Depth 5
    }
} catch {
    @{error_type=$failure} | ConvertTo-Json -Compress
    exit 1
}
"""

_SAFE_CODES = {
    "scheduler_unavailable", "scheduler_query_failed", "scheduler_identity_changed",
    "scheduler_task_changed", "scheduler_disable_first", "scheduler_registration_failed",
}


def _system_binary(relative: str) -> Path:
    if os.name != "nt":
        raise ScheduleError("windows_required")
    root = os.environ.get("SystemRoot")
    if not root or not Path(root).is_absolute():
        raise ScheduleError("windows_system_path_unavailable")
    binary = Path(root) / "System32" / relative
    if not binary.is_file():
        raise ScheduleError("windows_system_binary_unavailable")
    return binary


def _windows_timezone() -> str:
    try:
        result = subprocess.run(
            [str(_system_binary("tzutil.exe")), "/g"], capture_output=True,
            stdin=subprocess.DEVNULL, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ScheduleError("windows_timezone_unavailable") from None
    # Windows time-zone identifiers are invariant ASCII even on Korean Windows.
    name = result.stdout.decode("ascii", errors="replace").strip()
    if result.returncode or not name or any(ord(char) < 32 or ord(char) > 126 for char in name):
        raise ScheduleError("windows_timezone_unavailable")
    return name


def _scheduler_call(request: dict) -> dict:
    script = base64.b64encode(_SCHEDULER_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            [str(_system_binary("WindowsPowerShell/v1.0/powershell.exe")),
             "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ScheduleError("scheduler_unavailable") from None
    try:
        report = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(report, dict):
            raise ValueError
    except (UnicodeError, ValueError):
        raise ScheduleError("scheduler_response_invalid") from None
    if report.get("error_type") or result.returncode:
        code = report.get("error_type")
        raise ScheduleError(code if code in _SAFE_CODES else "scheduler_unavailable")
    if not isinstance(report.get("exists"), bool) or not isinstance(report.get("user_sid"), str):
        raise ScheduleError("scheduler_response_invalid")
    return report


def _task_spec(config_path) -> dict:
    from .study_runner import StudyConfigError, load_config

    path = Path(config_path).expanduser().resolve()
    config = load_config(path)
    schedule = config.get("schedule", {})
    if schedule.get("time") != "07:30" or schedule.get("timezone") != "Asia/Seoul":
        raise StudyConfigError("schedule_timing_invalid")
    repo = path.parent.parent.parent
    executable = repo / "LMS-MCP" / ".venv" / "Scripts" / "python.exe"
    if not executable.is_file():
        raise StudyConfigError("schedule_python_missing")
    arguments = subprocess.list2cmdline([
        "-m", "ssu_lms_mcp.study_cli", "--config", str(path), "run", "--scheduled",
    ])
    identity = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
    return {
        "owner": f"ssu-study:v1:{identity}", "executable": str(executable),
        "arguments": arguments, "working_directory": str(repo),
        "data_dir": Path(config["data_dir"]),
    }


def _start_boundary(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_aware_datetime_required")
    now = now.astimezone(SEOUL)
    boundary = datetime.combine(now.date(), time(7, 30), tzinfo=SEOUL)
    if boundary < now:
        boundary += timedelta(days=1)
    return boundary.isoformat(timespec="seconds")


def _build_task_xml(spec: dict, user_sid: str, *, enabled: bool, now: datetime) -> str:
    task = ET.Element("Task", {"xmlns": TASK_NS, "version": "1.2"})

    def child(parent, name, value=None, **attributes):
        element = ET.SubElement(parent, name, attributes)
        if value is not None:
            element.text = str(value)
        return element

    registration = child(task, "RegistrationInfo")
    child(registration, "Author", user_sid)
    child(registration, "Description", "SSU private study pipeline; Seoul 07:30 and logon catchup.")
    child(registration, "Source", spec["owner"])
    triggers = child(task, "Triggers")
    daily = child(triggers, "CalendarTrigger", id="SeoulDaily")
    child(daily, "StartBoundary", _start_boundary(now))
    child(daily, "Enabled", "true")
    child(child(daily, "ScheduleByDay"), "DaysInterval", "1")
    logon = child(triggers, "LogonTrigger", id="OfflineCatchup")
    child(logon, "Enabled", "true")
    child(logon, "UserId", user_sid)
    principals = child(task, "Principals")
    principal = child(principals, "Principal", id="CurrentUser")
    child(principal, "UserId", user_sid)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    child(settings, "DisallowStartIfOnBatteries", "false")
    child(settings, "StopIfGoingOnBatteries", "false")
    child(settings, "AllowHardTerminate", "false")
    child(settings, "StartWhenAvailable", "true")
    child(settings, "RunOnlyIfNetworkAvailable", "false")
    child(settings, "AllowStartOnDemand", "true")
    child(settings, "Enabled", "true" if enabled else "false")
    child(settings, "Hidden", "false")
    child(settings, "RunOnlyIfIdle", "false")
    child(settings, "WakeToRun", "false")
    child(settings, "ExecutionTimeLimit", "PT0S")
    actions = child(task, "Actions", Context="CurrentUser")
    action = child(actions, "Exec")
    child(action, "Command", spec["executable"])
    child(action, "Arguments", spec["arguments"])
    child(action, "WorkingDirectory", spec["working_directory"])
    return ET.tostring(task, encoding="unicode", xml_declaration=False)


def _owned_task(xml: str, spec: dict, user_sid: str) -> bool:
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError):
        return False
    if root.tag != f"{{{TASK_NS}}}Task":
        return False
    expected = {
        "t:RegistrationInfo/t:Source": spec["owner"],
        "t:Principals/t:Principal/t:UserId": user_sid,
        "t:Principals/t:Principal/t:LogonType": "InteractiveToken",
        "t:Actions/t:Exec/t:Command": spec["executable"],
        "t:Actions/t:Exec/t:Arguments": spec["arguments"],
        "t:Actions/t:Exec/t:WorkingDirectory": spec["working_directory"],
    }
    # Task Scheduler omits the default LeastPrivilege element on XML export.
    if root.findtext("t:Principals/t:Principal/t:RunLevel", default="LeastPrivilege", namespaces=_NS) != "LeastPrivilege":
        return False
    if any(root.findtext(path, namespaces=_NS) != value for path, value in expected.items()):
        return False
    actions = root.find("t:Actions", _NS)
    principals = root.find("t:Principals", _NS)
    return actions is not None and len(actions) == 1 and principals is not None and len(principals) == 1


def _require_owned(snapshot: dict, spec: dict, *, enabling: bool) -> None:
    if snapshot["exists"]:
        if not _owned_task(snapshot.get("xml", ""), spec, snapshot["user_sid"]):
            raise ScheduleError("scheduler_name_conflict")
    elif enabling:
        raise ScheduleError("scheduler_disable_first")


def _status_report(snapshot: dict, spec: dict, windows_timezone: str) -> dict:
    exists = snapshot["exists"]
    owned = exists and _owned_task(snapshot.get("xml", ""), spec, snapshot["user_sid"])
    report = {
        "status": "ok" if not exists or owned else "blocked",
        "task": TASK_NAME, "exists": exists, "owned": owned,
        "enabled": snapshot.get("enabled") if exists else False,
        "windows_timezone": windows_timezone,
        "timing": None,
    }
    if exists and not owned:
        # Unrelated task contents, identities and command lines are never exposed.
        report["error_type"] = "scheduler_name_conflict"
        return report
    if not exists:
        return report
    root = ET.fromstring(snapshot["xml"])
    daily = root.find("t:Triggers/t:CalendarTrigger", _NS)
    logon = root.find("t:Triggers/t:LogonTrigger", _NS)
    boundary = root.findtext("t:Triggers/t:CalendarTrigger/t:StartBoundary", namespaces=_NS)
    report["timing"] = {
        "start_boundary": boundary,
        "time": boundary[11:16] if boundary else None,
        "timezone": "Asia/Seoul" if boundary and boundary.endswith("+09:00") else None,
        "explicit_offset": boundary[-6:] if boundary and boundary[-6:-5] in {"+", "-"} else None,
        "days_interval": root.findtext("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval", namespaces=_NS),
        "daily_enabled": daily is not None and daily.findtext("t:Enabled", default="true", namespaces=_NS) == "true",
        "logon_catchup": logon is not None and logon.findtext("t:Enabled", default="true", namespaces=_NS) == "true",
        "start_when_available": root.findtext("t:Settings/t:StartWhenAvailable", namespaces=_NS) == "true",
        "wake_to_run": root.findtext("t:Settings/t:WakeToRun", namespaces=_NS) == "true",
        "multiple_instances": root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=_NS),
    }
    report.update({key: snapshot.get(key) for key in (
        "state", "last_result", "last_run_local", "next_run_local",
    )})
    return report


def schedule_status(config_path) -> dict:
    """Read actual local scheduler state; never create, enable or run a task."""
    spec = _task_spec(config_path)
    windows_timezone = _windows_timezone()
    snapshot = _scheduler_call({"operation": "status"})
    return _status_report(snapshot, spec, windows_timezone)


def configure_schedule(config_path, enabled: bool) -> dict:
    """Register disabled first, then explicitly enable an owned current-user task."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled_must_be_boolean")
    spec = _task_spec(config_path)
    windows_timezone = _windows_timezone()
    snapshot = _scheduler_call({"operation": "status"})
    _require_owned(snapshot, spec, enabling=enabled)
    xml = _build_task_xml(spec, snapshot["user_sid"], enabled=enabled, now=datetime.now(SEOUL))
    directory = spec["data_dir"] / "scheduler"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix="ssu-study-", suffix=".xml", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-16") as stream:
            stream.write('<?xml version="1.0" encoding="UTF-16"?>\n' + xml)
        actual = _scheduler_call({
            "operation": "configure", "enabled": enabled, "xml_path": filename,
            "expected_xml": snapshot.get("xml"), "user_sid": snapshot["user_sid"],
        })
    finally:
        Path(filename).unlink(missing_ok=True)
    _require_owned(actual, spec, enabling=True)
    if actual.get("enabled") is not enabled:
        raise ScheduleError("scheduler_state_mismatch")
    return _status_report(actual, spec, windows_timezone)
