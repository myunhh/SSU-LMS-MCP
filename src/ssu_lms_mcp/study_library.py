"""Loopback-only editor for the user's native OneDrive artifact directory."""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID

from filelock import FileLock

from .study_artifacts import ArtifactError, ArtifactRepository

_MAX_BODY = 8 * 1024 * 1024
_RATINGS = {"understood": "이해함", "uncertain": "애매함", "unknown": "모름"}
_TITLE_FIELDS = {"materials": "자료명", "reviews": "질문", "records": "제목"}
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "style-src-elem 'self'; style-src-attr 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


class LibraryError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _root_identity(root):
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()


def _atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ssu-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _under(root, relative):
    parts = Path(relative)
    if parts.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in parts.parts):
        raise LibraryError("artifact_path_invalid")
    root = root.resolve()
    target = root
    for part in parts.parts:
        target = target / part
        if target.is_symlink() or target.is_junction():
            raise LibraryError("artifact_path_invalid")
    if not target.resolve().is_relative_to(root):
        raise LibraryError("artifact_path_invalid")
    return target


def _application_files():
    source = files("ssu_lms_mcp").joinpath("artifact_web")
    distribution = {}

    def collect(directory, prefix=""):
        for entry in directory.iterdir():
            name = prefix + entry.name
            if entry.is_dir():
                collect(entry, name + "/")
            elif entry.is_file():
                distribution[name] = entry.read_bytes()

    collect(source)
    if not {"index.html", "app.js", "styles.css"}.issubset(distribution):
        raise LibraryError("artifact_application_incomplete")
    return distribution


def _artifact_listing(root):
    artifacts = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            relative = entry.relative_to(root).as_posix()
            target = _under(root, relative)
            if target.is_dir():
                pending.append(target)
            elif target.is_file():
                stat = target.stat()
                artifacts.append({
                    "path": relative,
                    "name": target.name,
                    "size": stat.st_size,
                    "modified_ms": stat.st_mtime_ns // 1_000_000,
                })
    return sorted(artifacts, key=lambda item: item["path"].casefold())


def install_application(config, config_path):
    """Write the OneDrive launcher; web assets are served from the package, not copied."""
    root = Path(config["artifact"]["root"])
    ArtifactRepository(root).manifest()
    executable, configuration = str(Path(sys.executable).resolve()), str(Path(config_path).resolve())
    if any(character in executable + configuration for character in '\r\n%"'):
        raise LibraryError("artifact_launcher_path_invalid")
    launcher = (
        '@echo off\r\nchcp 65001 >nul\r\n'
        f'"{executable}" -m ssu_lms_mcp.study_cli --config "{configuration}" library --open\r\n'
        'if errorlevel 1 pause\r\n'
    ).encode()
    expected = hashlib.sha256(launcher).hexdigest()
    target = _under(root, "열기.cmd")
    # The launcher embeds this PC's paths, so its record stays in the local data directory.
    record_path = Path(config["data_dir"]) / "library-launcher.json"
    lock = FileLock(str(Path(config["data_dir"]) / "library-assets.lock"), timeout=15)
    with lock:
        previous = None
        if record_path.exists():
            try:
                previous = json.loads(record_path.read_text(encoding="utf-8"))["sha256"]
                if not isinstance(previous, str):
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise LibraryError("artifact_application_manifest_invalid") from None
        if target.exists():
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual not in {expected, previous}:
                raise LibraryError("artifact_application_modified")
            if actual != expected:
                _atomic_bytes(target, launcher)
        else:
            _atomic_bytes(target, launcher)
        _atomic_bytes(record_path, json.dumps({"schema_version": 1, "sha256": expected}).encode("utf-8"))
    return {"root": str(root), "launcher": str(target)}


class _LibraryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, config, port=None):
        self.config = config
        self.root = Path(config["artifact"]["root"]).resolve()
        self.repository = ArtifactRepository(self.root)
        self.repository.manifest()
        self.csrf = secrets.token_urlsafe(32)
        self.identity = _root_identity(self.root)
        self.application_files = _application_files()
        self.artifact_root = _under(self.root, "artifacts")
        if self.artifact_root.exists() and not self.artifact_root.is_dir():
            raise LibraryError("artifact_path_invalid")
        self.artifact_root.mkdir(exist_ok=True)
        super().__init__(("127.0.0.1", config["artifact"]["port"] if port is None else port), _Handler)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"
        self.authority = urlsplit(self.origin).netloc


class _Handler(BaseHTTPRequestHandler):
    server_version = "SSUArtifact/1"
    sys_version = ""

    def log_message(self, *_):
        # Document names, URL queries and user answers are private.
        pass

    def _headers(self, status, length, content_type, *, attachment=False, sandbox_document=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        csp = _CSP
        if sandbox_document:
            csp = (
                "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                "font-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
        elif attachment:
            csp = "sandbox; default-src 'none'"
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Frame-Options", "DENY")
        if attachment:
            self.send_header("Content-Disposition", "attachment")
        self.end_headers()

    def _send(self, status, content, content_type="application/json; charset=utf-8", *, attachment=False):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self._headers(status, len(content), content_type, attachment=attachment)
        if self.command != "HEAD":
            self.wfile.write(content)

    def _guard(self, *, mutation=False):
        if self.headers.get_all("Host", []) != [self.server.authority]:
            raise LibraryError("artifact_host_forbidden")
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            raise LibraryError("artifact_origin_forbidden")
        if mutation:
            token = self.headers.get("X-SSU-CSRF", "")
            if not token.isascii() or not hmac.compare_digest(token, self.server.csrf):
                raise LibraryError("artifact_csrf_invalid")
            if self.headers.get_content_type() != "application/json":
                raise LibraryError("artifact_json_required")

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise LibraryError("artifact_body_invalid")
        length = int(lengths[0])
        if not 0 < length <= _MAX_BODY:
            raise LibraryError("artifact_body_too_large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise LibraryError("artifact_body_invalid")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise LibraryError("artifact_body_invalid") from None
        if not isinstance(result, dict):
            raise LibraryError("artifact_body_invalid")
        return result

    @staticmethod
    def _id(value):
        try:
            identity = str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise LibraryError("artifact_document_not_found") from None
        if identity != value:
            raise LibraryError("artifact_document_not_found")
        return identity

    def _document_path(self, path, prefix):
        value = path.removeprefix(prefix)
        return self._id(value)

    def _get(self, path):
        repository = self.server.repository
        if path == "/health":
            return self._send(200, {"application": "ssu-artifact-library", "schema_version": 1,
                                    "root_identity": self.server.identity})
        if path == "/api/bootstrap":
            fields = ("id", "title", "parent_id", "kind", "role", "properties", "updated_at", "revision", "managed_key")
            documents = [{field: document.get(field) for field in fields} for document in repository.list_documents()]
            return self._send(200, {"manifest": repository.manifest(), "documents": documents,
                                    "csrf_token": self.server.csrf, "root_path": str(self.server.root),
                                    "base_url": self.server.origin})
        if path == "/api/artifacts":
            return self._send(200, {"artifacts": _artifact_listing(self.server.artifact_root),
                                    "root_path": str(self.server.artifact_root)})
        if path.startswith("/api/documents/"):
            document = repository.get_document(self._document_path(path, "/api/documents/"))
            return self._send(200, {"document": document})
        if path.startswith("/artifacts/"):
            relative = path.removeprefix("/artifacts/")
            target = _under(self.server.artifact_root, relative)
            if not target.is_file():
                raise LibraryError("artifact_file_not_found")
            html_document = target.suffix.casefold() in {".html", ".htm"}
            content_type = "text/html; charset=utf-8" if html_document else "application/octet-stream"
            with target.open("rb") as stream:
                self._headers(200, os.fstat(stream.fileno()).st_size, content_type,
                              attachment=not html_document, sandbox_document=html_document)
                if self.command != "HEAD":
                    shutil.copyfileobj(stream, self.wfile, 1024 * 1024)
            return
        relative = "index.html" if path == "/" else path.removeprefix("/")
        if relative in self.server.application_files:
            return self._send(200, self.server.application_files[relative],
                              mimetypes.guess_type(relative)[0] or "application/octet-stream")
        if relative.startswith("assets/") and re.fullmatch(r"assets/[A-Za-z0-9][A-Za-z0-9._-]{0,200}", relative):
            target = _under(self.server.root, relative)
            if not target.is_file():
                raise LibraryError("artifact_file_not_found")
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            inline = mime in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}
            with target.open("rb") as stream:
                self._headers(200, os.fstat(stream.fileno()).st_size, mime, attachment=not inline)
                if self.command != "HEAD":
                    shutil.copyfileobj(stream, self.wfile, 1024 * 1024)
            return
        raise LibraryError("artifact_document_not_found")

    def _mutate(self, path):
        body = self._body()
        repository = self.server.repository
        if self.command == "POST" and path == "/api/documents":
            if set(body) - {"title", "content", "parent_id", "properties"}:
                raise LibraryError("artifact_body_invalid")
            parent_id = body.get("parent_id") or repository.manifest()["hub_id"]
            parent = repository.get_document(self._id(parent_id))
            role = parent.get("role") if parent["kind"] == "database" else None
            document = repository.create_document({
                "title": body.get("title", "제목 없음"), "content": body.get("content", ""),
                "parent_id": parent_id, "kind": "page", "role": role,
                "properties": body.get("properties", {}), "managed_key": None,
            })
            return self._send(201, {"document": document})
        if self.command == "PATCH" and path.startswith("/api/documents/"):
            if set(body) - {"revision", "title", "content", "properties"}:
                raise LibraryError("artifact_body_invalid")
            identity = self._document_path(path, "/api/documents/")
            document = repository.get_document(identity)
            changes = {key: value for key, value in body.items() if key != "revision"}
            if "title" in changes:
                property_name = _TITLE_FIELDS.get(document.get("role"), "title")
                if property_name in document.get("properties", {}):
                    if not isinstance(changes.get("properties", {}), dict):
                        raise LibraryError("artifact_body_invalid")
                    changes["properties"] = {**changes.get("properties", {}), property_name: changes["title"]}
            saved = repository.update_document(identity, changes, expected_revision=body.get("revision"), user_edit=True)
            return self._send(200, {"document": saved})
        if self.command == "POST" and path.startswith("/api/reviews/") and path.endswith("/submit"):
            if set(body) != {"revision", "rating", "answer"} or body.get("rating") not in _RATINGS or not isinstance(body.get("answer"), str):
                raise LibraryError("artifact_review_invalid")
            identity = self._id(path[len("/api/reviews/"):-len("/submit")])
            document = repository.get_document(identity)
            if document.get("role") != "reviews" or not document.get("managed_key"):
                raise LibraryError("artifact_review_unmanaged")
            saved = repository.update_document(identity, {"properties": {
                "평가": _RATINGS[body["rating"]], "내답": body["answer"], "제출": "__YES__",
            }}, expected_revision=body["revision"], user_edit=True)
            return self._send(200, {"document": saved})
        raise LibraryError("artifact_document_not_found")

    def _handle(self):
        try:
            self.connection.settimeout(15)
            mutation = self.command in {"POST", "PATCH"}
            self._guard(mutation=mutation)
            location = urlsplit(self.path)
            if location.scheme or location.netloc or location.query:
                raise LibraryError("artifact_path_invalid")
            path = unquote(location.path, errors="strict")
            if "\\" in path or "\x00" in path or ".." in path.split("/"):
                raise LibraryError("artifact_path_invalid")
            if mutation:
                self._mutate(path)
            else:
                self._get(path)
        except (LibraryError, ArtifactError) as exc:
            code = exc.code
            status = 409 if code in {"artifact_revision_conflict", "artifact_duplicate_key"} else 400
            if code in {"artifact_document_not_found", "artifact_file_not_found"}:
                status = 404
            elif code in {"artifact_host_forbidden", "artifact_origin_forbidden", "artifact_csrf_invalid"}:
                status = 403
            elif code == "artifact_body_too_large":
                status = 413
            self._send(status, {"error": code})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._send(500, {"error": "artifact_request_failed"})

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PATCH = _handle


def make_server(config, *, port=None):
    return _LibraryServer(config, port=port)


def serve_library(config):
    with make_server(config) as server:
        server.serve_forever(poll_interval=0.25)


def library_status(config):
    origin = f'http://127.0.0.1:{config["artifact"]["port"]}'
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(Request(origin + "/health", headers={"Accept": "application/json"}), timeout=1) as response:
            value = json.load(response)
    except (URLError, TimeoutError, OSError) as exc:
        if isinstance(exc, HTTPError):
            raise LibraryError("artifact_port_in_use") from None
        return {"status": "stopped", "url": origin}
    except (ValueError, TypeError):
        raise LibraryError("artifact_port_in_use") from None
    if (not isinstance(value, dict) or value.get("application") != "ssu-artifact-library"
            or value.get("root_identity") != _root_identity(config["artifact"]["root"])):
        raise LibraryError("artifact_port_in_use")
    return {"status": "running", "url": origin, "root": config["artifact"]["root"]}


def launch_library(config, config_path, *, open_browser=True):
    installation = install_application(config, config_path)
    status = library_status(config)
    if status["status"] != "running":
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        child = subprocess.Popen([sys.executable, "-m", "ssu_lms_mcp.study_cli", "--config",
                                  str(Path(config_path).resolve()), "library", "--serve"], **kwargs)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise LibraryError("artifact_server_start_failed")
            status = library_status(config)
            if status["status"] == "running":
                break
            time.sleep(0.1)
        else:
            child.terminate()
            child.wait(timeout=5)
            raise LibraryError("artifact_server_start_timeout")
    opened = webbrowser.open(status["url"]) if open_browser else False
    return {"status": "ready", **installation, "url": status["url"], "browser_opened": bool(opened)}
