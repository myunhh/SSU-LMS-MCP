import pytest

from ssu_lms_mcp.study_artifacts import ArtifactError
from ssu_lms_mcp.study_publish import PublicationError, StudyPublisher
from ssu_lms_mcp.study_store import StudyStore


@pytest.fixture
def publisher(tmp_path):
    with StudyStore(tmp_path / "study.sqlite3") as store:
        client = StudyPublisher(store, {"cloud_command": [], "artifact": {"root": str(tmp_path / "library")}})
        client.initialize([])
        yield client


def test_generated_snapshots_preserve_notes_user_title_and_publication_identity(publisher):
    key = "briefing:2026-09-28"
    page = publisher._row("records", key, {"제목": "Daily"}, "Personal draft")
    publisher._set_body(page, key, "First report")
    original = publisher._fetch(page["id"])
    publisher.repository.update_document(page["id"], {
        "title": "My own title", "content": original["content"] + "\nMy handwritten proof",
    }, expected_revision=original["revision"], user_edit=True)
    publisher._set_body(page, key, "Second report")
    again = publisher._row("records", key, {"제목": "Automatic rename"}, "Discard my draft")
    saved = publisher._fetch(page["id"])
    assert again == page
    assert saved["title"] == "My own title"
    assert saved["content"].index("Second report") < saved["content"].index("First report")
    assert "Personal draft" in saved["content"] and "My handwritten proof" in saved["content"]
    assert "Discard my draft" not in saved["content"]
    assert [row["id"] for row in publisher._rows("records")] == [page["id"]]


def test_replay_never_resurrects_deleted_snapshot_or_duplicates_new_snapshot(publisher):
    page = publisher._row("records", "version:one", {"제목": "Lecture"}, "My notes")
    publisher._set_body(page, "version:one", "Source explanation")
    document = publisher._fetch(page["id"])
    publisher.repository.update_document(page["id"], {"content": "Entirely rewritten personal notes"},
                                         expected_revision=document["revision"], user_edit=True)
    publisher._set_body(page, "version:one", "Source explanation")
    assert publisher._fetch(page["id"])["content"] == "Entirely rewritten personal notes"
    publisher._set_body(page, "version:one", "New explanation")
    saved = publisher._fetch(page["id"])
    publisher._set_body(page, "version:one", "New explanation")
    assert publisher._fetch(page["id"]) == saved
    assert saved["content"].count("New explanation") == 1
    assert "Source explanation" not in saved["content"]


def test_creation_checkpoint_loss_recovers_existing_page_without_replacing_notes(publisher):
    page = publisher._row("records", "version:one", {"제목": "Lecture"}, "Existing notes")
    publisher.store.set_meta("artifact:row:records:version:one", None)
    recovered = publisher._row("records", "version:one", {"제목": "Updated"}, "Replacement")
    assert recovered == page
    assert publisher._fetch(page["id"])["content"].startswith("Existing notes")
    assert len(publisher._rows("records")) == 1


def test_snapshot_cas_preserves_concurrent_editor_changes(publisher, monkeypatch):
    page = publisher._row("records", "one", {"제목": "Lecture"}, "Notes")
    update = publisher.repository.update_document
    fired = False

    def race(document_id, changes, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            current = publisher._fetch(document_id)
            update(document_id, {"content": "Concurrent user notes"},
                   expected_revision=current["revision"], user_edit=True)
        return update(document_id, changes, **kwargs)

    monkeypatch.setattr(publisher.repository, "update_document", race)
    with pytest.raises(ArtifactError, match="artifact_revision_conflict"):
        publisher._set_body(page, "one", "New snapshot")
    assert publisher._fetch(page["id"])["content"] == "Concurrent user notes"
    publisher._set_body(page, "one", "New snapshot")
    assert publisher._fetch(page["id"])["content"].endswith("Concurrent user notes")


def test_wrong_upload_receipt_cannot_be_committed(publisher):
    class CrossedReceipt:
        def request(self, *args, **kwargs):
            return {"status": "saved", "sha256": "b" * 64, "size": 7}
    publisher.bridge = CrossedReceipt()
    with pytest.raises(PublicationError, match="upload_receipt_mismatch"):
        publisher.upload({"source": {}, "sha256": "a" * 64, "size": 7})


def test_late_response_invalidates_transport_instead_of_returning_another_upload():
    import io
    from types import SimpleNamespace

    from ssu_lms_mcp.study_publish import CloudBridge

    bridge = CloudBridge([])
    bridge.process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(), wait=lambda **_: 0)
    bridge.reader = SimpleNamespace(join=lambda **_: None)
    bridge.responses.put({"request_id": "previous-request", "ok": True, "result": {"sha256": "a" * 64}})
    with pytest.raises(PublicationError, match="cloud_response_identity_mismatch"):
        bridge.request("onedrive-upload", artifact={"sha256": "b" * 64})
    assert bridge.process is None


def test_crash_after_page_creation_cannot_resurrect_deleted_generated_content(publisher):
    from ssu_lms_mcp.study_publish import _managed

    key, body = "version:crash", "Original generated explanation"
    _, managed = _managed(key, body)
    page = publisher._row("records", key, {"제목": "Lecture"}, managed)
    document = publisher._fetch(page["id"])
    publisher.repository.update_document(
        page["id"], {"content": "I replaced the generated material"},
        expected_revision=document["revision"], user_edit=True)
    # Simulate a crash before the publisher reaches _set_body; the file itself
    # already records which generated snapshot was delivered.
    publisher._set_body(page, key, body)
    assert publisher._fetch(page["id"])["content"] == "I replaced the generated material"


def test_modified_material_keeps_page_identity_and_current_version_lineage(publisher):
    source = {"key": "material:7:1", "partition": "material:7", "kind": "material",
              "course_id": 7, "course_name": "Course", "title": "Original title",
              "metadata_hash": "a", "source_url": "https://canvas.ssu.ac.kr/courses/7"}
    publisher.store.observe([source], [source["partition"]])
    original = publisher.store.record_version(source["key"], {
        "sha256": "a" * 64, "size": 5, "filename": "original.pdf", "path": "/private/original.pdf"})
    first = publisher.publish_baseline(original, {"web_url": "https://example.org/original.pdf"})
    document = publisher._fetch(first["artifact_page_id"])
    publisher.repository.update_document(document["id"], {"title": "Personal title", "content": "Original personal notes"},
                                         expected_revision=document["revision"], user_edit=True)
    publisher.store.observe([{**source, "metadata_hash": "b", "title": "Modified title"}], [source["partition"]])
    revised = publisher.store.record_version(source["key"], {
        "sha256": "b" * 64, "size": 9, "filename": "modified.pdf", "path": "/private/modified.pdf"})
    second = publisher.publish_baseline(revised, {"web_url": "https://example.org/modified.pdf"})
    assert second["artifact_page_id"] == first["artifact_page_id"]
    assert revised["previous_version_id"] == original["id"]
    assert publisher.store.get_version(original["id"])["source"] == original["source"]
    assert publisher.publish_baseline(original, {})["historical"] is True
    saved = publisher._fetch(document["id"])
    assert saved["properties"]["SHA256"] == revised["sha256"]
    assert saved["properties"]["원본"] == "https://example.org/modified.pdf"
    assert saved["title"] == "Personal title"
    assert saved["content"] == "Original personal notes"
