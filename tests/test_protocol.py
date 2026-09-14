"""실제 MCP 메시지 왕복으로 독립 실행·도구·오류 계약을 검증한다."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from ssu_lms_mcp import server
from ssu_lms_mcp.config import Settings
from ssu_lms_mcp.models import Notice

TOOL_NAMES = {
    "session_status", "refresh_session", "list_courses", "list_assignments", "list_deadlines",
    "list_notices", "list_materials", "list_discussions", "list_board_posts",
    "download_material", "download_board_attachment", "list_files",
    "read_file", "search_files", "storage_status",
}


def _data(result):
    assert not result.isError, result
    if result.structuredContent is not None:
        content = result.structuredContent
    else:
        content = json.loads(result.content[0].text)
    return content["result"] if isinstance(content, dict) and set(content) == {"result"} else content


def _environment(data_dir):
    env = dict(os.environ)
    env["SSU_LMS_DATA_DIR"] = str(data_dir)
    env["PYTHONUTF8"] = "1"
    env.pop("SSU_LMS_USERNAME", None)
    env.pop("SSU_LMS_PASSWORD", None)
    return env


async def _assert_session(client):
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert tools.keys() == TOOL_NAMES
    assert tools["list_notices"].annotations.readOnlyHint is True
    assert tools["download_material"].annotations.readOnlyHint is False
    assert tools["download_material"].inputSchema["properties"]["allow_lti"]["default"] is True
    assert tools["list_board_posts"].annotations.readOnlyHint is False
    assert "attachment_id" in tools["download_board_attachment"].inputSchema["properties"]
    state = _data(await client.call_tool("session_status", {}))
    assert state["authenticated"] is False
    error = await client.call_tool("list_notices", {})
    assert error.isError is True
    assert "login" in error.content[0].text
    assert "/api/lms/login" not in error.content[0].text


async def test_stdio_outside_repo_local_document_roundtrip(tmp_path):
    state = tmp_path / "state"
    folder = state / "materials" / "123"
    folder.mkdir(parents=True)
    (folder / "sample.txt").write_text("운영체제 강의자료: 프로세스 스케줄링", encoding="utf-8")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ssu_lms_mcp", "serve"],
        env=_environment(state),
        cwd=str(tmp_path),
    )
    # JSON-RPC가 아닌 로그가 stdout에 섞이면 SDK 파싱/초기화가 실패한다.
    async with asyncio.timeout(30), stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as client:
            await client.initialize()
            await _assert_session(client)
            files = _data(await client.call_tool("list_files", {"course_id": 123}))
            assert files[0]["path"] == "123/sample.txt"
            doc = _data(await client.call_tool("read_file", {"path": files[0]["path"]}))
            assert "프로세스" in doc["text"]
            search = _data(await client.call_tool("search_files", {"query": "스케줄링"}))
            assert "123/sample.txt" in json.dumps(search, ensure_ascii=False)
            escaped = await client.call_tool("read_file", {"path": "../session_state.json"})
            assert escaped.isError is True


async def test_stdio_custom_storage_existing_documents(tmp_path):
    state = tmp_path / "private-state"
    root = tmp_path / "외부 자료"
    folder = root / "내 노트"
    folder.mkdir(parents=True)
    document = folder / "lecture.txt"
    document.write_text("외부 저장소 운영체제 교안", encoding="utf-8")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ssu_lms_mcp", "serve", "--materials-dir", str(root)],
        env=_environment(state), cwd=str(tmp_path),
    )
    async with asyncio.timeout(30), stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as client:
            await client.initialize()
            storage = _data(await client.call_tool("storage_status", {}))
            assert storage == {"materials_dir": str(root.resolve()), "exists": True}
            files = _data(await client.call_tool("list_files", {}))
            assert len(files) == 1 and files[0]["path"] == "내 노트/lecture.txt"
            doc = _data(await client.call_tool("read_file", {"path": files[0]["absolute_path"]}))
            assert "외부 저장소" in doc["text"]
            found = _data(await client.call_tool("search_files", {"query": "운영체제"}))
            assert "내 노트/lecture.txt" in json.dumps(found, ensure_ascii=False)
    assert not (state / "materials").exists()
    assert not (state / "session_state.json").exists()


async def test_notice_payload_and_validation_over_mcp(tmp_path, monkeypatch):
    class FakeSession:
        def __init__(self, settings):
            pass

        @asynccontextmanager
        async def client(self):
            yield object()

    async def fake_notices(client, course_id):
        assert course_id == 12
        return [Notice(id=7, course_id=12, title="휴강 공지", message_text="다음 주는 휴강입니다.")]

    monkeypatch.setattr(server, "SessionManager", FakeSession)
    monkeypatch.setattr(server.notices, "list_notices", fake_notices)
    mcp = server.create_server(Settings(data_dir=tmp_path))
    async with create_connected_server_and_client_session(mcp) as client:
        result = _data(await client.call_tool("list_notices", {"course_id": 12}))
        assert result[0]["message_text"] == "다음 주는 휴강입니다."
        assert (await client.call_tool("list_notices", {"course_id": -1})).isError is True
        assert (await client.call_tool("download_material", {"course_id": 12})).isError is True


async def test_upstream_error_does_not_leak_signed_url(tmp_path, monkeypatch):
    @asynccontextmanager
    async def fake_client(self):
        request = httpx.Request("GET", "https://canvas.ssu.ac.kr/?token=secret-cookie")
        raise httpx.HTTPStatusError("secret-cookie", request=request, response=httpx.Response(502, request=request))
        yield  # pragma: no cover

    monkeypatch.setattr(server.SessionManager, "client", fake_client)
    async with create_connected_server_and_client_session(server.create_server(Settings(data_dir=tmp_path))) as client:
        error = await client.call_tool("list_courses", {})
        assert error.isError is True
        assert "502" in error.content[0].text
        assert "secret-cookie" not in str(error)


async def test_board_and_automatic_lti_over_mcp(tmp_path, monkeypatch):
    from ssu_lms_mcp.models import BoardPost, NoticeAttachment

    @asynccontextmanager
    async def fake_client(self):
        yield object()

    async def fake_posts(client, settings, course_id):
        return [BoardPost(id=12, course_id=course_id, board_id=2, title="교안",
                          attachments=[NoticeAttachment(id="91", name="slides.pdf")])]

    async def fake_download(settings, client, course_id, post_id, filename, *, attachment_id, board_id):
        assert (course_id, post_id, filename, attachment_id, board_id) == (7, 12, None, "91", 2)
        return {"status": "saved", "path": "7/board/2/12/91.pdf"}

    lti_flags = []

    async def fake_material(settings, client, course_id, material_id, *, allow_lti):
        lti_flags.append(allow_lti)
        return {"status": "saved" if allow_lti else "skipped"}

    monkeypatch.setattr(server.SessionManager, "client", fake_client)
    monkeypatch.setattr(server.board, "list_board_posts", fake_posts)
    monkeypatch.setattr(server.downloads, "download_board_attachment", fake_download)
    monkeypatch.setattr(server.downloads, "download_material", fake_material)
    mcp = server.create_server(Settings(data_dir=tmp_path))
    async with create_connected_server_and_client_session(mcp) as client:
        posts = _data(await client.call_tool("list_board_posts", {"course_id": 7}))
        assert posts[0]["board_id"] == 2 and posts[0]["attachments"][0]["id"] == "91"
        result = _data(await client.call_tool("download_board_attachment", {
            "course_id": 7, "post_id": 12, "board_id": 2, "attachment_id": "91",
        }))
        assert result["status"] == "saved"
        await client.call_tool("download_material", {"course_id": 7, "material_id": 8})
        await client.call_tool("download_material", {"course_id": 7, "material_id": 8, "allow_lti": False})
        assert lti_flags == [True, False]


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
async def test_http_transports_and_host_protection(tmp_path, transport):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "ssu_lms_mcp", "serve", "--transport", transport, "--port", str(port)],
            cwd=tmp_path, env=_environment(tmp_path / "state"),
            stdout=log, stderr=log,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(trust_env=False) as http:
                for _ in range(100):
                    if process.poll() is not None:
                        pytest.fail("HTTP MCP 프로세스가 조기 종료되었습니다.")
                    try:
                        await http.get(base + "/", timeout=0.2)
                        break
                    except (httpx.ConnectError, httpx.TimeoutException):
                        await asyncio.sleep(0.1)
                else:
                    pytest.fail("HTTP MCP 서버 시작 시간 초과")
                endpoint = "/mcp" if transport == "streamable-http" else "/sse"
                if transport == "sse":
                    bad = await http.get(base + endpoint, headers={"Host": "attacker.example", "Accept": "text/event-stream"})
                else:
                    bad = await http.post(base + endpoint, headers={"Host": "attacker.example"}, json={})
                assert bad.status_code == 421
                if transport == "streamable-http":
                    bad = await http.post(base + endpoint, headers={"Origin": "https://attacker.example"}, json={})
                    assert bad.status_code == 403
            connection = streamable_http_client(base + "/mcp") if transport == "streamable-http" else sse_client(base + "/sse")
            async with asyncio.timeout(20), connection as streams:
                async with ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=10)) as client:
                    await client.initialize()
                    await _assert_session(client)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
