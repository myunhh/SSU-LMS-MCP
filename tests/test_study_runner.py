"""Consumer-visible state transitions using real SQLite and real PDF evidence."""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from filelock import FileLock

from ssu_lms_mcp import study_runner as runner
from ssu_lms_mcp.study_store import StudyStore


def pdf_bytes(text):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), text)
        return document.tobytes()


def descriptor(number=1, course_id=7):
    return {
        "key": f"material:{course_id}:{number}", "partition": f"material:{course_id}",
        "kind": "material", "course_id": course_id, "material_id": number,
        "title": f"Lecture {number}", "module_name": "Week 1", "course_name": f"Course {course_id}",
        "professor": "", "source_url": f"https://canvas.ssu.ac.kr/courses/{course_id}/modules/items/{number}",
        "metadata_hash": "unchanged",
    }


@pytest.fixture
def study(tmp_path, monkeypatch):
    bridge = tmp_path / "cloud.mjs"
    bridge.write_text("", encoding="utf-8")
    config = {
        "schema_version": 1, "data_dir": str(tmp_path / "study"),
        "lms": {"data_dir": str(tmp_path / "lms"), "materials_dir": str(tmp_path / "originals")},
        "artifact": {"root": str(tmp_path / "artifacts"), "port": 8766},
        "course_ids": [7, 8], "cloud_command": [sys.executable, str(bridge)],
        "model": {"command": sys.executable, "model": "test-boundary", "thinking": "high",
                  "timeout_seconds": 60, "pages_per_batch": 8},
        "schedule": {"time": "07:30", "timezone": "Asia/Seoul"},
    }
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    state = SimpleNamespace(config_path=config_path, config=config, items=[], data={},
                            failed_partitions=set(), acquisition_failures=set(), acquisition_skips={},
                            review_failures=set(), fail_all_reviews=False, exam_failure=False,
                            sync_failure=None, artifact_submissions=[],
                            originals={}, canonical={}, children={}, reviews={}, briefings={},
                            notifications=[], model_runs=[], exam_runs=[], exams={})

    def add(number=1, course_id=7, text="Original lecture"):
        source = descriptor(number, course_id)
        state.items.append(source)
        state.data[source["key"]] = pdf_bytes(text)
        return source

    state.add = add

    async def discover(settings, course_ids):
        partitions = {f"{kind}:{cid}" for cid in course_ids for kind in ("material", "board")}
        successful = partitions - state.failed_partitions
        return {
            "courses": [{"id": str(cid), "name": f"Course {cid}", "professor": ""} for cid in course_ids],
            "items": [row for row in state.items if row["partition"] in successful],
            "successful_partitions": sorted(successful),
            "errors": [{"partition": key, "error_type": "session_required"} for key in sorted(state.failed_partitions)],
        }

    async def acquire(settings, source):
        key = source["key"]
        if key in state.acquisition_failures:
            return {"status": "failed", "key": key, "reason": "download_failed"}
        if key in state.acquisition_skips:
            return {"status": "skipped", "key": key, "reason": state.acquisition_skips[key]}
        raw = state.data[key]
        digest = hashlib.sha256(raw).hexdigest()
        path = settings.materials_dir / str(source["course_id"]) / digest / "lecture.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(raw)
        return {"status": "saved", "key": key, "sha256": digest,
                "filename": path.name, "path": str(path), "size": len(raw)}

    def package(bundle, course, previous, related, model_config, output_dir):
        state.model_runs.append({"hash": bundle["source_sha256"], "previous": previous})
        return {"title": "Study " + bundle["pages"][0]["text"].strip(),
                "source_sha256": bundle["source_sha256"], "page_count": bundle["page_count"],
                "quizzes": [{"id": "q1", "prompt": "Recall the definition", "answer": "Definition",
                             "explanation": "Source evidence", "pages": [1], "concept": "Definition"}],
                "review_required": False, "review_reasons": []}

    def exam(packages, scope, reviews, model_config, output_dir):
        state.exam_runs.append({"keys": [row["key"] for row in packages], "reviews": reviews,
                                "hashes": [row["package"]["source_sha256"] for row in packages]})
        return {"title": scope["title"], "scope": scope, "review_required": False,
                "review_reasons": [], "sources": [{"source_key": row["key"], "pages": [1]} for row in packages]}

    class Publisher:
        def __init__(self, store, config):
            self.store = store

        def __enter__(self):
            # A publisher in a model worker thread would violate SQLite affinity.
            self.store.get_meta("runner:hub")
            return self

        def __exit__(self, *args):
            pass

        def initialize(self, courses):
            return {"hub_id": "11111111-1111-4111-8111-111111111111",
                    "hub_url": "http://127.0.0.1:8766/#/page/11111111-1111-4111-8111-111111111111"}

        def upload(self, version):
            raw = Path(version["path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == version["sha256"]
            state.originals.setdefault(version["sha256"], raw)
            return {"status": "saved", "sha256": version["sha256"], "itemId": version["id"]}

        def publish_baseline(self, version, uploaded):
            if not version["is_latest"]:
                return {"baseline": bool(version["baseline"]), "historical": True}
            state.canonical[version["key"]] = {"id": version["id"], "baseline": bool(version["baseline"]),
                                              "review_required": version.get("review_required", False)}
            return {"artifact_page_id": version["key"], "artifact_url": "http://127.0.0.1:8766/#/page/22222222-2222-4222-8222-222222222222",
                    "baseline": bool(version["baseline"]), "review_required": version.get("review_required", False)}

        def publish(self, version, package, uploaded, related):
            state.children[version["id"]] = package
            if version["is_latest"]:
                state.canonical[version["key"]] = {"id": version["id"], "baseline": False}
            return {"artifact_page_id": version["key"], "artifact_url": "http://127.0.0.1:8766/#/page/22222222-2222-4222-8222-222222222222",
                    "version_page_id": version["id"]}

        def publish_reviews(self, version_id):
            if state.fail_all_reviews or version_id in state.review_failures:
                raise OSError("must never disclose a signed upstream URL or model stdout")
            for row in self.store.reviews_for_version(version_id):
                state.reviews[row["key"]] = row
            return {"published": len(self.store.reviews_for_version(version_id))}

        def sync_reviews(self):
            if state.sync_failure == "exception":
                raise OSError("private artifact reconciliation failure")
            if state.sync_failure:
                return {"imported": 0, "failures": [{"error_type": "review_sync_failed"}]}
            imported = 0
            while state.artifact_submissions:
                row = self.store.record_review(**state.artifact_submissions[0])
                state.reviews[row["key"]] = row
                state.artifact_submissions.pop(0)
                imported += 1
            return {"imported": imported, "failures": []}

        def briefing(self, report):
            state.briefings[report["date"]] = json.loads(json.dumps(report))
            return {"artifact_page_id": report["date"], "artifact_url": "http://127.0.0.1:8766/#/page/33333333-3333-4333-8333-333333333333"}

        def publish_exam(self, exam, key):
            if state.exam_failure:
                raise OSError("private cloud failure")
            state.exams[key] = exam
            return {"artifact_page_id": key, "artifact_url": "http://127.0.0.1:8766/#/page/44444444-4444-4444-8444-444444444444"}

    def notify(title, body):
        state.notifications.append((title, body))
        return True

    monkeypatch.setattr(runner, "discover", discover)
    monkeypatch.setattr(runner, "acquire", acquire)
    monkeypatch.setattr(runner, "build_package", package)
    monkeypatch.setattr(runner, "build_exam", exam)
    monkeypatch.setattr(runner, "StudyPublisher", Publisher)
    monkeypatch.setattr(runner, "notify", notify)
    monkeypatch.setattr(runner, "_now", lambda: datetime(2026, 9, 28, 8, 0, tzinfo=runner.SEOUL))
    return state


def open_store(study):
    return StudyStore(Path(study.config["data_dir"]) / "study.sqlite3")


@pytest.fixture
def flat_drive(study, monkeypatch):
    from ssu_lms_mcp.study_publish import PublicationError, StudyPublisher

    class Drive:
        def __init__(self):
            self.files = {}
            self.offline = False

        def request(self, action, **data):
            assert action == "onedrive-upload"
            if self.offline:
                raise PublicationError("cloud_unavailable")
            artifact = data["artifact"]
            raw = Path(artifact["path"]).read_bytes()
            name = artifact["filename"]
            existing = self.files.get(name)
            if existing is not None and existing != raw:
                raise PublicationError("onedrive_name_conflict")
            self.files[name] = raw
            return {"status": "exists" if existing is not None else "saved",
                    "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                    "itemId": name, "onedrivePath": "Course/" + name}

    drive = Drive()

    class Publisher(runner.StudyPublisher):
        upload = StudyPublisher.upload

        def __init__(self, store, config):
            super().__init__(store, config)
            self.bridge = drive

    monkeypatch.setattr(runner, "StudyPublisher", Publisher)
    return drive


@pytest.mark.parametrize("acquisition_outage", [False, True])
async def test_forced_retry_keeps_verified_renamed_upload(study, flat_drive, monkeypatch, acquisition_outage):
    source = study.add()
    flat_drive.files["lecture.pdf"] = b"Existing different original"
    failed = await runner.run_study(study.config_path)
    assert failed["failures"][0]["error_type"] == "onedrive_name_conflict"
    acquire = runner.acquire

    async def renamed(settings, item):
        artifact = await acquire(settings, item)
        if artifact["status"] == "saved":
            path = Path(artifact["path"]).with_name("lecture-v2.pdf")
            path.write_bytes(Path(artifact["path"]).read_bytes())
            artifact.update(filename=path.name, path=str(path))
        return artifact

    monkeypatch.setattr(runner, "acquire", renamed)
    if acquisition_outage:
        flat_drive.offline = True
        assert (await runner.run_study(study.config_path))["status"] == "partial"
        flat_drive.offline = False
        study.acquisition_failures.add(source["key"])
    result = await runner.run_study(study.config_path, keys=[source["key"]])
    assert result["status"] == ("partial" if acquisition_outage else "done")
    assert flat_drive.files == {
        "lecture.pdf": b"Existing different original", "lecture-v2.pdf": study.data[source["key"]],
    }
    with open_store(study) as store:
        version = store.current_version(source["key"])
        assert version["filename"] == "lecture.pdf"
        assert version["stages"]["uploaded"]["itemId"] == "lecture-v2.pdf"
        assert version["stages"]["published"]["version_page_id"] == version["id"]
        assert len(store.reviews_for_version(version["id"])) == 1


@pytest.mark.parametrize("new_revision", [False, True])
async def test_unverified_pending_original_stays_blocked_until_its_identity_is_refreshed(
        study, flat_drive, monkeypatch, new_revision):
    source = study.add()
    acquire = runner.acquire
    artifact = await acquire(runner._settings(study.config), source)
    with open_store(study) as store:
        store.observe([source], [source["partition"]])
        legacy = store.record_version(source["key"], {**artifact, "filename": "자료"}, baseline=True)
        store.fail_stage(legacy["id"], "uploaded", "cloud_unavailable")

    async def unverified(settings, item):
        return {"status": "review_required", "key": item["key"], "reason": "original_filename_unverified"}

    monkeypatch.setattr(runner, "acquire", unverified)
    blocked = await runner.run_study(study.config_path)
    assert blocked["status"] == "partial"
    assert not flat_drive.files
    with open_store(study) as store:
        assert store.get_version(legacy["id"])["errors"]["uploaded"]["error_type"] == "original_filename_unverified"
    monkeypatch.setattr(runner, "acquire", acquire)
    study.acquisition_failures.add(source["key"])
    assert (await runner.run_study(study.config_path, keys=[source["key"]]))["status"] == "partial"
    assert not flat_drive.files
    study.acquisition_failures.clear()
    if new_revision:
        study.data[source["key"]] = pdf_bytes("A new independently named source revision")
    recovered = await runner.run_study(study.config_path)
    assert recovered["status"] == ("partial" if new_revision else "done")
    assert flat_drive.files == {"lecture.pdf": study.data[source["key"]]}
    with open_store(study) as store:
        current = store.current_version(source["key"])
        assert current["stages"]["uploaded"]["itemId"] == "lecture.pdf"
        if new_revision:
            old = store.get_version(legacy["id"])
            assert "uploaded" not in old["stages"]
            assert old["errors"]["uploaded"]["error_type"] == "original_filename_unverified"


async def test_first_baseline_archives_without_model_and_same_id_bytes_create_revision(study):
    source = study.add()
    initialized = await runner.initialize_study(study.config_path)
    assert initialized["baseline"] == "metadata_only"
    with open_store(study) as store:
        assert store.versions() == []
    baseline = await runner.run_study(study.config_path, scheduled=True)
    assert baseline["status"] == "done"
    assert study.model_runs == []
    with open_store(study) as store:
        old = store.versions()[0]
        assert old["baseline"] is True
        assert set(old["stages"]) == {"uploaded", "published"}
        assert store.reviews_for_version(old["id"]) == []
    original = study.data[source["key"]]
    study.data[source["key"]] = pdf_bytes("Changed definition; same LMS ID and filename")
    revised = await runner.run_study(study.config_path)
    assert revised["status"] == "done"
    with open_store(study) as store:
        before, after = store.versions()
        assert after["sha256"] != before["sha256"]
        assert after["baseline"] is False
        assert set(after["stages"]) == {"uploaded", "extracted", "generated", "published"}
        assert study.model_runs[0]["previous"]["bundle"] == before["stages"]["extracted"]
        assert len(store.reviews_for_version(after["id"])) == 1
    assert study.originals[old["sha256"]] == original
    assert study.canonical[source["key"]]["id"] == after["id"]
    assert len(study.briefings) == 1
    assert len(study.notifications) == 1


async def test_baseline_download_failure_does_not_turn_retry_into_mass_analysis(study):
    first, second = study.add(), study.add(course_id=8)
    study.acquisition_failures.add(first["key"])
    failed = await runner.run_study(study.config_path, scheduled=True)
    assert failed["status"] == "partial"
    assert {row["course_id"]: row["status"] for row in failed["courses"]} == {7: "partial", 8: "done"}
    with open_store(study) as store:
        assert store.get_meta("runner:successful_date") is None
        assert [row["key"] for row in store.versions()] == [second["key"]]
    study.acquisition_failures.clear()
    recovered = await runner.run_study(study.config_path, scheduled=True)
    assert recovered["status"] == "done"
    assert study.model_runs == []
    with open_store(study) as store:
        assert all(row["baseline"] for row in store.versions())
        assert store.get_meta("runner:successful_date") == "2026-09-28"


async def test_forced_baseline_review_failure_resumes_during_failed_scan_without_regression(study):
    source = study.add()
    await runner.run_study(study.config_path)
    with open_store(study) as store:
        old_id = store.versions()[0]["id"]
    study.review_failures.add(old_id)
    failed = await runner.run_study(study.config_path, keys=[source["key"]])
    assert failed["status"] == "partial"
    with open_store(study) as store:
        old = store.get_version(old_id)
        assert old["baseline"] is False
        assert old["stages"]["published"]["baseline"] is True
        assert "generated" in old["stages"]
        assert old["errors"]["published"]["error_type"] == "io_error"
        assert store.get_meta("runner:successful_date") is None
    study.data[source["key"]] = pdf_bytes("A newer version can finish independently")
    await runner.run_study(study.config_path)
    with open_store(study) as store:
        newest = store.versions()[-1]
        assert "published" in newest["stages"]
    study.review_failures.clear()
    study.failed_partitions = {"material:7", "board:7", "material:8", "board:8"}
    recovered = await runner.run_study(study.config_path)
    assert recovered["status"] == "partial"  # Failed discovery is never reported as success.
    with open_store(study) as store:
        old = store.get_version(old_id)
        assert old["errors"] == {}
        assert not old["stages"]["published"].get("baseline", False)
        assert len(store.reviews_for_version(old_id)) == 1
        assert store.reviews_for_version(old_id)[0]["active"] is False
        assert store.get_meta("runner:successful_date") is None
    assert study.canonical[source["key"]]["id"] == newest["id"]
    assert len(study.model_runs) == 2
    assert "private" not in json.dumps(recovered)


async def test_incomplete_pdf_remains_visible_and_retryable_without_learning_package(study):
    source = study.add()
    study.data[source["key"]] = b"%PDF-1.7\nnot a complete PDF"
    result = await runner.run_study(study.config_path, keys=[source["key"]])
    assert result["status"] == "partial"
    assert result["materials"][0]["status"] == "review_required"
    assert study.canonical[source["key"]]["review_required"] is True
    assert study.model_runs == []
    assert study.children == {}
    with open_store(study) as store:
        version = store.versions()[0]
        assert version["stages"]["extracted"]["complete"] is False
        assert "generated" not in version["stages"]
        assert "published" not in version["stages"]
        assert version["errors"]["extracted"]["error_type"] == "document_review_required"
        assert store.get_meta("runner:successful_date") is None
    repeated = await runner.run_study(study.config_path)
    assert repeated["status"] == "partial"
    assert len(study.notifications) == 1


async def test_extraction_recovery_publishes_study_page_after_nonbaseline_metadata(study, monkeypatch):
    source = study.add()

    def failed_render(*args, **kwargs):
        raise OSError("Temporary renderer failure")

    with monkeypatch.context() as patch:
        patch.setattr(fitz.Page, "get_pixmap", failed_render)
        failed = await runner.run_study(study.config_path, keys=[source["key"]])
    assert failed["status"] == "partial"
    assert failed["materials"][0]["status"] == "review_required"
    with open_store(study) as store:
        version = store.current_version(source["key"])
        assert version["baseline"] is False
        assert version["stages"]["extracted"]["review_reasons"] == ["page_1:page_render_failed"]
        checkpoint = store.get_meta("runner:material:" + version["id"])
        assert checkpoint["baseline"] is False
        assert "version_page_id" not in checkpoint
    recovered = await runner.run_study(study.config_path)
    assert recovered["status"] == "done"
    assert recovered["materials"][0]["status"] == "published"
    assert study.children[version["id"]]["source_sha256"] == version["sha256"]
    with open_store(study) as store:
        current = store.current_version(source["key"])
        assert current["id"] == version["id"]
        assert current["errors"] == {}
        assert current["stages"]["published"]["version_page_id"] == version["id"]
        review = store.reviews_for_version(version["id"])[0]
        assert study.reviews[review["key"]]["version_id"] == version["id"]


async def test_review_publish_outage_keeps_answer_and_recovers_without_duplicate_attempt(study):
    source = study.add()
    await runner.run_study(study.config_path, keys=[source["key"]])
    with open_store(study) as store:
        version = store.versions()[0]
        review_key = store.reviews_for_version(version["id"])[0]["key"]
    study.fail_all_reviews = True
    attempted = await runner.review_study(study.config_path, review_key=review_key,
                                          rating="unknown", answer="My original typed answer")
    assert attempted["status"] == "partial"
    assert attempted["review"]["attempts"][0]["answer"] == "My original typed answer"
    study.fail_all_reviews = False
    assert (await runner.run_study(study.config_path))["status"] == "done"
    with open_store(study) as store:
        row = store.reviews_for_version(version["id"])[0]
        assert len(row["attempts"]) == 1
        assert row["rating"] == "unknown"
        assert store.get_meta("runner:review_pending") == []
    assert study.reviews[review_key]["attempts"] == row["attempts"]


async def test_crashed_review_intake_replays_one_event_exactly_once(study):
    source = study.add()
    await runner.run_study(study.config_path, keys=[source["key"]])
    with open_store(study) as store:
        version = store.versions()[0]
        review_key = store.reviews_for_version(version["id"])[0]["key"]
        event = {"review_key": review_key, "rating": "understood", "answer": "Evidence",
                 "at": "2026-09-28T08:00:00+09:00", "event_id": "interrupted-intake"}
        store.set_meta("runner:review_intake", event)
        expected = store.record_review(**event)
    await runner.run_study(study.config_path)
    with open_store(study) as store:
        row = store.reviews_for_version(version["id"])[0]
        assert row["attempts"] == expected["attempts"]
        assert row["due_at"] == expected["due_at"]
        assert row["interval_index"] == 1
        assert store.get_meta("runner:review_intake") is None


async def test_older_artifact_submission_cannot_override_newer_cli_rating(study):
    source = study.add()
    await runner.run_study(study.config_path, keys=[source["key"]])
    with open_store(study) as store:
        review_key = store.reviews_for_version(store.current_version(source["key"])["id"])[0]["key"]
    study.artifact_submissions.append({
        "review_key": review_key, "rating": "unknown", "answer": "Earlier artifact answer",
        "at": "2026-09-28T07:45:00+09:00", "event_id": "earlier-artifact",
    })
    result = await runner.review_study(
        study.config_path, review_key=review_key, rating="understood", answer="Newer CLI answer",
    )
    assert result["status"] == "done"
    assert result["review"]["rating"] == "understood"
    assert [row["answer"] for row in result["review"]["attempts"]] == [
        "Earlier artifact answer", "Newer CLI answer",
    ]
    assert result["review"]["due_at"] == "2026-09-28T23:00:00+00:00"
    assert study.reviews[review_key]["rating"] == "understood"


@pytest.mark.parametrize("failure", ["exception", "result"])
async def test_review_sync_outage_keeps_all_cli_intents_until_reconciled(study, failure, monkeypatch):
    source = study.add()
    await runner.run_study(study.config_path, keys=[source["key"]])
    with open_store(study) as store:
        version = store.current_version(source["key"])
        review_key = store.reviews_for_version(version["id"])[0]["key"]
    study.artifact_submissions.append({
        "review_key": review_key, "rating": "unknown", "answer": "Queued artifact answer",
        "at": "2026-09-28T07:45:00+09:00", "event_id": "queued-artifact",
    })
    study.sync_failure = failure
    first = await runner.review_study(
        study.config_path, review_key=review_key, rating="uncertain", answer="First CLI answer",
    )
    monkeypatch.setattr(runner, "_now", lambda: datetime(2026, 9, 28, 8, 5, tzinfo=runner.SEOUL))
    second = await runner.review_study(
        study.config_path, review_key=review_key, rating="understood", answer="Second CLI answer",
    )
    assert first["status"] == second["status"] == "partial"
    assert first["review"]["attempts"] == second["review"]["attempts"] == []
    with open_store(study) as store:
        assert [row["answer"] for row in store.get_meta("runner:review_intake")] == [
            "First CLI answer", "Second CLI answer",
        ]
    study.sync_failure = None
    assert (await runner.run_study(study.config_path))["status"] == "done"
    with open_store(study) as store:
        row = store.reviews_for_version(version["id"])[0]
        assert [attempt["answer"] for attempt in row["attempts"]] == [
            "Queued artifact answer", "First CLI answer", "Second CLI answer",
        ]
        assert row["rating"] == "understood"
        assert row["due_at"] == "2026-09-28T23:05:00+00:00"
        assert store.get_meta("runner:review_intake") is None
        assert store.get_meta("runner:review_pending") == []
    assert study.reviews[review_key]["attempts"] == row["attempts"]
    await runner.run_study(study.config_path)
    with open_store(study) as store:
        assert store.reviews_for_version(version["id"])[0]["attempts"] == row["attempts"]


async def test_delayed_cli_replay_preserves_a_later_artifact_assessment(study, monkeypatch):
    source = study.add()
    await runner.run_study(study.config_path, keys=[source["key"]])
    with open_store(study) as store:
        version = store.current_version(source["key"])
        review_key = store.reviews_for_version(version["id"])[0]["key"]
    study.sync_failure = "result"
    failed = await runner.review_study(
        study.config_path, review_key=review_key, rating="unknown", answer="Earlier CLI answer",
    )
    assert failed["status"] == "partial"
    study.artifact_submissions.append({
        "review_key": review_key, "rating": "understood", "answer": "Later artifact answer",
        "at": "2026-09-28T09:00:00+09:00", "event_id": "later-artifact",
    })
    study.sync_failure = None
    monkeypatch.setattr(runner, "_now", lambda: datetime(2026, 9, 28, 9, 5, tzinfo=runner.SEOUL))
    assert (await runner.run_study(study.config_path))["status"] == "done"
    with open_store(study) as store:
        row = store.reviews_for_version(version["id"])[0]
        assert [attempt["answer"] for attempt in row["attempts"]] == ["Earlier CLI answer", "Later artifact answer"]
        assert row["rating"] == "understood"
        assert row["due_at"] == "2026-09-29T00:00:00+00:00"
        assert store.get_meta("runner:review_intake") is None
    assert study.reviews[review_key]["rating"] == "understood"


async def test_exam_exact_scope_rejects_unprocessed_latest_and_caches_failed_publication(study):
    first, same_course, other = study.add(), study.add(number=2), study.add(course_id=8)
    await runner.run_study(study.config_path, keys=[row["key"] for row in (first, same_course, other)])
    with open_store(study) as store:
        for version in store.versions():
            key = store.reviews_for_version(version["id"])[0]["key"]
            store.record_review(key, "unknown", answer=version["key"], event_id=version["id"])
    study.exam_failure = True
    failed = await runner.exam_study(study.config_path, course_ids=[], source_keys=[first["key"]], title="Definitions")
    assert failed["status"] == "partial"
    study.exam_failure = False
    completed = await runner.exam_study(study.config_path, course_ids=[], source_keys=[first["key"]], title="Definitions")
    assert completed["status"] == "done"
    assert len(study.exam_runs) == 1
    assert study.exam_runs[0]["keys"] == [first["key"]]
    assert {row["material_key"] for row in study.exam_runs[0]["reviews"]} == {first["key"]}
    repeat = await runner.exam_study(study.config_path, course_ids=[], source_keys=[first["key"]], title="Definitions")
    assert repeat["artifact_page_id"] == completed["artifact_page_id"]
    assert len(study.exams) == 1
    with pytest.raises(runner.StudyScopeError, match="source_keys_unknown"):
        await runner.exam_study(study.config_path, course_ids=[], source_keys=[first["key"], "material:7:999"], title="Invalid")
    with open_store(study) as store:
        prior = store.versions()[0]
        store.record_version(first["key"], {**prior, "sha256": "f" * 64}, baseline=False)
    with pytest.raises(runner.StudyScopeError, match="exam_sources_unprocessed"):
        await runner.exam_study(study.config_path, course_ids=[7], source_keys=[], title="No stale version")


async def test_course_exam_rejects_discovered_source_with_failed_download(study):
    ready, missing = study.add(), study.add(number=2)
    study.acquisition_failures.add(missing["key"])
    result = await runner.run_study(study.config_path, keys=[ready["key"]])
    assert result["status"] == "partial"
    with open_store(study) as store:
        assert store.current_version(missing["key"]) is None
        assert store.source(missing["key"]) is not None
    with pytest.raises(runner.StudyScopeError, match="exam_sources_unprocessed"):
        await runner.exam_study(study.config_path, course_ids=[7], source_keys=[], title="Entire course")
    assert study.exam_runs == []
    explicit = await runner.exam_study(
        study.config_path, course_ids=[], source_keys=[ready["key"]], title="Only the ready source",
    )
    assert explicit["status"] == "done"
    assert study.exam_runs[0]["keys"] == [ready["key"]]
    with pytest.raises(runner.StudyScopeError, match="exam_sources_unprocessed"):
        await runner.exam_study(
            study.config_path, course_ids=[], source_keys=[ready["key"], missing["key"]], title="Both sources",
        )


@pytest.mark.parametrize("reason", ["video", "unsupported_material_type", "html"])
async def test_course_exam_excludes_only_observed_unsupported_skips(study, reason):
    ready, skipped = study.add(), study.add(number=2)
    study.acquisition_skips[skipped["key"]] = reason
    assert (await runner.run_study(study.config_path, keys=[ready["key"]]))["status"] == "done"
    result = await runner.exam_study(study.config_path, course_ids=[7], source_keys=[], title="Eligible course sources")
    assert result["status"] == "done"
    assert study.exam_runs[0]["keys"] == [ready["key"]]
    with pytest.raises(runner.StudyScopeError, match="exam_sources_unprocessed"):
        await runner.exam_study(
            study.config_path, course_ids=[7], source_keys=[skipped["key"]], title="Explicit skipped source",
        )
    study.acquisition_skips.clear()
    study.acquisition_failures.add(skipped["key"])
    await runner.run_study(study.config_path)
    with pytest.raises(runner.StudyScopeError, match="exam_sources_unprocessed"):
        await runner.exam_study(study.config_path, course_ids=[7], source_keys=[], title="Failed eligible source")


async def test_course_exam_rejects_video_replaced_by_unacquired_pdf_after_initialize(study):
    ready, revised = study.add(), study.add(number=2)
    revised["metadata_hash"] = "video-descriptor"
    study.acquisition_skips[revised["key"]] = "video"
    assert (await runner.run_study(study.config_path, keys=[ready["key"]]))["status"] == "done"
    revised["metadata_hash"] = "pdf-descriptor"
    study.acquisition_skips.clear()
    assert (await runner.initialize_study(study.config_path))["status"] == "done"
    with pytest.raises(runner.StudyScopeError, match="exam_sources_unprocessed"):
        await runner.exam_study(study.config_path, course_ids=[7], source_keys=[], title="Revised course")
    assert study.exam_runs == []
    assert (await runner.run_study(study.config_path, keys=[revised["key"]]))["status"] == "done"
    assert (await runner.exam_study(
        study.config_path, course_ids=[7], source_keys=[], title="Revised course",
    ))["status"] == "done"
    assert set(study.exam_runs[0]["keys"]) == {ready["key"], revised["key"]}


async def test_course_exam_does_not_trust_legacy_skip_without_metadata_identity(study):
    ready, skipped = study.add(), study.add(number=2)
    study.acquisition_skips[skipped["key"]] = "video"
    await runner.run_study(study.config_path, keys=[ready["key"]])
    with open_store(study) as store:
        store.set_meta("runner:acquisition:" + skipped["key"], {"status": "skipped", "reason": "video"})
    with pytest.raises(runner.StudyScopeError, match="exam_sources_unprocessed"):
        await runner.exam_study(study.config_path, course_ids=[7], source_keys=[], title="Legacy classification")
    assert study.exam_runs == []


async def test_course_exam_does_not_treat_oversized_document_as_ineligible(study):
    ready, oversized = study.add(), study.add(number=2)
    study.acquisition_skips[oversized["key"]] = "size_limit"
    await runner.run_study(study.config_path, keys=[ready["key"]])
    with pytest.raises(runner.StudyScopeError, match="exam_sources_unprocessed"):
        await runner.exam_study(study.config_path, course_ids=[7], source_keys=[], title="Whole course")
    assert study.exam_runs == []


async def test_restored_revision_drives_canonical_force_reviews_and_exam_scope(study):
    source = study.add()
    original = study.data[source["key"]]
    await runner.run_study(study.config_path, keys=[source["key"]])
    with open_store(study) as store:
        first = store.current_version(source["key"])
    study.data[source["key"]] = pdf_bytes("Replacement source version")
    await runner.run_study(study.config_path)
    with open_store(study) as store:
        second = store.current_version(source["key"])
    study.data[source["key"]] = original
    restored = await runner.run_study(study.config_path)
    assert restored["status"] == "done"
    assert restored["materials"][0]["version_id"] == first["id"]
    assert study.canonical[source["key"]]["id"] == first["id"]
    assert {row["hash"] for row in study.model_runs} == {first["sha256"], second["sha256"]}
    assert len(study.model_runs) == 2
    with open_store(study) as store:
        assert store.current_version(source["key"])["id"] == first["id"]
        assert store.reviews_for_version(first["id"])[0]["active"] is True
        assert store.reviews_for_version(second["id"])[0]["active"] is False
    study.acquisition_failures.add(source["key"])
    forced = await runner.run_study(study.config_path, keys=[source["key"]])
    assert forced["status"] == "partial"
    with open_store(study) as store:
        assert store.current_version(source["key"])["id"] == first["id"]
    exam = await runner.exam_study(study.config_path, course_ids=[7], source_keys=[], title="Restored source")
    assert exam["status"] == "done"
    assert study.exam_runs[0]["hashes"] == [first["sha256"]]


@pytest.mark.parametrize("delayed", [False, True])
async def test_revision_after_restoration_compares_with_restored_source_even_when_delayed(study, monkeypatch, delayed):
    from ssu_lms_mcp.study_content import compare_documents

    source = study.add()
    original = study.data[source["key"]]
    await runner.run_study(study.config_path, keys=[source["key"]])
    with open_store(study) as store:
        first = store.current_version(source["key"])
    study.data[source["key"]] = pdf_bytes("Replacement source version")
    await runner.run_study(study.config_path)
    study.data[source["key"]] = original
    await runner.run_study(study.config_path)
    with fitz.open(stream=original, filetype="pdf") as document:
        document.new_page().insert_text((72, 72), "Appendix to the restored lecture")
        study.data[source["key"]] = document.tobytes()
    third_hash = hashlib.sha256(study.data[source["key"]]).hexdigest()
    generate = runner.build_package
    failed = False

    def package(bundle, course, previous, related, model_config, output_dir):
        nonlocal failed
        if delayed and bundle["source_sha256"] == third_hash and not failed:
            failed = True
            raise runner.ContentError("model_unavailable")
        result = generate(bundle, course, previous, related, model_config, output_dir)
        result["changes"] = compare_documents(previous["bundle"] if previous else None, bundle)
        return result

    monkeypatch.setattr(runner, "build_package", package)
    result = await runner.run_study(study.config_path)
    with open_store(study) as store:
        third = store.current_version(source["key"])
    if delayed:
        assert result["status"] == "partial"
        study.data[source["key"]] = pdf_bytes("Fourth revision becomes current before retry")
        assert (await runner.run_study(study.config_path))["status"] == "done"
        with open_store(study) as store:
            assert store.current_version(source["key"])["id"] != third["id"]
    else:
        assert result["status"] == "done"
    generated = next(row for row in study.model_runs if row["hash"] == third_hash)
    assert generated["previous"]["bundle"] == first["stages"]["extracted"]
    assert generated["previous"]["package"] == first["stages"]["generated"]
    changes = study.children[third["id"]]["changes"]
    assert changes["added_pages"] == [2]
    assert changes["changed_pages"] == []
    assert changes["removed_pages"] == []


async def test_unknown_forced_key_is_not_silently_ignored_and_lock_is_released(study):
    source = study.add()
    with pytest.raises(runner.StudyScopeError, match="source_keys_unknown"):
        await runner.run_study(study.config_path, keys=[source["key"], "material:7:999"])
    with open_store(study) as store:
        assert store.versions() == []
    assert (await runner.run_study(study.config_path, keys=[source["key"]]))["status"] == "done"
    lock_path = Path(study.config["data_dir"]) / "study.lock"
    with FileLock(str(lock_path), timeout=0):
        assert (await runner.run_study(study.config_path))["status"] == "running"


async def test_status_uses_only_local_state_and_reports_pending_work(study, monkeypatch):
    source = study.add()
    study.fail_all_reviews = True
    await runner.run_study(study.config_path, keys=[source["key"]])

    def forbidden(*args, **kwargs):
        raise AssertionError("Status attempted remote work")

    monkeypatch.setattr(runner, "discover", forbidden)
    monkeypatch.setattr(runner, "StudyPublisher", forbidden)
    monkeypatch.setattr(runner, "build_package", forbidden)
    result = runner.study_status(study.config_path)
    assert result["status"] == "partial"
    assert result["counts"]["pending"] == 1
    assert result["pending"][0]["key"] == source["key"]
    assert result["hub_url"] == "http://127.0.0.1:8766/#/page/11111111-1111-4111-8111-111111111111"


def test_invalid_config_never_echoes_input_and_rejects_shell_commands(study):
    config = dict(study.config)
    config["data_dir"] = "relative/private-secret-value"
    study.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(runner.StudyConfigError) as error:
        runner.load_config(study.config_path)
    assert error.value.code == "data_dir_invalid"
    assert "private-secret" not in str(error.value)
    shell_command = study.config_path.parent / "unsafe.cmd"
    shell_command.write_text("", encoding="utf-8")
    config["data_dir"] = study.config["data_dir"]
    config["model"] = {**config["model"], "command": str(shell_command)}
    study.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(runner.StudyConfigError, match="command_shell_script_disallowed"):
        runner.load_config(study.config_path)


@pytest.mark.parametrize("location", ["data_dir", "lms_data", "materials", "ancestor"])
def test_artifact_directory_cannot_overlap_private_state_or_originals(study, location):
    config = json.loads(json.dumps(study.config))
    targets = {
        "data_dir": config["data_dir"],
        "lms_data": config["lms"]["data_dir"],
        "materials": config["lms"]["materials_dir"],
        "ancestor": str(study.config_path.parent),
    }
    config["artifact"]["root"] = targets[location]
    study.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(runner.StudyConfigError) as error:
        runner.load_config(study.config_path)
    assert error.value.code == "artifact_root_overlaps_runtime"
