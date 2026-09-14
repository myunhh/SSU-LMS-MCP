import asyncio
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ssu_lms_mcp import downloads
from ssu_lms_mcp.config import Settings as RealSettings


@dataclass
class Settings:
    data_dir: Path

    @property
    def materials_dir(self):
        return self.data_dir / "materials"

    @property
    def manifest_file(self):
        return self.data_dir / "material_manifest.json"


@dataclass
class Material:
    id: int
    item_type: str
    content_id: int | None = None
    title: str = "강의자료"
    module_name: str = "1주차"
    url: str | None = None


@pytest.fixture
def material_module(monkeypatch):
    package = types.ModuleType("ssu_lms_mcp.adapters")
    module = types.ModuleType("ssu_lms_mcp.adapters.materials")
    module.list_materials = lambda client, course_id: None
    monkeypatch.setitem(sys.modules, "ssu_lms_mcp.adapters", package)
    monkeypatch.setitem(sys.modules, "ssu_lms_mcp.adapters.materials", module)
    return module


async def test_file_download_is_local_unique_and_relative(tmp_path, monkeypatch, material_module):
    settings = Settings(tmp_path)

    async def listed(client, course_id):
        return [Material(10, "File", content_id=99, title="a/b")]

    async def canvas_source(client, content_id):
        assert content_id == 99
        return {
            "source": "canvas",
            "url": "https://canvas.ssu.ac.kr/file.pdf",
            "content_id": 99,
            "filename": "lecture.pdf",
        }

    async def saved(settings, url, destination, **kwargs):
        assert url == "https://canvas.ssu.ac.kr/file.pdf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"data")
        return "saved", None

    material_module.list_materials = listed
    monkeypatch.setattr(downloads, "_canvas_source", canvas_source)
    monkeypatch.setattr(downloads, "_download_url", saved)

    result = await downloads.download_material(settings, object(), 123, 10)
    assert result == {
        "course_id": 123,
        "item_id": 10,
        "status": "saved",
        "path": "123/1주차_a_b_10.pdf",
    }
    again = await downloads.download_material(settings, object(), 123, 10)
    assert again["status"] == "exists"


async def test_external_lti_defaults_on_with_opt_out_and_reservation_prevents_retry(
    tmp_path, monkeypatch, material_module
):
    settings = Settings(tmp_path)

    async def listed(client, course_id):
        return [
            Material(
                20,
                "ExternalTool",
                content_id=123,
                url="https://canvas.ssu.ac.kr/courses/1/external",
            )
        ]

    material_module.list_materials = listed
    launches = 0

    async def cancelled(settings, url):
        nonlocal launches
        launches += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(downloads, "_launch_external", cancelled)

    skipped = await downloads.download_material(settings, object(), 1, 20, allow_lti=False)
    assert skipped["status"] == "skipped" and launches == 0
    with pytest.raises(asyncio.CancelledError):
        await downloads.download_material(settings, object(), 1, 20)
    retry = await downloads.download_material(settings, object(), 1, 20)
    assert retry["status"] == "skipped" and launches == 1


async def test_lti_commons_source_is_cached_for_retry(tmp_path, monkeypatch, material_module):
    settings = Settings(tmp_path)

    async def listed(client, course_id):
        return [Material(30, "ExternalTool", content_id=123, url="https://canvas.ssu.ac.kr/tool")]

    async def launched(settings, url):
        return {"content_id": "opaque-id", "content_type": "file", "file_name": "handout.pdf"}

    calls = []

    async def download(settings, url, destination, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return "failed", "offline"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"pdf")
        return "saved", None

    material_module.list_materials = listed
    monkeypatch.setattr(downloads, "_launch_external", launched)
    monkeypatch.setattr(downloads, "_download_url", download)
    assert (await downloads.download_material(settings, object(), 1, 30, allow_lti=False))["status"] == "skipped"
    assert (await downloads.download_material(settings, object(), 1, 30))[
        "status"
    ] == "failed"
    assert (await downloads.download_material(settings, object(), 1, 30, allow_lti=False))["status"] == "saved"
    assert len(calls) == 2 and "content_id=opaque-id" in calls[0]


async def test_corrupt_manifest_fails_closed(tmp_path, material_module):
    settings = Settings(tmp_path)
    settings.data_dir.mkdir(exist_ok=True)
    settings.manifest_file.write_text("{bad", encoding="utf-8")

    async def listed(client, course_id):
        return [Material(1, "File", content_id=1)]

    material_module.list_materials = listed
    with pytest.raises(RuntimeError):
        await downloads.download_material(settings, object(), 1, 1)


def test_ssu_url_allowlist():
    assert downloads._valid_ssu_url("https://canvas.ssu.ac.kr/files/1")
    assert downloads._valid_ssu_url("https://smartid.ssu.ac.kr/login")
    assert not downloads._valid_ssu_url("https://evil.example/files/1")
    assert not downloads._valid_ssu_url("http://canvas.ssu.ac.kr/files/1")


async def test_concurrent_same_item_launches_once(tmp_path, monkeypatch, material_module):
    settings = Settings(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    launches = 0

    async def listed(*args):
        return [Material(9, "ExternalTool", url="https://canvas.ssu.ac.kr/tool")]

    async def launch(*args):
        nonlocal launches
        launches += 1
        entered.set()
        await release.wait()
        return {"content_id": "opaque-source", "content_type": "file", "file_name": "document.pdf"}

    async def save(settings, url, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mock-pdf")
        return "saved", None

    material_module.list_materials = listed
    monkeypatch.setattr(downloads, "_launch_external", launch)
    monkeypatch.setattr(downloads, "_download_url", save)
    first = asyncio.create_task(downloads.download_material(settings, object(), 1, 9))
    await asyncio.wait_for(entered.wait(), timeout=3)
    second = asyncio.create_task(downloads.download_material(settings, object(), 1, 9))
    try:
        await asyncio.sleep(0.12)
        assert launches == 1 and not second.done()
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert [result["status"] for result in results] == ["saved", "exists"]
    assert launches == 1


@pytest.mark.parametrize("item_type", ["Page", "Assignment", "Discussion", "Quiz", "ExternalUrl"])
async def test_non_file_items_never_use_canvas_file_metadata(tmp_path, material_module, item_type):
    async def listed(*args):
        return [Material(1, item_type, content_id=99)]

    material_module.list_materials = listed
    result = await downloads.download_material(Settings(tmp_path), object(), 1, 1)
    assert result["status"] == "skipped"


async def test_canvas_metadata_refreshes_after_failed_download(tmp_path, monkeypatch, material_module):
    settings = Settings(tmp_path)
    calls, urls = [], []

    async def listed(*args):
        return [Material(1, "File", content_id=99)]

    async def get(path, params=None, use_canvas=False):
        assert path == "/files/99" and use_canvas is True
        calls.append(path)
        return {"url": f"https://canvas.ssu.ac.kr/files/99/download?version={len(calls)}", "filename": "lecture.pptx"}

    async def download(settings, url, destination, **kwargs):
        urls.append(url)
        assert destination.suffix == ".pptx"
        return "failed", "테스트 연결 실패"

    material_module.list_materials = listed
    monkeypatch.setattr(downloads, "_download_url", download)
    client = SimpleNamespace(get=get)
    for _ in range(2):
        await downloads.download_material(settings, client, 1, 1)
    assert len(calls) == 2 and urls[0] != urls[1]


async def test_canvas_file_metadata_falls_back_after_401():
    calls = []

    async def get(path, params=None, use_canvas=False):
        calls.append(use_canvas)
        if use_canvas:
            request = httpx.Request("GET", "https://canvas.ssu.ac.kr/api/v1/files/9")
            raise httpx.HTTPStatusError("만료", request=request, response=httpx.Response(401, request=request))
        return {"url": "https://lms.ssu.ac.kr/files/9", "filename": "lecture.pdf"}

    source = await downloads._canvas_source(SimpleNamespace(get=get), 9)
    assert source["filename"] == "lecture.pdf" and calls == [True, False]


async def test_concurrent_manifest_updates_keep_all_entries(tmp_path):
    settings = Settings(tmp_path)
    await asyncio.gather(*(downloads._manifest_update(settings, str(index), {"status": "resolved"}) for index in range(12)))
    assert len(json.loads(settings.manifest_file.read_text("utf-8"))) == 12
    assert not list(tmp_path.glob("*.tmp"))


def _mock_http(monkeypatch, handler):
    factory = httpx.AsyncClient
    monkeypatch.setattr(downloads.httpx, "AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))


def _download_settings(tmp_path):
    settings = RealSettings(tmp_path)
    settings.session_file.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    return settings


async def test_commons_http_requires_attachment_and_never_sends_bearer(tmp_path, monkeypatch):
    settings = _download_settings(tmp_path)
    destination = settings.materials_dir / "1" / "lecture.pdf"
    visited = []

    def handle(request):
        visited.append(request)
        assert request.url.host == "commons.ssu.ac.kr"
        assert request.url.params["content_id"] == "opaque/id"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"embedded": True})

    _mock_http(monkeypatch, handle)
    status, _ = await downloads._download_url(settings, downloads._commons_url("opaque/id"), destination, require_attachment=True)
    assert status == "failed" and not destination.exists() and len(visited) == 1


@pytest.mark.parametrize("kind", ["big_header", "big_stream", "video", "html", "good"])
async def test_download_response_boundaries(tmp_path, monkeypatch, kind):
    settings = _download_settings(tmp_path)
    destination = settings.materials_dir / "1" / "file.pdf"
    monkeypatch.setattr(downloads, "SIZE_CAP", 16)

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 10
            yield b"b" * 10

    def handle(request):
        if kind == "big_header":
            return httpx.Response(200, headers={"content-length": "100"}, content=b"x")
        if kind == "big_stream":
            return httpx.Response(200, stream=Chunks())
        if kind == "video":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp4")
        if kind == "html":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"login")
        return httpx.Response(200, content=b"%PDF-1.7\n")

    _mock_http(monkeypatch, handle)
    status, _ = await downloads._download_url(settings, "https://canvas.ssu.ac.kr/files/9/download", destination)
    assert (status == "saved") is (kind == "good")
    assert destination.exists() is (kind == "good")
    assert not list(settings.materials_dir.rglob("*.part"))


async def test_download_redirect_never_leaks_cookies_to_other_host(tmp_path, monkeypatch):
    settings = _download_settings(tmp_path)
    visits = []

    def handle(request):
        visits.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://attacker.example/file"})

    _mock_http(monkeypatch, handle)
    status, _ = await downloads._download_url(settings, "https://canvas.ssu.ac.kr/files/9", settings.materials_dir / "1" / "x.pdf")
    assert status == "failed" and visits == ["canvas.ssu.ac.kr"]


def test_download_destination_cannot_escape_via_symlink(tmp_path):
    settings = Settings(tmp_path / "state")
    settings.materials_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (settings.materials_dir / "1").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("이 OS에서는 심볼릭 링크 생성 권한이 없습니다.")
    with pytest.raises(ValueError, match="밖"):
        downloads._destination(settings, 1, "week", "title", 2, "x.pdf")


async def test_switch_materials_root_keeps_lti_history_without_old_path_hit(tmp_path, monkeypatch, material_module):
    state, old_root, new_root = tmp_path / "state", tmp_path / "old", tmp_path / "external"
    old = RealSettings(data_dir=state, materials_dir=old_root)
    new = RealSettings(data_dir=state, materials_dir=new_root)
    for root in [old_root, new_root]:
        (root / "1").mkdir(parents=True)
        (root / "1/cached.pdf").write_bytes(b"old or unrelated file")
    await downloads._manifest_update(old, "1:9", {
        "status": "saved", "path": "1/cached.pdf", "lti_attempted": True,
        "source": "commons", "content_id": "known", "url": downloads._commons_url("known"), "filename": "new.pdf",
    })

    async def listed(*args):
        return [Material(9, "ExternalTool", title="교안")]

    async def forbidden(*args):
        raise AssertionError("저장 위치 변경 때문에 LTI를 다시 열면 안 됩니다.")

    async def save(settings, url, destination, **kwargs):
        assert destination.is_relative_to(new_root)
        destination.write_bytes(b"%PDF-1.7 new file")
        return "saved", None

    material_module.list_materials = listed
    monkeypatch.setattr(downloads, "_launch_external", forbidden)
    monkeypatch.setattr(downloads, "_download_url", save)
    result = await downloads.download_material(new, object(), 1, 9)
    assert result["status"] == "saved" and result["path"] != "1/cached.pdf"
    assert (new_root / result["path"]).read_bytes() == b"%PDF-1.7 new file"
    assert (old_root / "1/cached.pdf").read_bytes() == b"old or unrelated file"
    assert (new_root / "1/cached.pdf").read_bytes() == b"old or unrelated file"
    assert (await downloads.download_material(new, object(), 1, 9))["status"] == "exists"


@pytest.mark.parametrize("item_type", ["File", "ExternalTool"])
async def test_switch_root_never_overwrites_exact_destination(
    tmp_path, monkeypatch, material_module, item_type,
):
    old = RealSettings(data_dir=tmp_path / "state", materials_dir=tmp_path / "old")
    new = RealSettings(data_dir=old.data_dir, materials_dir=tmp_path / "external")
    material = Material(9, item_type, content_id=99, title="교안")
    source = {"source": "commons", "url": downloads._commons_url("known"),
              "content_id": "known", "filename": "lecture.pdf", "lti_attempted": True}
    await downloads._manifest_update(old, "1:9", {**source, "status": "saved", "path": "1/old.pdf"})
    destination = downloads._destination(new, 1, material.module_name, material.title, 9, "lecture.pdf")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"my annotated document")

    async def listed(*args):
        return [material]

    async def canvas_source(*args):
        return {**source, "source": "canvas"}

    async def forbidden(*args, **kwargs):
        raise AssertionError("기존 파일을 덮어쓰거나 LTI를 다시 열면 안 됩니다.")

    material_module.list_materials = listed
    monkeypatch.setattr(downloads, "_canvas_source", canvas_source)
    monkeypatch.setattr(downloads, "_launch_external", forbidden)
    monkeypatch.setattr(downloads, "_download_url", forbidden)
    for _ in range(2):
        result = await downloads.download_material(new, object(), 1, 9)
        assert result["status"] == "skipped" and "덮어쓰지" in result["reason"]
        assert destination.read_bytes() == b"my annotated document"
    if item_type == "ExternalTool":
        assert (await downloads._manifest_update(new, "1:9"))["lti_attempted"] is True


def test_legacy_cache_paths_only_apply_to_default_root(tmp_path):
    default = RealSettings(data_dir=tmp_path / "state")
    external = RealSettings(data_dir=default.data_dir, materials_dir=tmp_path / "external")
    entry = {"status": "saved", "path": "1/old.pdf", "lti_attempted": True}
    assert downloads._cached_path(default, entry) == default.materials_dir / "1/old.pdf"
    assert downloads._cached_path(external, entry) is None


@pytest.fixture
def board_module(monkeypatch):
    module = types.ModuleType("ssu_lms_mcp.board")
    calls = []
    post = SimpleNamespace(
        id=7, course_id=1, board_id=3, board_name="자료실", title="Lecture",
        attachments=[],
    )

    async def get_board_post(client, settings, course_id, post_id, *, board_id=None):
        calls.append((client, course_id, post_id, board_id))
        return post

    module.get_board_post = get_board_post
    monkeypatch.setitem(sys.modules, "ssu_lms_mcp.board", module)
    return SimpleNamespace(post=post, calls=calls)


def _attachment(attachment_id="11", name="lecture.pdf", **updates):
    return SimpleNamespace(**{
        "id": attachment_id, "name": name,
        "url": f"https://canvas.ssu.ac.kr/files/{attachment_id}/download?signature=private",
        "size": None, "content_type": "application/pdf", **updates,
    })


async def test_board_attachment_id_selects_inline_pdf_without_rescan(
    tmp_path, monkeypatch, board_module,
):
    settings = _download_settings(tmp_path)
    board_module.post.attachments = [_attachment("11"), _attachment("12")]
    client = object()
    visits = []

    def handle(request):
        visits.append(str(request.url))
        assert request.url.path == "/files/12/download"
        return httpx.Response(200, headers={
            "content-type": "application/pdf", "content-disposition": 'inline; filename="lecture.pdf"',
        }, content=b"%PDF-1.7\ninline file")

    _mock_http(monkeypatch, handle)
    result = await downloads.download_board_attachment(
        settings, client, 1, 7, attachment_id="12", board_id=3,
    )
    assert result["status"] == "saved" and result["attachment_id"] == "12"
    assert board_module.calls == [(client, 1, 7, 3)]
    assert (settings.materials_dir / result["path"]).read_bytes() == b"%PDF-1.7\ninline file"
    board_module.post.attachments[1].url = "https://canvas.ssu.ac.kr/files/12/download?signature=renewed"
    again = await downloads.download_board_attachment(settings, client, 1, 7, attachment_id="12")
    assert again["status"] == "exists" and again["path"] == result["path"]
    assert len(visits) == 1
    manifest_text = settings.manifest_file.read_text("utf-8")
    assert "signature" not in manifest_text and "https://" not in manifest_text


async def test_board_download_switches_external_root_and_document_tools(
    tmp_path, monkeypatch, board_module,
):
    from ssu_lms_mcp import documents

    state = tmp_path / "private-state"
    old = RealSettings(data_dir=state, materials_dir=tmp_path / "first")
    new = RealSettings(data_dir=state, materials_dir=tmp_path / "외부 자료")
    state.mkdir()
    old.session_file.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    board_module.post.attachments = [_attachment("11", "lecture.txt", content_type="text/plain")]
    visits = []

    def handle(request):
        visits.append(request.url.path)
        return httpx.Response(200, headers={"content-type": "text/plain"},
                              content="외부 폴더의 운영체제 교안".encode())

    _mock_http(monkeypatch, handle)
    first = await downloads.download_board_attachment(old, object(), 1, 7, attachment_id="11")
    second = await downloads.download_board_attachment(new, object(), 1, 7, attachment_id="11")
    assert first["status"] == second["status"] == "saved" and len(visits) == 2
    assert (old.materials_dir / first["path"]).is_file()
    files = documents.list_files(new)
    assert len(files) == 1 and files[0]["path"] == second["path"]
    assert "운영체제" in documents.read_file(new, files[0]["absolute_path"])["text"]
    assert documents.search_files(new, "운영체제")["matched"] == 1
    assert (await downloads.download_board_attachment(new, object(), 1, 7, attachment_id="11"))["status"] == "exists"
    assert len(visits) == 2 and not (state / "materials").exists()


@pytest.mark.parametrize("selection,ids", [
    ({"filename": "lecture.pdf"}, ["11", "12"]),
    ({"attachment_id": "11", "filename": "wrong.pdf"}, ["11"]),
    ({"attachment_id": "11"}, ["11", "11"]),
    ({"attachment_id": "missing", "filename": "lecture.pdf"}, ["11"]),
    ({}, ["11"]),
])
async def test_board_ambiguous_contradictory_or_missing_selection_never_downloads(
    tmp_path, board_module, selection, ids,
):
    settings = Settings(tmp_path)
    board_module.post.attachments = [_attachment(value) for value in ids]
    result = await downloads.download_board_attachment(settings, object(), 1, 7, **selection)
    assert result["status"] in {"failed", "skipped"}
    assert not settings.materials_dir.exists() and not settings.manifest_file.exists()


async def test_board_unique_legacy_filename_and_stable_integer_id(
    tmp_path, monkeypatch, board_module,
):
    settings = _download_settings(tmp_path)
    board_module.post.attachments = [_attachment(11)]
    _mock_http(monkeypatch, lambda request: httpx.Response(200, content=b"%PDF-1.7\n"))
    result = await downloads.download_board_attachment(settings, object(), 1, 7, "lecture.pdf")
    assert result["status"] == "saved" and result["attachment_id"] == "11"
    assert board_module.calls[0][3] is None


@pytest.mark.parametrize("filename", ["../CON:lecture.pdf", "x" * 200 + ".pdf"])
async def test_board_paths_isolate_courses_boards_posts_and_same_extension_ids(
    tmp_path, monkeypatch, board_module, filename,
):
    settings = _download_settings(tmp_path)
    _mock_http(monkeypatch, lambda request: httpx.Response(
        200, content=b"%PDF-1.7\n" + request.url.path.encode(),
    ))
    paths = set()
    for course_id, board_id, post_id, attachment_id in [
        (1, 3, 7, "11"), (1, 3, 7, "12"), (1, 4, 7, "11"),
        (1, 3, 8, "11"), (2, 3, 7, "11"),
    ]:
        board_module.post.course_id = course_id
        board_module.post.board_id = board_id
        board_module.post.id = post_id
        board_module.post.attachments = [_attachment(attachment_id, filename)]
        result = await downloads.download_board_attachment(
            settings, object(), course_id, post_id, attachment_id=attachment_id, board_id=board_id,
        )
        assert result["status"] == "saved"
        path = settings.materials_dir / result["path"]
        assert path.suffix == ".pdf" and len(path.name) < 50
        assert path.resolve().is_relative_to(settings.materials_dir.resolve())
        assert path.read_bytes() == b"%PDF-1.7\n/files/" + attachment_id.encode() + b"/download"
        paths.add(path)
    assert len(paths) == 5 and all(path.is_file() for path in paths)
    assert len(json.loads(settings.manifest_file.read_text("utf-8"))) == 5


async def test_board_concurrent_download_reuses_file(tmp_path, monkeypatch, board_module):
    settings = _download_settings(tmp_path)
    board_module.post.attachments = [_attachment()]
    entered, release = asyncio.Event(), asyncio.Event()
    visits = []

    async def handle(request):
        visits.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, content=b"%PDF-1.7\n")

    _mock_http(monkeypatch, handle)
    first = asyncio.create_task(downloads.download_board_attachment(settings, object(), 1, 7, attachment_id="11"))
    await asyncio.wait_for(entered.wait(), timeout=3)
    second = asyncio.create_task(downloads.download_board_attachment(settings, object(), 1, 7, attachment_id="11"))
    try:
        await asyncio.sleep(0.12)
        assert len(visits) == 1 and not second.done()
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert [result["status"] for result in results] == ["saved", "exists"]
    assert results[0]["path"] == results[1]["path"] and len(visits) == 1


@pytest.mark.parametrize("content_type,body", [
    ("application/pdf", b"<!DOCTYPE html><html>login</html>"),
    ("application/pdf", b'{"error":"login required"}'),
    ("application/octet-stream", b"[1, 2]"),
    ("application/json", b"%PDF-1.7\n"),
    ("application/problem+json", b'{"status":403}'),
    ("application/xhtml+xml", b"<html>login</html>"),
    ("application/pdf", b""),
    ("video/mp4", b"%PDF-1.7\n"),
    ("application/octet-stream", b"\x1aE\xdf\xa3webm"),
    ("application/octet-stream", b"RIFFxxxxAVI data"),
])
async def test_board_invalid_bodies_never_persist_files(
    tmp_path, monkeypatch, board_module, content_type, body,
):
    settings = _download_settings(tmp_path)
    board_module.post.attachments = [_attachment()]
    _mock_http(monkeypatch, lambda request: httpx.Response(200, headers={
        "content-type": content_type, "content-disposition": 'inline; filename="lecture.pdf"',
    }, content=body))
    result = await downloads.download_board_attachment(settings, object(), 1, 7, attachment_id="11")
    assert result["status"] in {"failed", "skipped"} and "path" not in result
    assert not list(settings.materials_dir.rglob("*.pdf"))
    assert not list(settings.materials_dir.rglob("*.part"))


@pytest.mark.parametrize("url", [
    "https://attacker.example/files/11", "https://smartid.ssu.ac.kr/files/11",
    "http://canvas.ssu.ac.kr/files/11", "https://canvas.ssu.ac.kr:444/files/11",
    "https://user:pass@canvas.ssu.ac.kr/files/11", "https://canvas.ssu.ac.kr:bad/files/11",
    "https://canvas.ssu.ac.kr/\nfiles/11", "https://canvas.ssu.ac.kr/\\files/11",
    "https://[invalid/files/11", "/files/11",
])
async def test_invalid_file_urls_rejected_before_io(tmp_path, monkeypatch, board_module, url):
    settings = Settings(tmp_path)
    board_module.post.attachments = [_attachment(url=url)]

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid URL must be rejected before reading cookies or opening HTTP")

    monkeypatch.setattr(downloads, "_cookies", forbidden)
    monkeypatch.setattr(downloads.httpx, "AsyncClient", forbidden)
    result = await downloads.download_board_attachment(settings, object(), 1, 7, attachment_id="11")
    assert result["status"] == "failed" and not settings.manifest_file.exists()
    status, _ = await downloads._download_url(settings, url, settings.materials_dir / "file.pdf")
    assert status == "failed" and not settings.materials_dir.exists()


@pytest.mark.parametrize("location", [
    "https://smartid.ssu.ac.kr/files/11", "https://attacker.example/files/11",
    "http://canvas.ssu.ac.kr/files/11", "https://canvas.ssu.ac.kr:bad/files/11",
    "https://user@canvas.ssu.ac.kr/files/11", "https://canvas.ssu.ac.kr/\nfiles/11",
    "", "https://canvas.ssu.ac.kr/\\files/11", "https://[invalid/files/11",
])
async def test_invalid_redirects_are_never_followed(tmp_path, monkeypatch, location):
    settings = _download_settings(tmp_path)
    visits = []

    def handle(request):
        visits.append(request)
        return httpx.Response(302, headers={"location": location})

    _mock_http(monkeypatch, handle)
    destination = settings.materials_dir / "file.pdf"
    status, _ = await downloads._download_url(settings, "https://canvas.ssu.ac.kr/files/11", destination)
    assert status == "failed" and len(visits) == 1 and not destination.exists()


@pytest.mark.parametrize("updates", [
    {"size": downloads.SIZE_CAP + 1}, {"content_type": "video/mp4"}, {"name": "lecture.mp4"},
])
async def test_board_metadata_rejects_oversize_and_video_before_io(tmp_path, board_module, updates):
    settings = Settings(tmp_path)
    board_module.post.attachments = [_attachment(**updates)]
    result = await downloads.download_board_attachment(settings, object(), 1, 7, attachment_id="11")
    assert result["status"] == "skipped" and not settings.manifest_file.exists()


@pytest.mark.parametrize("extension", [".json", ".ipynb"])
async def test_actual_json_materials_are_not_mistaken_for_fake_pdfs(tmp_path, monkeypatch, extension):
    settings = _download_settings(tmp_path)
    body = b'{"cells": [], "nbformat": 4}'
    _mock_http(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "application/json"}, content=body,
    ))
    destination = settings.materials_dir / f"notebook{extension}"
    status, _ = await downloads._download_url(settings, "https://canvas.ssu.ac.kr/files/11", destination)
    assert status == "saved" and destination.read_bytes() == body


async def test_allowed_relative_redirect_downloads_inline_pdf(tmp_path, monkeypatch):
    settings = _download_settings(tmp_path)
    visits = []

    def handle(request):
        visits.append(request.url.path)
        if len(visits) == 1:
            return httpx.Response(302, headers={"location": "/files/11/download?signature=private"})
        return httpx.Response(200, content=b"%PDF-1.7\n")

    _mock_http(monkeypatch, handle)
    destination = settings.materials_dir / "file.pdf"
    status, _ = await downloads._download_url(settings, "https://canvas.ssu.ac.kr/files/11", destination)
    assert status == "saved" and visits == ["/files/11", "/files/11/download"]


@pytest.mark.parametrize("updates", [{"course_id": 2}, {"id": 8}, {"board_id": 4}, {"board_id": None}])
async def test_board_post_identity_mismatch_is_rejected(tmp_path, board_module, updates):
    settings = Settings(tmp_path)
    board_module.post.attachments = [_attachment()]
    for name, value in updates.items():
        setattr(board_module.post, name, value)
    result = await downloads.download_board_attachment(settings, object(), 1, 7, attachment_id="11", board_id=3)
    assert result["status"] == "failed" and not settings.manifest_file.exists()


@pytest.mark.parametrize("details", [{}, {"content_id": "movie-id", "content_type": "video"}])
async def test_failed_or_video_lti_attempt_is_permanently_cached(
    tmp_path, monkeypatch, material_module, details,
):
    settings = Settings(tmp_path)
    launches = []

    async def listed(*args):
        return [Material(1, "ExternalTool", url="https://canvas.ssu.ac.kr/tool")]

    async def launch(*args):
        launches.append(True)
        return details

    material_module.list_materials = listed
    monkeypatch.setattr(downloads, "_launch_external", launch)
    for _ in range(2):
        result = await downloads.download_material(settings, object(), 1, 1)
        assert result["status"] == "skipped"
    assert len(launches) == 1
    assert json.loads(settings.manifest_file.read_text("utf-8"))["1:1"]["lti_attempted"] is True
