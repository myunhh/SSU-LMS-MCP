"""Persistence, partial-baseline, revision, and spaced-review regressions."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ssu_lms_mcp.study_store import StudyStore


def source(number=1, course_id=7, kind="material", metadata_hash="initial", title="교안.pdf"):
    return {
        "key": f"{kind}:{course_id}:{number}",
        "partition": f"{kind}:{course_id}",
        "kind": kind,
        "course_id": course_id,
        "title": title,
        "module_name": "1주차",
        "course_name": "자료구조",
        "professor": "교수",
        "source_url": f"https://canvas.ssu.ac.kr/courses/{course_id}",
        "metadata_hash": metadata_hash,
        "material_id": number,
    }


def artifact(digest="a", path="/private/versions/a/교안.pdf"):
    return {"sha256": digest * 64, "filename": "교안.pdf", "path": path, "size": 512}


def quiz(identifier="recall"):
    return {
        "id": identifier, "prompt": "스택의 순서는?", "answer": "후입선출",
        "explanation": "마지막에 삽입한 원소를 먼저 꺼낸다.", "pages": [2, 3],
        "concept": "스택", "kind": "recall",
    }


def add_version(store, item=None, digest="a", baseline=False):
    item = item or source()
    store.observe([item], [item["partition"]])
    return store.record_version(item["key"], artifact(digest), baseline=baseline)


def test_partition_baseline_is_independent_and_survives_failed_discovery(tmp_path):
    path = tmp_path / "state.sqlite"
    material, board = source(), source(kind="board")
    with StudyStore(path) as store:
        first = store.observe([material, board], [material["partition"]])
        assert first == [{**material, "baseline": True, "metadata_changed": False}]
        assert store.source(board["key"]) is None
        assert store.observe([], []) == []

    with StudyStore(path) as store:
        later = source(number=2)
        scan = store.observe([material, later, board], [material["partition"], board["partition"]])
        assert [(row["key"], row["baseline"]) for row in scan] == [
            (material["key"], True), (later["key"], False), (board["key"], True)
        ]
        revised = {**material, "metadata_hash": "revision", "title": "수정 교안.pdf"}
        assert store.observe([revised], [material["partition"]])[0]["metadata_changed"] is True
        replay = store.observe([revised], [material["partition"]])[0]
        assert replay["baseline"] is False
        assert replay["metadata_changed"] is False
        assert {item["key"] for item in store.sources()} == {
            material["key"], later["key"], board["key"]
        }
        next_board = source(number=2, kind="board")
        assert store.observe([next_board], [board["partition"]])[0]["baseline"] is False


def test_empty_success_initializes_partition_but_failed_observation_is_atomic(tmp_path):
    item = source()
    with StudyStore(tmp_path / "state.sqlite") as store:
        store.observe([], [item["partition"]])
        assert store.observe([item], [item["partition"]])[0]["baseline"] is False
        other = source(course_id=8)
        invalid = {**source(number=2, course_id=8)}
        invalid.pop("metadata_hash")
        with pytest.raises(KeyError):
            store.observe([other, invalid], [other["partition"]])
        assert store.source(other["key"]) is None
        assert store.observe([other], [other["partition"]])[0]["baseline"] is True


def test_replay_and_failure_preserve_immutable_versions_and_completed_stages(tmp_path):
    path = tmp_path / "state.sqlite"
    evidence = {"pages": [{"page": 1, "text": "정의", "flags": []}], "complete": True}
    with StudyStore(path) as store:
        version = add_version(store, baseline=True)
        store.save_stage(version["id"], "uploaded", {"item_id": "drive-item", "path": "original.pdf"})
        store.save_stage(version["id"], "extracted", evidence)
        store.fail_stage(version["id"], "generated", "ModelTimeout")

    with StudyStore(path) as store:
        replay = store.record_version(source()["key"], artifact(path="/must/not/replace.pdf"), baseline=True)
        assert replay["id"] == version["id"]
        assert replay["path"] == artifact()["path"]
        assert replay["stages"]["extracted"] == evidence
        assert replay["errors"]["generated"]["error_type"] == "ModelTimeout"
        assert [row["id"] for row in store.versions(pending_only=True)] == [version["id"]]
        promoted = store.record_version(source()["key"], artifact(), baseline=True, force=True)
        assert promoted["baseline"] is False
        assert promoted["stages"] == replay["stages"]
        store.save_stage(version["id"], "generated", {"package": {"title": "스택"}})
        assert store.get_version(version["id"])["errors"] == {}
        store.save_stage(version["id"], "published", {"id": "artifact-page", "url": "http://127.0.0.1:8766/#/page/page"})
        assert store.versions(pending_only=True) == []
        store.fail_stage(version["id"], "published", "ArtifactUnavailable")
        assert store.get_stage(version["id"], "published")["id"] == "artifact-page"
        assert [row["id"] for row in store.versions(pending_only=True)] == [version["id"]]
        assert [row["id"] for row in store.versions()] == [version["id"]]


@pytest.mark.parametrize("upload_error", [None, "UploadConflict"])
def test_pending_upload_uses_reacquired_identity_without_rewriting_version(tmp_path, upload_error):
    path = tmp_path / "state.sqlite"
    item = source(title="lecture.pdf")
    original = {
        **artifact(), "filename": "lecture.pdf", "path": str(tmp_path / "lecture.pdf"),
    }
    corrected = {
        **original, "filename": "lecture-v2.pdf", "path": str(tmp_path / "lecture-v2.pdf"),
    }
    with StudyStore(path) as store:
        store.observe([item], [item["partition"]])
        version = store.record_version(item["key"], original)
        if upload_error:
            store.fail_stage(version["id"], "uploaded", upload_error)
        renamed = {**item, "metadata_hash": "renamed", "title": "lecture-v2.pdf"}
        store.observe([renamed], [renamed["partition"]])
        store.record_version(item["key"], corrected)

    with StudyStore(path) as store:
        retry = store.current_version(item["key"])
        assert retry["id"] == version["id"]
        assert retry["upload_artifact"] == corrected
        assert {key: retry[key] for key in original} == original
        assert retry["source"] == version["source"]
        assert retry["previous_version_id"] == version["previous_version_id"]
        store.fail_stage(version["id"], "uploaded", "UploadUnavailable")

    receipt = {"item_id": "drive-item", "path": "lecture-v2.pdf", "sha256": original["sha256"]}
    with StudyStore(path) as store:
        retry = store.get_version(version["id"])
        assert retry["upload_artifact"] == corrected
        assert retry["errors"]["uploaded"]["error_type"] == "UploadUnavailable"
        store.save_stage(version["id"], "uploaded", receipt)

    with StudyStore(path) as store:
        completed = store.get_version(version["id"])
        assert "upload_artifact" not in completed
        assert "uploaded" not in completed["errors"]
        assert completed["stages"]["uploaded"] == receipt
        assert {key: completed[key] for key in original} == original
        assert [row["id"] for row in store.versions()] == [version["id"]]


def test_completed_upload_receipt_survives_metadata_only_rename(tmp_path):
    path = tmp_path / "state.sqlite"
    receipt = {"item_id": "original-item", "path": "lecture.pdf", "sha256": "a" * 64}
    with StudyStore(path) as store:
        version = add_version(store)
        store.save_stage(version["id"], "uploaded", receipt)
        store.save_stage(version["id"], "published", {"id": "material-page"})
        before = store.get_version(version["id"])
        renamed = source(metadata_hash="renamed", title="lecture-v2.pdf")
        store.observe([renamed], [renamed["partition"]])
        acquired = {
            **artifact(), "filename": "lecture-v2.pdf", "path": str(tmp_path / "lecture-v2.pdf"),
        }
        assert store.record_version(renamed["key"], acquired) == before

    with StudyStore(path) as store:
        assert store.get_version(version["id"]) == before
        assert store.versions(pending_only=True) == []


def test_pending_upload_rename_back_to_original_drops_stale_identity(tmp_path):
    path = tmp_path / "state.sqlite"
    with StudyStore(path) as store:
        version = add_version(store)
        renamed = {**artifact(), "filename": "lecture-v2.pdf", "path": str(tmp_path / "lecture-v2.pdf")}
        store.record_version(version["key"], renamed)
        store.record_version(version["key"], artifact())

    with StudyStore(path) as store:
        retry = store.get_version(version["id"])
        assert "upload_artifact" not in retry
        assert {key: retry[key] for key in artifact()} == artifact()


def test_same_source_new_hash_never_overwrites_snapshot_or_resets_reviews(tmp_path):
    instant = "2026-09-28T07:30:00+09:00"
    with StudyStore(tmp_path / "state.sqlite") as store:
        old = add_version(store, baseline=True)
        assert store.versions(pending_only=True) == []
        store.save_stage(old["id"], "generated", {"title": "원본", "summary": []})
        card = store.add_reviews(old["id"], [quiz()], now=instant)[0]
        attempted = store.record_review(card["key"], "unknown", answer="선입선출", at=instant)
        changed = source(metadata_hash="revised", title="변경된 교안")
        store.observe([changed], [changed["partition"]])
        new = store.record_version(changed["key"], artifact("b"), baseline=True)
        assert new["id"] != old["id"]
        assert new["baseline"] is False
        assert store.get_version(old["id"])["source"]["title"] == "교안.pdf"
        assert new["source"]["title"] == "변경된 교안"
        assert store.reviews_for_version(old["id"])[0]["active"] is True
        store.save_stage(new["id"], "generated", {"title": "변경", "summary": []})
        new_card = store.add_reviews(new["id"], [quiz()], now=instant)[0]
        assert new_card["key"] != card["key"]
        old_card = store.add_reviews(old["id"], [quiz()], now="2026-10-01T00:00:00Z")[0]
        assert old_card == {**attempted, "active": False}
        store.save_stage(old["id"], "generated", {"title": "원본", "summary": []})
        assert store.reviews_for_version(old["id"])[0]["active"] is False
        assert [row["key"] for row in store.due_reviews(now=instant)] == [new_card["key"]]
        assert store.weak_reviews() == []


def test_understood_intervals_and_weak_reset_have_exact_utc_due_dates(tmp_path):
    at = "2026-09-28T07:30:00+09:00"
    expected = [
        "2026-09-28T22:30:00+00:00", "2026-10-01T22:30:00+00:00",
        "2026-10-08T22:30:00+00:00", "2026-10-22T22:30:00+00:00",
        "2026-11-21T22:30:00+00:00", "2026-12-21T22:30:00+00:00",
    ]
    with StudyStore(tmp_path / "state.sqlite") as store:
        version = add_version(store)
        card = store.add_reviews(version["id"], [quiz()], now=at)[0]
        assert card["due_at"] == "2026-09-27T22:30:00+00:00"
        for index, due in enumerate(expected):
            card = store.record_review(card["key"], "understood", at=at, event_id=f"event-{index}")
            assert card["due_at"] == due
            assert card["interval_index"] == min(index + 1, 4)
            at = due
        for rating in ("uncertain", "unknown"):
            card = store.record_review(card["key"], rating, at=at)
            assert card["interval_index"] == 0
            assert datetime.fromisoformat(card["due_at"]) == datetime.fromisoformat(at) + timedelta(days=1)
            at = card["due_at"]
        assert at == "2026-12-23T22:30:00+00:00"
        card = store.record_review(card["key"], "understood", at=at)
        assert card["due_at"] == "2026-12-24T22:30:00+00:00"
        assert card["interval_index"] == 1


def test_review_events_and_page_links_survive_replay_and_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    at = "2026-09-28T00:00:00Z"
    with StudyStore(path) as store:
        version = add_version(store)
        card = store.add_reviews(version["id"], [quiz()], now=at)[0]
        result = store.record_review(card["key"], "unknown", "큐와 혼동", at=at, event_id="artifact-edit")
        store.link_review_page(card["key"], "review-page")
        store.set_meta("artifact:material", {"page_id": "material-page", "children": ["block-1"]})

    with StudyStore(path) as store:
        replay = store.record_review(card["key"], "unknown", "큐와 혼동", at=at, event_id="artifact-edit")
        assert replay == {**result, "artifact_page_id": "review-page"}
        assert replay["attempts"] == [{
            "rating": "unknown", "answer": "큐와 혼동", "at": "2026-09-28T00:00:00+00:00",
            "event_id": "artifact-edit",
        }]
        readded = store.add_reviews(version["id"], [quiz(), quiz()], artifact_page_id="must-not-replace", now=at)
        assert readded == [replay]
        assert store.get_meta("artifact:material") == {"page_id": "material-page", "children": ["block-1"]}
        assert store.get_meta("missing", {"pending": True}) == {"pending": True}
        other = store.add_reviews(version["id"], [quiz("other")], now=at)[0]
        with pytest.raises(ValueError, match="different review"):
            store.record_review(other["key"], "understood", at=at, event_id="artifact-edit")
        assert store.reviews_for_version(version["id"])[0]["key"] == other["key"]
        assert store.reviews_for_version(version["id"])[0]["attempts"] == []


def test_due_boundary_weak_priority_answers_and_course_scope(tmp_path):
    at = datetime(2026, 9, 28, tzinfo=UTC)
    with StudyStore(tmp_path / "state.sqlite") as store:
        first = add_version(store)
        other_course = add_version(store, source(course_id=8))
        store.save_stage(first["id"], "generated", {"title": "스택"})
        store.save_stage(other_course["id"], "generated", {"title": "다른 과목"})
        understood, uncertain, unknown = store.add_reviews(
            first["id"], [quiz("one"), quiz("two"), quiz("three")], now=at
        )
        external = store.add_reviews(other_course["id"], [quiz()], now=at)[0]
        store.record_review(understood["key"], "understood", at=at)
        store.record_review(uncertain["key"], "uncertain", "잘 모르겠음", at=at)
        store.record_review(unknown["key"], "unknown", "첫 답", at=at + timedelta(seconds=1))
        store.record_review(unknown["key"], "unknown", "두 번째 답", at=at + timedelta(seconds=1))
        store.record_review(external["key"], "unknown", "다른 과목", at=at)
        assert store.due_reviews(now=at + timedelta(days=1, microseconds=-1)) == []
        due = store.due_reviews(now=at + timedelta(days=1, seconds=1))
        assert [row["key"] for row in due] == [
            external["key"], unknown["key"], uncertain["key"], understood["key"]
        ]
        weak = store.weak_reviews(course_ids=[7])
        assert [row["key"] for row in weak] == [unknown["key"], uncertain["key"]]
        assert [attempt["answer"] for attempt in weak[0]["attempts"]] == ["첫 답", "두 번째 답"]
        assert weak[0]["answer"] == "후입선출"
        assert store.weak_reviews(course_ids=[]) == []
        assert store.weak_reviews(course_ids=[7], limit=1) == weak[:1]
        assert store.due_reviews(now=at + timedelta(days=2), limit=0) == []


def test_naive_timestamps_and_invalid_ratings_do_not_change_review(tmp_path):
    with StudyStore(tmp_path / "state.sqlite") as store:
        version = add_version(store)
        with pytest.raises(ValueError, match="timezone-aware"):
            store.add_reviews(version["id"], [quiz()], now=datetime(2026, 9, 28))
        assert store.reviews_for_version(version["id"]) == []
        card = store.add_reviews(version["id"], [quiz()], now="2026-09-28T00:00:00Z")[0]
        with pytest.raises(ValueError, match="timezone-aware"):
            store.record_review(card["key"], "unknown", at="2026-09-28T00:00:00")
        with pytest.raises(ValueError, match="rating"):
            store.record_review(card["key"], "good", at="2026-09-28T00:00:00Z")
        assert store.reviews_for_version(version["id"]) == [card]


def test_related_materials_choose_latest_generated_same_course_and_stable_link(tmp_path):
    with StudyStore(tmp_path / "state.sqlite") as store:
        old = add_version(store)
        store.save_stage(old["id"], "generated", {"package": {"title": "이전", "summary": []}})
        current = store.record_version(source()["key"], artifact("b"))
        other = add_version(store, source(number=2))
        store.save_stage(other["id"], "generated", {"title": "연결 개념", "summary": []})
        wrong_course = add_version(store, source(course_id=8))
        store.save_stage(wrong_course["id"], "generated", {"title": "다른 과목"})
        assert {row["version_id"] for row in store.related_materials(7)} == {old["id"], other["id"]}
        package = {"title": "수정본", "summary": [{"text": "정의", "pages": [1], "evidence": "source"}]}
        store.save_stage(current["id"], "generated", {"package": package})
        store.save_stage(current["id"], "published", {"artifact_url": "http://127.0.0.1:8766/#/page/current"})
        assert store.related_materials(7, exclude_key=other["key"]) == [{
            "key": current["key"], "title": "수정본", "package": package,
            "version_id": current["id"], "artifact_url": "http://127.0.0.1:8766/#/page/current",
        }]
        assert store.related_materials(7, limit=0) == []


def test_restored_hash_reuses_history_and_only_reconciles_publication(tmp_path):
    path = tmp_path / "state.sqlite"
    key = source()["key"]
    with StudyStore(path) as store:
        original = add_version(store, baseline=True)
        stages = {
            "uploaded": {"item_id": "original-upload"},
            "extracted": {"complete": True, "pages": [{"page": 1, "text": "스택"}]},
            "generated": {"package": {"title": "원본"}},
            "published": {"version_page_id": "original-page"},
        }
        for stage, data in stages.items():
            store.save_stage(original["id"], stage, data)
        card = store.add_reviews(original["id"], [quiz()], now="2026-09-28T00:00:00Z")[0]
        store.link_review_page(card["key"], "review-page")
        attempted = store.record_review(
            card["key"], "understood", "내 답", at="2026-09-28T01:00:00Z", event_id="attempt-a"
        )
        store.fail_stage(original["id"], "published", "ArtifactUnavailable")
        errors = store.get_version(original["id"])["errors"]
        other = store.record_version(key, artifact("b"))
        store.save_stage(other["id"], "generated", {"package": {"title": "수정본"}})
        other_card = store.add_reviews(other["id"], [quiz()], now="2026-09-29T00:00:00Z")[0]
        restored = store.record_version(key, artifact(path="/new-download.pdf"), baseline=True)
        assert restored["id"] == original["id"]
        assert restored["is_current"] is True
        assert restored["baseline"] is False
        assert restored["path"] == original["path"]
        assert restored["source"] == original["source"]
        assert restored["stages"] == {name: data for name, data in stages.items() if name != "published"}
        assert restored["errors"] == errors
        assert store.reviews_for_version(original["id"]) == [attempted]
        assert store.reviews_for_version(other["id"]) == [{**other_card, "active": False}]
        assert store.related_materials(7) == [{
            "key": key, "title": "원본", "package": stages["generated"]["package"],
            "version_id": original["id"],
        }]

    with StudyStore(path) as store:
        assert [(v["id"], v["is_current"]) for v in store.versions()] == [
            (original["id"], True), (other["id"], False)
        ]
        current = store.current_version(key)
        assert current == restored
        forced = store.record_version(key, current, force=True)
        assert forced == restored
        assert store.reviews_for_version(original["id"]) == [attempted]
        assert store.current_version("missing") is None
        assert original["id"] in {v["id"] for v in store.versions(pending_only=True)}
        store.save_stage(original["id"], "published", stages["published"])
        unchanged = store.record_version(key, artifact(path="/another-download.pdf"))
        assert unchanged["stages"] == stages
        assert unchanged["errors"] == {}
        assert original["id"] not in {v["id"] for v in store.versions(pending_only=True)}


def test_new_revision_captures_restored_head_and_preserves_pending_lineage(tmp_path):
    path = tmp_path / "state.sqlite"
    with StudyStore(path) as store:
        original = add_version(store)
        key = original["key"]
        other = store.record_version(key, artifact("b"))
        restored = store.record_version(key, artifact("a"))
        assert original["previous_version_id"] is None
        assert other["previous_version_id"] == original["id"]
        assert restored["previous_version_id"] is None

    with StudyStore(path) as store:
        current = store.record_version(key, artifact("c"))
        assert current["previous_version_id"] == original["id"]
        assert store.get_version(current["previous_version_id"])["sha256"] == original["sha256"]
        expected = {
            original["id"]: None,
            other["id"]: original["id"],
            current["id"]: original["id"],
        }

    with StudyStore(path) as store:
        assert store.current_version(key)["id"] == current["id"]
        assert {
            version["id"]: version["previous_version_id"]
            for version in store.versions(pending_only=True)
        } == expected
        restored = store.record_version(key, artifact("b"), force=True)
        assert restored["previous_version_id"] == original["id"]
        assert {
            version["id"]: version["previous_version_id"] for version in store.versions()
        } == expected


def test_historical_generation_cannot_steal_active_reviews_from_pending_current(tmp_path):
    path = tmp_path / "state.sqlite"
    at = "2026-09-28T00:00:00Z"
    with StudyStore(path) as store:
        original = add_version(store)
        store.save_stage(original["id"], "generated", {"title": "원본"})
        card = store.add_reviews(original["id"], [quiz()], now=at)[0]
        historical = store.record_version(original["key"], artifact("b"))
        current = store.record_version(original["key"], artifact("c"))
        store.save_stage(historical["id"], "generated", {"title": "늦은 과거 생성"})
        old_card = store.add_reviews(historical["id"], [quiz()], now=at)[0]
        assert old_card["active"] is False
        assert store.current_version(original["key"])["id"] == current["id"]
        assert store.due_reviews(now=at) == [card]
        assert [v["version_id"] for v in store.related_materials(7)] == [original["id"]]

    with StudyStore(path) as store:
        assert store.due_reviews(now=at) == [card]
        store.save_stage(current["id"], "generated", {"title": "현재"})
        new_card = store.add_reviews(current["id"], [quiz()], now=at)[0]
        assert store.due_reviews(now=at) == [new_card]
        store.save_stage(historical["id"], "generated", {"title": "다시 생성된 과거"})
        assert store.due_reviews(now=at) == [new_card]
        assert store.reviews_for_version(original["id"]) == [{**card, "active": False}]


def test_ungenerated_restoration_keeps_last_active_head_until_generated(tmp_path):
    with StudyStore(tmp_path / "state.sqlite") as store:
        original = add_version(store, baseline=True)
        other = store.record_version(original["key"], artifact("b"))
        store.save_stage(other["id"], "generated", {"title": "수정본"})
        card = store.add_reviews(other["id"], [quiz()], now="2026-09-28T00:00:00Z")[0]
        restored = store.record_version(original["key"], artifact())
        assert restored["is_current"] is True
        assert restored["baseline"] is False
        assert store.reviews_for_version(other["id"]) == [card]
        assert [v["version_id"] for v in store.related_materials(7)] == [other["id"]]
        store.save_stage(original["id"], "generated", {"title": "원본"})
        new_card = store.add_reviews(original["id"], [quiz()], now="2026-09-29T00:00:00Z")[0]
        assert new_card["active"] is True
        assert store.reviews_for_version(other["id"]) == [{**card, "active": False}]


def test_legacy_database_migrates_latest_acquired_and_latest_generated_heads(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with StudyStore(path) as store:
        original = add_version(store)
        store.save_stage(original["id"], "generated", {"title": "원본"})
        card = store.add_reviews(original["id"], [quiz()], now="2026-09-28T00:00:00Z")[0]
        attempted = store.record_review(
            card["key"], "unknown", "이전 답", at="2026-09-28T01:00:00Z", event_id="legacy"
        )
        original_snapshot = store.get_version(original["id"])
        ungenerated = add_version(store, source(number=2))
        current = store.record_version(original["key"], artifact("b"))
    # Remove only the new schema additions; immutable versions and learner data
    # stay in place while migration reconstructs conservative insertion lineage.
    with sqlite3.connect(path) as database:
        database.execute("DROP TABLE version_heads")
        database.execute("ALTER TABLE versions DROP COLUMN previous_version_id")
    with StudyStore(path) as store:
        assert store.current_version(original["key"])["id"] == current["id"]
        assert store.current_version(ungenerated["key"])["id"] == ungenerated["id"]
        assert store.get_version(original["id"]) == {**original_snapshot, "is_current": False}
        assert store.get_version(current["id"])["previous_version_id"] == original["id"]
        assert store.get_version(ungenerated["id"])["previous_version_id"] is None
        assert store.reviews_for_version(original["id"]) == [attempted]
        assert [v["version_id"] for v in store.related_materials(7)] == [original["id"]]
        assert store.add_reviews(
            ungenerated["id"], [quiz()], now="2026-09-28T00:00:00Z"
        )[0]["active"] is False
        store.record_version(original["key"], artifact())
    with StudyStore(path) as store:
        assert store.current_version(original["key"])["id"] == original["id"]
        assert store.reviews_for_version(original["id"]) == [attempted]
        assert store.get_version(original["id"]) == original_snapshot
        assert store.get_version(current["id"])["previous_version_id"] == original["id"]


def test_delayed_review_intake_replays_schedule_in_event_order(tmp_path):
    with StudyStore(tmp_path / "state.sqlite") as store:
        version = add_version(store)
        card = store.add_reviews(version["id"], [quiz()], now="2026-09-28T00:00:00Z")[0]
        store.record_review(
            card["key"], "understood", "새 답", at="2026-09-30T00:00:00Z", event_id="newer"
        )
        replayed = store.record_review(
            card["key"], "understood", "지연된 답", at="2026-09-29T00:00:00Z", event_id="older"
        )
        assert replayed["rating"] == "understood"
        assert replayed["interval_index"] == 2
        assert replayed["due_at"] == "2026-10-03T00:00:00+00:00"
        assert [a["event_id"] for a in replayed["attempts"]] == ["older", "newer"]
        reset = store.record_review(
            card["key"], "unknown", "중간 답", at="2026-09-29T12:00:00Z", event_id="middle"
        )
        assert reset["rating"] == "understood"
        assert reset["interval_index"] == 1
        assert reset["due_at"] == "2026-10-01T00:00:00+00:00"
        assert [a["answer"] for a in reset["attempts"]] == ["지연된 답", "중간 답", "새 답"]
        assert store.record_review(
            card["key"], "unknown", "중간 답", at="2026-09-29T12:00:00Z", event_id="middle"
        ) == reset


def test_review_column_schema_migration_preserves_history_and_takes_prechange_backup(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with StudyStore(path) as store:
        version = add_version(store)
        store.save_stage(version["id"], "generated", {"title": "Lesson"})
        review = store.add_reviews(version["id"], [quiz()], artifact_page_id="original-page")[0]
        expected = store.record_review(review["key"], "uncertain", "All original answer",
                                       at="2026-09-28T01:00:00Z", event_id="legacy-event")
        store.set_meta("notion:review:pending:" + review["key"], {"answer": "Unacknowledged draft"})
        version_before = store.get_version(version["id"])
    with sqlite3.connect(path) as database:
        database.execute("ALTER TABLE reviews RENAME COLUMN artifact_page_id TO notion_page_id")
    with StudyStore(path) as store:
        assert store.reviews_for_version(version["id"]) == [expected]
        assert store.get_version(version["id"]) == version_before
        assert store.get_meta("notion:review:pending:" + review["key"]) == {"answer": "Unacknowledged draft"}
    with sqlite3.connect(str(path) + ".pre-artifact.sqlite3") as backup:
        assert backup.execute("SELECT notion_page_id FROM reviews").fetchone()[0] == "original-page"
        assert backup.execute("SELECT answer FROM review_attempts").fetchone()[0] == "All original answer"
    with sqlite3.connect(path) as current:
        columns = {row[1] for row in current.execute("PRAGMA table_info(reviews)")}
        assert "artifact_page_id" in columns and "notion_page_id" not in columns


def test_cutover_metadata_conflict_rolls_back_every_reference_and_attempt(tmp_path):
    with StudyStore(tmp_path / "state.sqlite") as store:
        version = add_version(store)
        review = store.add_reviews(version["id"], [quiz()])[0]
        store.record_review(review["key"], "unknown", "Preserved", event_id="notion:event")
        store.set_meta("notion:hub", {"hub_id": "hub"})
        store.set_meta("notion:row:records:one", {"id": "first"})
        store.set_meta("artifact:row:records:one", {"id": "different"})
        store.save_stage(version["id"], "published", {"notion_page_id": "page"})
        with pytest.raises(ValueError, match="artifact_metadata_conflict"):
            store.migrate_artifact_references(
                {"hub": {"url": "http://127.0.0.1:8766/#/page/hub"}},
                {"hub_id": "hub", "collections": {}}, {"verified": True})
        assert store.get_meta("notion:hub") == {"hub_id": "hub"}
        assert store.get_meta("artifact:hub") is None
        assert store.get_stage(version["id"], "published") == {"notion_page_id": "page"}
        assert store.reviews_for_version(version["id"])[0]["attempts"][0]["event_id"] == "notion:event"


def test_incomplete_prechange_backup_never_allows_schema_cutover(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with StudyStore(path):
        pass
    with sqlite3.connect(path) as database:
        database.execute("ALTER TABLE reviews RENAME COLUMN artifact_page_id TO notion_page_id")
    backup = tmp_path / "legacy.sqlite.pre-artifact.sqlite3"
    backup.write_bytes(b"")
    with pytest.raises(ValueError, match="study_backup_invalid"):
        StudyStore(path)
    with sqlite3.connect(path) as database:
        assert "notion_page_id" in {row[1] for row in database.execute("PRAGMA table_info(reviews)")}
    assert backup.read_bytes() == b""


def test_interrupted_backup_has_no_completed_destination(tmp_path, monkeypatch):
    with StudyStore(tmp_path / "state.sqlite") as store:
        version = add_version(store)
        destination = tmp_path / "snapshot.sqlite"

        def interrupted(*args):
            raise OSError("interrupted publication")

        with monkeypatch.context() as patch:
            patch.setattr("ssu_lms_mcp.study_store.os.link", interrupted)
            with pytest.raises(OSError):
                store.backup(destination)
        assert not destination.exists()
        store.backup(destination)
        with sqlite3.connect(destination) as snapshot:
            assert snapshot.execute("SELECT id FROM versions").fetchone()[0] == version["id"]
