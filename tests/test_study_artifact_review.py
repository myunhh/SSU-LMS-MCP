"""Exercise durable review intake against actual files and SQLite, including CAS races."""
from datetime import datetime, timedelta

import pytest

from ssu_lms_mcp.study_artifact_review import publish_reviews, sync_reviews
from ssu_lms_mcp.study_publish import StudyPublisher
from ssu_lms_mcp.study_store import StudyStore


class Interrupted(BaseException):
    pass


@pytest.fixture
def study(tmp_path):
    store = StudyStore(tmp_path / "study.sqlite")
    service = StudyPublisher(store, {"cloud_command": [], "artifact": {"root": str(tmp_path / "library")}})
    service.initialize([])
    yield service
    service.store.close()


def add_version(service, number=1, digest="a", generated=True):
    store = service.store
    source = {"key": f"material:7:{number}", "partition": "material:7", "kind": "material",
              "course_id": 7, "title": "자료구조", "module_name": "1주차", "course_name": "자료구조",
              "professor": "교수", "source_url": "https://canvas.ssu.ac.kr/courses/7",
              "metadata_hash": digest, "material_id": number}
    store.observe([source], [source["partition"]])
    version = store.record_version(source["key"], {
        "sha256": digest * 64, "filename": "교안.pdf", "path": "/private/교안.pdf", "size": 10,
    })
    if generated:
        store.save_stage(version["id"], "generated", {"title": "자료구조"})
    store.add_reviews(version["id"], [{"id": "recall", "prompt": "스택의 순서는?", "answer": "후입선출",
                                      "concept": "스택", "explanation": "마지막에 넣은 원소가 먼저 나온다.", "pages": [2]}],
                      now="2026-09-28T00:00:00Z")
    service._row("records", "version:" + version["id"], {"제목": "자료구조"}, "Full lesson")
    return version


def card(service, version):
    return service.store.reviews_for_version(version["id"])[0]


def page(service, version):
    return service._fetch(card(service, version)["artifact_page_id"])


def publish(service, version):
    result = publish_reviews(service, version["id"])
    assert result["failures"] == []
    return result


def edit(service, version, *, properties=None, content=None, title=None):
    document = page(service, version)
    changes = {"properties": properties} if properties is not None else {}
    if content is not None:
        changes["content"] = content
    if title is not None:
        changes["title"] = title
    return service.repository.update_document(document["id"], changes,
                                               expected_revision=document["revision"], user_edit=True)


def submit(service, version, rating="이해함", answer="후입선출"):
    return edit(service, version, properties={"평가": rating, "내답": answer, "제출": "__YES__"})


def reconnect(service, tmp_path):
    service.store.close()
    service.store = StudyStore(tmp_path / "study.sqlite")


def test_republication_keeps_drafts_notes_title_and_schedule(study):
    version = add_version(study)
    publish(study, version)
    submit(study, version, answer="선입선출")
    assert sync_reviews(study) == {"imported": 1, "failures": []}
    reviewed = card(study, version)
    submit(study, version, "애매함", "생각 중")
    before = edit(study, version, title="나의 질문", content="내 개인 필기")
    publish(study, version)
    publish(study, version)
    assert page(study, version) == before
    assert card(study, version) == reviewed
    assert len(study._rows("reviews")) == 1


def test_explicit_identical_resubmission_advances_and_unknown_resets(study):
    version = add_version(study)
    publish(study, version)
    for days in (1, 3):
        submitted = submit(study, version)
        assert sync_reviews(study) == {"imported": 1, "failures": []}
        assert card(study, version)["due_at"] == (
            datetime.fromisoformat(submitted["updated_at"]) + timedelta(days=days)).isoformat()
    submitted = submit(study, version, "모름", "빈 스택은?")
    assert sync_reviews(study)["imported"] == 1
    reviewed = card(study, version)
    assert reviewed["due_at"] == (datetime.fromisoformat(submitted["updated_at"]) + timedelta(days=1)).isoformat()
    assert [attempt["answer"] for attempt in reviewed["attempts"]] == ["후입선출", "후입선출", "빈 스택은?"]
    assert sync_reviews(study) == {"imported": 0, "failures": []}


def test_revised_source_keeps_old_history_and_other_course_cards(study):
    old, other = add_version(study), add_version(study, number=2)
    publish(study, old)
    publish(study, other)
    submit(study, old, "모름", "내 답")
    sync_reviews(study)
    previous = edit(study, old, content="지난 버전 필기")
    history = card(study, old)["attempts"]
    revised = add_version(study, digest="b")
    publish(study, revised)
    publish(study, old)
    assert page(study, old)["id"] == previous["id"]
    assert page(study, old)["content"] == "지난 버전 필기"
    assert page(study, old)["properties"]["활성"] == "__NO__"
    assert page(study, old)["properties"]["내답"] == "내 답"
    assert card(study, old)["attempts"] == history
    assert page(study, revised)["properties"]["활성"] == "__YES__"
    assert page(study, other)["properties"]["활성"] == "__YES__"
    assert revised["previous_version_id"] == old["id"]


@pytest.mark.parametrize("side", ["before", "after"])
def test_crash_around_ack_replays_once_and_preserves_new_resubmission(study, tmp_path, monkeypatch, side):
    version = add_version(study)
    publish(study, version)
    submit(study, version)
    original = study.repository.update_document
    failed = False

    def interrupt(identity, changes, **kwargs):
        nonlocal failed
        if changes.get("properties", {}).get("처리이벤트") and not failed:
            failed = True
            if side == "after":
                original(identity, changes, **kwargs)
            raise Interrupted()
        return original(identity, changes, **kwargs)

    monkeypatch.setattr(study.repository, "update_document", interrupt)
    with pytest.raises(Interrupted):
        sync_reviews(study)
    reconnect(study, tmp_path)
    if side == "after":
        submit(study, version)
    outcome = sync_reviews(study)
    assert outcome["failures"] == []
    expected = 2 if side == "after" else 1
    assert len(card(study, version)["attempts"]) == expected
    assert page(study, version)["properties"]["제출"] == "__NO__"
    assert sync_reviews(study) == {"imported": 0, "failures": []}


@pytest.mark.parametrize("committed", [False, True])
def test_write_ahead_submission_survives_attempt_commit_crash(study, tmp_path, monkeypatch, committed):
    version = add_version(study)
    publish(study, version)
    submit(study, version)
    original = study.store.record_review

    def interrupt(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise Interrupted()

    monkeypatch.setattr(study.store, "record_review", interrupt)
    with pytest.raises(Interrupted):
        sync_reviews(study)
    reconnect(study, tmp_path)
    assert sync_reviews(study)["failures"] == []
    assert [attempt["answer"] for attempt in card(study, version)["attempts"]] == ["후입선출"]
    assert page(study, version)["properties"]["제출"] == "__NO__"


def test_cas_closes_final_read_ack_race_without_clearing_new_answer(study, monkeypatch):
    version = add_version(study)
    publish(study, version)
    submit(study, version)
    original = study.repository.update_document
    raced = False

    def race(identity, changes, **kwargs):
        nonlocal raced
        if changes.get("properties", {}).get("처리이벤트") and not raced:
            raced = True
            submit(study, version, "모름", "더 최신 답")
        return original(identity, changes, **kwargs)

    monkeypatch.setattr(study.repository, "update_document", race)
    result = sync_reviews(study)
    assert result["imported"] == 1
    assert result["failures"][0]["error_type"] == "artifact_revision_conflict"
    assert page(study, version)["properties"]["내답"] == "더 최신 답"
    assert page(study, version)["properties"]["제출"] == "__YES__"
    assert sync_reviews(study)["failures"][0]["error_type"] == "review_draft_changed"
    assert sync_reviews(study) == {"imported": 1, "failures": []}
    assert [attempt["answer"] for attempt in card(study, version)["attempts"]] == ["후입선출", "더 최신 답"]


def test_pending_ack_preserves_new_unsubmitted_draft_after_restart(study, tmp_path, monkeypatch):
    version = add_version(study)
    publish(study, version)
    submit(study, version)
    original = study.store.set_meta

    def interrupt(key, value):
        if key.startswith("artifact:review:ack:"):
            raise Interrupted()
        original(key, value)

    monkeypatch.setattr(study.store, "set_meta", interrupt)
    with pytest.raises(Interrupted):
        sync_reviews(study)
    reconnect(study, tmp_path)
    before = edit(study, version, properties={"내답": "아직 제출하지 않은 답", "평가": "애매함"})
    assert sync_reviews(study) == {"imported": 0, "failures": []}
    assert page(study, version) == before
    assert len(card(study, version)["attempts"]) == 1
