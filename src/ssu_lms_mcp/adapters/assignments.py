from __future__ import annotations

import asyncio

from ..models import Assignment
from .canvas_client import CanvasClient
from .notices import html_to_text, is_session_unauthorized

_DEADLINE_CONCURRENCY = 5


async def _submitted(client: CanvasClient, cid: int) -> set[int]:
    try:
        rows = await client.get_all_pages(
            f"/courses/{cid}/students/submissions",
            params={"student_ids[]": "self", "per_page": 100},
            use_canvas=True,
        )
        return {
            x["assignment_id"] for x in rows if x.get("workflow_state") not in {None, "unsubmitted"}
        }
    except Exception as exc:
        if is_session_unauthorized(exc):
            raise
        return set()


async def list_assignments(client: CanvasClient, course_id: int) -> list[Assignment]:
    try:
        raw = await client.get(
            f"/courses/{course_id}/assignments", params={"per_page": 100, "order_by": "due_at"}
        )
    except Exception:
        raw = await client.get_all_pages(
            f"/courses/{course_id}/assignments",
            params={"per_page": 100, "order_by": "due_at"},
            use_canvas=True,
        )
    submitted = await _submitted(client, course_id)
    items = raw if isinstance(raw, list) else raw.get("assignments", raw.get("data", []))
    return [
        Assignment(
            id=x["id"],
            course_id=course_id,
            title=x.get("name", ""),
            due_at=x.get("due_at"),
            points_possible=x.get("points_possible"),
            submission_types=x.get("submission_types", []),
            html_url=x.get("html_url", ""),
            description=x.get("description") or "",
            description_text=html_to_text(x.get("description") or ""),
            submitted=x["id"] in submitted,
        )
        for x in items
        if x.get("id")
    ]


async def list_all_deadlines(client: CanvasClient, course_ids: list[int]) -> list[Assignment]:
    semaphore = asyncio.Semaphore(_DEADLINE_CONCURRENCY)

    async def fetch(course_id: int) -> list[Assignment]:
        async with semaphore:
            return await list_assignments(client, course_id)

    rows = await asyncio.gather(
        *(fetch(course_id) for course_id in course_ids), return_exceptions=True
    )
    out = []
    for row in rows:
        if isinstance(row, BaseException):
            if is_session_unauthorized(row):
                raise row
        else:
            out.extend(row)
    return sorted(out, key=lambda x: x.due_at or "9999")
