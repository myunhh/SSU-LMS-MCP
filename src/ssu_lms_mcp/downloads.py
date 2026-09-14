"""자료 원본을 안전하게 저장한다. LTI는 기본 허용하며 항목당 한 번만 실행한다."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx
from filelock import AsyncFileLock

from .urls import FILE_HOSTS, valid_url

SIZE_CAP = 80 * 1024 * 1024
VIDEO_TYPES = {"movie", "video", "external_video", "youtube", "zoom", "vimeo"}
ALLOWED_HOSTS = {"lms.ssu.ac.kr", "canvas.ssu.ac.kr", "commons.ssu.ac.kr", "smartid.ssu.ac.kr"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".wmv", ".flv", ".mpeg", ".mpg", ".ts", ".ogv"}


def _safe_name(value: str, fallback: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "").strip(" .")
    return value[:120] or fallback


def _root(settings) -> Path:
    return Path(settings.materials_dir).resolve()


def _under_root(settings, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(_root(settings))
    except ValueError as exc:
        raise ValueError("자료 저장 경로가 materials 디렉터리 밖입니다.") from exc
    return resolved


def _destination(
    settings, course_id: int, module: str, title: str, item_id: int, filename: str
) -> Path:
    extension = Path(filename).suffix.lower() or ".bin"
    path = (
        _root(settings)
        / str(course_id)
        / f"{_safe_name(module, '자료')}_{_safe_name(title, '자료')}_{item_id}{extension}"
    )
    return _under_root(settings, path)


def _relative(settings, path: Path) -> str:
    return _under_root(settings, path).relative_to(_root(settings)).as_posix()


def _valid_ssu_url(url: str) -> bool:
    return valid_url(url, hosts=ALLOWED_HOSTS)


def _item_value(item, name: str, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _manifest_lock(settings) -> AsyncFileLock:
    return AsyncFileLock(
        str(Path(settings.manifest_file).with_suffix(".lock")),
        timeout=60,
    )


def _item_lock(settings, key: str) -> AsyncFileLock:
    return AsyncFileLock(
        str(
            Path(settings.manifest_file).parent
            / f".{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}.download.lock"
        ),
        timeout=60,
    )


@asynccontextmanager
async def _locked(lock: AsyncFileLock):
    # 호출마다 별도 잠금 객체를 사용한다. 같은 객체를 공유하면 재진입 허용 때문에
    # 서로 다른 코루틴도 동시에 잠금을 획득할 수 있다.
    Path(lock.lock_file).parent.mkdir(parents=True, exist_ok=True)
    async with lock:
        yield


def _read_manifest(settings) -> dict:
    path = Path(settings.manifest_file)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("자료 매니페스트가 손상되어 안전하게 중단했습니다.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("자료 매니페스트 형식이 올바르지 않습니다.")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("status"), str) for entry in value.values()):
        raise RuntimeError("자료 매니페스트 항목이 손상되어 안전하게 중단했습니다.")
    return value


def _write_manifest(settings, manifest: dict) -> None:
    path = Path(settings.manifest_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


async def _manifest_update(settings, key: str, entry: dict | None = None) -> dict | None:
    async with _locked(_manifest_lock(settings)):
        manifest = _read_manifest(settings)
        if entry is not None:
            if entry.get("path"):
                entry = {**entry, "storage_root": str(_root(settings))}
            manifest[key] = entry
            _write_manifest(settings, manifest)
        return manifest.get(key)


def _cached_path(settings, entry: dict | None) -> Path | None:
    """다른 저장 폴더의 상대 경로를 현재 폴더의 파일로 오인하지 않는다."""
    if not entry or not entry.get("path"):
        return None
    stored_root = entry.get("storage_root")
    if stored_root is None:
        # 이 필드가 없던 구버전의 파일은 기본 자료 폴더에 저장되었다.
        stored_root = Path(settings.data_dir) / "materials"
    if Path(stored_root).resolve() != _root(settings):
        return None
    return _under_root(settings, _root(settings) / str(entry["path"]))


def _cookies(settings) -> httpx.Cookies:
    raw = json.loads(Path(settings.session_file).read_text(encoding="utf-8"))
    jar = httpx.Cookies()
    for cookie in raw.get("cookies", raw.get("storage_state", {}).get("cookies", [])):
        if cookie.get("name") and cookie.get("value"):
            jar.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", "").lstrip("."),
                path=cookie.get("path", "/"),
            )
    return jar


async def _download_url(settings, url: str, destination: Path, *, require_attachment: bool = False) -> tuple[str, str | None]:
    if not valid_url(url, hosts=FILE_HOSTS):
        return "failed", "허용되지 않은 다운로드 주소입니다."
    destination = _under_root(settings, destination)
    if destination.suffix.lower() in VIDEO_EXTENSIONS:
        return "skipped", "동영상 자료는 저장하지 않습니다."
    total = 0
    try:
        async with httpx.AsyncClient(
            cookies=_cookies(settings), follow_redirects=False, timeout=60
        ) as http:
            for _ in range(5):
                async with http.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location or "\\" in location or any(ord(c) < 32 for c in location):
                            return "failed", "허용되지 않은 리디렉션입니다."
                        url = str(httpx.URL(url).join(location))
                        if not valid_url(url, hosts=FILE_HOSTS):
                            return "failed", "허용되지 않은 리디렉션입니다."
                        continue
                    if response.status_code != 200:
                        return "failed", f"다운로드 응답 오류({response.status_code})"
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    json_expected = destination.suffix.lower() in {".json", ".ipynb"}
                    json_response = content_type in {"application/json", "text/json"} or content_type.endswith("+json")
                    if content_type in {"text/html", "application/xhtml+xml"} or (json_response and not json_expected):
                        return "failed", "HTML 또는 JSON 응답은 자료로 저장하지 않습니다."
                    if content_type.startswith("video/"):
                        return "skipped", "동영상 자료는 저장하지 않습니다."
                    if require_attachment and "attachment" not in response.headers.get("content-disposition", "").lower():
                        return "failed", "원본 파일 첨부 응답을 받지 못했습니다."
                    if int(response.headers.get("content-length", "0") or 0) > SIZE_CAP:
                        return "skipped", "파일 크기 상한을 초과했습니다."
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _under_root(settings, destination.parent)
                    fd, name = tempfile.mkstemp(prefix=f".{destination.stem[:40]}.", suffix=".part", dir=destination.parent)
                    temp = Path(name)
                    try:
                        with os.fdopen(fd, "wb") as output:
                            async for part in response.aiter_bytes(chunk_size=64 * 1024):
                                total += len(part)
                                if total > SIZE_CAP:
                                    return "skipped", "파일 크기 상한을 초과했습니다."
                                output.write(part)
                            output.flush()
                            os.fsync(output.fileno())
                        with temp.open("rb") as probe:
                            head = probe.read(512)
                        if (
                            head[4:8] == b"ftyp" or head.startswith((b"\x1aE\xdf\xa3", b"FLV", b"OggS"))
                            or (head.startswith(b"RIFF") and head[8:12] == b"AVI ")
                        ):
                            return "skipped", "동영상 자료는 저장하지 않습니다."
                        if (destination.suffix.lower() == ".pdf" or content_type == "application/pdf") and not head.startswith(b"%PDF-"):
                            return "failed", "PDF 파일 응답이 아닙니다."
                        normalized = head.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
                        if not head or normalized.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                            return "failed", "빈 파일 또는 HTML/JSON 응답은 자료로 저장하지 않습니다."
                        temp.replace(destination)
                        return "saved", None
                    finally:
                        temp.unlink(missing_ok=True)
            return "failed", "리디렉션 횟수 상한을 초과했습니다."
    except (OSError, ValueError, httpx.HTTPError, httpx.InvalidURL):
        return "failed", "자료 파일을 내려받지 못했습니다."


async def _canvas_source(client, content_id: int) -> dict:
    try:
        data = await client.get(f"/files/{content_id}", use_canvas=True)
    except httpx.HTTPError as first:
        try:
            data = await client.get(f"/files/{content_id}")
        except httpx.HTTPError:
            if isinstance(first, httpx.HTTPStatusError) and first.response.status_code in {401, 403, 419}:
                raise first
            raise
    if not isinstance(data, dict) or not data.get("url"):
        raise ValueError("Canvas 파일 주소를 찾지 못했습니다.")
    return {
        "source": "canvas",
        "url": str(data["url"]),
        "content_id": content_id,
        "filename": str(data.get("filename") or data.get("display_name") or "자료"),
    }


def _commons_url(content_id: str) -> str:
    return "https://commons.ssu.ac.kr/index.php?" + urlencode(
        {
            "module": "xn_media_content2013",
            "act": "dispXn_media_content2013DownloadContent",
            "content_id": content_id,
        }
    )


@asynccontextmanager
async def _browser_context(settings):
    from playwright.async_api import async_playwright

    raw = json.loads(Path(settings.session_file).read_text(encoding="utf-8"))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        try:
            yield await browser.new_context(storage_state=raw.get("storage_state", raw))
        finally:
            await browser.close()


async def _launch_external(settings, url: str) -> dict:
    if not _valid_ssu_url(url):
        return {}
    async with _browser_context(settings) as context:
        page = await context.new_page()
        captured: dict = {}
        received = asyncio.Event()

        async def permit(route):
            await (route.continue_() if _valid_ssu_url(route.request.url) else route.abort())

        async def capture(response):
            if "/attendance_items/" in response.url and _valid_ssu_url(response.url):
                try:
                    captured.update((await response.json()).get("item_content_data") or {})
                    if captured:
                        received.set()
                except Exception:
                    pass

        await page.route("**/*", permit)
        page.on("response", capture)
        try:
            await page.goto(url, wait_until="networkidle", timeout=40_000)
            if not _valid_ssu_url(page.url):
                return {}
            try:
                async with asyncio.timeout(10):
                    await received.wait()
            except TimeoutError:
                pass
            return captured
        finally:
            await page.close()


async def download_material(
    settings, client, course_id: int, material_id: int, *, allow_lti: bool = True
) -> dict:
    from .adapters.materials import list_materials

    base, key = {"course_id": course_id, "item_id": material_id}, f"{course_id}:{material_id}"
    async with _locked(_item_lock(settings, key)):
        item = next(
            (
                m
                for m in await list_materials(client, course_id)
                if int(_item_value(m, "id", -1)) == material_id
            ),
            None,
        )
        if item is None:
            return {**base, "status": "skipped", "reason": "자료 항목을 찾을 수 없습니다."}
        item_type = (_item_value(item, "item_type", "") or "").lower()
        if item_type not in {"file", "externaltool"}:
            return {**base, "status": "skipped", "reason": "File 또는 ExternalTool 자료만 다운로드할 수 있습니다."}
        prior = await _manifest_update(settings, key)
        candidate = _cached_path(settings, prior)
        if candidate is not None and candidate.is_file():
            return {**base, "status": "exists", "path": _relative(settings, candidate)}
        # Canvas의 서명 URL은 만료되므로 다시 받아야 할 때 메타데이터를 재조회한다.
        # Commons는 캐시된 content_id를 사용해 LTI를 재실행하지 않는다.
        resolved = prior if item_type == "externaltool" and prior and prior.get("url") else None
        if resolved and resolved.get("source") == "commons" and resolved.get("content_id"):
            resolved = {**resolved, "url": _commons_url(str(resolved["content_id"]))}
        if not resolved and item_type == "externaltool":
            if prior and prior.get("lti_attempted"):
                return {**base, "status": "skipped", "reason": "이미 LTI 실행을 시도한 항목입니다."}
            if not allow_lti:
                return {
                    **base,
                    "status": "skipped",
                    "reason": "LTI 실행은 명시적 허용이 필요합니다.",
                }
            await _manifest_update(
                settings, key, {"status": "launch_reserved", "lti_attempted": True}
            )
            details = await _launch_external(settings, str(_item_value(item, "url", "")))
            content_id, content_type = (
                details.get("content_id"),
                (details.get("content_type") or "").lower(),
            )
            if not content_id or content_type in VIDEO_TYPES:
                reason = (
                    "동영상 또는 외부 자료입니다."
                    if content_type in VIDEO_TYPES
                    else "LTI에서 파일 정보를 찾지 못했습니다."
                )
                await _manifest_update(
                    settings, key, {"status": "skipped", "lti_attempted": True, "reason": reason}
                )
                return {**base, "status": "skipped", "reason": reason}
            resolved = {
                "status": "resolved",
                "lti_attempted": True,
                "source": "commons",
                "content_id": str(content_id),
                "filename": str(details.get("file_name") or "자료"),
                "url": _commons_url(str(content_id)),
            }
            await _manifest_update(settings, key, resolved)
        if not resolved:
            content_id = _item_value(item, "content_id")
            if not content_id:
                return {
                    **base,
                    "status": "skipped",
                    "reason": "다운로드 가능한 파일 자료가 아닙니다.",
                }
            resolved = await _canvas_source(client, int(content_id))
            resolved["status"] = "resolved"
            await _manifest_update(settings, key, resolved)
        destination = _destination(
            settings,
            course_id,
            str(_item_value(item, "module_name", "자료")),
            str(_item_value(item, "title", "자료")),
            material_id,
            str(resolved.get("filename") or "자료"),
        )
        if destination.exists():
            status, reason = "skipped", "저장 위치에 같은 이름의 파일이 있어 덮어쓰지 않았습니다. 기존 파일을 확인하세요."
        else:
            status, reason = await _download_url(
                settings, str(resolved["url"]), destination,
                require_attachment=resolved.get("source") == "commons",
            )
        result = {**base, "status": status}
        if status == "saved":
            result["path"] = _relative(settings, destination)
        if reason:
            result["reason"] = reason
        resolved.update({"status": status, "path": result.get("path")})
        await _manifest_update(settings, key, resolved)
        return result


async def download_board_attachment(
    settings, client, course_id: int, post_id: int, filename: str | None = None,
    *, attachment_id: str | None = None, board_id: int | None = None,
) -> dict:
    from .board import get_board_post

    base = {"course_id": course_id, "post_id": post_id, "filename": filename}
    if attachment_id is None and filename is None:
        return {**base, "status": "failed", "reason": "첨부파일 ID 또는 파일명이 필요합니다."}
    post = await get_board_post(client, settings, course_id, post_id, board_id=board_id)
    if post is None:
        return {**base, "status": "skipped", "reason": "게시글을 찾을 수 없습니다."}
    if post.id != post_id or post.course_id != course_id or post.board_id is None or (board_id is not None and post.board_id != board_id):
        return {**base, "status": "failed", "reason": "게시글 또는 게시판 식별자가 일치하지 않습니다."}
    matches = [
        item for item in post.attachments
        if (str(item.id) == str(attachment_id) if attachment_id is not None else item.name == filename)
    ]
    if not matches:
        return {**base, "status": "skipped", "reason": "게시글 첨부파일을 찾을 수 없습니다."}
    if len(matches) != 1:
        return {**base, "status": "failed", "reason": "첨부파일 선택이 모호합니다. 고유한 첨부파일 ID를 사용하세요."}
    attachment = matches[0]
    if filename is not None and filename != attachment.name:
        return {**base, "status": "failed", "reason": "첨부파일 ID와 파일명이 일치하지 않습니다."}
    if attachment.id is None or not str(attachment.id):
        return {**base, "status": "failed", "reason": "안정적인 첨부파일 ID가 없습니다."}
    base.update(board_id=post.board_id, attachment_id=str(attachment.id), filename=attachment.name)
    if not attachment.url or not valid_url(attachment.url, hosts=FILE_HOSTS):
        return {**base, "status": "failed", "reason": "허용되지 않은 다운로드 주소입니다."}
    extension = Path(attachment.name).suffix.lower()
    content_type = (attachment.content_type or "").split(";", 1)[0].strip().lower()
    if extension in VIDEO_EXTENSIONS or content_type.startswith("video/"):
        return {**base, "status": "skipped", "reason": "동영상 자료는 저장하지 않습니다."}
    if attachment.size is not None and attachment.size > SIZE_CAP:
        return {**base, "status": "skipped", "reason": "파일 크기 상한을 초과했습니다."}
    # 파일명과 서명 URL이 변경되어도 식별자 기반 경로와 잠금은 동일하다.
    identity = json.dumps([course_id, post.board_id, post_id, str(attachment.id)], separators=(",", ":"))
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    key = f"board:{token}"
    extension = extension if re.fullmatch(r"\.[a-z0-9]{1,12}", extension) else ".bin"
    destination = _under_root(settings, _root(settings) / str(course_id) / f"board_{token}{extension}")
    async with _locked(_item_lock(settings, key)):
        prior = await _manifest_update(settings, key)
        candidate = _cached_path(settings, prior) or destination
        if candidate.is_file():
            result = {**base, "status": "exists", "path": _relative(settings, candidate)}
        else:
            status, reason = await _download_url(settings, attachment.url, destination)
            result = {**base, "status": status}
            if status == "saved":
                result["path"] = _relative(settings, destination)
            if reason:
                result["reason"] = reason
        # 게시판의 만료 가능한 서명 URL은 영속 상태에 기록하지 않는다.
        await _manifest_update(settings, key, result)
        return result
