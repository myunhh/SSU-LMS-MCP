"""Discover complete LMS source partitions and preserve immutable original files.

Only the initial ExternalTool resolution may launch LTI. Subsequent acquisitions
refresh the resolved file, not the viewer, and never replace the download cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from . import downloads
from .adapters.courses import list_courses
from .board import get_board_post, list_board_posts
from .models import Material
from .session import SessionManager, SessionRequiredError
from .urls import valid_url


class _SourceStop(Exception):
    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


def _positive_id(value) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError("invalid_identifier")
    return int(value)


def _attachment_id(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("invalid_identifier")
    # LearningX uses numeric IDs; also permit opaque IDs without accepting URLs.
    if not re.fullmatch(r"[\w.-]+", value, flags=re.ASCII):
        raise ValueError("invalid_identifier")
    return value


def _key(item: dict) -> str:
    cid = _positive_id(item.get("course_id"))
    if item.get("kind") == "material":
        return f"material:{cid}:{_positive_id(item.get('material_id'))}"
    if item.get("kind") == "board":
        bid = _positive_id(item.get("board_id"))
        pid = _positive_id(item.get("post_id"))
        aid = quote(_attachment_id(item.get("attachment_id")), safe="")
        return f"board:{cid}:{bid}:{pid}:{aid}"
    raise ValueError("invalid_source_kind")


def _digest(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _error_type(exc: Exception) -> str:
    if isinstance(exc, SessionRequiredError):
        return "session_required"
    if isinstance(exc, httpx.HTTPStatusError):
        return "session_required" if exc.response.status_code in {401, 403, 419} else "http_error"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "network_error"
    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        return "invalid_source_metadata"
    if isinstance(exc, OSError):
        return "storage_error"
    return "source_error"


async def _all_pages(client, path: str) -> list[dict]:
    # The ordinary material adapter skips failed modules. A baseline scan must
    # fail the entire partition instead of treating an incomplete list as empty.
    try:
        rows = await client.get_all_pages(path, params={"per_page": 100}, use_canvas=True)
    except httpx.HTTPError:
        rows = await client.get_all_pages(path, params={"per_page": 100})
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid_source_metadata")
    return rows


def _stable_launch_url(value, course_id: int, material_id: int) -> str:
    canonical = f"https://canvas.ssu.ac.kr/courses/{course_id}/modules/items/{material_id}"
    if not isinstance(value, str) or not valid_url(value, hosts={"canvas.ssu.ac.kr", "lms.ssu.ac.kr"}):
        return canonical
    parsed = urlsplit(value)
    page = rf"/courses/{course_id}/(?:modules/items/{material_id}|external_tools/[1-9][0-9]*)"
    if parsed.query or parsed.fragment or not re.fullmatch(page, parsed.path):
        return canonical
    return value


def _material_row(row: dict, course_id: int, module_id: int, module_name: str) -> Material:
    iid = _positive_id(row.get("id"))
    for field, expected in (("course_id", course_id), ("module_id", module_id)):
        if row.get(field) is not None and _positive_id(row[field]) != expected:
            raise ValueError("source_identifier_mismatch")
    item_type = str(row.get("type") or "")
    if item_type.lower() not in {"file", "externaltool"}:
        raise _SourceStop("skipped", "unsupported_material_type")
    content = row.get("content_id") or (row.get("content_details") or {}).get("content_id")
    return Material(
        id=iid, course_id=course_id, module_id=module_id,
        module_name=module_name, title=row.get("title") or "",
        item_type=item_type, position=row.get("position") or 0,
        content_id=_positive_id(content) if str(content).isdigit() else None,
        url=_stable_launch_url(row.get("html_url") or row.get("external_url") or row.get("url"), course_id, iid),
    )


async def _material(client, item: dict) -> Material:
    cid, mid = item["course_id"], item["material_id"]
    module_id = _positive_id(item.get("module_id"))
    path = f"/courses/{cid}/modules/{module_id}/items/{mid}"
    try:
        row = await client.get(path, use_canvas=True)
    except httpx.HTTPError:
        row = await client.get(path)
    # Never use descriptor metadata after an API failure: even a cached file
    # must still belong to this module item, and its content ID may have changed.
    if not isinstance(row, dict) or _positive_id(row.get("id")) != mid:
        raise ValueError("source_identifier_mismatch")
    return _material_row(row, cid, module_id, item.get("module_name", ""))


async def _materials(client, course_id: int) -> list[Material]:
    materials = []
    seen = set()
    for module in await _all_pages(client, f"/courses/{course_id}/modules"):
        mid = _positive_id(module.get("id"))
        rows = await _all_pages(client, f"/courses/{course_id}/modules/{mid}/items")
        for row in rows:
            item_type = str(row.get("type") or "")
            if item_type.lower() not in {"file", "externaltool"}:
                continue
            iid = _positive_id(row.get("id"))
            if iid in seen:
                raise ValueError("duplicate_material_identifier")
            seen.add(iid)
            materials.append(_material_row(row, course_id, mid, module.get("name") or ""))
    return materials


def _material_descriptor(material: Material, course: dict) -> dict:
    cid, mid = _positive_id(course["id"]), _positive_id(material.id)
    item = {
        "partition": f"material:{cid}", "kind": "material", "course_id": cid,
        "material_id": mid, "title": material.title, "module_name": material.module_name,
        "course_name": course["name"], "professor": course.get("professor", ""),
        "source_url": f"https://canvas.ssu.ac.kr/courses/{cid}/modules/items/{mid}",
        "module_id": _positive_id(material.module_id),
        "item_type": material.item_type.lower(), "content_id": material.content_id,
        "launch_url": _stable_launch_url(material.url, cid, mid),
    }
    item["key"] = _key(item)
    item["metadata_hash"] = _digest(item)
    return item


def _board_descriptors(posts, course: dict) -> list[dict]:
    cid = _positive_id(course["id"])
    items, seen = [], set()
    for post in posts:
        if post.course_id != cid:
            raise ValueError("course_identifier_mismatch")
        bid, pid = _positive_id(post.board_id), _positive_id(post.id)
        for attachment in post.attachments:
            aid = _attachment_id(attachment.id)
            item = {
                "partition": f"board:{cid}", "kind": "board", "course_id": cid,
                "board_id": bid, "post_id": pid, "attachment_id": aid,
                "title": post.title, "module_name": post.board_name,
                "course_name": course["name"], "professor": course.get("professor", ""),
                "filename": attachment.name,
                "source_url": f"https://canvas.ssu.ac.kr/learningx/lti/learningx_board/boards/{bid}/posts/{pid}",
            }
            item["key"] = _key(item)
            if item["key"] in seen:
                raise ValueError("duplicate_attachment_identifier")
            seen.add(item["key"])
            # Signed URLs, post read counts and attachment download state are
            # deliberately excluded. Bytes, not this signature, detect revisions.
            item["metadata_hash"] = _digest({
                **item, "size": attachment.size, "content_type": attachment.content_type,
            })
            items.append(item)
    return items


async def discover(settings, course_ids: list[int]) -> dict:
    """Scan each course's material and board partitions independently.

    An empty course selection means all courses in the enrolled-course catalog.
    No partial partition is reported as a successful baseline.
    """
    result = {"courses": [], "items": [], "successful_partitions": [], "errors": []}
    selected = []
    try:
        selected = list(dict.fromkeys(_positive_id(cid) for cid in course_ids))
        async with SessionManager(settings).client() as client:
            courses = [course.model_dump() for course in await list_courses(client)]
            catalog = {_positive_id(course["id"]): course for course in courses}
            selected = selected or list(catalog)
            for cid in selected:
                course = catalog.get(cid)
                if course is None:
                    result["errors"].extend({"partition": f"{kind}:{cid}", "error_type": "course_unavailable"}
                                            for kind in ("material", "board"))
                    continue
                result["courses"].append(course)
                for kind in ("material", "board"):
                    partition = f"{kind}:{cid}"
                    try:
                        if kind == "material":
                            items = [_material_descriptor(row, course) for row in await _materials(client, cid)]
                        else:
                            items = _board_descriptors(await list_board_posts(client, settings, cid), course)
                    except Exception as exc:
                        result["errors"].append({"partition": partition, "error_type": _error_type(exc)})
                    else:
                        result["items"].extend(items)
                        result["successful_partitions"].append(partition)
    except Exception as exc:
        finished = set(result["successful_partitions"]) | {row["partition"] for row in result["errors"]}
        partitions = [f"{kind}:{cid}" for cid in selected for kind in ("material", "board")]
        for partition in partitions or ["courses"]:
            if partition not in finished:
                result["errors"].append({"partition": partition, "error_type": _error_type(exc)})
    return result


async def _material_source(settings, client, item: dict) -> dict:
    cid, mid = item["course_id"], item["material_id"]
    material = await _material(client, item)
    if material.item_type.lower() == "file":
        if material.content_id is None:
            raise _SourceStop("review_required", "file_identifier_missing")
        # Always refresh Canvas metadata: URLs expire and the same module ID can
        # refer to a replacement content ID. Never take the cached-file shortcut.
        return await downloads._canvas_source(client, material.content_id)

    manifest_key = f"{cid}:{mid}"
    prior = await downloads._manifest_update(settings, manifest_key)
    if prior:
        if (str(prior.get("content_type") or "").strip().lower() in downloads.VIDEO_TYPES
                or (prior.get("status") == "skipped" and prior.get("reason") == "동영상 또는 외부 자료입니다.")):
            raise _SourceStop("skipped", "video")
        if prior.get("source") == "commons" and prior.get("content_id"):
            if not prior.get("filename"):
                raise _SourceStop("review_required", "original_filename_missing")
            # Legacy Commons records came from str(file_name or "자료").
            # Only that historical fallback is ambiguous; retain other supplied
            # names without rewriting the cache or replaying attendance.
            if (prior.get("filename_verified") is not True
                    and ("filename_verified" in prior or prior["filename"] == "자료")):
                raise _SourceStop("review_required", "original_filename_unverified")
            return {**prior, "url": downloads._commons_url(str(prior["content_id"]))}
        if prior.get("source") == "canvas" and prior.get("content_id"):
            return await downloads._canvas_source(client, _positive_id(prior["content_id"]))
        if prior.get("lti_attempted") or prior.get("path") or prior.get("url"):
            raise _SourceStop("review_required", "resolved_file_source_unavailable")

    # Share the existing downloader's reservation and lock. Cancellation or a
    # failed launch must never cause another attendance-affecting viewer launch.
    reserved = {**(prior or {}), "status": "launch_reserved", "lti_attempted": True}
    await downloads._manifest_update(settings, manifest_key, reserved)
    details = await downloads._launch_external(settings, str(material.url or ""))
    content_id = details.get("content_id")
    content_type = str(details.get("content_type") or "").strip().lower()
    if not content_id or content_type in downloads.VIDEO_TYPES:
        await downloads._manifest_update(settings, manifest_key, {
            **reserved, "status": "skipped", "content_type": content_type,
        })
        raise _SourceStop("skipped" if content_type in downloads.VIDEO_TYPES else "review_required",
                          "video" if content_type in downloads.VIDEO_TYPES else "resolved_file_source_unavailable")
    resolved = {
        **reserved, "status": "resolved", "source": "commons", "content_type": content_type,
        "content_id": str(content_id),
        "filename": details.get("file_name") if isinstance(details.get("file_name"), str) else None,
        "filename_verified": isinstance(details.get("file_name"), str) and bool(details["file_name"]),
        "url": downloads._commons_url(str(content_id)),
    }
    await downloads._manifest_update(settings, manifest_key, resolved)
    return resolved


async def _board_source(settings, client, item: dict) -> dict:
    post = await get_board_post(client, settings, item["course_id"], item["post_id"], board_id=item["board_id"])
    if (post.course_id, post.board_id, post.id) != (item["course_id"], item["board_id"], item["post_id"]):
        raise ValueError("source_identifier_mismatch")
    matches = [attachment for attachment in post.attachments if str(attachment.id) == item["attachment_id"]]
    if len(matches) != 1:
        raise _SourceStop("review_required", "attachment_unavailable_or_ambiguous")
    attachment = matches[0]
    if (attachment.content_type or "").lower().startswith("video/"):
        raise _SourceStop("skipped", "video")
    if attachment.size is not None and attachment.size > downloads.SIZE_CAP:
        raise _SourceStop("skipped", "size_limit")
    return {"source": "board", "filename": attachment.name, "url": attachment.url}


def _filename(value) -> str:
    if not isinstance(value, str) or not value:
        raise _SourceStop("review_required", "original_filename_missing")
    if len(value.encode("utf-8")) > 240:
        raise _SourceStop("review_required", "unsafe_original_filename")
    if (value != value.strip(" .") or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', value)
            or re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.IGNORECASE)):
        raise _SourceStop("review_required", "unsafe_original_filename")
    if Path(value).suffix.lower() in downloads.VIDEO_EXTENSIONS:
        raise _SourceStop("skipped", "video")
    if Path(value).suffix.lower() in {".html", ".htm", ".xhtml"}:
        raise _SourceStop("skipped", "html")
    return value


def _contained(settings, course_id: int, path: Path) -> Path:
    root = Path(settings.materials_dir).resolve()
    boundary = root / str(course_id) / "study_versions"
    resolved = path.resolve()
    # Do not follow course/version directory links, even to another course under
    # the same materials root. Existing personal files must never be repurposed.
    if boundary.resolve() != boundary or resolved != path.absolute() or not resolved.is_relative_to(boundary):
        raise _SourceStop("failed", "unsafe_storage_path")
    return resolved


async def _save_version(settings, item: dict, source: dict) -> dict:
    filename = _filename(source.get("filename"))
    root = Path(settings.materials_dir).resolve() / str(item["course_id"]) / "study_versions"
    root = _contained(settings, item["course_id"], root)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=root) as scratch:
        temporary = _contained(settings, item["course_id"], Path(scratch) / filename)
        status, reason = await downloads._download_url(
            settings, str(source.get("url") or ""), temporary,
            require_attachment=source.get("source") == "commons",
        )
        if status != "saved":
            # The downloader returns fixed public diagnostics, never exception
            # text, request URLs, response bodies, cookies or headers.
            raise _SourceStop(status, reason or "download_failed")
        with temporary.open("rb") as stream:
            probe = stream.read(4096).removeprefix(b"\xef\xbb\xbf").lower()
            if re.search(rb"<!doctype\s+html|<html(?:\s|>)", probe):
                raise _SourceStop("skipped", "html")
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        token = hashlib.sha256(item["key"].encode("utf-8")).hexdigest()[:24]
        destination = _contained(settings, item["course_id"], root / token / digest / filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _contained(settings, item["course_id"], destination)
        try:
            # Atomic no-overwrite publication; a crash cannot expose a partial
            # immutable version. Temporary and destination are on the same disk.
            os.link(temporary, destination)
        except FileExistsError:
            if not destination.is_file():
                raise _SourceStop("failed", "immutable_version_conflict") from None
            with destination.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise _SourceStop("failed", "immutable_version_conflict") from None
        return {
            "status": "saved", "key": item["key"], "filename": filename,
            "path": str(destination), "sha256": digest, "size": temporary.stat().st_size,
        }


async def acquire(settings, item: dict) -> dict:
    """Fetch current bytes even when an older file/version already exists."""
    key = ""
    try:
        key = _key(item)
        if item.get("key") != key or item.get("partition") != f"{item['kind']}:{item['course_id']}":
            raise ValueError("source_identifier_mismatch")
        # Validate normalized IDs before building API paths or filesystem paths.
        item = {**item, "course_id": _positive_id(item["course_id"])}
        if item["kind"] == "material":
            item["material_id"] = _positive_id(item["material_id"])
            lock_key = f"{item['course_id']}:{item['material_id']}"
        else:
            item["board_id"] = _positive_id(item["board_id"])
            item["post_id"] = _positive_id(item["post_id"])
            lock_key = "study-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        async with downloads._locked(downloads._item_lock(settings, lock_key)):
            async with SessionManager(settings).client() as client:
                source = await (_material_source(settings, client, item) if item["kind"] == "material"
                                else _board_source(settings, client, item))
                return await _save_version(settings, item, source)
    except _SourceStop as exc:
        return {"status": exc.status, "key": key, "reason": exc.reason}
    except Exception as exc:
        return {"status": "failed", "key": key, "reason": _error_type(exc)}
