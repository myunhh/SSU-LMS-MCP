"""Durable, public-only desktop event journal for LMS automation."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT = 2048
CHANNEL_IDS = {"notices", "assignments", "materials", "parser"}
CHANNEL_PHASES = {"started", "progress", "notice", "result", "completed"}
CHANNEL_STATUSES = {"running", "completed", "partial", "failed", "skipped"}
_SECRET = re.compile(r"""(?i)\bbearer\s+[^\s,;]+|\b(?:password|passwd|token|secret|username|credential|api[_-]?key)["']?\s*[=:]\s*(?:"[^"]*"|'[^']*'|[^\s,;"'}]+)""")
_AUTH_HEADER = re.compile(r"""(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*["']?\s*[:=]\s*[^\r\n]*""")
_QUERY = re.compile(r"(https?://[^\s?]+)\?[^\s]+")


def _now():
    return datetime.now(UTC).isoformat()


def sanitize_text(value, *, limit=MAX_TEXT):
    """Bound public text and remove common credential and URL-query forms."""
    text = str(value or "").replace("\x00", " ")
    text = _AUTH_HEADER.sub(r"\1: [redacted]", text)
    text = _QUERY.sub(r"\1?[redacted]", _SECRET.sub("[redacted]", text))
    return text[:limit]


def valid_run_id(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return str(parsed) if str(parsed) == value.lower() else None


def run_context(environ=None):
    """Return correlated desktop context, falling back safely for schedules."""
    environ = os.environ if environ is None else environ
    run_id = valid_run_id(environ.get("LMS_DESK_RUN_ID"))
    manual = environ.get("LMS_DESK_TRIGGER") == "manual"
    return {"run_id": run_id or str(uuid.uuid4()), "source": "manual" if manual and run_id else "scheduled"}


def _automation(data_dir):
    return Path(data_dir) / "automation"


def heartbeat_fresh(data_dir, *, now=None):
    """True while the desktop is alive, including when its popups are muted."""
    try:
        value = json.loads((_automation(data_dir) / "desk-heartbeat.json").read_text(encoding="utf-8"))
        expiry = datetime.fromisoformat(value["expiresAt"].replace("Z", "+00:00"))
        current = now or datetime.now(expiry.tzinfo or UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return expiry > current
    except (KeyError, OSError, TypeError, ValueError):
        return False


def append_event(data_dir, event):
    """Append one contract-shaped JSONL event under a process-safe lock."""
    if not isinstance(event, dict) or event.get("type") not in {
        "run.started", "run.completed", "notification", "activity"
    }:
        raise ValueError("unsupported desk event")
    run_id = valid_run_id(event.get("runId"))
    if not run_id:
        raise ValueError("runId must be a UUID")
    at = event.get("at", _now())
    try:
        parsed_at = datetime.fromisoformat(at.replace("Z", "+00:00")) if isinstance(at, str) else None
    except ValueError as exc:
        raise ValueError("at must be an ISO timestamp with a timezone") from exc
    if parsed_at is None or parsed_at.tzinfo is None:
        raise ValueError("at must be an ISO timestamp with a timezone")
    row = {"v": 1, "id": valid_run_id(event.get("id")) or str(uuid.uuid4()), "runId": run_id,
           "type": event["type"], "at": at,
           "mode": sanitize_text(event.get("mode"), limit=64),
           "source": event.get("source") if event.get("source") in {"manual", "scheduled"} else "scheduled"}
    for key in ("title", "status", "summary", "detail", "activityType"):
        if isinstance(event.get(key), str) and event[key]:
            row[key] = sanitize_text(event[key])
    if event.get("activityType") == "channel":
        if event.get("type") != "activity" or event.get("channelId") not in CHANNEL_IDS:
            raise ValueError("invalid workflow channel event")
        if event.get("phase") not in CHANNEL_PHASES or event.get("status") not in CHANNEL_STATUSES:
            raise ValueError("invalid workflow channel state")
        row.update(channelId=event["channelId"], phase=event["phase"], status=event["status"])
        for key in ("itemKey", "contentHash"):
            if isinstance(event.get(key), str) and event[key]:
                row[key] = sanitize_text(event[key], limit=256)
        if event.get("mention") is True and event["channelId"] == "notices" and event["phase"] == "notice":
            row["mention"] = True
    root = _automation(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "desk-events.jsonl"
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    with FileLock(str(path) + ".lock"):
        if path.exists() and path.stat().st_size + len(encoded.encode("utf-8")) > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    return row


def journal_notification(data_dir, title, body, *, mode="upcoming", context=None):
    """Always retain notification history; use native fallback if desktop is absent."""
    context = context or run_context()
    try:
        append_event(data_dir, {"runId": context["run_id"], "type": "notification", "mode": mode,
                                "source": context["source"], "title": title, "detail": body})
        return heartbeat_fresh(data_dir)
    except (OSError, ValueError):
        return False
