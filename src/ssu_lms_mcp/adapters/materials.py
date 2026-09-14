from __future__ import annotations

import asyncio

from ..models import Material
from .canvas_client import CanvasClient
from .notices import is_session_unauthorized

_MATERIAL_CONCURRENCY = 5

MATERIAL_TYPE_LABELS = {
    "ExternalTool": "강의자료",
    "File": "파일",
    "Page": "페이지",
    "Video": "영상",
    "Assignment": "과제",
    "Discussion": "토론",
    "Quiz": "퀴즈",
    "ExternalUrl": "링크",
    "SubHeader": "구분",
}


def material_type_label(item_type: str | None) -> str:
    return MATERIAL_TYPE_LABELS.get(item_type or "", item_type or "자료")


async def list_materials(client: CanvasClient, course_id: int) -> list[Material]:
    try:
        mods = await client.get_all_pages(
            f"/courses/{course_id}/modules", params={"per_page": 100}, use_canvas=True
        )
    except Exception:
        mods = await client.get(f"/courses/{course_id}/modules", params={"per_page": 100})
    mods = mods if isinstance(mods, list) else mods.get("data", mods.get("modules", []))
    out = []
    for mod in mods:
        mid = mod.get("id")
        try:
            items = await client.get_all_pages(
                f"/courses/{course_id}/modules/{mid}/items",
                params={"per_page": 100},
                use_canvas=True,
            )
        except Exception:
            try:
                items = await client.get(
                    f"/courses/{course_id}/modules/{mid}/items", params={"per_page": 100}
                )
            except Exception as e:
                if is_session_unauthorized(e):
                    raise
                continue
        for x in items if isinstance(items, list) else items.get("data", []):
            content = x.get("content_id") or (x.get("content_details") or {}).get("content_id")
            out.append(
                Material(
                    id=x.get("id", 0),
                    course_id=course_id,
                    module_name=mod.get("name", ""),
                    title=x.get("title", ""),
                    item_type=x.get("type", ""),
                    url=x.get("html_url") or x.get("external_url") or x.get("url"),
                    position=x.get("position", 0),
                    content_id=int(content) if str(content).isdigit() else None,
                    module_id=mid,
                )
            )
    return out


async def list_all_materials(client: CanvasClient, course_ids: list[int]) -> list[Material]:
    semaphore = asyncio.Semaphore(_MATERIAL_CONCURRENCY)

    async def fetch(course_id: int) -> list[Material]:
        async with semaphore:
            return await list_materials(client, course_id)

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
    return sorted(out, key=lambda x: (x.course_id, x.position, x.id))
