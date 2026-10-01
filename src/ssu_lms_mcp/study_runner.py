"""Durable orchestration for the private, daily LMS study workspace.

Only model synthesis runs in a worker thread. SQLite, source state and the
synchronous publisher stay on the thread that owns the study lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from .config import Settings
from .reminders import SEOUL, notify
from .study_content import ContentError, build_exam, build_package, extract_document
from .study_publish import StudyPublisher
from .study_sources import acquire, discover
from .study_store import RATINGS, StudyStore

_FILENAME_REVIEW_ERRORS = {
    "original_filename_missing", "original_filename_unverified", "unsafe_original_filename",
}


class StudyConfigError(ValueError):
    """Invalid local configuration; the code never includes input or secrets."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class StudyScopeError(ValueError):
    """An explicit source/course selection cannot be fulfilled in its entirety."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _ids(values, error=StudyScopeError):
    if not isinstance(values, list):
        raise error("course_ids_invalid")
    result = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise error("course_ids_invalid")
        if not str(value).isascii() or not str(value).isdigit() or int(value) <= 0:
            raise error("course_ids_invalid")
        result.add(int(value))
    return sorted(result)


def _keys(values):
    if values is None:
        return []
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in values
    ):
        raise StudyScopeError("source_keys_invalid")
    return sorted(set(values))


def _absolute(value, code, *, file=False):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StudyConfigError(code)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise StudyConfigError(code)
    path = path.resolve()
    if (file and not path.is_file()) or (not file and path.exists() and not path.is_dir()):
        raise StudyConfigError(code)
    return path


def load_config(config_path) -> dict:
    """Validate local paths without executing cloud/model commands or reading secrets."""
    try:
        path = Path(config_path).expanduser().resolve()
        with path.open(encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, ValueError, TypeError):
        raise StudyConfigError("config_unreadable") from None
    if not isinstance(config, dict) or type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise StudyConfigError("config_schema_invalid")
    try:
        lms, model = config["lms"], config["model"]
        if not isinstance(lms, dict) or not isinstance(model, dict):
            raise StudyConfigError("config_sections_invalid")
        root = _absolute(config.get("data_dir"), "data_dir_invalid")
        lms_root = _absolute(lms.get("data_dir"), "lms_data_dir_invalid")
        materials = _absolute(lms.get("materials_dir"), "materials_dir_invalid")
        artifact = config.get("artifact")
        if not isinstance(artifact, dict) or type(artifact.get("port")) is not int or not 1024 <= artifact["port"] <= 65535:
            raise StudyConfigError("artifact_config_invalid")
        artifact_root = _absolute(artifact.get("root"), "artifact_root_invalid")
        if any(artifact_root.is_relative_to(directory) or directory.is_relative_to(artifact_root)
               for directory in (root, lms_root, materials)):
            raise StudyConfigError("artifact_root_overlaps_runtime")
        # A model subprocess must not inherit a checkout's project configuration.
        for directory in (root, lms_root, materials, artifact_root):
            if any((parent / ".git").exists() for parent in (directory, *directory.parents)):
                raise StudyConfigError("runtime_directory_in_repository")
            if path.parent.name == "study" and path.parent.parent.name == ".omp":
                if directory.is_relative_to(path.parent.parent.parent):
                    raise StudyConfigError("runtime_directory_in_repository")
        command = config.get("cloud_command")
        if not isinstance(command, list) or len(command) != 2:
            raise StudyConfigError("cloud_command_invalid")
        cloud = [str(_absolute(value, "cloud_command_invalid", file=True)) for value in command]
        executable = _absolute(model.get("command"), "model_command_invalid", file=True)
        if executable.suffix.lower() in {".bat", ".cmd"} or Path(cloud[0]).suffix.lower() in {".bat", ".cmd"}:
            raise StudyConfigError("command_shell_script_disallowed")
        if not isinstance(model.get("model"), str) or not model["model"].strip():
            raise StudyConfigError("model_name_invalid")
        if model.get("thinking") not in {"off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"}:
            raise StudyConfigError("model_thinking_invalid")
        timeout, batch = model.get("timeout_seconds"), model.get("pages_per_batch")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise StudyConfigError("model_timeout_invalid")
        if type(batch) is not int or not 1 <= batch <= 8:
            raise StudyConfigError("model_batch_size_invalid")
        if config.get("schedule") != {"time": "07:30", "timezone": "Asia/Seoul"}:
            raise StudyConfigError("schedule_invalid")
        course_ids = _ids(config.get("course_ids"), StudyConfigError)
        for directory in (root, lms_root, materials):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Exercise Settings validation now, rather than after a partially committed run.
        Settings.from_env(data_dir=lms_root, materials_dir=materials)
    except StudyConfigError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise StudyConfigError("config_invalid") from None
    return {
        **config, "data_dir": str(root), "course_ids": course_ids,
        "lms": {**lms, "data_dir": str(lms_root), "materials_dir": str(materials)},
        "artifact": {**artifact, "root": str(artifact_root)},
        "cloud_command": cloud, "model": {**model, "command": str(executable)},
    }


def _settings(config):
    return Settings.from_env(**{key: config["lms"][key] for key in ("data_dir", "materials_dir")})


def _database(config):
    return Path(config["data_dir"]) / "study.sqlite3"


def _lock(config):
    return FileLock(str(Path(config["data_dir"]) / "study.lock"), timeout=0)


def _now():
    return datetime.now(SEOUL)


def _code(value, fallback):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else fallback


def _error(exc):
    if isinstance(exc, ContentError):
        return _code(str(exc), "content_error")
    code = getattr(exc, "code", None)
    if code is not None:
        return _code(code, "publisher_error")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "operation_timeout"
    if isinstance(exc, OSError):
        return "io_error"
    return "operation_failed"


def _failure(stage, error_type, source=None, **fields):
    result = {"stage": stage, "error_type": _code(error_type, "operation_failed"), **fields}
    if source:
        result.update({key: source[key] for key in ("key", "course_id", "partition") if key in source})
    return result


def _allowed(source, config):
    return not config["course_ids"] or int(source["course_id"]) in config["course_ids"]


async def _scan(store, config):
    try:
        scan = await discover(_settings(config), config["course_ids"])
    except Exception as exc:
        scan = {"courses": [], "items": [], "successful_partitions": [],
                "errors": [{"partition": "courses", "error_type": _error(exc)}]}
    courses = {int(row["id"]): {**row, "id": int(row["id"])} for row in store.get_meta("runner:courses", [])}
    for row in scan["courses"]:
        if not config["course_ids"] or int(row["id"]) in config["course_ids"]:
            courses[int(row["id"])] = {**row, "id": int(row["id"])}
    store.set_meta("runner:courses", list(courses.values()))
    observed = store.observe(
        [item for item in scan["items"] if _allowed(item, config)], scan["successful_partitions"]
    )
    failures = []
    for row in scan["errors"]:
        partition = row.get("partition", "courses")
        fields = {"partition": partition}
        kind, _, cid = partition.partition(":")
        if kind in {"material", "board"} and cid.isdigit():
            fields["course_id"] = int(cid)
        failures.append(_failure("discovery", row.get("error_type"), **fields))
    return scan, observed, failures


def _public_material(version, status, **fields):
    source = version["source"]
    published = version["stages"].get("published", {})
    return {"key": version["key"], "version_id": version["id"],
            "course_id": int(source["course_id"]), "title": source["title"],
            "status": status, "artifact_url": published.get("artifact_url"), **fields}


def _needs_work(version):
    stages = version["stages"]
    if version["errors"] or "uploaded" not in stages or "published" not in stages:
        return True
    return not version["baseline"] and (
        "generated" not in stages or not stages["published"].get("version_page_id")
    )


def _previous(store, version, config):
    previous_id = version.get("previous_version_id")
    previous = store.get_version(previous_id) if previous_id is not None else None
    if previous is None:
        return None
    bundle = previous["stages"].get("extracted")
    if bundle is None or not bundle.get("complete"):
        bundle = extract_document(
            previous["path"], Path(config["data_dir"]) / "versions" / previous["id"] / "evidence"
        )
        store.save_stage(previous["id"], "extracted", bundle)
    if not bundle.get("complete"):
        raise ContentError("previous_document_review_required")
    if bundle.get("source_sha256") != previous["sha256"]:
        raise ContentError("previous_source_hash_mismatch")
    return {"bundle": bundle, "package": previous["stages"].get("generated")}


async def _process(store, publisher, config, version, current_id, courses):
    version = {**version, "is_latest": version["id"] == current_id}
    stage = "uploaded"
    try:
        uploaded = store.get_stage(version["id"], stage)
        if uploaded is None or stage in version["errors"]:
            uploaded = publisher.upload(version)
            store.save_stage(version["id"], stage, uploaded)
        if version["baseline"]:
            stage = "published"
            if store.get_stage(version["id"], stage) is None or stage in version["errors"]:
                published = publisher.publish_baseline(version, uploaded)
                store.save_stage(version["id"], stage, {**published, "baseline": True})
            return _public_material(store.get_version(version["id"]), "baseline"), []
        stage = "extracted"
        bundle = store.get_stage(version["id"], stage)
        if bundle is None or not bundle.get("complete") or stage in version["errors"]:
            output = Path(config["data_dir"]) / "versions" / version["id"]
            bundle = extract_document(version["path"], output / "evidence")
            store.save_stage(version["id"], stage, bundle)
        if bundle.get("source_sha256") != version["sha256"]:
            raise ContentError("source_hash_mismatch")
        if not bundle.get("complete"):
            store.fail_stage(version["id"], stage, "document_review_required")
            failures = [_failure(stage, "document_review_required", version["source"], version_id=version["id"])]
            metadata = {**version, "review_required": True, "review_reasons": bundle.get("review_reasons", [])}
            try:
                published = publisher.publish_baseline(metadata, uploaded)
                store.set_meta("runner:material:" + version["id"], published)
            except Exception as exc:
                store.fail_stage(version["id"], "published", _error(exc))
                failures.append(_failure("published", _error(exc), version["source"], version_id=version["id"]))
            return _public_material(store.get_version(version["id"]), "review_required", review_required=True), failures
        stage = "generated"
        package = store.get_stage(version["id"], stage)
        related = store.related_materials(int(version["source"]["course_id"]), exclude_key=version["key"])
        if package is None or stage in version["errors"]:
            source = version["source"]
            course = courses.get(int(source["course_id"])) or {
                "id": int(source["course_id"]), "name": source["course_name"], "professor": source.get("professor", "")
            }
            package = await asyncio.to_thread(
                build_package, bundle, course, _previous(store, version, config), related,
                config["model"], Path(config["data_dir"]) / "versions" / version["id"] / "model",
            )
            store.save_stage(version["id"], stage, package)
        stage = "published"
        published = store.get_meta("runner:material:" + version["id"])
        if not published or not published.get("version_page_id") or "published" not in version["stages"]:
            published = publisher.publish(version, package, uploaded, related)
            store.set_meta("runner:material:" + version["id"], published)
        store.add_reviews(version["id"], package["quizzes"])
        publisher.publish_reviews(version["id"])
        # Publication is complete only after the durable review rows are remotely visible.
        store.save_stage(version["id"], stage, published)
        return _public_material(store.get_version(version["id"]), "published",
                                review_required=package.get("review_required", False)), []
    except Exception as exc:
        code = _error(exc)
        store.fail_stage(version["id"], stage, code)
        return _public_material(store.get_version(version["id"]), "partial", failed_stage=stage), [
            _failure(stage, code, version["source"], version_id=version["id"])
        ]


def _resume_review_intake(store):
    events = store.get_meta("runner:review_intake") or []
    if isinstance(events, dict):
        events = [events]
    pending = set(store.get_meta("runner:review_pending", []))
    for event in sorted(events, key=lambda row: datetime.fromisoformat(row["at"])):
        row = store.record_review(**event)
        pending.add(row["version_id"])
        store.set_meta("runner:review_pending", sorted(pending))
    if events:
        store.set_meta("runner:review_intake", None)
    return pending


def _sync_reviews(store, publisher):
    failures = []
    # Import checked artifact drafts before publishing schedules or committing newer
    # CLI intents. A failed reconciliation must leave those intents durable.
    try:
        result = publisher.sync_reviews()
        for failure in result.get("failures", []):
            failures.append(_failure("review_sync", failure.get("error_type", "review_sync_failed")))
    except Exception as exc:
        failures.append(_failure("review_sync", _error(exc)))
    if failures:
        return failures
    pending = _resume_review_intake(store)
    for version_id in sorted(pending):
        try:
            publisher.publish_reviews(version_id)
        except Exception as exc:
            version = store.get_version(version_id)
            failures.append(_failure("review_publish", _error(exc), version["source"] if version else None,
                                     version_id=version_id))
        else:
            pending.remove(version_id)
            store.set_meta("runner:review_pending", sorted(pending))
    return failures


def _due(store):
    count = sum(len(store.reviews_for_version(row["id"])) for row in store.versions())
    return store.due_reviews(limit=count)


def _summarize(report):
    materials = report.get("materials", [])
    failures = report["failures"]
    for material in materials:
        source_failures = [row for row in failures if row.get("key") == material["key"]]
        if source_failures:
            material["failures"] = source_failures
            if material["status"] != "review_required":
                material["status"] = "partial"
    report["summary"] = {
        "materials": len(materials), "baseline": sum(row["status"] == "baseline" for row in materials),
        "published": sum(row["status"] == "published" for row in materials),
        "skipped": sum(row["status"] == "skipped" for row in materials),
        "review_required": sum(bool(row.get("review_required")) for row in materials),
        "failures": len(failures), "due_reviews": len(report.get("due", [])),
    }
    course_ids = set(report.get("course_ids", [])) | {row["course_id"] for row in materials}
    course_ids.update(row["course_id"] for row in failures if "course_id" in row)
    report["courses"] = [{"course_id": cid, "status": "partial" if any(
        row.get("course_id") == cid or "course_id" not in row for row in failures
    ) else "done"} for cid in sorted(course_ids)]
    report["status"] = "partial" if failures else "done"


def _toast(store, report):
    key = "runner:toast:" + report["date"]
    state = store.get_meta(key)
    if state is None:
        # Claim before contacting Windows: a crash must not duplicate a toast request.
        state = {"requested": True, "accepted": None}
        store.set_meta(key, state)
        try:
            state["accepted"] = bool(notify(
                "SSU 학습 브리핑",
                f"학습 자료 {report['summary']['materials']}개 · 복습 {len(report['due'])}개"
                + (" · 일부 작업 확인 필요" if report["failures"] else ""),
            ))
        except Exception:
            state["accepted"] = False
        store.set_meta(key, state)
    report["notification"] = {**state, "note": "accepted는 요청 수락이며 화면 표시 확인이 아닙니다."}
    if state["accepted"] is not True:
        report["failures"].append(_failure("notification", "toast_not_accepted"))


async def initialize_study(config_path) -> dict:
    config = load_config(config_path)
    try:
        with _lock(config), StudyStore(_database(config)) as store:
            scan, observed, failures = await _scan(store, config)
            hub = store.get_meta("runner:hub", {})
            try:
                with StudyPublisher(store, config) as publisher:
                    hub = publisher.initialize(scan["courses"])
                    store.set_meta("runner:hub", hub)
            except Exception as exc:
                failures.append(_failure("initialize", _error(exc)))
            report = {"status": "partial" if failures else "done", **hub,
                      "observed_sources": len(observed), "failures": failures,
                      "baseline": "metadata_only", "courses": scan["courses"]}
            store.set_meta("runner:last_initialize", report)
            return report
    except Timeout:
        return {"status": "running"}


async def run_study(config_path, *, keys: list[str] | None = None, scheduled: bool = False) -> dict:
    config, forced = load_config(config_path), _keys(keys)
    try:
        with _lock(config), StudyStore(_database(config)) as store:
            now = _now()
            date = now.date().isoformat()
            if scheduled and (now.hour, now.minute) < (7, 30):
                return {"status": "skipped", "reason": "before_schedule", "date": date}
            if scheduled and store.get_meta("runner:successful_date") == date:
                return {"status": "skipped", "reason": "already_completed", "date": date}
            scan, observed, failures = await _scan(store, config)
            known = {row["key"]: row for row in store.sources() if _allowed(row, config)}
            if set(forced) - known.keys():
                raise StudyScopeError("source_keys_unknown")
            targets = {row["key"]: row for row in observed}
            targets.update({key: known[key] for key in forced})
            report = {"date": date, "materials": [], "failures": failures,
                      "course_ids": config["course_ids"] or [int(row["id"]) for row in scan["courses"]],
                      "successful_partitions": scan["successful_partitions"], "due": []}
            results = {}
            for key, source in targets.items():
                try:
                    artifact = await acquire(_settings(config), source)
                    status = artifact.get("status")
                    store.set_meta("runner:acquisition:" + key, {
                        "status": _code(status, "failed"),
                        "reason": _code(artifact.get("reason"), "acquisition_failed"),
                        "metadata_hash": source["metadata_hash"],
                    })
                    if status == "saved":
                        version = store.record_version(
                            key, artifact, baseline=source.get("baseline", False), force=key in forced)
                        # Only fresh, verified acquisition of this hash can clear a
                        # filename barrier; an outage or a different revision cannot.
                        upload_error = version["errors"].get("uploaded")
                        if upload_error and upload_error["error_type"] in _FILENAME_REVIEW_ERRORS:
                            store.clear_stage_error(version["id"], "uploaded")
                    elif status == "skipped" and key not in forced:
                        results[key] = {"key": key, "course_id": int(source["course_id"]),
                                        "title": source["title"], "status": "skipped",
                                        "reason": _code(artifact.get("reason"), "source_skipped")}
                    else:
                        reason = _code(artifact.get("reason"), "acquisition_failed")
                        report["failures"].append(_failure("acquire", reason, source))
                        results[key] = {"key": key, "course_id": int(source["course_id"]),
                                        "title": source["title"], "status": "partial",
                                        "review_required": status == "review_required"}
                except Exception as exc:
                    store.set_meta("runner:acquisition:" + key, {
                        "status": "failed", "reason": _error(exc),
                        "metadata_hash": source["metadata_hash"],
                    })
                    report["failures"].append(_failure("acquire", _error(exc), source))
                    results[key] = {"key": key, "course_id": int(source["course_id"]),
                                    "title": source["title"], "status": "partial"}
                # A known immutable baseline can still be explicitly promoted during
                # a transient download outage; the acquisition error remains visible.
                if key in forced:
                    current = store.current_version(key)
                    if current:
                        store.record_version(key, current.get("upload_artifact") or current, force=True)
            versions = [row for row in store.versions() if _allowed(row["source"], config)]
            current_ids = {key: row["id"] for key in {row["key"] for row in versions}
                           if (row := store.current_version(key)) is not None}
            courses = {int(row["id"]): row for row in store.get_meta("runner:courses", [])}
            try:
                with StudyPublisher(store, config) as publisher:
                    try:
                        hub = publisher.initialize(scan["courses"])
                        store.set_meta("runner:hub", hub)
                    except Exception as exc:
                        report["failures"].append(_failure("initialize", _error(exc)))
                    report["failures"].extend(_sync_reviews(store, publisher))
                    for version in versions:
                        if _needs_work(version):
                            acquisition = store.get_meta("runner:acquisition:" + version["key"], {})
                            blocked = acquisition.get("reason")
                            if blocked not in _FILENAME_REVIEW_ERRORS:
                                blocked = version["errors"].get("uploaded", {}).get("error_type")
                            if (blocked in _FILENAME_REVIEW_ERRORS
                                    and ("uploaded" not in version["stages"] or "uploaded" in version["errors"])):
                                store.fail_stage(version["id"], "uploaded", blocked)
                                result = _public_material(
                                    store.get_version(version["id"]), "partial",
                                    failed_stage="uploaded", review_required=True)
                                errors = [_failure("uploaded", blocked, version["source"], version_id=version["id"])]
                            else:
                                result, errors = await _process(store, publisher, config, version,
                                                                current_ids[version["key"]], courses)
                            report["failures"].extend(errors)
                            if version["id"] == current_ids[version["key"]]:
                                results[version["key"]] = result
                        elif version["id"] == current_ids[version["key"]] and version["key"] in targets:
                            results.setdefault(version["key"], _public_material(
                                version, "baseline" if version["baseline"] else "published",
                                review_required=version["stages"].get("generated", {}).get("review_required", False)))
                    report["materials"] = list(results.values())
                    report["due"] = _due(store)
                    _summarize(report)
                    _toast(store, report)
                    _summarize(report)
                    try:
                        report["briefing"] = publisher.briefing(report)
                        store.set_meta("runner:briefing:" + date, report["briefing"])
                    except Exception as exc:
                        report["failures"].append(_failure("briefing", _error(exc)))
            except Exception as exc:
                report["failures"].append(_failure("publisher", _error(exc)))
            report["materials"] = list(results.values())
            _summarize(report)
            report["hub_url"] = store.get_meta("runner:hub", {}).get("hub_url")
            store.set_meta("runner:last_report", report)
            if not report["failures"]:
                store.set_meta("runner:successful_date", date)
            elif store.get_meta("runner:successful_date") == date:
                store.set_meta("runner:successful_date", None)
            return report
    except Timeout:
        return {"status": "running"}


def study_status(config_path) -> dict:
    """Read only local state; never construct a publisher or contact the LMS."""
    config = load_config(config_path)
    if not _database(config).exists():
        return {"status": "not_initialized", "hub_url": None, "counts": {"sources": 0, "versions": 0,
                "pending": 0, "due_reviews": 0}, "pending": [], "due": [], "failures": []}
    with StudyStore(_database(config)) as store:
        versions = store.versions()
        pending = [_public_material(row, "pending", errors=row["errors"]) for row in versions if _needs_work(row)]
        last = store.get_meta("runner:last_report", {})
        failures = list(last.get("failures", store.get_meta("runner:last_initialize", {}).get("failures", [])))
        review_pending = store.get_meta("runner:review_pending", [])
        if review_pending or store.get_meta("runner:review_intake"):
            failures.extend(store.get_meta("runner:last_review", {}).get("failures", []))
        due = _due(store)
        return {"status": "partial" if failures or pending or review_pending or store.get_meta("runner:review_intake") else "done",
                "hub_url": store.get_meta("runner:hub", {}).get("hub_url"),
                "counts": {"sources": len(store.sources()), "versions": len(versions),
                           "pending": len(pending), "due_reviews": len(due)},
                "pending": pending, "due": due, "failures": failures, "last_report": last,
                "successful_date": store.get_meta("runner:successful_date"),
                "review_publications_pending": store.get_meta("runner:review_pending", [])}


async def review_study(config_path, *, review_key, rating, answer="") -> dict:
    config = load_config(config_path)
    if not isinstance(review_key, str) or not review_key or rating not in RATINGS or not isinstance(answer, str):
        raise StudyScopeError("review_input_invalid")
    try:
        with _lock(config), StudyStore(_database(config)) as store:
            reviews = {row["key"]: row for version in store.versions() for row in store.reviews_for_version(version["id"])}
            if review_key not in reviews:
                raise StudyScopeError("review_key_unknown")
            event = {"review_key": review_key, "rating": rating, "answer": answer,
                     "at": _now().isoformat(), "event_id": str(uuid4())}
            queued = store.get_meta("runner:review_intake") or []
            if isinstance(queued, dict):
                queued = [queued]
            store.set_meta("runner:review_intake", [*queued, event])
            failures = []
            try:
                with StudyPublisher(store, config) as publisher:
                    failures = _sync_reviews(store, publisher)
            except Exception as exc:
                failures.append(_failure("review_publish", _error(exc)))
            row = next(row for row in store.reviews_for_version(reviews[review_key]["version_id"]) if row["key"] == review_key)
            report = {"status": "partial" if failures else "done", "review": row, "failures": failures}
            store.set_meta("runner:last_review", {"status": report["status"], "review_key": review_key, "failures": failures})
            return report
    except Timeout:
        return {"status": "running"}


async def exam_study(config_path, *, course_ids: list[int], source_keys: list[str], title: str) -> dict:
    config = load_config(config_path)
    selected_courses, selected_keys = _ids(course_ids), _keys(source_keys)
    if not (selected_courses or selected_keys) or not isinstance(title, str) or not title.strip():
        raise StudyScopeError("exam_scope_required")
    try:
        with _lock(config), StudyStore(_database(config)) as store:
            sources = {row["key"]: row for row in store.sources()}
            if set(selected_keys) - sources.keys():
                raise StudyScopeError("source_keys_unknown")
            known_courses = {int(row["course_id"]) for row in sources.values()}
            if set(selected_courses) - known_courses:
                raise StudyScopeError("course_ids_unknown")
            chosen = set(selected_keys)
            for key, source in sources.items():
                if int(source["course_id"]) not in selected_courses:
                    continue
                acquisition = store.get_meta("runner:acquisition:" + key, {})
                if (
                    acquisition.get("metadata_hash") == source["metadata_hash"]
                    and acquisition.get("status") == "skipped"
                    and acquisition.get("reason") in {"video", "unsupported_material_type", "html"}
                ):
                    continue
                chosen.add(key)
            if not chosen:
                raise StudyScopeError("exam_sources_unprocessed")
            versions = []
            for key in sorted(chosen):
                version = store.current_version(key)
                if version is None or version["baseline"] or _needs_work(version) or "generated" not in version["stages"]:
                    raise StudyScopeError("exam_sources_unprocessed")
                versions.append(version)
            scope = {"course_ids": selected_courses, "source_keys": selected_keys, "title": title}
            try:
                with StudyPublisher(store, config) as publisher:
                    failures = _sync_reviews(store, publisher)
                    if failures:
                        return {"status": "partial", "scope": scope, "failures": failures}
                    all_reviews = [row for version in versions for row in store.reviews_for_version(version["id"])]
                    review_count = sum(len(store.reviews_for_version(row["id"])) for row in store.versions())
                    weak = [row for row in store.weak_reviews(
                        course_ids=sorted({int(row["source"]["course_id"]) for row in versions}), limit=review_count
                    ) if row["material_key"] in chosen]
                    identity = {"scope": scope, "versions": [[row["key"], row["sha256"]] for row in versions],
                                "reviews": [{"key": row["key"], "attempts": row["attempts"]} for row in all_reviews]}
                    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                                    separators=(",", ":")).encode("utf-8")).hexdigest()
                    cache_key = "runner:exam:" + key
                    cached = store.get_meta(cache_key, {})
                    exam = cached.get("exam")
                    if exam is None:
                        exam = await asyncio.to_thread(
                            build_exam, [{"key": row["key"], "package": row["stages"]["generated"]} for row in versions],
                            scope, weak, config["model"], Path(config["data_dir"]) / "exams" / key,
                        )
                        cached = {"exam": exam}
                        store.set_meta(cache_key, cached)
                    published = cached.get("published")
                    if published is None:
                        published = publisher.publish_exam(exam, key)
                        store.set_meta(cache_key, {**cached, "published": published})
                    return {"status": "review_required" if exam["review_required"] else "done",
                            **published, "key": key, "scope": scope, "review_required": exam["review_required"],
                            "review_reasons": exam.get("review_reasons", []), "failures": []}
            except Exception as exc:
                report = {"status": "partial", "scope": scope, "failures": [_failure("exam", _error(exc))]}
                store.set_meta("runner:last_exam_failure", report)
                return report
    except Timeout:
        return {"status": "running"}
