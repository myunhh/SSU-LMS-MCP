"""One-shot LMS collection with persistent change tracking."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from filelock import AsyncFileLock

from .adapters.assignments import list_assignments
from .adapters.courses import list_courses
from .adapters.materials import list_materials
from .adapters.notices import list_notices
from .board import list_board_posts
from .downloads import download_board_attachment, download_material
from .session import SessionManager, SessionRequiredError

SCHEMA_VERSION = 1


def automation_dir(settings):
    return Path(settings.data_dir) / "automation"


def automation_lock(settings):
    return AsyncFileLock(str(automation_dir(settings) / "latest.json.lock"), timeout=60)


@asynccontextmanager
async def _locked(settings):
    root = automation_dir(settings)
    root.mkdir(parents=True, exist_ok=True)
    async with automation_lock(settings):
        yield root


def _now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read(path, default):
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError, TypeError):
        return default


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _safe(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        str(key): _safe(item)
        for key, item in value.items()
        if key not in {"description", "message", "url"}
    }


def _key(kind, row):
    if kind == "course":
        return f"course:{row.get('id')}"
    if kind == "board_post":
        return f"board_post:{row.get('course_id')}:{row.get('board_id')}:{row.get('id')}"
    return f"{kind}:{row.get('course_id')}:{row.get('id')}"


def _digest(row):
    return hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _download_key(row):
    if row.get("attachment_id"):
        return f"download:board:{row.get('course_id')}:{row.get('board_id')}:{row.get('post_id')}:{row.get('attachment_id')}"
    return f"download:material:{row.get('course_id')}:{row.get('material_id')}"


def _download_digest(row):
    return _digest({key: value for key, value in row.items() if key not in {"status", "reason"}})


def _merge_course_rows(prior, rows, successful_ids):
    return sorted(
        [row for row in prior if row.get("course_id") not in successful_ids] + rows,
        key=lambda row: (row.get("course_id", 0), row.get("board_id", 0), row.get("id", 0)),
    )


def _successful_download(row):
    return row.get("status") in {"saved", "exists"} and bool(row.get("path"))


def _error(partition, exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return {
        "partition": partition,
        "type": type(exc).__name__[:80],
        "status": int(status) if isinstance(status, int) else None,
    }


def _is_auth(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(exc, SessionRequiredError) or (
        isinstance(exc, httpx.HTTPStatusError) and status in {401, 403, 419}
    )


async def collect(settings, *, course_ids=None, download=True, allow_lti=True, include_boards=True):
    """Collect once. Curation must use ``automation_lock(settings)`` (latest.json.lock)."""
    async with _locked(settings) as root:
        latest_path, pending_path, state_path = (
            root / "latest.json",
            root / "pending.json",
            root / "state.json",
        )
        prior = _read(latest_path, {})
        prior = prior if prior.get("schema_version") == SCHEMA_VERSION else {}
        state = _read(state_path, {})
        hashes = state.get("hashes", {}) if isinstance(state.get("hashes"), dict) else {}
        old = _read(pending_path, {}).get("items", [])
        pending = {
            item["key"]: item
            for item in old
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
        result = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "status": "ok",
            "courses": [],
            "notices": [],
            "assignments": [],
            "materials": [],
            "board_posts": [],
            "downloads": [],
            "errors": [],
        }
        async with SessionManager(settings).client() as client:
            try:
                courses = [_safe(row) for row in await list_courses(client)]
                wanted = None if course_ids is None else {int(value) for value in course_ids}
                result["courses"] = sorted(
                    (row for row in courses if wanted is None or row.get("id") in wanted),
                    key=lambda row: row["id"],
                )
            except Exception as exc:
                if _is_auth(exc):
                    raise SessionRequiredError() from None
                result["courses"] = prior.get("courses", [])
                result["errors"].append(_error("courses", exc))
                wanted = {int(value) for value in course_ids} if course_ids is not None else set()
            ids = sorted(
                wanted if course_ids is not None else {row["id"] for row in result["courses"]}
            )
            if course_ids is not None:
                result["courses"] = sorted(
                    [row for row in prior.get("courses", []) if row.get("id") not in ids]
                    + result["courses"],
                    key=lambda row: row["id"],
                )
            for name, loader in (
                ("notices", list_notices),
                ("assignments", list_assignments),
                ("materials", list_materials),
            ):
                rows, successful_ids = [], set()
                for cid in ids:
                    try:
                        rows.extend(_safe(item) for item in await loader(client, cid))
                        successful_ids.add(cid)
                    except Exception as exc:
                        if _is_auth(exc):
                            raise SessionRequiredError() from None
                        result["errors"].append(_error(f"{name}:{cid}", exc))
                previous_rows = [row for row in prior.get(name, []) if course_ids is not None or row.get("course_id") in ids]
                result[name] = _merge_course_rows(previous_rows, rows, successful_ids)
            if include_boards:
                rows, successful_ids = [], set()
                for cid in ids:
                    try:
                        rows.extend(
                            _safe(item) for item in await list_board_posts(client, settings, cid)
                        )
                        successful_ids.add(cid)
                    except Exception as exc:
                        if _is_auth(exc):
                            raise SessionRequiredError() from None
                        result["errors"].append(_error(f"board_posts:{cid}", exc))
                previous_rows = [row for row in prior.get("board_posts", []) if course_ids is not None or row.get("course_id") in ids]
                result["board_posts"] = _merge_course_rows(previous_rows, rows, successful_ids)
            else:
                result["board_posts"] = prior.get("board_posts", [])
            if download:
                for row in result["materials"]:
                    if row.get("course_id") in ids and row.get("item_type", "").lower() in {
                        "file",
                        "externaltool",
                    }:
                        try:
                            raw = await download_material(
                                settings,
                                client,
                                int(row["course_id"]),
                                int(row["id"]),
                                allow_lti=allow_lti,
                            )
                            result["downloads"].append(
                                {
                                    **raw,
                                    "course_id": row["course_id"],
                                    "material_id": row["id"],
                                    "title": row.get("title", ""),
                                    "module_name": row.get("module_name", ""),
                                }
                            )
                        except Exception as exc:
                            if _is_auth(exc):
                                raise SessionRequiredError() from None
                            result["errors"].append(
                                _error(f"download:material:{row.get('id')}", exc)
                            )
                if include_boards:
                    for row in result["board_posts"]:
                        for attachment in (
                            row.get("attachments", []) if row.get("course_id") in ids else []
                        ):
                            try:
                                raw = await download_board_attachment(
                                    settings,
                                    client,
                                    int(row["course_id"]),
                                    int(row["id"]),
                                    attachment.get("name"),
                                    attachment_id=str(attachment.get("id")),
                                    board_id=int(row.get("board_id", 0)),
                                )
                                result["downloads"].append(
                                    {
                                        **raw,
                                        "course_id": row["course_id"],
                                        "post_id": row["id"],
                                        "board_id": row.get("board_id"),
                                        "attachment_id": attachment.get("id"),
                                        "title": row.get("title", ""),
                                        "module_name": row.get("board_name", ""),
                                    }
                                )
                            except Exception as exc:
                                if _is_auth(exc):
                                    raise SessionRequiredError() from None
                                result["errors"].append(
                                    _error(f"download:board:{row.get('id')}", exc)
                                )
            result["download_attempts"] = [_safe(row) for row in result["downloads"]]
            for attempt in result["download_attempts"]:
                if attempt.get("status") == "failed":
                    result["errors"].append({"partition": _download_key(attempt), "type": "DownloadFailed", "status": None})
            prior_successes = [
                row for row in prior.get("downloads", []) if _successful_download(row)
            ]
            current_successes = [row for row in result["downloads"] if _successful_download(row)]
            downloads = {_download_key(row): row for row in prior_successes}
            downloads.update({_download_key(row): row for row in current_successes})
            result["downloads"] = [downloads[key] for key in sorted(downloads)]
        result["downloads"] = [_safe(row) for row in result["downloads"]]
        result["status"] = "partial" if result["errors"] else "ok"
        for field, kind in {
            "notices": "notice",
            "assignments": "assignment",
            "materials": "material",
            "board_posts": "board_post",
        }.items():
            for row in result[field]:
                key, digest = _key(kind, row), _digest(row)
                if hashes.get(key) != digest:
                    pending[key] = {"key": key, "kind": kind, "hash": digest, "data": row}
                    hashes[key] = digest
        for row in result["downloads"]:
            key, digest = _download_key(row), _download_digest(row)
            if hashes.get(key) != digest:
                pending[key] = {"key": key, "kind": "download", "hash": digest, "data": row}
                hashes[key] = digest
        _write(latest_path, result)
        _write(
            pending_path,
            {"items": [pending[key] for key in sorted(pending)], "generated_at": _now()},
        )
        _write(state_path, {"hashes": hashes})
        return result
