"""Durable learner submissions with atomic artifact compare-and-swap acknowledgements."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from .study_artifacts import ArtifactError

_RATINGS = {"이해함": "understood", "애매함": "uncertain", "모름": "unknown"}
_KOREAN = {value: key for key, value in _RATINGS.items()}
_PRIORITY = {"unknown": 0, "uncertain": 1, None: 2, "understood": 3}
_PENDING = "artifact:review:pending:"
_COMPLETED = "artifact:review:ack:"
_YES, _NO = "__YES__", "__NO__"


class ReviewPublicationError(RuntimeError):
    """An implementation-defined code, never an upstream exception message."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ReviewPublicationError(code)


def _failure(key, stage, exc):
    return {"key": key, "stage": stage,
            "error_type": exc.code if isinstance(exc, (ReviewPublicationError, ArtifactError)) else stage + "_failed"}


def _remote_key(row):
    identity = str(row.get("id", row.get("url", ""))) if isinstance(row, dict) else "invalid"
    return "remote:" + hashlib.sha256(identity.encode()).hexdigest()[:20]


def _page_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        _fail("review_invalid_page")


def _timestamp(value):
    if not isinstance(value, str):
        _fail("review_missing_last_edit")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            _fail("review_invalid_last_edit")
        return instant.astimezone(UTC).isoformat()
    except ValueError:
        _fail("review_invalid_last_edit")


def _inventory(store):
    versions = store.versions()
    reviews = {row["key"]: row for version in versions
               for row in store.reviews_for_version(version["id"])}
    return versions, reviews


def _schedule(review):
    return {
        "date:복습일:start": review["due_at"],
        "date:복습일:end": None,
        "date:복습일:is_datetime": 1,
        "최근평가": _KOREAN.get(review["rating"]),
        "시도": len(review["attempts"]),
        "활성": _YES if review["active"] else _NO,
        "난도우선": _PRIORITY[review["rating"]],
    }

def _same_property(name, actual, expected):
    if name in ("date:복습일:start", "date:복습일:end") and actual is not None and expected is not None:
        try:
            return _timestamp(actual) == _timestamp(expected)
        except ReviewPublicationError:
            return False
    return actual == expected



def _properties(review, source_url):
    from .study_render import safe_text

    return {"질문": safe_text(review["prompt"]), "키": review["key"], "과목ID": review["course_id"],
            "개념": safe_text(review["concept"]), "자료": source_url, **_schedule(review)}


def _fetch(publisher, page_id, review):
    page = publisher._fetch(page_id)
    if not isinstance(page, dict) or _page_id(page.get("id")) != page_id:
        _fail("review_page_conflict")
    props = page.get("properties")
    if not isinstance(props, dict) or props.get("키") != review["key"]:
        _fail("review_key_changed")
    return page, props


def _snapshot(page, props):
    submitted = props.get("제출")
    if submitted not in (_YES, _NO):
        _fail("review_invalid_submission")
    rating, answer = props.get("평가"), props.get("내답")
    if submitted == _YES:
        if not isinstance(rating, str) or rating not in _RATINGS:
            _fail("review_invalid_rating")
        if not isinstance(answer, str):
            _fail("review_invalid_answer")
    return {"submitted": submitted == _YES, "rating": rating, "answer": answer,
            "edited_at": _timestamp(page.get("updated_at") or props.get("마지막수정"))}


def _event_id(page_id, key, snapshot):
    data = [page_id, key, snapshot["edited_at"], snapshot["rating"], snapshot["answer"]]
    return "artifact:" + hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _complete(store, pending, status):
    store.set_meta(_COMPLETED + pending["event_id"], {
        "status": status, "review_key": pending["review_key"], "page_id": pending["page_id"],
    })
    store.set_meta(_PENDING + pending["review_key"], None)


def _validate_pending(pending, review):
    if not isinstance(pending, dict) or pending.get("review_key") != review["key"]:
        _fail("review_invalid_pending_event")
    page_id = _page_id(pending.get("page_id"))
    if review["artifact_page_id"] and _page_id(review["artifact_page_id"]) != page_id:
        _fail("review_page_conflict")
    if (pending.get("rating") not in _RATINGS or not isinstance(pending.get("answer"), str)
            or pending.get("event_id") != _event_id(page_id, review["key"], pending)):
        _fail("review_invalid_pending_event")
    _timestamp(pending["edited_at"])
    _timestamp(pending["observed_edited_at"])
    return page_id


def _resume(publisher, review, pending, result):
    store = publisher.store
    page_id = _validate_pending(pending, review)
    if store.get_meta(_COMPLETED + pending["event_id"]) is not None:
        store.set_meta(_PENDING + review["key"], None)
        return review
    already_recorded = any(attempt["event_id"] == pending["event_id"] for attempt in review["attempts"])
    review = store.record_review(review["key"], _RATINGS[pending["rating"]], pending["answer"],
                                 at=pending["edited_at"], event_id=pending["event_id"])
    if not already_recorded:
        result["imported"] += 1
    page, props = _fetch(publisher, page_id, review)
    if props.get("처리이벤트") == pending["event_id"]:
        # The atomic acknowledgement succeeded before interruption. Any checked
        # submission now is a NEW event, even with identical text and rating.
        _complete(store, pending, "acknowledged")
        return review
    fresh = _snapshot(page, props)
    schedule = _schedule(review)
    same_draft = fresh["rating"] == pending["rating"] and fresh["answer"] == pending["answer"]
    if not fresh["submitted"]:
        # The learner withdrew/changed the draft. Publish only the recorded
        # schedule; do not toggle a potentially newer submission back off.
        publisher._update(page_id, schedule, expected_revision=page["revision"])
        _complete(store, pending, "withdrawn")
        _fail("review_submission_withdrawn")
    if not same_draft:
        _complete(store, pending, "superseded")
        _fail("review_draft_changed")
    if fresh["edited_at"] != pending["observed_edited_at"]:
        # Our publication or a personal-note edit also changes last-edited.
        # Preserve the event identity, defer clearing, and require a stable read
        # on a later run instead of recording this same answer twice.
        store.set_meta(_PENDING + review["key"], {**pending, "observed_edited_at": fresh["edited_at"]})
        _fail("review_draft_changed")
    publisher._update(page_id, {**schedule, "제출": _NO, "처리이벤트": pending["event_id"]},
                      expected_revision=page["revision"])
    _complete(store, pending, "acknowledged")
    return review


def _remote_rows(rows, reviews, stage, failures):
    """Index trustworthy card identities; duplicate identities are not guessed."""
    indexed, conflicts = {}, set()
    for row in rows:
        safe_key = _remote_key(row)
        try:
            if not isinstance(row, dict) or not isinstance(row.get("키"), str) or not row["키"]:
                _fail("review_invalid_key")
            key = row["키"]
            if key not in reviews:
                _fail("review_unknown_key")
            safe_key = key
            page_id = _page_id(row.get("id") or row.get("url"))
            linked = reviews[key]["artifact_page_id"]
            if linked and _page_id(linked) != page_id:
                conflicts.add(key)
                _fail("review_page_conflict")
            if key in indexed and indexed[key][0] != page_id:
                conflicts.add(key)
                _fail("review_duplicate_key")
            indexed[key] = (page_id, row)
        except Exception as exc:
            failures.append(_failure(safe_key, stage, exc))
    for key in conflicts:
        indexed.pop(key, None)
    return indexed, conflicts


def sync_reviews(publisher):
    """Import only explicit learner submissions; replay durable pending events."""
    result = {"imported": 0, "failures": []}
    store = publisher.store
    _, reviews = _inventory(store)
    handled = set()
    for key, review in reviews.items():
        try:
            pending = store.get_meta(_PENDING + key)
            if pending is None:
                continue
            reviews[key] = _resume(publisher, review, pending, result)
        except Exception as exc:
            handled.add(key)
            result["failures"].append(_failure(key, "review_sync", exc))
    try:
        rows = publisher._rows("reviews", view="incoming")
        if not isinstance(rows, list):
            _fail("review_invalid_rows")
    except Exception as exc:
        result["failures"].append(_failure("reviews", "review_sync", exc))
        return result
    indexed, _ = _remote_rows(rows, reviews, "review_sync", result["failures"])
    for key, (page_id, candidate) in indexed.items():
        if key in handled:
            continue
        review = reviews[key]
        try:
            seen = _snapshot({}, candidate)
            if not seen["submitted"]:
                _fail("review_invalid_submission")
            page, props = _fetch(publisher, page_id, review)
            fresh = _snapshot(page, props)
            if not fresh["submitted"]:
                continue  # A stale view row is not a new attempt.
            if seen != fresh:
                _fail("review_draft_changed")
            event_id = _event_id(page_id, key, fresh)
            if store.get_meta(_COMPLETED + event_id) is not None:
                _fail("review_replayed_submission")
            if not review["artifact_page_id"]:
                store.link_review_page(key, page_id)
            pending = {"review_key": key, "page_id": page_id, "event_id": event_id,
                       "rating": fresh["rating"], "answer": fresh["answer"],
                       "edited_at": fresh["edited_at"], "observed_edited_at": fresh["edited_at"]}
            # Persist intent BEFORE recording: a crash after the SQLite attempt
            # commit cannot leave an event whose remote acknowledgement is lost.
            store.set_meta(_PENDING + key, pending)
            _resume(publisher, review, pending, result)
        except Exception as exc:
            result["failures"].append(_failure(key, "review_sync", exc))
    return result


def publish_reviews(publisher, version_id):
    """Upsert immutable card bodies and system fields without consuming drafts.

    Returns per-card failures. The publisher facade must treat any failures as
    incomplete publication so the runner does not checkpoint a partial version.
    """
    store = publisher.store
    result = {"published": 0, "deactivated": 0, "failures": []}
    versions, reviews = _inventory(store)
    version = next((item for item in versions if item["id"] == version_id), None)
    if version is None:
        result["failures"].append(_failure("reviews", "review_publish", ReviewPublicationError("review_unknown_version")))
        return result
    try:
        rows = publisher._rows("reviews")
        if not isinstance(rows, list):
            _fail("review_invalid_rows")
    except Exception as exc:
        result["failures"].append(_failure(version_id, "review_publish", exc))
        return result
    indexed, conflicts = _remote_rows(rows, reviews, "review_publish", result["failures"])
    for key, review in reviews.items():
        if review["material_key"] != version["key"] or key in conflicts:
            continue
        target = review["version_id"] == version_id
        obsolete = not review["active"]
        if not target and not obsolete:
            continue
        try:
            page_id = indexed[key][0] if key in indexed else review["artifact_page_id"]
            if page_id:
                page_id = _page_id(page_id)
            if not target and not page_id:
                continue  # Never create missing historical cards just to hide them.
            if target:
                source_url = publisher._record_link(version_id)
                desired = _properties(review, source_url)
            else:
                desired = {"활성": _NO}
            if page_id:
                page, props = _fetch(publisher, page_id, review)
                changed = {name: value for name, value in desired.items()
                           if not _same_property(name, props.get(name), value)}
                if changed:
                    publisher._update(page_id, changed, expected_revision=page["revision"])
            else:
                from .study_render import render_review

                created = publisher._row("reviews", key, desired, render_review(review, source_url))
                page_id = _page_id(created.get("id") or created.get("url"))
            if review["artifact_page_id"] != page_id:
                store.link_review_page(key, page_id)
            result["published" if target else "deactivated"] += 1
        except Exception as exc:
            result["failures"].append(_failure(key, "review_publish", exc))
    return result
