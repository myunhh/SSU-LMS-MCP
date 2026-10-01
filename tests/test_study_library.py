"""Real loopback HTTP and filesystem boundaries; no cloud/user data."""
import http.client
import json
import threading
from contextlib import contextmanager
from uuid import uuid4

import pytest

from ssu_lms_mcp.study_artifacts import ArtifactRepository
from ssu_lms_mcp.study_library import LibraryError, install_application, library_status, make_server

HUB = "00000000-0000-4000-8000-000000000001"
COLLECTIONS = {
    "materials": "00000000-0000-4000-8000-000000000002",
    "reviews": "00000000-0000-4000-8000-000000000003",
    "records": "00000000-0000-4000-8000-000000000004",
}


@contextmanager
def running(config):
    server = make_server(config, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "OneDrive 학습 자료"
    repository = ArtifactRepository(root)
    repository.initialize(hub_id=HUB, title="학습 허브", collections=COLLECTIONS)
    repository.create_document({"id": HUB, "title": "학습 허브", "parent_id": None,
                                "kind": "page", "role": None, "content": "내 기존 노트"})
    for role, identity in COLLECTIONS.items():
        repository.create_document({"id": identity, "title": role, "kind": "database",
                                    "role": role, "parent_id": HUB, "content": ""})
    config = {"artifact": {"root": str(root), "port": 8766}, "data_dir": str(tmp_path / "private")}
    with running(config) as server:
        yield server, repository, config


def request(server, method, path, payload=None, headers=None):
    supplied = {"Origin": server.origin, "X-SSU-CSRF": server.csrf}
    supplied.update(headers or {})
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    if body is not None:
        supplied.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        raw = response.read()
        data = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        return response.status, data, dict(response.getheaders())
    finally:
        connection.close()


def test_created_edited_document_survives_server_restart_and_stale_tab(library):
    server, repository, config = library
    status, result, _ = request(server, "POST", "/api/documents", {
        "parent_id": HUB, "title": "나의 증명", "content": "처음 생각", "properties": {"분야": "집합"},
    })
    assert status == 201
    first = result["document"]
    body = "# 내 증명\n\n수정한 수식 $A \\cup B$\n\n<details><summary>근거</summary>내 답</details>"
    status, result, _ = request(server, "PATCH", "/api/documents/" + first["id"], {
        "revision": first["revision"], "title": "수정한 증명", "content": body,
    })
    assert status == 200
    saved = result["document"]
    assert saved["revision"] != first["revision"]
    stale, result, _ = request(server, "PATCH", "/api/documents/" + first["id"], {
        "revision": first["revision"], "content": "다른 탭의 오래된 본문",
    })
    assert stale == 409 and result["error"] == "artifact_revision_conflict"
    with running(config) as reopened:
        status, result, _ = request(reopened, "GET", "/api/documents/" + first["id"])
    assert status == 200
    document = result["document"]
    assert document["title"] == "수정한 증명" and document["content"] == body
    assert document["properties"]["분야"] == "집합"
    assert document["revision"] == saved["revision"]
    assert repository.get_document(HUB)["content"] == "내 기존 노트"


@pytest.mark.parametrize("headers", [
    {"Origin": "https://foreign.example"},
    {"Host": "rebind.example"},
    {"X-SSU-CSRF": "incorrect"},
    {"Content-Type": "text/plain"},
])
def test_cross_origin_and_unauthorized_mutations_do_not_create_documents(library, headers):
    server, repository, _ = library
    before = repository.list_documents()
    status, _, _ = request(server, "POST", "/api/documents", {"title": "unauthorized"}, headers)
    assert status in {400, 403}
    assert repository.list_documents() == before


def test_submission_keeps_exact_answer_and_rejects_stale_resubmission(library):
    server, repository, _ = library
    identity, key = str(uuid4()), "version:quiz-one"
    card = repository.create_document({
        "id": identity, "title": "내가 이해한 원리", "parent_id": COLLECTIONS["reviews"],
        "kind": "page", "role": "reviews", "managed_key": key, "content": "문제와 정답",
        "properties": {"키": key, "질문": "내가 이해한 원리", "제출": "__NO__", "내답": "", "평가": None},
    })
    answer = '  제 생각은 "겹치는 원소"입니다.\n아직 확신하지 못합니다.  '
    status, result, _ = request(server, "POST", f"/api/reviews/{identity}/submit", {
        "revision": card["revision"], "rating": "uncertain", "answer": answer,
    })
    assert status == 200
    saved = result["document"]
    assert saved["properties"]["내답"] == answer
    assert saved["properties"]["평가"] == "애매함"
    assert saved["properties"]["제출"] == "__YES__"
    assert saved["content"] == "문제와 정답"
    assert "시도" not in saved["properties"]  # HTTP submission does not invent a completed review.
    status, _, _ = request(server, "POST", f"/api/reviews/{identity}/submit", {
        "revision": card["revision"], "rating": "understood", "answer": "obsolete tab",
    })
    assert status == 409
    assert repository.get_document(identity)["properties"]["내답"] == answer


def test_unmanaged_note_is_not_turned_into_a_scheduled_review(library):
    server, repository, _ = library
    _, result, _ = request(server, "POST", "/api/documents", {
        "parent_id": COLLECTIONS["reviews"], "title": "개인 복습 메모", "content": "내 메모",
    })
    note = result["document"]
    status, _, _ = request(server, "POST", f'/api/reviews/{note["id"]}/submit', {
        "revision": note["revision"], "rating": "understood", "answer": "not a managed card",
    })
    assert status == 400
    assert repository.get_document(note["id"]) == note


def test_private_state_and_traversal_are_not_static_assets(library):
    server, _, _ = library
    for path in ("/library.json", "/documents/" + HUB + ".json", "/vendor/%2e%2e/library.json", "/api/documents/..%5cprivate.json"):
        status, body, _ = request(server, "GET", path)
        assert status in {400, 404}
        assert "documents" not in body and "csrf_token" not in body
    status, _, headers = request(server, "GET", "/api/bootstrap")
    assert status == 200
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers


def test_active_attachment_is_download_only_and_cannot_execute(library):
    server, _, _ = library
    assets = server.root / "assets"
    (assets / "untrusted.html").write_text("<script>fetch('/api/bootstrap')</script>", encoding="utf-8")
    status, _, headers = request(server, "GET", "/assets/untrusted.html")
    assert status == 200
    assert headers["Content-Disposition"] == "attachment"
    assert headers["Content-Security-Policy"] == "sandbox; default-src 'none'"


def test_arbitrary_artifacts_are_indexed_and_linked_without_markdown(library):
    server, _, _ = library
    folder = server.artifact_root / "course"
    folder.mkdir()
    html = folder / "summary.html"
    html.write_text("<script>window.artifact_script_ran = true</script><h1>Full notes</h1>", encoding="utf-8")
    data = folder / "model-output.bin"
    data.write_bytes(b"\x00artifact-bytes")

    status, listing, _ = request(server, "GET", "/api/artifacts")
    assert status == 200
    assert listing["root_path"] == str(server.artifact_root)
    assert {item["path"] for item in listing["artifacts"]} == {
        "course/summary.html", "course/model-output.bin",
    }
    assert next(item for item in listing["artifacts"] if item["path"] == "course/model-output.bin")["size"] == 15

    status, page, headers = request(server, "GET", "/artifacts/course/summary.html")
    assert status == 200 and b"Full notes" in page
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "Content-Disposition" not in headers
    assert "sandbox; default-src 'none'" in headers["Content-Security-Policy"]
    assert "script-src" not in headers["Content-Security-Policy"]

    status, payload, headers = request(server, "GET", "/artifacts/course/model-output.bin")
    assert status == 200 and payload == b"\x00artifact-bytes"
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Disposition"] == "attachment"
    assert headers["Content-Security-Policy"] == "sandbox; default-src 'none'"
    assert request(server, "GET", "/artifacts/%2e%2e/library.json")[0] == 400


def test_launcher_does_not_adopt_another_library_on_the_same_port(library, tmp_path):
    server, _, config = library
    selected = {**config, "artifact": {**config["artifact"], "port": server.server_address[1]}}
    assert library_status(selected)["status"] == "running"
    selected["artifact"]["root"] = str(tmp_path / "another-private-library")
    with pytest.raises(LibraryError) as error:
        library_status(selected)
    assert error.value.code == "artifact_port_in_use"


def test_launcher_install_adds_only_the_launcher_and_keeps_user_edits(library, tmp_path):
    server, _, config = library
    before = {path.name for path in server.root.iterdir()}
    installed = install_application(config, tmp_path / "settings.json")
    launcher = server.root / "열기.cmd"
    assert installed["launcher"] == str(launcher)
    assert {path.name for path in server.root.iterdir()} - before == {"열기.cmd"}
    assert b"library --open" in launcher.read_bytes()

    launcher.write_text("rem my shortcut", encoding="utf-8")
    with pytest.raises(LibraryError) as error:
        install_application(config, tmp_path / "settings.json")
    assert error.value.code == "artifact_application_modified"
    assert launcher.read_text(encoding="utf-8") == "rem my shortcut"



def test_synced_executable_changes_never_run_in_editor_origin(library):
    server, _, _ = library
    status, trusted, _ = request(server, "GET", "/app.js")
    assert status == 200
    (server.root / "app.js").write_text("window.stolen = true", encoding="utf-8")
    (server.root / "vendor").mkdir(exist_ok=True)
    (server.root / "vendor" / "injected.js").write_text("window.stolen = true", encoding="utf-8")
    status, current, _ = request(server, "GET", "/app.js")
    assert status == 200 and current == trusted
    assert b"window.stolen = true" not in current
    assert request(server, "GET", "/vendor/injected.js")[0] == 404
