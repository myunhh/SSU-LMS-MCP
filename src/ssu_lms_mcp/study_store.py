"""Transactional study state; the caller supplies a private runtime database path.

Source descriptors and stage payloads must contain stable links, never signed
binary-download URLs. A version snapshots its source and acquisition metadata;
replaying discovery or acquisition cannot rewrite an existing version. Pending
uploads separately retain the latest verified artifact identity until success.

Current acquisition and generated-review heads are persisted separately from
immutable version order and first-acquisition predecessor identities. Restoring
content reuses its history and reconciles publication without repeating completed
acquisition or generation work.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

STAGES = frozenset({"uploaded", "extracted", "generated", "published"})
RATINGS = frozenset({"understood", "uncertain", "unknown"})
INTERVAL_DAYS = (1, 3, 7, 14, 30)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _utc(value=None):
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("study timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _limit(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("limit must be a nonnegative integer")
    return value


class StudyStore:
    """Own a WAL SQLite connection and commit each state transition atomically."""

    def __init__(self, path):
        if str(path) != ":memory:":
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            existing = {row["name"] for row in self._db.execute("PRAGMA table_info(reviews)")}
            if "notion_page_id" in existing and str(path) != ":memory:":
                backup = Path(str(path) + ".pre-artifact.sqlite3")
                if not backup.exists():
                    self.backup(backup)
                else:
                    with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as snapshot:
                        columns = {row[1] for row in snapshot.execute("PRAGMA table_info(reviews)")}
                        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or "notion_page_id" not in columns:
                            raise ValueError("study_backup_invalid")
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS partitions (
                    name TEXT PRIMARY KEY,
                    initialized_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    key TEXT PRIMARY KEY,
                    partition_name TEXT NOT NULL REFERENCES partitions(name),
                    metadata_hash TEXT NOT NULL,
                    descriptor TEXT NOT NULL,
                    baseline INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY,
                    material_key TEXT NOT NULL REFERENCES sources(key),
                    sha256 TEXT NOT NULL,
                    artifact TEXT NOT NULL,
                    source TEXT NOT NULL,
                    course_id INTEGER NOT NULL,
                    baseline INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    previous_version_id TEXT REFERENCES versions(id),
                    UNIQUE(material_key, sha256)
                );
                CREATE TABLE IF NOT EXISTS stages (
                    version_id TEXT NOT NULL REFERENCES versions(id),
                    name TEXT NOT NULL,
                    data TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY(version_id, name)
                );
                CREATE TABLE IF NOT EXISTS stage_errors (
                    version_id TEXT NOT NULL REFERENCES versions(id),
                    name TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    failed_at TEXT NOT NULL,
                    PRIMARY KEY(version_id, name)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    key TEXT PRIMARY KEY,
                    version_id TEXT NOT NULL REFERENCES versions(id),
                    material_key TEXT NOT NULL,
                    course_id INTEGER NOT NULL,
                    quiz TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    interval_index INTEGER NOT NULL DEFAULT 0,
                    rating TEXT,
                    active INTEGER NOT NULL,
                    artifact_page_id TEXT
                );
                CREATE INDEX IF NOT EXISTS reviews_due ON reviews(active, due_at);
                CREATE INDEX IF NOT EXISTS reviews_version ON reviews(version_id);
                CREATE TABLE IF NOT EXISTS review_attempts (
                    id INTEGER PRIMARY KEY,
                    review_key TEXT NOT NULL REFERENCES reviews(key),
                    rating TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    at TEXT NOT NULL,
                    event_id TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS attempts_review ON review_attempts(review_key, id);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS version_heads (
                    material_key TEXT PRIMARY KEY REFERENCES sources(key),
                    current_version_id TEXT NOT NULL REFERENCES versions(id),
                    active_review_version_id TEXT REFERENCES versions(id)
                );
                INSERT OR IGNORE INTO version_heads
                    (material_key, current_version_id, active_review_version_id)
                SELECT v.material_key, v.id, (
                    SELECT generated.id FROM versions generated
                    JOIN stages s ON s.version_id = generated.id AND s.name = 'generated'
                    WHERE generated.material_key = v.material_key
                    ORDER BY generated.rowid DESC LIMIT 1
                )
                FROM versions v
                WHERE v.rowid = (
                    SELECT MAX(latest.rowid) FROM versions latest
                    WHERE latest.material_key = v.material_key
                );
                UPDATE reviews SET active = COALESCE(version_id = (
                    SELECT active_review_version_id FROM version_heads h
                    WHERE h.material_key = reviews.material_key
                ), 0);
                COMMIT;
            """)
            with self._transaction():
                review_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(reviews)")}
                if "notion_page_id" in review_columns:
                    self._db.execute("ALTER TABLE reviews RENAME COLUMN notion_page_id TO artifact_page_id")
                columns = {row["name"] for row in self._db.execute("PRAGMA table_info(versions)")}
                if "previous_version_id" not in columns:
                    self._db.execute(
                        "ALTER TABLE versions ADD COLUMN previous_version_id TEXT REFERENCES versions(id)"
                    )
                    # Legacy stores did not retain restoration transitions. Preserve
                    # their prior insertion-order lineage without rebuilding history.
                    self._db.execute("""
                        UPDATE versions SET previous_version_id = (
                            SELECT prior.id FROM versions prior
                            WHERE prior.material_key = versions.material_key
                              AND prior.rowid < versions.rowid
                            ORDER BY prior.rowid DESC LIMIT 1
                        )
                    """)
        except BaseException:
            self._db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self._db.close()

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def observe(self, items, successful_partitions):
        """Complete each successful partition's baseline, including empty scans.

        Items from failed partitions are ignored. Baseline eligibility persists
        across acquisition failures, but is permanently cleared by a revision.
        Missing items are never deleted by a partial or subsequent discovery.
        """
        successful = set(successful_partitions)
        now = _utc().isoformat()
        observed = []
        with self._transaction():
            initialized = {
                row["name"] for row in self._db.execute("SELECT name FROM partitions")
            }
            for partition in sorted(successful):
                self._db.execute(
                    "INSERT OR IGNORE INTO partitions VALUES (?, ?)", (partition, now)
                )
            seen = set()
            for item in items:
                if item["partition"] not in successful or item["key"] in seen:
                    continue
                seen.add(item["key"])
                descriptor = {
                    key: value for key, value in item.items()
                    if key not in {"baseline", "metadata_changed"}
                }
                prior = self._db.execute(
                    "SELECT * FROM sources WHERE key = ?", (item["key"],)
                ).fetchone()
                changed = prior is not None and prior["metadata_hash"] != item["metadata_hash"]
                baseline = (
                    bool(prior["baseline"]) and not changed if prior is not None
                    else item["partition"] not in initialized
                )
                self._db.execute(
                    """INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                           metadata_hash = excluded.metadata_hash,
                           descriptor = excluded.descriptor,
                           baseline = excluded.baseline,
                           last_seen_at = excluded.last_seen_at""",
                    (item["key"], item["partition"], item["metadata_hash"],
                     _json(descriptor), int(baseline), now, now),
                )
                observed.append({**descriptor, "baseline": baseline, "metadata_changed": changed})
        return observed

    def sources(self):
        return [
            {**json.loads(row["descriptor"]), "baseline": bool(row["baseline"])}
            for row in self._db.execute("SELECT descriptor, baseline FROM sources ORDER BY key")
        ]

    def source(self, key):
        row = self._db.execute(
            "SELECT descriptor, baseline FROM sources WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else {
            **json.loads(row["descriptor"]), "baseline": bool(row["baseline"])
        }

    def record_version(self, key, artifact, baseline=False, force=False):
        """Observe the hash, capturing its first predecessor without rewriting history."""
        sha256 = artifact["sha256"]
        version_id = hashlib.sha256(_json([key, sha256]).encode("utf-8")).hexdigest()
        with self._transaction():
            head = self._db.execute(
                "SELECT current_version_id FROM version_heads WHERE material_key = ?", (key,)
            ).fetchone()
            existing = self._db.execute(
                "SELECT id, artifact FROM versions WHERE material_key = ? AND sha256 = ?", (key, sha256)
            ).fetchone()
            if existing is not None:
                version_id = existing["id"]
                if self.get_stage(version_id, "uploaded") is None:
                    snapshot = {
                        name: artifact.get(name) for name in ("filename", "path", "size")
                    }
                    upload_key = f"upload_artifact:{version_id}"
                    if snapshot != json.loads(existing["artifact"]):
                        self._db.execute(
                            """INSERT INTO metadata VALUES (?, ?)
                               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                            (upload_key, _json({**snapshot, "sha256": sha256})),
                        )
                    else:
                        self._db.execute("DELETE FROM metadata WHERE key = ?", (upload_key,))
                restored = head is not None and head["current_version_id"] != version_id
                if force or restored:
                    self._db.execute("UPDATE versions SET baseline = 0 WHERE id = ?", (version_id,))
                if restored:
                    self._db.execute(
                        "DELETE FROM stages WHERE version_id = ? AND name = 'published'",
                        (version_id,),
                    )
            else:
                source = self.source(key)
                if source is None:
                    raise KeyError(key)
                source.pop("baseline")
                known = self._db.execute(
                    "SELECT 1 FROM versions WHERE material_key = ? LIMIT 1", (key,)
                ).fetchone()
                snapshot = {
                    name: artifact.get(name) for name in ("filename", "path", "size")
                }
                self._db.execute(
                    """INSERT INTO versions
                       (id, material_key, sha256, artifact, source, course_id, baseline,
                        created_at, previous_version_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (version_id, key, sha256, _json(snapshot), _json(source), source["course_id"],
                     int(bool(baseline) and known is None and not force), _utc().isoformat(),
                     None if head is None else head["current_version_id"]),
                )
            self._db.execute(
                """INSERT INTO version_heads (material_key, current_version_id)
                   VALUES (?, ?) ON CONFLICT(material_key) DO UPDATE SET
                       current_version_id = excluded.current_version_id""",
                (key, version_id),
            )
            self._activate_generated_current(key)
            return self.get_version(version_id)

    def _version(self, row):
        stages = {
            stage["name"]: json.loads(stage["data"])
            for stage in self._db.execute(
                "SELECT name, data FROM stages WHERE version_id = ? ORDER BY name", (row["id"],)
            )
        }
        errors = {
            error["name"]: {"error_type": error["error_type"], "failed_at": error["failed_at"]}
            for error in self._db.execute(
                "SELECT * FROM stage_errors WHERE version_id = ? ORDER BY name", (row["id"],)
            )
        }
        head = self._db.execute(
            "SELECT current_version_id FROM version_heads WHERE material_key = ?",
            (row["material_key"],),
        ).fetchone()
        upload_artifact = (
            self.get_meta(f"upload_artifact:{row['id']}") if "uploaded" not in stages else None
        )
        return {
            "id": row["id"], "key": row["material_key"], "sha256": row["sha256"],
            "previous_version_id": row["previous_version_id"],
            **json.loads(row["artifact"]), "source": json.loads(row["source"]),
            **({"upload_artifact": upload_artifact} if upload_artifact is not None else {}),
            "baseline": bool(row["baseline"]), "stages": stages, "errors": errors,
            "is_current": head is not None and head["current_version_id"] == row["id"],
        }

    def get_version(self, version_id):
        row = self._db.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        return None if row is None else self._version(row)

    def current_version(self, key):
        row = self._db.execute(
            """SELECT v.* FROM versions v
               JOIN version_heads h ON h.current_version_id = v.id
               WHERE h.material_key = ?""",
            (key,),
        ).fetchone()
        return None if row is None else self._version(row)

    def versions(self, pending_only=False):
        query = "SELECT v.* FROM versions v"
        if pending_only:
            query += """ WHERE (v.baseline = 0 AND NOT EXISTS (
                SELECT 1 FROM stages s WHERE s.version_id = v.id AND s.name = 'published'
            )) OR EXISTS (SELECT 1 FROM stage_errors e WHERE e.version_id = v.id)"""
        return [self._version(row) for row in self._db.execute(query + " ORDER BY v.rowid")]

    def _stage_name(self, stage):
        if stage not in STAGES:
            raise ValueError("unknown study stage")

    def _require_version(self, version_id):
        row = self._db.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(version_id)
        return row

    def save_stage(self, version_id, stage, data):
        self._stage_name(stage)
        if not isinstance(data, dict):
            raise TypeError("stage data must be a dictionary")
        payload = _json(data)
        with self._transaction():
            version = self._require_version(version_id)
            self._db.execute(
                """INSERT INTO stages VALUES (?, ?, ?, ?)
                   ON CONFLICT(version_id, name) DO UPDATE SET
                       data = excluded.data, completed_at = excluded.completed_at""",
                (version_id, stage, payload, _utc().isoformat()),
            )
            self._db.execute(
                "DELETE FROM stage_errors WHERE version_id = ? AND name = ?", (version_id, stage)
            )
            if stage == "uploaded":
                self._db.execute(
                    "DELETE FROM metadata WHERE key = ?", (f"upload_artifact:{version_id}",)
                )
            if stage == "generated":
                self._activate_generated_current(version["material_key"])

    def fail_stage(self, version_id, stage, error_type):
        """Keep successful payloads, and persist only a sanitized error category."""
        self._stage_name(stage)
        with self._transaction():
            self._require_version(version_id)
            self._db.execute(
                """INSERT INTO stage_errors VALUES (?, ?, ?, ?)
                   ON CONFLICT(version_id, name) DO UPDATE SET
                       error_type = excluded.error_type, failed_at = excluded.failed_at""",
                (version_id, stage, error_type, _utc().isoformat()),
            )

    def clear_stage_error(self, version_id, stage):
        self._stage_name(stage)
        with self._transaction():
            self._require_version(version_id)
            self._db.execute(
                "DELETE FROM stage_errors WHERE version_id = ? AND name = ?", (version_id, stage)
            )

    def get_stage(self, version_id, stage):
        self._stage_name(stage)
        row = self._db.execute(
            "SELECT data FROM stages WHERE version_id = ? AND name = ?", (version_id, stage)
        ).fetchone()
        return None if row is None else json.loads(row["data"])

    def related_materials(self, course_id, exclude_key=None, limit=8):
        rows = self._db.execute(
            """SELECT v.*, s.data AS generated FROM versions v
               JOIN stages s ON s.version_id = v.id AND s.name = 'generated'
               JOIN version_heads h ON h.active_review_version_id = v.id
               WHERE v.course_id = ? AND (? IS NULL OR v.material_key != ?)
               ORDER BY v.rowid DESC LIMIT ?""",
            (course_id, exclude_key, exclude_key, _limit(limit)),
        ).fetchall()
        related = []
        for row in rows:
            generated = json.loads(row["generated"])
            package = generated.get("package", generated)
            source = json.loads(row["source"])
            record = {
                "key": row["material_key"], "title": package.get("title", source["title"]),
                "package": package, "version_id": row["id"],
            }
            published = self.get_stage(row["id"], "published") or {}
            url = published.get("artifact_url")
            if url:
                record["artifact_url"] = url
            related.append(record)
        return related

    def _active_version(self, key):
        row = self._db.execute(
            "SELECT active_review_version_id FROM version_heads WHERE material_key = ?",
            (key,),
        ).fetchone()
        return row["active_review_version_id"] if row else None

    def _activate_generated_current(self, key):
        self._db.execute(
            """UPDATE version_heads SET active_review_version_id = current_version_id
               WHERE material_key = ? AND EXISTS (
                   SELECT 1 FROM stages s
                   WHERE s.version_id = current_version_id AND s.name = 'generated'
               )""",
            (key,),
        )
        self._db.execute(
            "UPDATE reviews SET active = COALESCE(version_id = ?, 0) WHERE material_key = ?",
            (self._active_version(key), key),
        )

    def add_reviews(self, version_id, quizzes, artifact_page_id=None, now=None):
        due_at = _utc(now).isoformat()
        keys = []
        with self._transaction():
            version = self._require_version(version_id)
            active = version_id == self._active_version(version["material_key"])
            for quiz in quizzes:
                key = f"{version_id}:{quiz['id']}"
                self._db.execute(
                    """INSERT INTO reviews
                           (key, version_id, material_key, course_id, quiz, due_at, active, artifact_page_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(key) DO NOTHING""",
                    (key, version_id, version["material_key"], version["course_id"],
                     _json(quiz), due_at, int(active), artifact_page_id),
                )
                if artifact_page_id is not None:
                    self._db.execute(
                        "UPDATE reviews SET artifact_page_id = COALESCE(artifact_page_id, ?) WHERE key = ?",
                        (artifact_page_id, key),
                    )
                if key not in keys:
                    keys.append(key)
            return [self._review_by_key(key) for key in keys]

    def _review(self, row):
        quiz = json.loads(row["quiz"])
        attempts = [
            {name: attempt[name] for name in ("rating", "answer", "at", "event_id")}
            for attempt in self._db.execute(
                "SELECT * FROM review_attempts WHERE review_key = ? ORDER BY at, id", (row["key"],)
            )
        ]
        return {
            "key": row["key"], "version_id": row["version_id"],
            "material_key": row["material_key"], "course_id": row["course_id"],
            "prompt": quiz["prompt"], "answer": quiz["answer"],
            "explanation": quiz.get("explanation", ""), "pages": quiz.get("pages", []),
            "concept": quiz.get("concept", ""), "due_at": row["due_at"],
            "interval_index": row["interval_index"], "rating": row["rating"],
            "attempts": attempts, "active": bool(row["active"]),
            "artifact_page_id": row["artifact_page_id"],
        }

    def _review_by_key(self, key):
        row = self._db.execute("SELECT * FROM reviews WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return self._review(row)

    def record_review(self, review_key, rating, answer="", at=None, event_id=None):
        """Advance intervals once per intake event; retain every typed attempt.

        interval_index is the next understood interval (initially/reset to zero).
        The fifth and subsequent consecutive understood answers stay at 30 days.
        Delayed events are replayed chronologically; equal timestamps retain
        intake order, so an older durable submission cannot replace a newer one.
        """
        if rating not in RATINGS:
            raise ValueError("rating must be understood, uncertain, or unknown")
        instant = _utc(at)
        with self._transaction():
            if event_id is not None:
                event = self._db.execute(
                    "SELECT review_key FROM review_attempts WHERE event_id = ?", (event_id,)
                ).fetchone()
                if event is not None:
                    if event["review_key"] != review_key:
                        raise ValueError("review event belongs to a different review")
                    return self._review_by_key(review_key)
            row = self._db.execute("SELECT * FROM reviews WHERE key = ?", (review_key,)).fetchone()
            if row is None:
                raise KeyError(review_key)
            newer = self._db.execute(
                "SELECT 1 FROM review_attempts WHERE review_key = ? AND at > ? LIMIT 1",
                (review_key, instant.isoformat()),
            ).fetchone()
            self._db.execute(
                "INSERT INTO review_attempts (review_key, rating, answer, at, event_id) VALUES (?, ?, ?, ?, ?)",
                (review_key, rating, answer, instant.isoformat(), event_id),
            )
            if newer is None:
                attempts = [{"rating": rating, "at": instant.isoformat()}]
                next_index = row["interval_index"]
            else:
                attempts = self._db.execute(
                    "SELECT rating, at FROM review_attempts WHERE review_key = ? ORDER BY at, id",
                    (review_key,),
                )
                next_index = 0
            for attempt in attempts:
                rating = attempt["rating"]
                instant = _utc(attempt["at"])
                if rating == "understood":
                    days = INTERVAL_DAYS[next_index]
                    next_index = min(next_index + 1, len(INTERVAL_DAYS) - 1)
                else:
                    days, next_index = 1, 0
            self._db.execute(
                "UPDATE reviews SET rating = ?, interval_index = ?, due_at = ? WHERE key = ?",
                (rating, next_index, (instant + timedelta(days=days)).isoformat(), review_key),
            )
            return self._review_by_key(review_key)

    def due_reviews(self, now=None, limit=30):
        rows = self._db.execute(
            """SELECT * FROM reviews WHERE active = 1 AND due_at <= ?
               ORDER BY CASE rating WHEN 'unknown' THEN 0 WHEN 'uncertain' THEN 1 ELSE 2 END,
                        due_at, key LIMIT ?""",
            (_utc(now).isoformat(), _limit(limit)),
        ).fetchall()
        return [self._review(row) for row in rows]

    def weak_reviews(self, course_ids=None, limit=30):
        limit = _limit(limit)
        query = "SELECT * FROM reviews WHERE active = 1 AND rating IN ('unknown', 'uncertain')"
        params = []
        if course_ids is not None:
            ids = list(course_ids)
            if not ids:
                return []
            query += " AND course_id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
        query += " ORDER BY CASE rating WHEN 'unknown' THEN 0 ELSE 1 END, due_at, key LIMIT ?"
        params.append(limit)
        return [self._review(row) for row in self._db.execute(query, params).fetchall()]

    def reviews_for_version(self, version_id):
        return [
            self._review(row) for row in self._db.execute(
                "SELECT * FROM reviews WHERE version_id = ? ORDER BY key", (version_id,)
            ).fetchall()
        ]

    def link_review_page(self, review_key, page_id):
        with self._transaction():
            changed = self._db.execute(
                "UPDATE reviews SET artifact_page_id = ? WHERE key = ?", (page_id, review_key)
            )
            if changed.rowcount == 0:
                raise KeyError(review_key)

    def get_meta(self, key, default=None):
        row = self._db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_meta(self, key, value):
        with self._transaction():
            self._db.execute(
                "INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, _json(value)),
            )

    def backup(self, destination):
        """Create an independent SQLite snapshot, including committed WAL data."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".study-backup-", suffix=".sqlite3", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(temporary)) as target:
                self._db.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("study_backup_invalid")
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            # Atomic exclusive publication: an interrupted snapshot has no final name.
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)

    def migrate_artifact_references(self, mapping, manifest, report, *, destination_ids=None):
        """Commit cutover after file verification; keep immutable sources intact."""
        import re
        import uuid
        from urllib.parse import urlsplit

        def convert(value, field=None):
            if isinstance(value, list):
                return [convert(item, field) for item in value]
            if isinstance(value, dict):
                return {{"notion_page_id": "artifact_page_id", "notion_url": "artifact_url"}.get(key, key):
                        convert(item, key) for key, item in value.items()}
            if not isinstance(value, str):
                return value
            if field == "event_id" and value.startswith("notion:"):
                return "artifact:" + value[len("notion:"):]
            if field not in {"notion_url", "artifact_url", "material_url", "hub_url", "url", "source_url"}:
                return value
            try:
                parsed = urlsplit(value)
            except ValueError:
                return value
            host = parsed.hostname or ""
            if parsed.scheme == "https" and any(host == domain or host.endswith("." + domain)
                                                for domain in ("notion.so", "notion.site", "notion.com")):
                match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F-]{36})$", parsed.path.rstrip("/"))
                if match:
                    identity = str(uuid.UUID(match.group(1)))
                    if identity in mapping:
                        return mapping[identity]["url"]
            return value

        with self._transaction():
            legacy = self.get_meta("notion:hub") is not None
            native_ids = set(mapping if destination_ids is None else destination_ids)
            for row in self._db.execute("SELECT key, value FROM metadata").fetchall():
                key = row["key"].replace("notion:", "artifact:") if row["key"].startswith("notion:") else row["key"]
                value = convert(json.loads(row["value"]))
                existing = self.get_meta(key)
                if key != row["key"] and existing is not None and existing != value:
                    raise ValueError("artifact_metadata_conflict")
                if row["key"].startswith("notion:row:") and value and value.get("id") not in mapping:
                    raise ValueError("artifact_cached_page_missing")
                self._db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, _json(value)))
                if key != row["key"]:
                    self._db.execute("DELETE FROM metadata WHERE key = ?", (row["key"],))
            for row in self._db.execute("SELECT version_id, data FROM stages WHERE name = 'published'").fetchall():
                receipt = json.loads(row["data"])
                for field in ("notion_page_id", "artifact_page_id", "version_page_id"):
                    allowed = mapping if legacy or "notion_page_id" in receipt else native_ids
                    if receipt.get(field) and receipt[field] not in allowed:
                        raise ValueError("artifact_published_page_missing")
                self._db.execute("UPDATE stages SET data = ? WHERE version_id = ? AND name = 'published'",
                                 (_json(convert(receipt)), row["version_id"]))
            for row in self._db.execute("SELECT id, event_id FROM review_attempts WHERE event_id LIKE 'notion:%'").fetchall():
                self._db.execute("UPDATE review_attempts SET event_id = ? WHERE id = ?",
                                 (convert(row["event_id"], "event_id"), row["id"]))
            for row in self._db.execute("SELECT artifact_page_id FROM reviews WHERE artifact_page_id IS NOT NULL"):
                if row["artifact_page_id"] not in (mapping if legacy else native_ids):
                    raise ValueError("artifact_review_page_missing")
            hub = {"hub_id": manifest["hub_id"], "hub_url": mapping[manifest["hub_id"]]["url"]}
            values = {"artifact:hub": hub, "artifact:migration": report}
            values.update({"artifact:db:" + role: {"id": identity}
                           for role, identity in manifest["collections"].items()})
            for key, value in values.items():
                self._db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, _json(value)))
