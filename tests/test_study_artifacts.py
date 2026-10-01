"""Isolated persistence, concurrent-draft, identity, and filesystem regressions."""
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from ssu_lms_mcp import study_artifacts as artifacts
from ssu_lms_mcp.study_artifacts import ArtifactError, ArtifactRepository, document_url

HUB = str(uuid.UUID(int=1))
COLLECTIONS = {role: str(uuid.UUID(int=index)) for index, role in enumerate(("materials", "reviews", "records"), 2)}
CREATED = "2026-03-02T09:12:13+09:00"
UPDATED = "2026-03-03T10:14:15+09:00"


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts.tempfile, "gettempdir", lambda: str(tmp_path / "local"))
    monkeypatch.setenv("OneDrive", str(tmp_path / "onedrive"))
    monkeypatch.setattr(artifacts, "_now", lambda previous=None: (
        datetime.fromisoformat(previous) + timedelta(seconds=1)
    ).isoformat() if previous else "2026-09-28T12:00:00+00:00")
    repo = ArtifactRepository(tmp_path / "onedrive" / "학습 아티팩트")
    repo.initialize(hub_id=HUB, title="SSU 학습", collections=COLLECTIONS, source={"notion_id": HUB})
    return repo


def page(**changes):
    return {
        "id": str(uuid.UUID(int=10)), "title": "원래 제목", "parent_id": COLLECTIONS["reviews"],
        "kind": "page", "role": "reviews", "content": "## 내 노트\n원문 전체\n\n$$x^2$$",
        "properties": {"키": "review:7:recall", "내답": "", "제출": "__NO__", "마지막수정": UPDATED},
        "managed_key": "review:7:recall", "created_at": CREATED, "updated_at": UPDATED,
        **changes,
    }


def store_remote(path, document):
    """Represent an independently synchronized, correctly signed document."""
    unsigned = {key: value for key, value in document.items() if key != "revision"}
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_text(json.dumps({**unsigned, "revision": hashlib.sha256(canonical).hexdigest()}, ensure_ascii=False), encoding="utf-8")


def assert_error(code, action):
    with pytest.raises(ArtifactError) as caught:
        action()
    assert caught.value.code == code
    assert str(caught.value) == code


def test_stale_acknowledgement_cannot_erase_newer_answer_or_draft(repository):
    first = repository.create_document(page())
    publisher_snapshot = ArtifactRepository(repository.root).get_document(first["id"])
    learner = {"content": first["content"] + "\n새 개인 노트", "properties": {"내답": "수정한 답", "제출": "__YES__"}}
    saved = repository.update_document(first["id"], learner, expected_revision=first["revision"], user_edit=True)
    assert_error("artifact_revision_conflict", lambda: repository.update_document(
        first["id"], {"properties": {"내답": "", "제출": "__NO__"}}, expected_revision=publisher_snapshot["revision"],
    ))
    # Caller-owned unsaved text is never consumed or rewritten by a failed save.
    draft = {"content": "아직 저장되지 않은 초안", "properties": {"내답": "다음 답"}}
    before = json.loads(json.dumps(draft))
    assert_error("artifact_revision_conflict", lambda: repository.update_document(
        first["id"], draft, expected_revision=first["revision"], user_edit=True,
    ))
    assert draft == before
    assert ArtifactRepository(repository.root).get_document(first["id"]) == saved
    assert saved["content"] == learner["content"]
    assert saved["properties"]["내답"] == "수정한 답"
    assert saved["properties"]["제출"] == "__YES__"


def test_two_concurrent_saves_have_exactly_one_winner(repository):
    first = repository.create_document(page())
    barrier = threading.Barrier(2)

    def save(answer):
        client = ArtifactRepository(repository.root)
        barrier.wait(timeout=10)
        try:
            return client.update_document(first["id"], {"properties": {"내답": answer}},
                                          expected_revision=first["revision"], user_edit=True)
        except ArtifactError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ("첫째 답", "둘째 답")))
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == 1
    assert results.count("artifact_revision_conflict") == 1
    assert repository.get_document(first["id"]) == successes[0]


def test_imported_metadata_and_unknown_provenance_survive_edits(repository):
    imported = page(
        schema='CREATE TABLE ("제출" CHECKBOX)',
        views=[{"id": "notion-view", "filter": {"제출": True}, "unknown_view_option": [1, 2]}],
        source={"notion_id": str(uuid.UUID(int=10)), "source_digest": "original", "unrecognized": {"keep": ["원문", 7], "updated": 1}},
    )
    imported["properties"].update({"복습일": {"start": "2026-09-28", "end": None}, "알수없는필드": ["original"], "제출": "__YES__"})
    original = repository.create_document(imported)
    assert original["created_at"] == CREATED
    assert original["updated_at"] == UPDATED
    assert original["properties"] == imported["properties"]
    assert original["schema"] == imported["schema"]
    assert original["views"] == imported["views"]
    saved = repository.update_document(original["id"], {
        "title": "내가 정한 제목", "content": original["content"] + "\n내 메모",
        "properties": {"내답": "직접 쓴 답"}, "source": {"unrecognized": {"updated": 2}},
    }, expected_revision=original["revision"], user_edit=True)
    assert saved["created_at"] == CREATED
    assert saved["updated_at"] != UPDATED
    assert saved["properties"]["마지막수정"] == saved["updated_at"]
    assert saved["properties"]["복습일"] == imported["properties"]["복습일"]
    assert saved["properties"]["알수없는필드"] == ["original"]
    assert saved["properties"]["제출"] == "__YES__"
    assert saved["schema"] == imported["schema"]
    assert saved["views"] == imported["views"]
    assert saved["source"] == {**imported["source"], "unrecognized": {"keep": ["원문", 7], "updated": 2}}
    assert set(saved["user_fields"]) == {"title", "content", "properties.내답"}
    later = repository.update_document(original["id"], {"properties": {"시도": 1}}, expected_revision=saved["revision"])
    assert later["user_fields"] == saved["user_fields"]
    assert later["content"] == saved["content"]
    assert later["title"] == saved["title"]
    saved["source"]["unrecognized"]["keep"].append("caller-only")
    assert repository.get_document(original["id"])["source"]["unrecognized"]["keep"] == ["원문", 7]


def test_new_user_pages_have_no_managed_identity_and_can_share_parent(repository):
    first = repository.create_document({"title": "개인 페이지", "parent_id": COLLECTIONS["reviews"], "content": "내 문서"})
    second = repository.create_document({"title": "개인 페이지", "parent_id": COLLECTIONS["reviews"], "content": "다른 문서"})
    assert first["id"] != second["id"]
    assert first["managed_key"] is None
    assert second["managed_key"] is None
    assert first["user_fields"] == []
    assert {row["content"] for row in repository.list_documents()} == {"내 문서", "다른 문서"}
    assert document_url(first["id"], 8767) == f"http://127.0.0.1:8767/#/page/{first['id']}"


def test_failed_atomic_replace_retains_complete_previous_document_and_can_replay(repository, monkeypatch):
    original = repository.create_document(page())
    path = repository.root / "documents" / (original["id"] + ".json")
    previous_bytes = path.read_bytes()
    real_replace = artifacts.os.replace

    def fail_replace(source, destination):
        raise OSError("private path and upstream detail must not escape")

    with monkeypatch.context() as fault:
        fault.setattr(artifacts.os, "replace", fail_replace)
        assert_error("artifact_repository_io_error", lambda: repository.update_document(
            original["id"], {"content": "새 노트"}, expected_revision=original["revision"], user_edit=True,
        ))
    assert artifacts.os.replace is real_replace
    assert path.read_bytes() == previous_bytes
    assert list(path.parent.iterdir()) == [path]
    assert repository.get_document(original["id"]) == original
    saved = repository.update_document(original["id"], {"content": "새 노트"}, expected_revision=original["revision"], user_edit=True)
    assert repository.get_document(original["id"]) == saved
    assert saved["content"] == "새 노트"


def test_replayed_noop_preserves_timestamp_revision_and_file(repository):
    original = repository.create_document(page())
    path = repository.root / "documents" / (original["id"] + ".json")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    replay = repository.update_document(original["id"], {
        "title": original["title"], "content": original["content"], "properties": {"내답": ""},
    }, expected_revision=original["revision"], user_edit=True)
    assert replay == original
    assert replay["user_fields"] == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert repository.update_document(original["id"], {}, expected_revision=original["revision"]) == original


def test_duplicate_managed_identity_and_id_cannot_overwrite_document(repository):
    original = repository.create_document(page())
    assert_error("artifact_duplicate_key", lambda: repository.create_document(page(content="overwrite")))
    assert_error("artifact_duplicate_key", lambda: repository.create_document(page(id=str(uuid.UUID(int=11)))))
    other_role = repository.create_document(page(id=str(uuid.UUID(int=12)), role="materials", parent_id=COLLECTIONS["materials"]))
    assert other_role["managed_key"] == original["managed_key"]
    assert repository.get_document(original["id"]) == original


def test_remotely_introduced_duplicate_managed_identity_blocks_reads_and_writes(repository):
    original = repository.create_document(page())
    duplicate = {**original, "id": str(uuid.UUID(int=11))}
    store_remote(repository.root / "documents" / (duplicate["id"] + ".json"), duplicate)
    assert_error("artifact_duplicate_key", repository.list_documents)
    assert_error("artifact_duplicate_key", lambda: repository.get_document(original["id"]))
    assert_error("artifact_duplicate_key", lambda: repository.update_document(
        original["id"], {"content": "must not write"}, expected_revision=original["revision"],
    ))


@pytest.mark.parametrize("changes", [
    {"id": str(uuid.UUID(int=99))}, {"parent_id": HUB}, {"kind": "database"},
    {"role": "materials"}, {"managed_key": "other"}, {"properties": {"키": "other"}},
    {"created_at": UPDATED}, {"unsupported": "must not disappear"},
    {"updated_at": CREATED}, {"revision": "0" * 64},
])
def test_identity_and_unsupported_changes_are_rejected_atomically(repository, changes):
    original = repository.create_document(page())
    assert_error("artifact_invalid_document", lambda: repository.update_document(
        original["id"], changes, expected_revision=original["revision"],
    ))
    assert repository.get_document(original["id"]) == original


@pytest.mark.parametrize("corruption,code", [
    ("invalid_json", "artifact_invalid_document"),
    ("duplicate_json_key", "artifact_invalid_document"),
    ("changed_without_revision", "artifact_repository_conflict"),
    ("conflicted_copy", "artifact_duplicate_key"),
    ("renamed_copy", "artifact_repository_conflict"),
    ("unknown_file", "artifact_repository_conflict"),
])
def test_corrupt_and_conflicted_documents_never_get_silently_skipped(repository, corruption, code):
    original = repository.create_document(page())
    path = repository.root / "documents" / (original["id"] + ".json")
    if corruption == "invalid_json":
        path.write_text('{"title":', encoding="utf-8")
    elif corruption == "duplicate_json_key":
        path.write_text('{"title":"duplicate",' + path.read_text(encoding="utf-8")[1:], encoding="utf-8")
    elif corruption == "changed_without_revision":
        path.write_text(json.dumps({**original, "content": "remote edit with stale hash"}), encoding="utf-8")
    elif corruption == "conflicted_copy":
        path.with_name(original["id"] + "-PC.json").write_bytes(path.read_bytes())
    elif corruption == "renamed_copy":
        path.rename(path.with_name("conflicted-document.json"))
    else:
        (path.parent / "unfinished.tmp").write_text("incomplete", encoding="utf-8")
    assert_error(code, repository.list_documents)
    assert_error(code, lambda: repository.create_document({"title": "must not hide damage"}))


def test_initialization_is_idempotent_but_never_reassigns_repository(repository):
    original = repository.manifest()
    repository.create_document(page())
    assert repository.initialize(hub_id=HUB, title="caller title", collections=COLLECTIONS) == original
    assert_error("artifact_repository_conflict", lambda: repository.initialize(
        hub_id=str(uuid.UUID(int=99)), title="replacement", collections=COLLECTIONS,
    ))
    assert repository.manifest() == original
    assert repository.get_document(str(uuid.UUID(int=10)))["content"] == page()["content"]
    assert not repository._lock_path.is_relative_to(repository.root)
    assert repository._lock_path == ArtifactRepository(repository.root)._lock_path


def test_missing_and_conflicted_manifest_fail_closed(repository, tmp_path):
    fresh = ArtifactRepository(tmp_path / "uninitialized")
    assert_error("artifact_repository_uninitialized", fresh.manifest)
    assert_error("artifact_repository_uninitialized", lambda: fresh.create_document({"title": "no manifest"}))
    path = repository.root / "library.json"
    path.with_name("library-PC.json").write_bytes(path.read_bytes())
    assert_error("artifact_repository_conflict", repository.list_documents)


def test_corrupt_manifest_cannot_be_reinitialized(repository):
    path = repository.root / "library.json"
    path.write_text("{broken", encoding="utf-8")
    assert_error("artifact_repository_conflict", lambda: repository.initialize(hub_id=HUB, title="SSU", collections=COLLECTIONS))
    assert path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("identifier", ["../outside", "..\\outside", "/etc/passwd", "C:\\private.json", "not-a-uuid", HUB + "/../../outside"])
def test_document_identifiers_cannot_cross_path_boundary(repository, identifier):
    assert_error("artifact_invalid_document", lambda: repository.get_document(identifier))
    assert_error("artifact_invalid_document", lambda: repository.create_document({"id": identifier, "title": "bad"}))
    assert_error("artifact_invalid_document", lambda: document_url(identifier))
    assert repository.list_documents() == []


@pytest.mark.parametrize("boundary", ["root", "documents", "document", "manifest", "assets"])
def test_symlink_escape_is_rejected_without_touching_target(repository, tmp_path, boundary):
    original = repository.create_document(page())
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "private.json"
    marker.write_text("private data", encoding="utf-8")
    if boundary == "root":
        link = tmp_path / "alias"
        target = repository.root
    elif boundary in ("documents", "assets"):
        link = repository.root / boundary
        link.rename(tmp_path / (boundary + "-original"))
        target = outside
    else:
        link = repository.root / "library.json" if boundary == "manifest" else repository.root / "documents" / (original["id"] + ".json")
        link.rename(tmp_path / (boundary + "-original.json"))
        target = marker
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symlinks")
    if boundary == "root":
        assert_error("artifact_repository_conflict", lambda: ArtifactRepository(link))
    else:
        assert_error("artifact_repository_conflict", repository.list_documents)
    assert marker.read_text(encoding="utf-8") == "private data"


@pytest.mark.parametrize("source", [
    {"access_token": "private-token-value"},
    {"unknown": {"Authorization": "Bearer private-token-value"}},
    {"url": "https://example.com/file?X-Amz-Signature=private-token-value"},
    {"attachments": [{"url": "https://example.com/file?tempauth=private-token-value"}]},
    {"url": "https://user:private-token-value@example.com/file"},
])
def test_sensitive_provenance_is_rejected_not_silently_copied(repository, source):
    assert_error("artifact_invalid_document", lambda: repository.create_document(page(source=source)))
    assert repository.list_documents() == []


@pytest.mark.parametrize("changes", [
    {"created_at": "2026-09-28"}, {"updated_at": "2026-09-28T12:00:00"},
    {"properties": {"number": float("nan")}}, {"properties": {7: "non-string key"}},
    {"managed_key": "mismatch"}, {"source": ["not an object"]}, {"surprise": "unknown field"},
    {"revision": "0" * 64},
])
def test_invalid_import_never_creates_partial_document(repository, changes):
    assert_error("artifact_invalid_document", lambda: repository.create_document(page(**changes)))
    assert repository.list_documents() == []


def test_missing_document_and_error_codes_never_expose_input(repository):
    assert_error("artifact_document_not_found", lambda: repository.get_document(str(uuid.UUID(int=99))))
    assert str(ArtifactError("C:/private/password=secret")) == "artifact_repository_error"
    assert str(ArtifactError("Artifact_UpperCase")) == "artifact_repository_error"
