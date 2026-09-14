from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..models import Course
from .canvas_client import CanvasClient
from .notices import is_session_unauthorized

_COURSE_CONCURRENCY = 5


async def _professor(c: CanvasClient, cid: int) -> str:
    try:
        x = await c.get(
            f"/courses/{cid}/users",
            params={"enrollment_type[]": "teacher", "per_page": 10},
            use_canvas=True,
        )
        x = x if isinstance(x, list) else x.get("data", [])
        return ", ".join(t.get("name", "") for t in x if t.get("name"))
    except Exception as e:
        if is_session_unauthorized(e):
            raise
        return ""


async def _progress(c: CanvasClient, cid: int) -> tuple[float | None, int]:
    try:
        rows = await c.get_all_pages(
            f"/courses/{cid}/modules",
            params={"per_page": 100, "include[]": "items"},
            use_canvas=True,
        )
        total = sum(len(x.get("items", [])) for x in rows)
        done = sum(
            1
            for x in rows
            for y in x.get("items", [])
            if y.get("completion_requirement", {}).get("completed")
        )
        return (round(done / total * 100, 1), total) if total else (None, 0)
    except Exception as e:
        if is_session_unauthorized(e):
            raise
        return None, 0


def _attendance(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


async def list_course_ids(client: CanvasClient) -> list[int]:
    try:
        raw = await client.get("/courses", params={"enrollment_state": "active", "per_page": 50})
    except Exception:
        raw = await client.get(
            "/courses", params={"enrollment_state": "active", "per_page": 50}, use_canvas=True
        )
    return [
        x["id"]
        for x in (raw if isinstance(raw, list) else raw.get("courses", raw.get("data", [])))
        if x.get("id")
    ]


async def list_courses(
    client: CanvasClient, attendance_cache_file: Path | None = None
) -> list[Course]:
    params = {
        "enrollment_state": "active",
        "per_page": 50,
        "include[]": ["term", "total_students", "course_progress"],
    }
    try:
        raw = await client.get("/courses", params=params)
    except Exception:
        raw = await client.get("/courses", params=params, use_canvas=True)
    cache = _attendance(attendance_cache_file)
    items = raw if isinstance(raw, list) else raw.get("courses", raw.get("data", []))
    semaphore = asyncio.Semaphore(_COURSE_CONCURRENCY)

    async def build(x: dict) -> Course:
        cid = x["id"]
        async with semaphore:
            professor, (fallback, count) = await asyncio.gather(
                _professor(client, cid), _progress(client, cid)
            )
        att = cache.get(str(cid))
        cp = x.get("course_progress") or {}
        progress = (
            att.get("rate")
            if isinstance(att, dict) and att.get("rate") is not None
            else (
                round(cp.get("requirement_completed_count", 0) / cp["requirement_count"] * 100, 1)
                if cp.get("requirement_count")
                else fallback
            )
        )
        term = x.get("term") or {}
        return Course(
            id=cid,
            name=x.get("name", x.get("course_name", "")),
            course_code=x.get("course_code", ""),
            term=term.get("name", "") if isinstance(term, dict) else "",
            professor=professor,
            credits=x.get("credits"),
            progress=progress,
            materials=count,
        )

    return list(await asyncio.gather(*(build(x) for x in items if x.get("id"))))
