"""Read-only Notion hub export, verified artifact import, then SQLite cutover.

No Notion mutation tools are exposed here. Source files and pages are never
removed. Partial imports have no SQLite cutover and can be resumed safely;
a completed import never overwrites later learner edits.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .study_artifacts import ArtifactError, ArtifactRepository
from .study_publish import CloudBridge, PublicationError, _snapshot_inventory

_UUID = r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
_URL = re.compile(r"https?://[^\s<>\"\]\)}]+")
_READ_ONLY = frozenset({"notion-fetch", "notion-query-data-sources", "notion-get-comments",
                        "notion-download-attachment"})


class MigrationError(PublicationError):
    pass


def _identity(value):
    matches = re.findall(_UUID, str(value))
    if len(matches) != 1:
        raise MigrationError("migration_identifier_invalid")
    return str(uuid.UUID(matches[0]))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _unwrap(result):
    if not isinstance(result, dict) or result.get("isError"):
        raise MigrationError("migration_source_failed")
    structured = result.get("structuredContent")
    texts = [part["text"] for part in result.get("content", [])
             if isinstance(part, dict) and part.get("type") == "text"]
    if structured is None:
        if not texts:
            raise MigrationError("migration_source_empty")
        try:
            structured = json.loads("\n".join(texts))
        except ValueError:
            structured = {"text": "\n".join(texts)}
    if not isinstance(structured, dict):
        raise MigrationError("migration_source_invalid")
    result = dict(structured)
    if not result.get("text") and texts:
        result["text"] = "\n".join(texts)
    return result


def _complete(response):
    text = response.get("text", "")
    if (response.get("truncated") or response.get("unknown_block_count")
            or response.get("unknown_block_ids") or response.get("has_more")
            or re.search(r"<unknown\b|<synced_block_reference\b[^>]*\bnotice=", text)):
        raise MigrationError("migration_source_incomplete")
    return response


def _section(text, name, *, required=True):
    found = re.search(r"<" + name + r"\b[^>]*>([\s\S]*?)</" + name + r">", text)
    if not found:
        if required:
            raise MigrationError("migration_source_structure_missing")
        return None
    return found.group(1).removeprefix("\n").removesuffix("\n")


def _attrs(value):
    return {name: html.unescape(text).strip("{}")
            for name, text in re.findall(r'([\w-]+)="([^"]*)"', value)}


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, TypeError, AttributeError):
        raise MigrationError("migration_timestamp_invalid") from None


def _notion_url(value):
    try:
        parsed = urlsplit(value.strip("{}"))
        host = parsed.hostname or ""
        if parsed.scheme == "https" and any(host == domain or host.endswith("." + domain)
                                            for domain in ("notion.so", "notion.com", "notion.site")):
            return _identity(parsed.path)
    except (ValueError, MigrationError):
        pass
    return None


def _uploaded_url(value):
    try:
        parsed = urlsplit(html.unescape(value))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and (host in {"prod-files-secure.s3.us-west-2.amazonaws.com", "file.notion.so"}
                 or host.endswith(".notionusercontent.com")
                 or (host in {"s3.us-west-2.amazonaws.com", "s3.amazonaws.com"}
                     and parsed.path.startswith(("/secure.notion-static.com/", "/prod-files-secure/")))))


class _AssetRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not _uploaded_url(newurl):
            raise MigrationError("migration_asset_redirect_invalid")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _source_fingerprint(value):
    if isinstance(value, dict):
        return {key: _source_fingerprint(item) for key, item in value.items() if key != "expiry_time"}
    if isinstance(value, list):
        return [_source_fingerprint(item) for item in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r'^Here is the result of "fetch"[^\n]*\n', "", value)
    return _URL.sub(lambda match: urlsplit(match.group())._replace(query="").geturl()
                    if _uploaded_url(match.group()) else match.group(), value)


def _file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _download_asset(url, directory, *, max_bytes=512 * 1024 * 1024):
    """Stream only known Notion uploaded-file hosts; no credentials or URL logs."""
    if not _uploaded_url(url):
        raise MigrationError("migration_asset_origin_invalid")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or directory.resolve().parent != directory.parent.resolve():
        raise MigrationError("migration_asset_path_invalid")
    temporary = None
    deadline = time.monotonic() + 300
    try:
        with build_opener(_AssetRedirect()).open(Request(html.unescape(url)), timeout=60) as response:
            if not _uploaded_url(response.url):
                raise MigrationError("migration_asset_redirect_invalid")
            expected = response.headers.get("Content-Length")
            if expected is not None and int(expected) > max_bytes:
                raise MigrationError("migration_asset_limit")
            size, digest = 0, hashlib.sha256()
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".import-", delete=False) as output:
                temporary = Path(output.name)
                while True:
                    if time.monotonic() > deadline:
                        raise MigrationError("migration_asset_timeout")
                    chunk = response.read1(1024 * 1024)
                    if time.monotonic() > deadline:
                        raise MigrationError("migration_asset_timeout")
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise MigrationError("migration_asset_limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if expected is not None and int(expected) != size:
                raise MigrationError("migration_asset_incomplete")
        extension = Path(unquote(urlsplit(url).path)).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            extension = ".bin"
        filename = digest.hexdigest() + extension
        destination = directory / filename
        if destination.exists():
            if destination.is_symlink() or _file_digest(destination) != digest.hexdigest():
                raise MigrationError("migration_asset_conflict")
            temporary.unlink()
        else:
            os.replace(temporary, destination)
        temporary = None
        return {"path": "assets/" + filename, "sha256": digest.hexdigest(), "size": size}
    except MigrationError:
        raise
    except Exception:
        raise MigrationError("migration_asset_download_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class NotionReader:
    """Only documented read tools, never the former publication CRUD facade."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.observed = {}
        self.discussions = {}

    def call(self, name, arguments):
        if name not in _READ_ONLY:
            raise MigrationError("migration_mutation_forbidden")
        return _unwrap(self.bridge.request("notion-call", name=name, arguments=arguments))

    def fetch(self, identity):
        response = _complete(self.call("notion-fetch", {
            "id": identity, "include_discussions": True, "include_transcript": True,
        }))
        fingerprint = _digest(_source_fingerprint(response))
        if identity in self.observed and self.observed[identity] != fingerprint:
            raise MigrationError("migration_source_changed")
        self.observed[identity] = fingerprint
        return response

    def comments(self, identity):
        response = _complete(self.call("notion-get-comments", {
            "page_id": identity, "include_all_blocks": True, "include_resolved": True,
        }))
        if not isinstance(response.get("text"), str):
            raise MigrationError("migration_comments_incomplete")
        if 'kind="suggested_edit"' in response["text"]:
            raise MigrationError("migration_suggested_edit_unavailable")
        fingerprint = _digest(_source_fingerprint(response))
        if identity in self.discussions and self.discussions[identity] != fingerprint:
            raise MigrationError("migration_source_changed")
        self.discussions[identity] = fingerprint
        return response

    def verify(self):
        for identity in list(self.observed):
            self.fetch(identity)
        for identity in list(self.discussions):
            self.comments(identity)

    def attachment(self, identity):
        response = _complete(self.call("notion-download-attachment", {"file_upload_id": identity}))
        if not isinstance(response.get("content"), str):
            raise MigrationError("migration_attachment_incomplete")
        return response["content"].encode("utf-8")

    def rows(self, source_url, views):
        indexed = {}
        unfiltered = [view for view in views
                      if view["state"].get("dataSourceUrl", "").strip("{}") == source_url
                      and not view["state"].get("advancedFilter") and not view["state"].get("filter")]
        if not unfiltered:
            raise MigrationError("migration_unfiltered_view_required")
        # Unfiltered cursor enumeration covers both archive partitions without
        # consuming the plan-limited SQL quota. The caller verifies it again.
        for archived in (False, True):
            cursor, cursors, ids = None, set(), set()
            while True:
                data = {"mode": "view", "view_url": unfiltered[0]["url"],
                        "page_size": 100, "is_archived": archived}
                if cursor:
                    data["start_cursor"] = cursor
                result = self.call("notion-query-data-sources", {"data": data})
                if result.get("truncated") or not isinstance(result.get("results"), list):
                    raise MigrationError("migration_rows_invalid")
                for row in result["results"]:
                    identity = _identity(row.get("url") or row.get("id"))
                    if identity in ids or (archived and identity in indexed):
                        raise MigrationError("migration_duplicate_identity")
                    ids.add(identity)
                    indexed[identity] = {**row, **({"archived": True} if archived else {})}
                if result.get("has_more") is False:
                    if result.get("next_cursor"):
                        raise MigrationError("migration_pagination_gap")
                    break
                cursor = result.get("next_cursor")
                if not result.get("has_more") or not cursor or cursor in cursors or not result["results"]:
                    raise MigrationError("migration_pagination_gap")
                cursors.add(cursor)
        return indexed


def _install_assets(root, staging, records):
    for record in records:
        destination = root / record["path"]
        if destination.is_symlink() or not destination.resolve().is_relative_to(root.resolve()):
            raise MigrationError("migration_asset_path_invalid")
        if destination.exists():
            if destination.stat().st_size != record["size"] or _file_digest(destination) != record["sha256"]:
                raise MigrationError("migration_asset_conflict")
            continue
        temporary = None
        try:
            with (staging / destination.name).open("rb") as source:
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".import-", delete=False) as output:
                    temporary = Path(output.name)
                    shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _migrate_notion(config, store, asset_directory):
    """Import the configured SSU hub; return verified counts/mapping/report.

    The caller owns live invocation. This function never publishes to Notion.
    Every remote read completes before document creation or metadata cutover.
    """
    prior = store.get_meta("artifact:migration")
    hub = store.get_meta("notion:hub")
    if not hub and prior:
        hub = {"hub_id": prior["source_hub_id"]}
    if not hub:
        raise MigrationError("migration_source_hub_missing")
    hub_id = _identity(hub["hub_id"])
    root = Path(config["artifact"]["root"])
    repository = ArtifactRepository(root)
    port = config["artifact"].get("port", 8766)
    file_limit = config["artifact"].get("max_attachment_bytes", 512 * 1024 * 1024)
    total_limit = config["artifact"].get("max_import_bytes", 2 * 1024 * 1024 * 1024)
    if any(type(limit) is not int or limit <= 0 for limit in (file_limit, total_limit)):
        raise MigrationError("migration_limits_invalid")
    collections = {}
    for role in ("materials", "reviews", "records"):
        database = store.get_meta("notion:db:" + role) or store.get_meta("artifact:db:" + role)
        if not database:
            raise MigrationError("migration_collection_missing")
        collections[role] = _identity(database["id"])
    roles = {identity: role for role, identity in collections.items()}
    documents, source_owners, source_counts, source_rows = {}, {}, {}, {}
    pending = [(hub_id, None, None, None)]
    with CloudBridge(config["cloud_command"]) as bridge:
        reader = NotionReader(bridge)
        while pending:
            identity, parent_id, role, row = pending.pop(0)
            if identity in documents:
                raise MigrationError("migration_duplicate_identity")
            response = reader.fetch(identity)
            if _identity(response.get("url")) != identity:
                raise MigrationError("migration_source_identity_conflict")
            text = response.get("text")
            kind = response.get("metadata", {}).get("type")
            if kind not in {"page", "database"} or not isinstance(text, str):
                raise MigrationError("migration_source_structure_missing")
            ancestor = _section(text, "ancestor-path")
            ancestors = {_identity(url) for url in re.findall(r'url="([^\"]+)"', ancestor)}
            if parent_id and parent_id not in ancestors:
                raise MigrationError("migration_out_of_scope_descendant")
            role = roles.get(identity, role)
            source = {"notion_id": identity, "notion_url": "https://www.notion.so/" + identity.replace("-", "")}
            if row and row.get("archived"):
                source["archived"] = True
            if kind == "database":
                properties, content = {}, text
                states, views = {}, []
                for attrs, body in re.findall(r"<data-source\b([^>]*)>([\s\S]*?)</data-source>", text):
                    source_url = _attrs(attrs).get("url", "")
                    source_id = _identity(source_url)
                    if source_id in source_owners:
                        raise MigrationError("migration_linked_source_out_of_scope")
                    source_owners[source_id] = identity
                    state = json.loads(_section(body, "data-source-state"))
                    full = reader.fetch(source_url)
                    states[source_url] = {"state": state, "full": full}
                if not states:
                    raise MigrationError("migration_database_schema_missing")
                for attrs, body in re.findall(r"<view\b([^>]*)>([\s\S]*?)</view>", text):
                    view_url = _attrs(attrs).get("url", "")
                    state = json.loads(body)
                    views.append({"url": view_url, "state": state, "full": reader.fetch(view_url)})
                for source_url in states:
                    rows = reader.rows(source_url, views)
                    source_counts[source_url] = len(rows)
                    source_rows[source_url] = (views, set(rows))
                    pending.extend((row_id, identity, role, values) for row_id, values in rows.items())
                extra = {"schema": states, "views": views}
            else:
                properties = json.loads(_section(text, "properties"))
                if not isinstance(properties, dict):
                    raise MigrationError("migration_properties_invalid")
                content = _section(text, "content")
                comments = reader.comments(identity)
                source["comments"] = comments
                discussions = set(re.findall(r"discussion://[^\s<>\"]+", text))
                if any(url not in comments["text"] for url in discussions):
                    raise MigrationError("migration_comments_incomplete")
                for child_kind, attrs in re.findall(r"<(page|database|folder)\b([^>]*)>", content):
                    child_url = _attrs(attrs).get("url")
                    if not child_url:
                        raise MigrationError("migration_child_identity_missing")
                    pending.append((_identity(child_url), identity, None, None))
                extra = {}
                for field in ("icon", "cover", "verification"):
                    if field in response:
                        source[field] = response[field]
                # Keep full discussions visible as well as preserving exact metadata.
                if re.search(r"<discussion\b", comments["text"]):
                    content += "\n\n## 가져온 댓글\n" + comments["text"]
            source["publication_snapshots"] = _snapshot_inventory(content)
            updated = response.get("page_last_edited_at") or properties.get("마지막수정")
            created = (row or {}).get("createdTime") or response.get("created_time")
            document = {"id": identity, "parent_id": parent_id, "kind": kind, "role": role,
                        "title": response.get("title", ""), "properties": properties, "content": content,
                        "managed_key": properties.get("키") if role else None, "source": source,
                        "user_fields": ["title"], **extra}
            if updated:
                document["updated_at"] = _timestamp(updated)
                if "마지막수정" in properties:
                    properties["마지막수정"] = document["updated_at"]
            if created:
                document["created_at"] = _timestamp(created)
            elif updated:
                document["created_at"] = document["updated_at"]
                source["created_time_unavailable"] = True
            documents[identity] = document
        if any(identity not in documents for identity in collections.values()):
            raise MigrationError("migration_collection_out_of_scope")
        reader.verify()
        for source_url, (views, expected_ids) in source_rows.items():
            if set(reader.rows(source_url, views)) != expected_ids:
                raise MigrationError("migration_source_changed")
    mapping = {identity: {"id": identity, "url": repository.document_url(identity, port)} for identity in documents}
    assets, external_links = {}, set()

    def transform(value):
        if isinstance(value, dict):
            if value.get("type") == "external" and isinstance(value.get("external"), dict):
                return value
            if value.get("type") == "file" and isinstance(value.get("file"), dict):
                uploaded = value["file"].get("url")
                if not _uploaded_url(uploaded or ""):
                    raise MigrationError("migration_asset_origin_invalid")
                return {**{key: transform(item) for key, item in value.items() if key != "file"},
                        "file": {"url": asset(uploaded)}}
            return {
                key: (item if key == "내답" else
                      "artifact:" + item[len("notion:"):] if key == "처리이벤트" and isinstance(item, str) and item.startswith("notion:")
                      else transform(item))
                for key, item in value.items() if key != "expiry_time"
            }
        if isinstance(value, list):
            return [transform(item) for item in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'^Here is the result of "fetch"[^\n]*\n', "", value)
        def rewrite(match):
            url = html.unescape(match.group())
            if _uploaded_url(url):
                return asset(url)
            identity = _notion_url(url)
            if identity in mapping:
                return mapping[identity]["url"]
            if identity:
                external_links.add(identity)
            return match.group()
        value = _URL.sub(rewrite, value)
        return re.sub(r"file-upload://(" + _UUID + r")",
                      lambda match: text_attachment(str(uuid.UUID(match.group(1)))), value)

    def asset(url):
        if url not in assets:
            remaining = total_limit - sum(record["size"] for record in assets.values())
            assets[url] = _download_asset(url, asset_directory, max_bytes=min(file_limit, remaining))
        return "/" + assets[url]["path"]

    def text_attachment(identity):
        key = "file-upload://" + identity
        if key not in assets:
            with CloudBridge(config["cloud_command"]) as bridge:
                content = NotionReader(bridge).attachment(identity)
            digest = hashlib.sha256(content).hexdigest()
            if len(content) > min(file_limit, total_limit - sum(record["size"] for record in assets.values())):
                raise MigrationError("migration_asset_limit")
            directory = asset_directory
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink():
                raise MigrationError("migration_asset_path_invalid")
            path = directory / (digest + ".txt")
            if path.exists():
                if path.is_symlink() or path.read_bytes() != content:
                    raise MigrationError("migration_asset_conflict")
            else:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=directory, prefix=".import-", delete=False) as output:
                        temporary = Path(output.name)
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            assets[key] = {"path": "assets/" + path.name, "sha256": digest, "size": len(content)}
        return "/" + assets[key]["path"]

    for identity, document in documents.items():
        original_url = document["source"]["notion_url"]
        documents[identity] = transform(document)
        documents[identity]["source"]["notion_url"] = original_url
        documents[identity]["source"]["import_digest"] = _digest(documents[identity])
    backup_path = Path(str(store.path) + ".artifact-import-" + uuid.uuid4().hex[:10] + ".sqlite3")
    backup = store.backup(backup_path)
    manifest = repository.initialize(hub_id=hub_id, title=documents[hub_id]["title"],
                                     collections=collections, source={"notion_hub_id": hub_id})
    _install_assets(root, asset_directory, {record["path"]: record for record in assets.values()}.values())
    created_count, preserved_count, created_ids = 0, 0, set()
    for identity, document in documents.items():
        try:
            existing = repository.get_document(identity)
        except ArtifactError as exc:
            if exc.code != "artifact_document_not_found":
                raise
            repository.create_document(document)
            created_count += 1
            created_ids.add(identity)
        else:
            if (existing.get("source", {}).get("notion_id") != identity
                    or existing["source"].get("import_digest") != document["source"]["import_digest"]):
                raise MigrationError("migration_existing_document_conflict")
            preserved_count += 1
    inventory = {document["id"]: document for document in repository.list_documents()}
    for identity, document in documents.items():
        saved = inventory.get(identity)
        if (not saved or saved.get("source", {}).get("import_digest") != document["source"]["import_digest"]
                or saved["parent_id"] != document["parent_id"] or saved["managed_key"] != document["managed_key"]):
            raise MigrationError("migration_verification_failed")
        if identity in created_ids and any(saved.get(key) != document.get(key)
                                                for key in ("title", "content", "properties", "schema", "views")):
            raise MigrationError("migration_verification_failed")
    asset_records = {record["path"]: record for record in assets.values()}
    for record in asset_records.values():
        path = root / record["path"]
        if path.is_symlink() or path.stat().st_size != record["size"] or _file_digest(path) != record["sha256"]:
            raise MigrationError("migration_asset_verification_failed")
    counts = {"documents": len(documents), "pages": sum(d["kind"] == "page" for d in documents.values()),
              "databases": sum(d["kind"] == "database" for d in documents.values())}
    counts.update({role: sum(d["kind"] == "page" and d["role"] == role for d in documents.values()) for role in collections})
    report = {"status": "done", "replay": prior is not None, "source_hub_id": hub_id,
              "counts": counts, "mapping": mapping,
              "created": created_count, "preserved": preserved_count, "backup": backup,
              "attachments": {"verified": True, "count": len(asset_records), "files": list(asset_records.values())},
              "completeness": {"verified": True, "data_sources": source_counts,
                               "out_of_scope_links": sorted(external_links), "source_untouched": True}}
    store.migrate_artifact_references(mapping, manifest, report, destination_ids=inventory)
    return report


def migrate_notion(config, store):
    """Return a verified ``status=done`` report, or a safe coded failure."""
    try:
        with tempfile.TemporaryDirectory(prefix=".artifact-import-", dir=store.path.parent) as staging:
            return _migrate_notion(config, store, Path(staging))
    except (PublicationError, ArtifactError):
        raise
    except json.JSONDecodeError:
        raise MigrationError("migration_source_json_invalid") from None
    except ValueError as exc:
        code = str(exc)
        raise MigrationError(code if re.fullmatch(r"[a-z][a-z0-9_]+", code)
                             else "migration_invalid_state") from None
    except OSError:
        raise MigrationError("migration_io_failed") from None
    except (KeyError, TypeError):
        raise MigrationError("migration_source_structure_missing") from None
    except Exception:
        raise MigrationError("migration_failed") from None
