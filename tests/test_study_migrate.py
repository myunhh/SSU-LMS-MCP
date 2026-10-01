"""Complete, read-only import and fail-closed cutover against documented envelopes."""
import hashlib
import json
import re
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

from ssu_lms_mcp.study_artifacts import ArtifactRepository
from ssu_lms_mcp.study_migrate import MigrationError, migrate_notion
from ssu_lms_mcp.study_publish import StudyPublisher
from ssu_lms_mcp.study_store import StudyStore


def uid(number):
    return str(uuid.UUID(int=number))


def url(number):
    return "https://app.notion.com/p/" + uid(number).replace("-", "") + "?pvs=204"


class SourceHub:
    """Notion transport fixture with independent hierarchy and row inventories."""

    def __init__(self):
        self.calls = []
        self.flags = {}
        self.gap = False
        self.duplicate = False
        self.asset = False
        self.rows = {uid(n + 10): [{"url": url(n + 20), "createdTime": "2026-09-27T01:00:00Z"}]
                     for n in range(2, 5)}
        self.review_key = "review-key"
        self.record_key = "version:record"
        self.event = "notion:old-event"
        self.review_answer = "보존할 답"
        self.stamp = "2026-09-28T01:00:00Z"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def page(self, number, parent=None, properties=None, content="", title="자료"):
        ancestor = f'<parent-page url="{url(parent)}"/>' if parent else ""
        return {"metadata": {"type": "page"}, "title": title, "url": url(number),
                "text": f'<page url="{url(number)}"><ancestor-path>{ancestor}</ancestor-path>\n'
                        f'<properties>\n{json.dumps(properties or {}, ensure_ascii=False)}\n</properties>\n'
                        f'<content>\n{content}\n</content>\n</page>',
                "page_last_edited_at": self.stamp, "cover": None, "icon": None,
                **self.flags.get(number, {})}

    def fetch(self, value):
        identity = re.search(r"[0-9a-f-]{36}", value)
        number = uuid.UUID(identity.group() if identity else value).int
        if value.startswith("collection://"):
            return {"text": '<data-source-state>{"schema":{"자료명":{"type":"title"}}}</data-source-state>'}
        if value.startswith("view://"):
            return {"text": '<view-state>{"name":"All","type":"table"}</view-state>'}
        if number == 1:
            content = "Full hub notes\n" + "\n".join(f'<database url="{url(n)}">Collection {n}</database>' for n in range(2, 5))
            return self.page(1, content=content, title="SSU 학습 허브")
        if number in (2, 3, 4):
            source = "collection://" + uid(number + 10)
            state = {"name": "All", "dataSourceUrl": source, "type": "table"}
            text = (f'<database url="{url(number)}"><ancestor-path><parent-page url="{url(1)}"/></ancestor-path>'
                    f'<data-source url="{source}"><data-source-state>{{"schema":{{"키":{{"type":"text"}}}}}}</data-source-state></data-source>'
                    f'<view url="view://{uid(number + 30)}">{json.dumps(state)}</view></database>')
            return {"metadata": {"type": "database"}, "title": f"Collection {number}", "url": url(number), "text": text}
        if number == 22:
            return self.page(number, 2, {"키": "material:7:1", "자료명": "자료", "원본": "https://example.org/original.pdf"},
                             "Original metadata and personal notes")
        if number == 23:
            return self.page(number, 3, {"키": self.review_key, "질문": "질문", "내답": self.review_answer, "평가": "모름",
                                        "제출": "__NO__", "처리이벤트": self.event, "마지막수정": self.stamp}, "Learner notes")
        if number == 24:
            content = "Complete lesson\n$$a^2+b^2=c^2$$\n<table><tr><td>Full cell</td></tr></table>\n<details><summary>Answer</summary>Entire explanation</details>\n"
            content += f'<page url="{url(25)}">Nested notes</page>\n'
            if self.duplicate:
                content += f'<page url="{url(25)}">Duplicate child</page>\n'
            if self.asset:
                content += '<pdf src="https://prod-files-secure.s3.us-west-2.amazonaws.com/file.pdf?X-Amz-Signature=secret">Attachment</pdf>'
            return self.page(number, 4, {"키": self.record_key, "제목": "Lesson", "분류": "자료버전"}, content)
        if number == 25:
            return self.page(number, 24, {"custom": "Complete custom property"}, "All nested personal content", "My child")
        raise AssertionError(value)

    def request(self, action, *, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        assert action == "notion-call"
        if name == "notion-fetch":
            result = self.fetch(arguments["id"])
        elif name == "notion-get-comments":
            return {"structuredContent": {"suggested_edits_status": "not_enabled"}, "content": [{"type": "text", "text":
                    '<discussions><discussion id="one"><comment author="Learner">Full threaded comment</comment></discussion></discussions>'}]}
        elif name == "notion-query-data-sources":
            data = arguments["data"]
            if self.gap:
                return {"structuredContent": {"results": [], "has_more": True, "next_cursor": "unreachable"}}
            number = uuid.UUID(data["view_url"].removeprefix("view://")).int - 30
            result = {"results": [] if data["is_archived"] else self.rows[uid(number + 10)], "has_more": False}
        else:
            raise AssertionError("Mutation or undocumented tool: " + name)
        return {"structuredContent": deepcopy(result)}


@pytest.fixture
def migration(tmp_path, monkeypatch):
    cloud = SourceHub()
    monkeypatch.setattr("ssu_lms_mcp.study_migrate.CloudBridge", lambda command: cloud)
    config = {"cloud_command": [], "artifact": {"root": str(tmp_path / "library"), "port": 8766}}
    with StudyStore(tmp_path / "study.sqlite") as store:
        store.set_meta("notion:hub", {"hub_id": uid(1), "hub_url": url(1)})
        for number, role in enumerate(("materials", "reviews", "records"), 2):
            store.set_meta("notion:db:" + role, {"id": uid(number), "data_source_id": uid(number + 10)})
        yield config, store, cloud


def test_complete_import_preserves_hierarchy_properties_comments_schema_and_later_edits(migration):
    config, store, cloud = migration
    result = migrate_notion(config, store)
    assert result["counts"] == {"documents": 8, "pages": 5, "databases": 3, "materials": 1, "reviews": 1, "records": 1}
    assert result["completeness"]["verified"]
    assert Path(result["backup"]).is_file()
    repository = ArtifactRepository(config["artifact"]["root"])
    child = repository.get_document(uid(25))
    assert child["parent_id"] == uid(24)
    assert child["properties"] == {"custom": "Complete custom property"}
    assert "All nested personal content" in child["content"]
    lesson = repository.get_document(uid(24))
    assert "Entire explanation" in lesson["content"] and "Full cell" in lesson["content"]
    assert "Full threaded comment" in lesson["content"]
    assert repository.get_document(uid(2))["schema"]["collection://" + uid(12)]["state"]["schema"]["키"]["type"] == "text"
    assert repository.get_document(uid(2))["views"][0]["state"]["name"] == "All"
    changed = repository.update_document(uid(24), {"title": "Personal title", "content": "Entire user rewrite"},
                                          expected_revision=lesson["revision"], user_edit=True)
    rerun = migrate_notion(config, store)
    assert rerun["created"] == 0 and rerun["preserved"] == 8
    assert repository.get_document(uid(24)) == changed
    assert store.get_meta("notion:hub") is None
    assert store.get_meta("artifact:hub")["hub_url"] == result["mapping"][uid(1)]["url"]
    assert {name for name, _ in cloud.calls} == {"notion-fetch", "notion-get-comments", "notion-query-data-sources"}


@pytest.mark.parametrize("flags", [{"truncated": True}, {"unknown_block_count": 1}, {"unknown_block_ids": [uid(99)]}])
def test_incomplete_page_rejects_without_metadata_cutover(migration, flags):
    config, store, cloud = migration
    cloud.flags[24] = flags
    with pytest.raises(MigrationError, match="migration_source_incomplete"):
        migrate_notion(config, store)
    assert store.get_meta("notion:hub")["hub_id"] == uid(1)
    assert store.get_meta("artifact:migration") is None
    assert not (Path(config["artifact"]["root"]) / "library.json").exists()


@pytest.mark.parametrize("fault,code", [("gap", "migration_pagination_gap"), ("duplicate", "migration_duplicate_identity")])
def test_gaps_and_duplicate_descendants_never_commit_partial_import(migration, fault, code):
    config, store, cloud = migration
    setattr(cloud, fault, True)
    with pytest.raises(MigrationError, match=code):
        migrate_notion(config, store)
    assert store.get_meta("artifact:migration") is None
    assert store.get_meta("notion:hub") is not None


def test_uploaded_attachment_bytes_are_verified_and_signed_urls_never_persist(migration, monkeypatch):
    config, store, cloud = migration
    cloud.asset = True
    binary = b"%PDF-complete fixture original"
    digest = hashlib.sha256(binary).hexdigest()

    def download(url, directory, *, max_bytes):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (digest + ".pdf")
        path.write_bytes(binary)
        return {"path": "assets/" + path.name, "sha256": digest, "size": len(binary)}

    monkeypatch.setattr("ssu_lms_mcp.study_migrate._download_asset", download)
    result = migrate_notion(config, store)
    assert result["attachments"]["files"] == [{"path": "assets/" + digest + ".pdf", "sha256": digest, "size": len(binary)}]
    repository = ArtifactRepository(config["artifact"]["root"])
    serialized = json.dumps(repository.list_documents())
    assert "X-Amz-Signature" not in serialized and "secret" not in serialized
    assert "/assets/" + digest + ".pdf" in repository.get_document(uid(24))["content"]


@pytest.mark.parametrize("answer", ["보존할 답", url(24), "notion: 학습자가 직접 쓴 답"])
def test_history_receipts_pending_events_and_acknowledgements_cut_over_together(migration, answer):
    from ssu_lms_mcp.study_artifact_review import _event_id

    config, store, cloud = migration
    cloud.review_answer = answer
    source = {"key": "material:7:1", "partition": "material:7", "kind": "material", "course_id": 7,
              "title": "Original", "course_name": "Course", "metadata_hash": "a", "source_url": "https://canvas.ssu.ac.kr/courses/7"}
    store.observe([source], [source["partition"]])
    version = store.record_version(source["key"], {"sha256": "a" * 64, "size": 4, "filename": "original.pdf", "path": "/original.pdf"})
    store.save_stage(version["id"], "generated", {"title": "Original package"})
    card = store.add_reviews(version["id"], [{"id": "one", "prompt": "Q", "answer": "A"}])[0]
    store.link_review_page(card["key"], uid(23))
    cloud.review_key, cloud.record_key = card["key"], "version:" + version["id"]
    snapshot = {"edited_at": "2026-09-28T01:00:00+00:00", "rating": "모름", "answer": answer}
    artifact_event = _event_id(uid(23), card["key"], snapshot)
    legacy_event = "notion:" + artifact_event.removeprefix("artifact:")
    cloud.event = legacy_event
    store.record_review(card["key"], "unknown", snapshot["answer"], at=snapshot["edited_at"], event_id=legacy_event)
    store.set_meta("notion:review:pending:" + card["key"], {**snapshot, "observed_edited_at": snapshot["edited_at"],
                   "event_id": legacy_event, "review_key": card["key"], "page_id": uid(23)})
    store.set_meta("notion:review:ack:" + legacy_event, {"status": "acknowledged", "review_key": card["key"], "page_id": uid(23)})
    store.save_stage(version["id"], "published", {"notion_page_id": uid(22), "notion_url": url(24), "version_page_id": uid(24)})
    before = store.get_version(version["id"])
    result = migrate_notion(config, store)
    published = store.get_stage(version["id"], "published")
    assert published == {"artifact_page_id": uid(22), "artifact_url": result["mapping"][uid(24)]["url"], "version_page_id": uid(24)}
    assert store.get_version(version["id"])["source"] == before["source"]
    assert store.get_stage(version["id"], "generated") == before["stages"]["generated"]
    assert store.get_meta("artifact:review:ack:" + artifact_event)["status"] == "acknowledged"
    publisher = StudyPublisher(store, config)
    assert publisher.sync_reviews() == {"imported": 0, "failures": []}
    reviewed = store.reviews_for_version(version["id"])[0]
    assert [attempt["event_id"] for attempt in reviewed["attempts"]] == [artifact_event]
    assert reviewed["attempts"][0]["answer"] == answer
    assert publisher._fetch(uid(23))["properties"]["내답"] == answer
    with sqlite3.connect(result["backup"]) as backup:
        assert backup.execute("SELECT event_id FROM review_attempts").fetchone()[0] == legacy_event


def test_row_export_follows_every_page_and_includes_archived_partition():
    from ssu_lms_mcp.study_migrate import NotionReader

    class ManyRows(SourceHub):
        def __init__(self):
            super().__init__()
            self.rows[uid(12)] = [{"url": url(n), "createdTime": "2026-09-27T01:00:00Z"} for n in range(100, 203)]

        def request(self, action, *, name, arguments):
            data = arguments.get("data", {})
            if name == "notion-query-data-sources" and data["mode"] == "view":
                if data["is_archived"]:
                    return {"structuredContent": {"results": [{"url": url(999)}], "has_more": False}}
                offset = int(data.get("start_cursor", "0"))
                rows = self.rows[uid(12)][offset:offset + 100]
                more = offset + 100 < len(self.rows[uid(12)])
                return {"structuredContent": {"results": rows, "has_more": more,
                                               "next_cursor": str(offset + 100) if more else None}}
            return super().request(action, name=name, arguments=arguments)

    source = "collection://" + uid(12)
    rows = NotionReader(ManyRows()).rows(source, [{"url": "view://" + uid(32), "state": {"dataSourceUrl": source}}])
    assert set(rows) == {uid(n) for n in range(100, 203)} | {uid(999)}
    assert rows[uid(999)]["archived"] is True
    assert rows[uid(202)]["createdTime"] == "2026-09-27T01:00:00Z"


def test_source_change_during_export_prevents_cutover(migration, monkeypatch):
    config, store, cloud = migration
    original = cloud.fetch
    seen = 0

    def changed(value):
        nonlocal seen
        result = original(value)
        if value == uid(24):
            seen += 1
            if seen > 1:
                result["text"] = result["text"].replace("Complete lesson", "New concurrent source edit")
        return result

    monkeypatch.setattr(cloud, "fetch", changed)
    with pytest.raises(MigrationError, match="migration_source_changed"):
        migrate_notion(config, store)
    assert store.get_meta("notion:hub") is not None
    assert store.get_meta("artifact:migration") is None


@pytest.mark.parametrize("advertised", [9, 99])
def test_asset_download_checks_entire_stream_and_cleans_incomplete_files(tmp_path, monkeypatch, advertised):
    import io

    from ssu_lms_mcp.study_migrate import _download_asset

    class Response(io.BytesIO):
        url = "https://prod-files-secure.s3.us-west-2.amazonaws.com/original.pdf?X-Amz-Signature=private"
        headers = {"Content-Length": str(advertised)}

    class Opener:
        def open(self, request, timeout):
            return Response(b"full-data")

    monkeypatch.setattr("ssu_lms_mcp.study_migrate.build_opener", lambda *handlers: Opener())
    directory = tmp_path / "assets"
    if advertised != 9:
        with pytest.raises(MigrationError, match="migration_asset_incomplete"):
            _download_asset(Response.url, directory)
        assert list(directory.iterdir()) == []
    else:
        result = _download_asset(Response.url, directory)
        assert (tmp_path / result["path"]).read_bytes() == b"full-data"
        assert result["sha256"] == hashlib.sha256(b"full-data").hexdigest()
        assert result["size"] == 9


@pytest.mark.parametrize("advertised", [None, "9"])
def test_asset_limit_rejects_advertised_and_chunked_oversize(tmp_path, monkeypatch, advertised):
    import io

    from ssu_lms_mcp.study_migrate import _download_asset

    class Response(io.BytesIO):
        url = "https://prod-files-secure.s3.us-west-2.amazonaws.com/original.pdf"
        headers = {} if advertised is None else {"Content-Length": advertised}

    class Opener:
        def open(self, request, timeout):
            return Response(b"full-data")

    monkeypatch.setattr("ssu_lms_mcp.study_migrate.build_opener", lambda *handlers: Opener())
    directory = tmp_path / "assets"
    with pytest.raises(MigrationError, match="migration_asset_limit"):
        _download_asset(Response.url, directory, max_bytes=8)
    assert list(directory.iterdir()) == []


def test_replay_preserves_native_publications_created_after_cutover(migration):
    config, store, cloud = migration
    cloud.rows[uid(13)] = []
    migrate_notion(config, store)
    publisher = StudyPublisher(store, config)
    source = {"key": "material:7:2", "partition": "material:7", "kind": "material", "course_id": 7,
              "title": "Native lesson", "course_name": "Course", "metadata_hash": "new",
              "source_url": "https://canvas.ssu.ac.kr/courses/7"}
    store.observe([], [source["partition"]])
    store.observe([source], [source["partition"]])
    version = store.record_version(source["key"], {"sha256": "b" * 64, "size": 4, "filename": "new.pdf", "path": "/new.pdf"})
    receipt = publisher.publish_baseline(store.get_version(version["id"]), {})
    store.save_stage(version["id"], "published", receipt)
    store.save_stage(version["id"], "generated", {"title": "Native lesson"})
    store.add_reviews(version["id"], [{"id": "native", "prompt": "Native question", "answer": "A"}])
    publisher.publish_reviews(version["id"])
    before = store.reviews_for_version(version["id"])
    result = migrate_notion(config, store)
    assert result["replay"] and result["created"] == 0
    assert store.get_stage(version["id"], "published") == receipt
    assert store.reviews_for_version(version["id"]) == before
    assert publisher.repository.get_document(receipt["artifact_page_id"])["title"] == "Native lesson"


def test_linked_external_source_cannot_manufacture_in_scope_ownership(migration, monkeypatch):
    config, store, cloud = migration
    fetch = cloud.fetch

    def outside(value):
        result = fetch(value)
        if value == uid(22):
            result["text"] = result["text"].replace(
                f'<parent-page url="{url(2)}"/>',
                f'<parent-data-source url="collection://{uid(12)}"/><parent-database url="{url(99)}"/>')
        return result

    monkeypatch.setattr(cloud, "fetch", outside)
    with pytest.raises(MigrationError, match="migration_out_of_scope_descendant"):
        migrate_notion(config, store)
    assert not (Path(config["artifact"]["root"]) / "library.json").exists()
    assert store.get_meta("notion:hub") is not None


def test_partial_import_cannot_open_a_publisher_before_cutover(migration, monkeypatch):
    from ssu_lms_mcp.study_publish import PublicationError

    config, store, _ = migration

    def interrupted(*args, **kwargs):
        raise ValueError("interrupted_cutover")

    monkeypatch.setattr(store, "migrate_artifact_references", interrupted)
    with pytest.raises(MigrationError, match="interrupted_cutover"):
        migrate_notion(config, store)
    assert ArtifactRepository(config["artifact"]["root"]).get_document(uid(23))
    with pytest.raises(PublicationError, match="artifact_migration_required"):
        StudyPublisher(store, config).sync_reviews()


def test_ordinary_notion_help_link_is_retained_without_becoming_a_page(migration, monkeypatch):
    config, store, cloud = migration
    fetch = cloud.fetch

    def with_help(value):
        result = fetch(value)
        if value == uid(25):
            result["text"] = result["text"].replace("All nested personal content", "[Help](https://www.notion.so/help)")
        return result

    monkeypatch.setattr(cloud, "fetch", with_help)
    migrate_notion(config, store)
    assert "[Help](https://www.notion.so/help)" in ArtifactRepository(config["artifact"]["root"]).get_document(uid(25))["content"]
