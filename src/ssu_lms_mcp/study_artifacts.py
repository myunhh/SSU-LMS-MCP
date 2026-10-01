"""Private, file-backed study documents with atomic writes and revision checks."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from filelock import FileLock, Timeout

_ROLES = frozenset({"materials", "reviews", "records"})
_DOCUMENT_FIELDS = frozenset({
    "id", "title", "parent_id", "kind", "role", "content", "properties", "schema",
    "views", "source", "managed_key", "created_at", "updated_at", "revision", "user_fields",
})
_REQUIRED_FIELDS = _DOCUMENT_FIELDS - {"schema", "views", "source"}
_IDENTITY_FIELDS = frozenset({"id", "parent_id", "kind", "role", "managed_key", "created_at"})
_UUID = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")
_SECRET_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey", "api_token",
    "client_secret", "password", "authorization", "cookie", "cookies", "secret",
    "download_url", "downloadurl", "signed_url", "signedurl", "@microsoft.graph.downloadurl",
})
_SIGNED_QUERY_KEYS = frozenset({
    "sig", "signature", "token", "access_token", "tempauth", "authkey", "jwt",
})


class ArtifactError(RuntimeError):
    """Safe public failure code; never retain document content or filesystem errors."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and re.fullmatch(r"artifact_[a-z0-9_]+", code) else "artifact_repository_error"
        super().__init__(self.code)


def _fail(code="artifact_invalid_document"):
    raise ArtifactError(code)


def _document_id(value):
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        _fail()
    return str(uuid.UUID(value))


def document_url(document_id, port=8766):
    """Return a loopback editor route, never a cloud or public sharing URL."""
    identifier = _document_id(document_id)
    if type(port) is not int or not 1 <= port <= 65535:
        _fail()
    return f"http://127.0.0.1:{port}/#/page/{identifier}"


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for child in value:
            _json_value(child)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for child in value.values():
            _json_value(child)
        return
    _fail()


def _canonical(value):
    try:
        _json_value(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ArtifactError("artifact_invalid_document") from None


def _copy(value):
    return json.loads(_canonical(value))


def _revision(document):
    return hashlib.sha256(_canonical({key: value for key, value in document.items() if key != "revision"})).hexdigest()


def _timestamp(value):
    if not isinstance(value, str) or value != value.strip():
        _fail()
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _fail()
        return parsed
    except (ValueError, OverflowError):
        raise ArtifactError("artifact_invalid_document") from None


def _now(previous=None):
    try:
        instant = datetime.now(UTC)
        if previous is not None:
            prior = _timestamp(previous).astimezone(UTC)
            if instant <= prior:
                instant = prior + timedelta(microseconds=1)
        return instant.isoformat()
    except (ValueError, OverflowError):
        raise ArtifactError("artifact_invalid_document") from None


def _source(value):
    """Reject credentials rather than silently discarding unknown provenance fields."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold().replace("-", "_") in _SECRET_KEYS:
                _fail()
            _source(child)
    elif isinstance(value, list):
        for child in value:
            _source(child)
    elif isinstance(value, str):
        for match in re.finditer(r"https?://[^\s<>\"']+", value, flags=re.IGNORECASE):
            try:
                parsed = urlsplit(match.group())
                if parsed.username is not None or parsed.password is not None:
                    _fail()
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
                    key = key.casefold()
                    if key in _SIGNED_QUERY_KEYS or key.startswith(("x-amz-", "x-goog-")):
                        _fail()
            except ValueError:
                raise ArtifactError("artifact_invalid_document") from None


def _merge_source(current, changes):
    merged = dict(current)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_source(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate_document(document, *, stored=False):
    if not isinstance(document, dict) or set(document) - _DOCUMENT_FIELDS or not _REQUIRED_FIELDS <= document.keys():
        _fail()
    if document["id"] != _document_id(document["id"]):
        _fail()
    if document["parent_id"] is not None:
        if document["parent_id"] != _document_id(document["parent_id"]) or document["parent_id"] == document["id"]:
            _fail()
    if not isinstance(document["title"], str) or not isinstance(document["content"], str):
        _fail()
    if document["kind"] not in ("page", "database") or document["role"] not in (*_ROLES, None):
        _fail()
    if not isinstance(document["properties"], dict):
        _fail()
    key = document["managed_key"]
    property_key = document["properties"].get("키")
    if key is not None:
        if not isinstance(key, str) or not key.strip() or document["role"] not in _ROLES or property_key != key:
            _fail()
    elif document["role"] is not None and property_key not in (None, ""):
        _fail()
    fields = document["user_fields"]
    if not isinstance(fields, list) or any(
        not isinstance(field, str) or not (field in ("title", "content") or (field.startswith("properties.") and len(field) > 11))
        for field in fields
    ) or len(fields) != len(set(fields)):
        _fail()
    _timestamp(document["created_at"])
    _timestamp(document["updated_at"])
    if "source" in document:
        if not isinstance(document["source"], dict):
            _fail()
        _source(document["source"])
    revision = document["revision"]
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        _fail()
    if stored and revision != _revision(document):
        _fail("artifact_repository_conflict")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _within(path, root):
    return path == root or root in path.parents


def _no_links(path):
    # Cloud placeholders can have FILE_ATTRIBUTE_REPARSE_POINT too. Only reject
    # symlinks/junctions, not ordinary OneDrive hydrated/dehydrated files.
    for component in (*reversed(path.parents), path):
        if component.is_symlink() or component.is_junction():
            _fail("artifact_repository_conflict")


class ArtifactRepository:
    """One atomic JSON per document; all readers and writers share a local lock."""

    document_url = staticmethod(document_url)

    def __init__(self, root):
        try:
            candidate = Path(root).expanduser()
            if ".." in candidate.parts:
                _fail("artifact_repository_conflict")
            self.root = Path(os.path.abspath(candidate))
            _no_links(self.root)
            normalized = os.path.normcase(str(self.root))
            lock_name = hashlib.sha256(normalized.encode("utf-8")).hexdigest() + ".lock"
            self._lock_path = Path(tempfile.gettempdir()).absolute() / "ssu-lms-mcp" / "artifact-locks" / lock_name
            roots = [self.root]
            roots.extend(Path(value).absolute() for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial") if (value := os.environ.get(name)))
            if any(_within(self._lock_path, sync_root) for sync_root in roots):
                _fail("artifact_repository_conflict")
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            if isinstance(exc, ArtifactError):
                raise
            raise ArtifactError("artifact_repository_conflict") from None

    @contextmanager
    def _locked(self):
        try:
            _no_links(self.root)
            _no_links(self._lock_path)
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            # A fresh lock per call also serializes threads sharing this instance.
            with FileLock(str(self._lock_path), timeout=60):
                _no_links(self.root)
                yield
        except Timeout:
            raise ArtifactError("artifact_repository_conflict") from None
        except (OSError, UnicodeError):
            raise ArtifactError("artifact_repository_io_error") from None

    def _path(self, *parts):
        path = self.root.joinpath(*parts)
        if not _within(path, self.root):
            _fail("artifact_repository_conflict")
        _no_links(path)
        return path

    def _read_json(self, path):
        _no_links(path)
        if not stat.S_ISREG(path.stat().st_mode):
            _fail("artifact_repository_conflict")
        try:
            value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                               parse_constant=lambda _: _fail())
            _canonical(value)
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise ArtifactError("artifact_invalid_document") from None

    def _write_json(self, path, value):
        encoded = _canonical(value) + b"\n"
        _no_links(path)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            _no_links(path)
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _manifest(self):
        path = self._path("library.json")
        if not path.exists():
            _fail("artifact_repository_uninitialized")
        try:
            value = self._read_json(path)
            self._validate_manifest(value)
        except ArtifactError as exc:
            if exc.code == "artifact_invalid_document":
                raise ArtifactError("artifact_repository_conflict") from None
            raise
        for entry in self.root.iterdir():
            if entry.name != "library.json" and entry.name.casefold().startswith("library") and entry.suffix.casefold() == ".json":
                _fail("artifact_repository_conflict")
            if entry.name not in ("documents", "assets") and entry.is_dir() and entry.name.casefold().startswith(("documents", "assets")):
                _fail("artifact_repository_conflict")
        for directory in ("documents", "assets"):
            if not self._path(directory).is_dir():
                _fail("artifact_repository_conflict")
        return value

    @staticmethod
    def _validate_manifest(value):
        if not isinstance(value, dict) or set(value) != {"schema_version", "hub_id", "title", "collections", "source"}:
            _fail()
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            _fail()
        if value["hub_id"] != _document_id(value["hub_id"]) or not isinstance(value["title"], str):
            _fail()
        collections = value["collections"]
        if not isinstance(collections, dict) or set(collections) != _ROLES:
            _fail()
        ids = [value["hub_id"]]
        for identifier in collections.values():
            if identifier != _document_id(identifier):
                _fail()
            ids.append(identifier)
        if len(set(ids)) != len(ids) or not isinstance(value["source"], dict):
            _fail()
        _source(value["source"])

    def initialize(self, *, hub_id, title, collections, source=None):
        if not isinstance(collections, dict):
            _fail()
        value = _copy({
            "schema_version": 1, "hub_id": _document_id(hub_id), "title": title,
            "collections": {key: _document_id(identifier) for key, identifier in collections.items()},
            "source": {} if source is None else source,
        })
        self._validate_manifest(value)
        with self._locked():
            if self._path("library.json").exists():
                existing = self._manifest()
                if existing["hub_id"] != value["hub_id"] or existing["collections"] != value["collections"]:
                    _fail("artifact_repository_conflict")
                self._documents()
                return existing
            if self.root.exists():
                if not self.root.is_dir():
                    _fail("artifact_repository_conflict")
                for entry in self.root.iterdir():
                    _no_links(entry)
                    if entry.name not in ("documents", "assets") or not entry.is_dir() or any(entry.iterdir()):
                        _fail("artifact_repository_conflict")
            self.root.mkdir(parents=True, exist_ok=True)
            for directory in ("documents", "assets"):
                self._path(directory).mkdir(exist_ok=True)
            self._write_json(self._path("library.json"), value)
            return value

    def manifest(self):
        with self._locked():
            return self._manifest()

    def _documents(self):
        self._manifest()
        documents = []
        ids, keys = set(), set()
        filenames = []
        for path in sorted(self._path("documents").iterdir(), key=lambda item: item.name):
            _no_links(path)
            if path.suffix.casefold() != ".json":
                _fail("artifact_repository_conflict")
            document = self._read_json(path)
            _validate_document(document, stored=True)
            identifier = document["id"]
            if identifier in ids:
                _fail("artifact_duplicate_key")
            ids.add(identifier)
            if document["managed_key"] is not None:
                identity = (document["role"], document["managed_key"])
                if identity in keys:
                    _fail("artifact_duplicate_key")
                keys.add(identity)
            filenames.append((path.name, identifier + ".json"))
            documents.append(document)
        if any(actual != expected for actual, expected in filenames):
            _fail("artifact_repository_conflict")
        return documents

    def list_documents(self):
        with self._locked():
            return self._documents()

    def get_document(self, document_id):
        identifier = _document_id(document_id)
        with self._locked():
            for document in self._documents():
                if document["id"] == identifier:
                    return document
            _fail("artifact_document_not_found")

    def create_document(self, document: dict):
        if not isinstance(document, dict) or set(document) - _DOCUMENT_FIELDS:
            _fail()
        value = _copy(document)
        value["id"] = _document_id(value["id"]) if "id" in value else str(uuid.uuid4())
        if value.get("parent_id") is not None:
            value["parent_id"] = _document_id(value["parent_id"])
        value.setdefault("parent_id", None)
        value.setdefault("kind", "page")
        value.setdefault("role", None)
        value.setdefault("content", "")
        value.setdefault("properties", {})
        if "managed_key" not in value:
            value["managed_key"] = value["properties"].get("키") if isinstance(value["properties"], dict) and value["role"] is not None else None
            if value["managed_key"] == "":
                value["managed_key"] = None
        now = _now()
        value.setdefault("created_at", now)
        value.setdefault("updated_at", now)
        value.setdefault("user_fields", [])
        if isinstance(value["properties"], dict) and "마지막수정" in value["properties"]:
            value["properties"]["마지막수정"] = value["updated_at"]
        revision = _revision(value)
        if "revision" in value and value["revision"] != revision:
            _fail()
        value["revision"] = revision
        _validate_document(value)
        with self._locked():
            for existing in self._documents():
                if existing["id"] == value["id"] or (
                    value["managed_key"] is not None and (existing["role"], existing["managed_key"]) == (value["role"], value["managed_key"])
                ):
                    _fail("artifact_duplicate_key")
            path = self._path("documents", value["id"] + ".json")
            if path.exists():
                _fail("artifact_duplicate_key")
            self._write_json(path, value)
            return value

    def update_document(self, document_id, changes: dict, *, expected_revision: str, user_edit: bool = False):
        identifier = _document_id(document_id)
        if not isinstance(changes, dict) or set(changes) - (_DOCUMENT_FIELDS - {"revision", "updated_at"}):
            _fail()
        if not isinstance(expected_revision, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_revision) or type(user_edit) is not bool:
            _fail()
        changes = _copy(changes)
        with self._locked():
            documents = self._documents()
            current = next((document for document in documents if document["id"] == identifier), None)
            if current is None:
                _fail("artifact_document_not_found")
            if current["revision"] != expected_revision:
                _fail("artifact_revision_conflict")
            for field in _IDENTITY_FIELDS & changes.keys():
                proposed = changes[field]
                if field in ("id", "parent_id") and proposed is not None:
                    proposed = _document_id(proposed)
                    changes[field] = proposed
                if proposed != current[field]:
                    _fail("artifact_invalid_document")
            updated = {**current, **changes}
            if "properties" in changes:
                if not isinstance(changes["properties"], dict):
                    _fail()
                updated["properties"] = {**current["properties"], **changes["properties"]}
                if "마지막수정" in current["properties"]:
                    updated["properties"]["마지막수정"] = current["updated_at"]
            if "source" in changes:
                if not isinstance(changes["source"], dict):
                    _fail()
                updated["source"] = _merge_source(current.get("source", {}), changes["source"])
            fields = changes.get("user_fields", current["user_fields"])
            if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
                _fail()
            updated["user_fields"] = list(dict.fromkeys([*current["user_fields"], *fields]))
            if user_edit:
                for field in ("title", "content"):
                    if updated[field] != current[field] and field not in updated["user_fields"]:
                        updated["user_fields"].append(field)
                for name, value in changes.get("properties", {}).items():
                    field = "properties." + name
                    changed = name not in current["properties"] or _canonical(value) != _canonical(current["properties"][name])
                    if name != "마지막수정" and changed and field not in updated["user_fields"]:
                        updated["user_fields"].append(field)
            _validate_document(updated)
            if _canonical(updated) == _canonical(current):
                return current
            updated["updated_at"] = _now(current["updated_at"])
            if "마지막수정" in updated["properties"]:
                updated["properties"] = {**updated["properties"], "마지막수정": updated["updated_at"]}
            updated["revision"] = _revision(updated)
            path = self._path("documents", identifier + ".json")
            # A cloud replacement is not covered by the local lock. Recheck the
            # complete file immediately before replacement instead of trusting it.
            latest = self._read_json(path)
            _validate_document(latest, stored=True)
            if latest != current:
                _fail("artifact_revision_conflict")
            self._write_json(path, updated)
            return updated
