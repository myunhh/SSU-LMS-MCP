import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from ssu_lms_mcp import board, downloads
from ssu_lms_mcp import study_sources as sources
from ssu_lms_mcp.config import Settings
from ssu_lms_mcp.models import BoardPost, Course, NoticeAttachment
from ssu_lms_mcp.session import SessionRequiredError


class LMS:
    def __init__(self, item_type="File"):
        self.item_type = item_type
        self.filename = "강의 원본.pdf"
        self.module_failure = False
        self.content_id = 9
        self.item_failure = None
        self.item_identifier = 7
        self.launch_url = "https://canvas.ssu.ac.kr/courses/1/modules/items/7"

    async def get_all_pages(self, path, **kwargs):
        if path.endswith("/modules"):
            return [{"id": 5, "name": "1주차"}]
        if self.module_failure:
            raise RuntimeError("private upstream detail")
        return [{
            "id": 7, "type": self.item_type, "content_id": self.content_id,
            "title": "강의", "html_url": self.launch_url,
        }]

    async def get(self, path, **kwargs):
        if path == "/courses/1/modules/5/items/7":
            if self.item_failure:
                raise self.item_failure
            return {
                "id": self.item_identifier, "module_id": 5, "type": self.item_type,
                "content_id": self.content_id, "html_url": self.launch_url,
            }
        return {"url": f"https://canvas.ssu.ac.kr{path}/download", "filename": self.filename}


def material_item():
    return {"kind": "material", "course_id": 1, "material_id": 7,
            "key": "material:1:7", "partition": "material:1", "module_id": 5,
            "item_type": "file", "content_id": 9,
            "launch_url": "https://canvas.ssu.ac.kr/courses/1/modules/items/7"}


def board_item():
    return {"kind": "board", "course_id": 1, "board_id": 2, "post_id": 7,
            "attachment_id": "7", "key": "board:1:2:7:7", "partition": "board:1"}


def board_post(url="https://canvas.ssu.ac.kr/files/7/download", name="첨부.pdf"):
    return BoardPost(id=7, course_id=1, board_id=2, title="교안", board_name="자료실", attachments=[
        NoticeAttachment(id="7", name=name, url=url, size=20, content_type="application/pdf"),
    ])


@pytest.fixture
def environment(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "state", materials_dir=tmp_path / "materials")
    settings.data_dir.mkdir()
    settings.session_file.write_text('{"cookies": []}', encoding="utf-8")
    client = LMS()

    class Session:
        def __init__(self, settings):
            pass

        @asynccontextmanager
        async def client(self):
            yield client

    async def courses(client):
        return [Course(id=1, name="수학", professor="교수")]

    async def posts(*args):
        return [board_post()]

    async def post(*args, **kwargs):
        return board_post()

    monkeypatch.setattr(sources, "SessionManager", Session)
    monkeypatch.setattr(sources, "list_courses", courses)
    monkeypatch.setattr(sources, "list_board_posts", posts)
    monkeypatch.setattr(sources, "get_board_post", post)
    return settings, client


def serve(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(downloads.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


async def test_module_failure_does_not_initialize_partial_partition(environment):
    settings, client = environment
    client.module_failure = True
    result = await sources.discover(settings, [1])
    assert result["successful_partitions"] == ["board:1"]
    assert [item["key"] for item in result["items"]] == ["board:1:2:7:7"]
    assert result["errors"] == [{"partition": "material:1", "error_type": "source_error"}]
    assert "private upstream detail" not in json.dumps(result)


async def test_discovery_keys_and_signatures_ignore_download_locations(environment, monkeypatch):
    settings, _ = environment
    first = await sources.discover(settings, [1])

    async def moved(*args):
        return [board_post("https://commons.ssu.ac.kr/changed-download-location")]

    monkeypatch.setattr(sources, "list_board_posts", moved)
    second = await sources.discover(settings, [1])
    assert {row["key"] for row in first["items"]} == {"material:1:7", "board:1:2:7:7"}
    assert first["items"] == second["items"]
    assert "changed-download-location" not in json.dumps(second)
    assert all("?" not in row["source_url"] for row in first["items"])


async def test_session_failure_reports_each_selected_partition(environment, monkeypatch):
    settings, _ = environment

    class Expired:
        def __init__(self, settings):
            pass

        @asynccontextmanager
        async def client(self):
            raise SessionRequiredError()
            yield  # pragma: no cover

    monkeypatch.setattr(sources, "SessionManager", Expired)
    result = await sources.discover(settings, [1, 2])
    assert result["successful_partitions"] == []
    assert {row["partition"] for row in result["errors"]} == {
        "material:1", "board:1", "material:2", "board:2",
    }
    assert all(row["error_type"] == "session_required" for row in result["errors"])


async def test_same_material_id_new_bytes_preserves_both_original_versions(environment, monkeypatch):
    settings, _ = environment
    body = b"%PDF-1.7\nfirst revision"
    requests = []

    def response(request):
        requests.append(request.url.path)
        return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})

    serve(monkeypatch, response)
    first = await sources.acquire(settings, material_item())
    assert first["status"] == "saved"
    body = b"%PDF-1.7\nsecond revision"
    second = await sources.acquire(settings, material_item())
    again = await sources.acquire(settings, material_item())
    assert first["sha256"] != second["sha256"]
    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == b"%PDF-1.7\nfirst revision"
    assert Path(second["path"]).read_bytes() == body
    assert second == again
    assert len(requests) == 3
    assert Path(second["path"]).name == "강의 원본.pdf"
    assert Path(second["path"]).is_relative_to(settings.materials_dir / "1" / "study_versions")
    assert second["sha256"] == hashlib.sha256(body).hexdigest()


async def test_cached_externaltool_refetches_without_lti_or_manifest_damage(environment, monkeypatch):
    settings, client = environment
    client.item_type = "ExternalTool"
    cached = settings.materials_dir / "1" / "old.pdf"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"old cached bytes")
    manifest = {
        "1:7": {"status": "saved", "source": "commons", "content_id": "known-content",
                "filename": "원래 이름.pdf", "lti_attempted": True, "path": "1/old.pdf",
                "storage_root": str(settings.materials_dir)},
        "other": {"status": "saved", "path": "personal.pdf"},
    }
    settings.manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    original_manifest = settings.manifest_file.read_bytes()

    async def forbidden(*args):
        pytest.fail("A known source must never relaunch LTI")

    monkeypatch.setattr(downloads, "_launch_external", forbidden)
    serve(monkeypatch, lambda request: httpx.Response(200, content=b"%PDF-1.7\nnew original", headers={
        "content-type": "application/pdf", "content-disposition": "attachment",
    }))
    result = await sources.acquire(settings, material_item())
    assert result["status"] == "saved"
    assert result["filename"] == "원래 이름.pdf"
    assert Path(result["path"]).read_bytes() == b"%PDF-1.7\nnew original"
    assert cached.read_bytes() == b"old cached bytes"
    assert settings.manifest_file.read_bytes() == original_manifest


async def test_cancelled_lti_reservation_is_not_replayed(environment, monkeypatch):
    settings, client = environment
    client.item_type = "ExternalTool"
    launches = 0

    async def cancelled(*args):
        nonlocal launches
        launches += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(downloads, "_launch_external", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await sources.acquire(settings, material_item())
    result = await sources.acquire(settings, material_item())
    assert result == {"status": "review_required", "key": "material:1:7",
                      "reason": "resolved_file_source_unavailable"}
    assert launches == 1


async def test_board_replacement_with_same_attachment_id_keeps_old_bytes(environment, monkeypatch):
    settings, _ = environment
    body = b"%PDF-1.7\nboard first"
    serve(monkeypatch, lambda request: httpx.Response(200, content=body))
    first = await sources.acquire(settings, board_item())
    body = b"%PDF-1.7\nboard changed"
    second = await sources.acquire(settings, board_item())
    assert first["status"] == second["status"] == "saved"
    assert first["sha256"] != second["sha256"]
    assert Path(first["path"]).read_bytes() == b"%PDF-1.7\nboard first"
    assert Path(second["path"]).read_bytes() == body


@pytest.mark.parametrize("filename", ["../outside.pdf", "sub/file.pdf", "C:\\outside.pdf", "CON.pdf", "name.pdf."])
async def test_unsafe_original_names_are_not_silently_rewritten(environment, monkeypatch, filename):
    settings, client = environment
    client.filename = filename
    serve(monkeypatch, lambda request: pytest.fail("Unsafe filenames must fail before download"))
    result = await sources.acquire(settings, material_item())
    assert result["status"] == "review_required"
    assert result["reason"] == "unsafe_original_filename"
    assert "path" not in result


@pytest.mark.parametrize("headers,body", [
    ({"content-type": "text/html"}, b"<html>login</html>"),
    ({"content-type": "application/octet-stream"}, b"<html>login</html>"),
    ({"content-type": "video/mp4"}, b"video payload"),
    ({"content-type": "application/octet-stream"}, b"\x00\x00\x00\x18ftypmp42"),
    ({"content-length": str(downloads.SIZE_CAP + 1)}, b"large"),
])
async def test_non_documents_and_declared_oversize_never_publish(environment, monkeypatch, headers, body):
    settings, client = environment
    client.filename = "payload.bin"
    serve(monkeypatch, lambda request: httpx.Response(200, headers=headers, content=body))
    result = await sources.acquire(settings, material_item())
    assert result["status"] in {"failed", "skipped"}
    assert "path" not in result
    assert not list(settings.materials_dir.rglob("payload.bin"))


async def test_undeclared_oversize_stream_is_bounded_and_removed(environment, monkeypatch):
    settings, client = environment
    client.filename = "payload.bin"
    monkeypatch.setattr(downloads, "SIZE_CAP", 10)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 11

    serve(monkeypatch, lambda request: httpx.Response(200, stream=Stream()))
    result = await sources.acquire(settings, material_item())
    assert result["status"] == "skipped"
    assert not list(settings.materials_dir.rglob("*.part"))
    assert not list(settings.materials_dir.rglob("payload.bin"))


async def test_external_redirect_is_not_followed_and_error_is_redacted(environment, monkeypatch):
    settings, _ = environment
    hosts = []

    def response(request):
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://untrusted.example/private-path"})

    serve(monkeypatch, response)
    result = await sources.acquire(settings, material_item())
    assert result["status"] == "failed"
    assert hosts == ["canvas.ssu.ac.kr"]
    assert "untrusted.example" not in json.dumps(result)
    assert "private-path" not in json.dumps(result)


async def test_network_exception_does_not_expose_request_details(environment, monkeypatch):
    settings, _ = environment

    async def broken(*args):
        raise httpx.ConnectError("private response and request details")

    monkeypatch.setattr(downloads, "_canvas_source", broken)
    result = await sources.acquire(settings, material_item())
    assert result == {"status": "failed", "key": "material:1:7", "reason": "network_error"}


async def test_corrupted_existing_version_is_never_overwritten(environment, monkeypatch):
    settings, _ = environment
    serve(monkeypatch, lambda request: httpx.Response(200, content=b"%PDF-1.7\noriginal"))
    first = await sources.acquire(settings, material_item())
    path = Path(first["path"])
    path.write_bytes(b"personal edit")
    result = await sources.acquire(settings, material_item())
    assert result["status"] == "failed"
    assert result["reason"] == "immutable_version_conflict"
    assert path.read_bytes() == b"personal edit"


async def test_descriptor_key_mismatch_never_fetches(environment, monkeypatch):
    settings, _ = environment
    serve(monkeypatch, lambda request: pytest.fail("Mismatched identifiers must fail before download"))
    result = await sources.acquire(settings, {**material_item(), "key": "board:1:2:7:7"})
    assert result == {"status": "failed", "key": "material:1:7", "reason": "invalid_source_metadata"}


async def test_course_directory_link_cannot_redirect_a_version(environment, monkeypatch, tmp_path):
    settings, _ = environment
    other = tmp_path / "other-course"
    other.mkdir()
    settings.materials_dir.mkdir()
    try:
        (settings.materials_dir / "1").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory links is not permitted on this platform")
    serve(monkeypatch, lambda request: pytest.fail("Directory links must fail before download"))
    result = await sources.acquire(settings, material_item())
    assert result["status"] == "failed"
    assert result["reason"] == "unsafe_storage_path"
    assert list(other.iterdir()) == []


async def test_initial_external_resolution_is_reused_for_revisions(environment, monkeypatch):
    settings, client = environment
    client.item_type = "ExternalTool"
    launches = 0
    body = b"%PDF-1.7\ninitial"

    async def launch(*args):
        nonlocal launches
        launches += 1
        return {"content_id": "resolved-original", "content_type": "file", "file_name": "lecture.pdf"}

    monkeypatch.setattr(downloads, "_launch_external", launch)
    serve(monkeypatch, lambda request: httpx.Response(200, content=body, headers={
        "content-type": "application/pdf", "content-disposition": "attachment",
    }))
    first = await sources.acquire(settings, material_item())
    body = b"%PDF-1.7\nreplaced"
    second = await sources.acquire(settings, material_item())
    assert first["status"] == second["status"] == "saved"
    assert first["sha256"] != second["sha256"]
    assert Path(first["path"]).read_bytes() == b"%PDF-1.7\ninitial"
    assert launches == 1


async def test_attachment_from_another_course_is_rejected(environment, monkeypatch):
    settings, _ = environment

    async def wrong_course(*args, **kwargs):
        return board_post().model_copy(update={"course_id": 2})

    monkeypatch.setattr(sources, "get_board_post", wrong_course)
    serve(monkeypatch, lambda request: pytest.fail("Course mismatch must fail before download"))
    result = await sources.acquire(settings, board_item())
    assert result["status"] == "failed"
    assert result["reason"] == "invalid_source_metadata"


async def test_direct_item_refresh_downloads_replacement_without_rescanning_modules(environment, monkeypatch):
    settings, client = environment
    discovered = await sources.discover(settings, [1])
    item = next(row for row in discovered["items"] if row["kind"] == "material")
    # The descriptor still names the old content. A failed whole-course listing
    # must not prevent refreshing this known item's replacement content.
    client.module_failure = True
    client.content_id = 10
    bodies = {
        "/files/9/download": b"%PDF-1.7\nstale original",
        "/files/10/download": b"%PDF-1.7\nreplacement original",
    }
    serve(monkeypatch, lambda request: httpx.Response(200, content=bodies[request.url.path]))
    result = await sources.acquire(settings, item)
    assert result["status"] == "saved"
    assert Path(result["path"]).read_bytes() == bodies["/files/10/download"]


@pytest.mark.parametrize("failure", [SessionRequiredError(), httpx.ConnectError("private upstream detail")])
async def test_failed_item_refresh_does_not_use_cached_source(environment, monkeypatch, failure):
    settings, client = environment
    client.item_type = "ExternalTool"
    client.item_failure = failure
    settings.manifest_file.write_text(json.dumps({
        "1:7": {"status": "saved", "source": "commons", "content_id": "cached-original",
                "filename": "original.pdf", "lti_attempted": True},
    }), encoding="utf-8")
    original = settings.manifest_file.read_bytes()
    serve(monkeypatch, lambda request: pytest.fail("Failed item refresh must not download stale source"))
    result = await sources.acquire(settings, material_item())
    assert result["status"] == "failed"
    assert result["reason"] in {"session_required", "network_error"}
    assert settings.manifest_file.read_bytes() == original


async def test_direct_item_identity_mismatch_prevents_file_acquisition(environment, monkeypatch):
    settings, client = environment
    client.item_identifier = 8
    serve(monkeypatch, lambda request: pytest.fail("A different module item must not be downloaded"))
    result = await sources.acquire(settings, material_item())
    assert result["status"] == "failed"
    assert result["reason"] == "invalid_source_metadata"


async def test_discovery_retains_only_stable_same_course_launch_pages(environment):
    settings, client = environment
    client.item_type = "ExternalTool"
    first = await sources.discover(settings, [1])
    client.launch_url = "https://canvas.ssu.ac.kr/courses/1/external_tools/retrieve?tracking=changed"
    second = await sources.discover(settings, [1])
    client.launch_url = "https://untrusted.example/external"
    third = await sources.discover(settings, [1])
    expected = next(row for row in first["items"] if row["kind"] == "material")
    assert expected["module_id"] == 5
    assert expected["item_type"] == "externaltool"
    assert expected["content_id"] == 9
    assert expected["launch_url"] == expected["source_url"]
    assert first["items"] == second["items"] == third["items"]


@pytest.mark.parametrize("kind", ["canvas", "lti", "board"])
@pytest.mark.parametrize("filename", [None, "", 42])
async def test_missing_original_metadata_stops_before_download(environment, monkeypatch, kind, filename):
    settings, client = environment
    item = material_item()
    launches = 0
    if kind == "canvas":
        original_get = client.get

        async def get(path, **kwargs):
            result = await original_get(path, **kwargs)
            if path.startswith("/files/"):
                result.update(filename=filename, display_name="Display label.pdf")
            return result

        monkeypatch.setattr(client, "get", get)
    elif kind == "lti":
        client.item_type = "ExternalTool"

        async def launch(*args):
            nonlocal launches
            launches += 1
            return {"content_id": "known-file", "content_type": "file", "file_name": filename}

        monkeypatch.setattr(downloads, "_launch_external", launch)
    else:
        item = board_item()

        async def post(*args, **kwargs):
            return board._post({
                "id": 7, "attachments": [{
                    "id": 7, "filename": filename, "display_name": "Attachment label.pdf",
                    "url": "https://canvas.ssu.ac.kr/files/7/download",
                }],
            }, 1, {"id": 2})

        monkeypatch.setattr(sources, "get_board_post", post)
    serve(monkeypatch, lambda request: pytest.fail("Unknown original name must stop before download"))
    for _ in range(2):
        result = await sources.acquire(settings, item)
        assert result == {
            "status": "review_required", "key": item["key"], "reason": "original_filename_missing",
        }
    assert not settings.materials_dir.exists()
    if kind == "lti":
        assert launches == 1


@pytest.mark.parametrize("kind,filename", [("canvas", "자료"), ("lti", "자료"), ("board", "첨부파일")])
async def test_legitimate_original_names_are_not_rejected_as_display_labels(
    environment, monkeypatch, kind, filename,
):
    settings, client = environment
    item = material_item()
    client.filename = filename
    if kind == "lti":
        client.item_type = "ExternalTool"

        async def launch(*args):
            return {"content_id": "known-file", "content_type": "file", "file_name": filename}

        monkeypatch.setattr(downloads, "_launch_external", launch)
    elif kind == "board":
        item = board_item()

        async def post(*args, **kwargs):
            return board._post({
                "id": 7, "attachments": [{
                    "id": 7, "filename": filename, "url": "https://canvas.ssu.ac.kr/files/7/download",
                }],
            }, 1, {"id": 2})

        monkeypatch.setattr(sources, "get_board_post", post)
    body = b"original document bytes"
    serve(monkeypatch, lambda request: httpx.Response(
        200, content=body, headers={"content-disposition": "attachment"},
    ))
    for _ in range(2):
        result = await sources.acquire(settings, item)
        assert result["status"] == "saved"
        assert result["filename"] == Path(result["path"]).name == filename
        assert Path(result["path"]).read_bytes() == body


@pytest.mark.parametrize("filename,provenance", [
    ("자료", {}), ("original.pdf", {"filename_verified": False}),
])
async def test_unverified_commons_names_stop_without_relaunch(environment, monkeypatch, filename, provenance):
    settings, client = environment
    client.item_type = "ExternalTool"
    await downloads._manifest_update(settings, "1:7", {
        "status": "resolved", "source": "commons", "content_id": "legacy",
        "filename": filename, "lti_attempted": True, **provenance,
    })
    original_manifest = settings.manifest_file.read_bytes()

    async def forbidden(*args):
        pytest.fail("Legacy filename ambiguity must not relaunch the viewer")

    monkeypatch.setattr(downloads, "_launch_external", forbidden)
    serve(monkeypatch, lambda request: pytest.fail("Unverified filename must stop before download"))
    result = await sources.acquire(settings, material_item())
    assert result == {
        "status": "review_required", "key": "material:1:7", "reason": "original_filename_unverified",
    }
    assert settings.manifest_file.read_bytes() == original_manifest


async def test_manual_video_skip_remains_video_during_study_acquisition(environment, monkeypatch):
    settings, client = environment
    client.item_type = "ExternalTool"
    launches = 0

    async def listed(*args):
        return [{"id": 7, "item_type": "ExternalTool", "url": client.launch_url}]

    async def launch(*args):
        nonlocal launches
        launches += 1
        return {"content_id": "video-source", "content_type": " MP4 "}

    monkeypatch.setattr("ssu_lms_mcp.adapters.materials.list_materials", listed)
    monkeypatch.setattr(downloads, "_launch_external", launch)
    serve(monkeypatch, lambda request: pytest.fail("Skipped video must never download"))
    manual = await downloads.download_material(settings, client, 1, 7)
    assert manual["status"] == "skipped"
    for _ in range(2):
        assert await sources.acquire(settings, material_item()) == {
            "status": "skipped", "key": "material:1:7", "reason": "video",
        }
    assert launches == 1
    assert not settings.materials_dir.exists()


@pytest.mark.parametrize("reason,expected", [
    ("동영상 또는 외부 자료입니다.", {"status": "skipped", "reason": "video"}),
    ("LTI에서 파일 정보를 찾지 못했습니다.",
     {"status": "review_required", "reason": "resolved_file_source_unavailable"}),
])
async def test_legacy_skip_reason_does_not_relaunch_or_guess_media_type(
    environment, monkeypatch, reason, expected,
):
    settings, client = environment
    client.item_type = "ExternalTool"
    await downloads._manifest_update(settings, "1:7", {
        "status": "skipped", "lti_attempted": True, "reason": reason,
    })

    async def forbidden(*args):
        pytest.fail("A prior skip must not launch the viewer again")

    monkeypatch.setattr(downloads, "_launch_external", forbidden)
    serve(monkeypatch, lambda request: pytest.fail("A prior skip must not download"))
    assert await sources.acquire(settings, material_item()) == {"key": "material:1:7", **expected}
