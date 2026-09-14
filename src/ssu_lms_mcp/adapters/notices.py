from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from ..models import Notice, NoticeAttachment
from .canvas_client import CanvasClient

MAX_TEXT_LEN = 8000
_NOTICE_CONCURRENCY = 5


def html_to_text(html: str, limit: int = MAX_TEXT_LEN) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    for tag in soup.find_all(
        [
            "p",
            "div",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "table",
            "blockquote",
            "pre",
        ]
    ):
        tag.append("\n")
    return "\n".join(
        " ".join(line.split()) for line in soup.get_text().splitlines() if line.split()
    )[:limit]


def is_session_unauthorized(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403, 419}


def _window() -> dict:
    return {
        "start_date": (datetime.now(UTC) - timedelta(days=180)).date().isoformat(),
        "end_date": (datetime.now(UTC) + timedelta(days=1)).date().isoformat(),
    }


def _notice(data: dict, cid: int) -> Notice:
    msg = data.get("message") or ""
    author = data.get("author", {})
    return Notice(
        id=data["id"],
        course_id=cid,
        title=data.get("title", ""),
        message=msg,
        message_text=html_to_text(msg),
        posted_at=data.get("posted_at", data.get("created_at")),
        author=author.get("display_name", "") if isinstance(author, dict) else "",
        html_url=data.get("html_url", ""),
        is_read=data.get("read_state") == "read",
        pinned=bool(data.get("pinned")),
        attachments=[
            NoticeAttachment(
                id=str(item.get("id") or item.get("attachment_id") or index),
                name=str(item.get("display_name") or item.get("filename") or item.get("name") or "첨부파일"),
                url=item.get("url") or item.get("download_url"),
                size=item.get("size"),
                content_type=item.get("content_type") or item.get("mime_type"),
            )
            for index, item in enumerate(data.get("attachments") or [])
            if isinstance(item, dict)
        ],
    )


async def list_notices(client: CanvasClient, course_id: int) -> list[Notice]:
    try:
        raw = await client.get_all_pages(
            "/announcements",
            params={"context_codes[]": f"course_{course_id}", "per_page": 50, **_window()},
            use_canvas=True,
        )
    except Exception:
        raw = await client.get(
            f"/courses/{course_id}/discussion_topics",
            params={"only_announcements": "true", "per_page": 50},
        )
    return [
        _notice(x, course_id)
        for x in (raw if isinstance(raw, list) else raw.get("data", []))
        if x.get("id")
    ]


async def list_discussions(client: CanvasClient, course_id: int) -> list[Notice]:
    try:
        raw = await client.get_all_pages(
            f"/courses/{course_id}/discussion_topics", params={"per_page": 50}, use_canvas=True
        )
    except Exception:
        raw = await client.get(f"/courses/{course_id}/discussion_topics", params={"per_page": 50})
    return [
        _notice(x, course_id)
        for x in (raw if isinstance(raw, list) else raw.get("data", []))
        if x.get("id")
    ]


async def list_all_notices(client: CanvasClient, course_ids: list[int]) -> list[Notice]:
    if not course_ids:
        return []
    try:
        raw = await client.get_all_pages(
            "/announcements",
            params={
                "context_codes[]": [f"course_{x}" for x in course_ids],
                "per_page": 100,
                **_window(),
            },
            use_canvas=True,
        )
    except Exception:
        semaphore = asyncio.Semaphore(_NOTICE_CONCURRENCY)

        async def fetch(course_id: int) -> list[Notice]:
            async with semaphore:
                return await list_notices(client, course_id)

        results = await asyncio.gather(
            *(fetch(course_id) for course_id in course_ids), return_exceptions=True
        )
        out = []
        for result in results:
            if isinstance(result, BaseException):
                if is_session_unauthorized(result):
                    raise result
            else:
                out.extend(result)
        return out
    out = []
    for x in raw if isinstance(raw, list) else raw.get("data", []):
        code = x.get("context_code", "")
        cid = (
            int(code.split("_", 1)[1])
            if code.startswith("course_") and code.split("_", 1)[1].isdigit()
            else 0
        )
        if x.get("id"):
            out.append(_notice(x, cid))
    return out
