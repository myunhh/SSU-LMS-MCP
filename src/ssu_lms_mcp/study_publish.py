"""Deterministic private artifact publication and original OneDrive uploads."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import uuid
from datetime import UTC, datetime

from .study_artifacts import ArtifactError, ArtifactRepository


class PublicationError(RuntimeError):
    """Safe machine-readable publication failure, without upstream response text."""

    def __init__(self, code):
        self.code = code if re.fullmatch(r"[A-Za-z0-9_]+", str(code)) else "publication_failed"
        super().__init__(self.code)


class CloudBridge:
    def __init__(self, command):
        self.command = list(command)
        self.process = None
        self.responses = queue.Queue()

    def __enter__(self):
        self.responses = queue.Queue()
        self.process = subprocess.Popen(
            [*self.command, "--serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            env={**os.environ, "PYTHONUTF8": "1"},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        process, responses = self.process, self.responses
        def receive():
            try:
                for line in process.stdout:
                    try:
                        responses.put(json.loads(line))
                    except ValueError:
                        responses.put({"ok": False, "error_type": "invalid_cloud_response"})
            except (OSError, ValueError):
                pass
            finally:
                responses.put(None)
        self.reader = threading.Thread(target=receive, daemon=True)
        self.reader.start()
        return self

    def request(self, action, **data):
        if self.process is None:
            self.__enter__()
        request_id = uuid.uuid4().hex
        try:
            self.process.stdin.write(json.dumps(
                {"action": action, "request_id": request_id, **data}, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            response = self.responses.get(timeout=240)
        except (OSError, ValueError, queue.Empty):
            self.__exit__()
            raise PublicationError("cloud_unavailable") from None
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            self.__exit__()
            raise PublicationError("cloud_response_identity_mismatch")
        if not response.get("ok"):
            code = response.get("error_type", "cloud_failed")
            raise PublicationError(code if re.fullmatch(r"[A-Za-z0-9_]+", str(code)) else "cloud_failed")
        if "result" not in response:
            self.__exit__()
            raise PublicationError("cloud_response_invalid")
        return response["result"]

    def __exit__(self, *_):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
            self.process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.process.kill()
            self.process.wait(timeout=15)
        finally:
            self.process.stdout.close()
            self.reader.join(timeout=2)
            self.process = None


DB_DEFINITIONS = {
    "materials": ("학습 자료", """CREATE TABLE ("자료명" TITLE, "키" RICH_TEXT, "과목" RICH_TEXT, "과목ID" NUMBER, "주차" RICH_TEXT, "교수" RICH_TEXT, "유형" SELECT('주차자료':blue,'게시판':orange), "상태" SELECT('기준선':gray,'완료':green,'검토필요':red), "LMS" URL, "원본" URL, "최신학습" URL, "SHA256" RICH_TEXT, "수정버전" NUMBER, "수집일" DATE)"""),
    "reviews": ("복습 큐", """CREATE TABLE ("질문" TITLE, "키" RICH_TEXT, "과목ID" NUMBER, "개념" RICH_TEXT, "자료" URL, "복습일" DATE, "평가" SELECT('이해함':green,'애매함':yellow,'모름':red), "내답" RICH_TEXT, "제출" CHECKBOX, "처리이벤트" RICH_TEXT, "최근평가" SELECT('이해함':green,'애매함':yellow,'모름':red), "시도" NUMBER, "활성" CHECKBOX, "난도우선" NUMBER, "마지막수정" LAST_EDITED_TIME)"""),
    "records": ("학습 페이지", """CREATE TABLE ("제목" TITLE, "키" RICH_TEXT, "분류" SELECT('자료버전':blue,'일일브리핑':green,'시험대비':orange), "과목ID" NUMBER, "자료키" RICH_TEXT, "SHA256" RICH_TEXT, "기준일" DATE, "검토필요" CHECKBOX)"""),
}
HUB_CONTENT = """매일 오전 7:30 한국 시간에 새 자료와 수정본을 확인합니다. 첫 수집은 원본 보관과 기준선 등록만 하고 AI 분석은 생략합니다.
## 학습 순서
1. 자료 DB에서 최신 학습 페이지와 OneDrive 원본을 엽니다.
2. 10분 학습 또는 심화 학습 후 셀프 테스트를 풉니다.
3. 복습 DB에 평가와 내 답을 입력한 뒤 제출을 체크합니다. 다음 실행에서 복습 일정이 갱신됩니다.
4. 시험 대비는 과목 또는 자료를 명시하여 요청합니다. 문제는 AI 연습문제이며 출제 예측이 아닙니다.
## 내 노트
이 영역은 자동으로 수정하지 않습니다."""
PERSONAL_NOTES = "\n## 내 노트\n이 영역은 자동으로 수정하지 않습니다."



def _has_snapshot(content, key, digest):
    marker = hashlib.sha256(key.encode()).hexdigest()[:20]
    start = f"SSU-AUTO {marker} {digest} BEGIN"
    end = f"SSU-AUTO {marker} END"
    lines = content.splitlines()
    positions = [index for index, line in enumerate(lines) if line == start]
    if not positions:
        return False
    if len(positions) != 1:
        raise PublicationError("artifact_snapshot_ambiguous")
    for line in lines[positions[0] + 1:]:
        if line == end:
            return True
        if line.startswith(f"SSU-AUTO {marker} ") and line.endswith(" BEGIN"):
            break
    raise PublicationError("artifact_snapshot_incomplete")


def _managed(key, body):
    marker = hashlib.sha256(key.encode()).hexdigest()[:20]
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return digest, f"SSU-AUTO {marker} {digest} BEGIN\n{body}\nSSU-AUTO {marker} END"


def _snapshot_inventory(content):
    snapshots = {}
    for marker, digest in re.findall(r"^SSU-AUTO ([0-9a-f]{20}) ([0-9a-f]{64}) BEGIN$", content, re.M):
        if f"SSU-AUTO {marker} END" not in content:
            raise PublicationError("artifact_snapshot_incomplete")
        snapshots.setdefault(marker, [])
        if digest not in snapshots[marker]:
            snapshots[marker].append(digest)
    return snapshots


class StudyPublisher:
    def __init__(self, store, config):
        if store.get_meta("notion:hub"):
            raise PublicationError("artifact_migration_required")
        self.store, self.config = store, config
        self.bridge = CloudBridge(config["cloud_command"])
        self.repository = ArtifactRepository(config["artifact"]["root"])
        self.port = config["artifact"].get("port", 8766)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.bridge.__exit__(*args)

    def _receipt(self, document):
        return {"id": document["id"], "url": self.repository.document_url(document["id"], self.port)}

    def _fetch(self, page_id):
        return self.repository.get_document(page_id)

    def _update(self, page_id, properties, *, expected_revision=None):
        page = self._fetch(page_id)
        title_field = {"materials": "자료명", "reviews": "질문", "records": "제목"}.get(page.get("role"))
        owned = page.get("user_fields", [])
        changes = {name: value for name, value in properties.items()
                   if f"properties.{name}" not in owned or name in {"제출", "처리이벤트"}}
        if "title" in owned or f"properties.{title_field}" in owned:
            changes.pop(title_field, None)
        update = {"properties": changes}
        if title_field in changes:
            update["title"] = changes[title_field]
        return self.repository.update_document(
            page_id, update, expected_revision=expected_revision or page["revision"])

    def initialize(self, courses):
        try:
            manifest = self.repository.manifest()
        except ArtifactError as exc:
            if exc.code != "artifact_repository_uninitialized":
                raise
            hub_id = str(uuid.uuid4())
            collections = {role: str(uuid.uuid4()) for role in DB_DEFINITIONS}
            manifest = self.repository.initialize(
                hub_id=hub_id, title="SSU 학습 허브", collections=collections)
        identities = [(manifest["hub_id"], None, "page", None, manifest["title"], HUB_CONTENT)]
        identities.extend((document_id, manifest["hub_id"], "database", role, DB_DEFINITIONS[role][0], "")
                          for role, document_id in manifest["collections"].items())
        for document_id, parent_id, kind, role, title, content in identities:
            try:
                self._fetch(document_id)
            except ArtifactError as exc:
                if exc.code != "artifact_document_not_found":
                    raise
                document = {"id": document_id, "parent_id": parent_id, "kind": kind,
                            "role": role, "title": title, "content": content, "properties": {}}
                if role:
                    document["schema"] = {"definition": DB_DEFINITIONS[role][1]}
                self.repository.create_document(document)
        hub = {"hub_id": manifest["hub_id"],
               "hub_url": self.repository.document_url(manifest["hub_id"], self.port)}
        self.store.set_meta("artifact:hub", hub)
        for role, document_id in manifest["collections"].items():
            self.store.set_meta("artifact:db:" + role, {"id": document_id})
        return {**hub, **{role + "_db_id": document_id
                         for role, document_id in manifest["collections"].items()}}

    def _rows(self, db_key, view=None):
        documents = self.repository.list_documents()
        rows = [{**document["properties"], "id": document["id"],
                 "url": self.repository.document_url(document["id"], self.port),
                 "마지막수정": document["updated_at"], "revision": document["revision"]}
                for document in documents
                if document.get("role") == db_key and document["kind"] == "page"
                and document.get("managed_key") and not document.get("source", {}).get("archived")]
        return [row for row in rows if row.get("제출") == "__YES__"] if view == "incoming" else rows

    def _row(self, db_key, unique_key, properties, content):
        documents = self.repository.list_documents()
        matches = [document for document in documents
                   if document.get("role") == db_key and document.get("managed_key") == unique_key]
        if len(matches) > 1:
            raise PublicationError("artifact_duplicate_identity")
        cache_key = f"artifact:row:{db_key}:{unique_key}"
        cached = self.store.get_meta(cache_key)
        if matches:
            document = matches[0]
            if document.get("source", {}).get("archived"):
                raise PublicationError("artifact_managed_page_archived")
            if cached and cached["id"] != document["id"]:
                raise PublicationError("artifact_identity_conflict")
            self._update(document["id"], {**properties, "키": unique_key},
                         expected_revision=document["revision"])
        else:
            if cached:
                raise PublicationError("artifact_managed_page_missing")
            title_field = {"materials": "자료명", "reviews": "질문", "records": "제목"}[db_key]
            defaults = {"평가": None, "내답": "", "제출": "__NO__", "처리이벤트": None} if db_key == "reviews" else {}
            document = self.repository.create_document({
                "parent_id": self.repository.manifest()["collections"][db_key],
                "kind": "page", "role": db_key, "managed_key": unique_key,
                "title": properties[title_field], "content": content + PERSONAL_NOTES,
                "properties": {**defaults, **properties, "키": unique_key},
                "source": {"publication_snapshots": _snapshot_inventory(content)},
            })
        page = self._receipt(document)
        self.store.set_meta(cache_key, page)
        return page

    def _set_body(self, page, key, body):
        digest, managed = _managed(key, body)
        document = self._fetch(page["id"])
        marker = hashlib.sha256(key.encode()).hexdigest()[:20]
        known = document.get("source", {}).get("publication_snapshots", {}).get(marker, [])
        if digest in known:
            return
        changes = {"source": {"publication_snapshots": {marker: [*known, digest]}}}
        if not _has_snapshot(document["content"], key, digest):
            changes["content"] = managed + "\n---\n" + document["content"]
        self.repository.update_document(page["id"], changes, expected_revision=document["revision"])

    def _is_latest(self, version):
        current = self.store.current_version(version["key"])
        return current is not None and current["id"] == version["id"]

    def upload(self, version):
        receipt = self.bridge.request(
            "onedrive-upload", source=version["source"], artifact=version.get("upload_artifact") or version)
        if (not isinstance(receipt, dict) or receipt.get("sha256") != version["sha256"]
                or receipt.get("size") != version["size"] or receipt.get("status") not in {"saved", "exists"}):
            raise PublicationError("upload_receipt_mismatch")
        return receipt

    def _material_properties(self, version, uploaded, state):
        from .study_render import safe_text
        source = version["source"]
        properties = {
            "자료명": safe_text(source["title"]), "과목": safe_text(source["course_name"]),
            "과목ID": source["course_id"], "주차": safe_text(source.get("module_name", "")),
            "교수": safe_text(source.get("professor", "")),
            "유형": "주차자료" if source["kind"] == "material" else "게시판", "상태": state,
            "LMS": source["source_url"], "원본": uploaded.get("web_url"), "SHA256": version["sha256"],
            "수정버전": sum(row["key"] == version["key"] for row in self.store.versions()),
        }
        seen_key = "artifact:first_seen:" + version["key"]
        seen = self.store.get_meta(seen_key)
        if not seen:
            seen = datetime.now(UTC).isoformat()
            self.store.set_meta(seen_key, seen)
        properties.update({"date:수집일:start": seen, "date:수집일:is_datetime": 1})
        return properties

    def publish_baseline(self, version, uploaded):
        if not self._is_latest(version):
            return {"baseline": bool(version["baseline"]), "historical": True}
        state = "검토필요" if version.get("review_required") else "기준선"
        from .study_render import safe_link, safe_text
        body = "원본과 메타데이터를 등록했습니다. 기준선 자료에는 AI 학습 요약을 만들지 않았습니다."
        if version.get("review_required"):
            body = "문서 분석을 완료하지 못했습니다. 검토가 필요합니다.\n" + "\n".join(
                "- " + safe_text(reason) for reason in version.get("review_reasons", []))
        if uploaded.get("web_url"):
            body += "\n" + safe_link("OneDrive 원본", uploaded["web_url"])
        page = self._row("materials", version["key"],
                         self._material_properties(version, uploaded, state), body)
        return {"artifact_page_id": page["id"], "artifact_url": page["url"],
                "baseline": bool(version["baseline"]), "review_required": state == "검토필요"}

    def publish(self, version, package, uploaded, related):
        from .study_render import render_package, safe_text
        key = "version:" + version["id"]
        body = render_package(package, version, uploaded, related)
        _, managed = _managed(key, body)
        page = self._row("records", key, {
            "제목": safe_text(version["source"]["title"]) + " · " + version["sha256"][:10],
            "분류": "자료버전", "과목ID": version["source"]["course_id"],
            "자료키": version["key"], "SHA256": version["sha256"],
            "검토필요": "__YES__" if package["review_required"] else "__NO__",
        }, managed)
        self._set_body(page, key, body)
        material_page = None
        if self._is_latest(version):
            properties = self._material_properties(
                version, uploaded, "검토필요" if package["review_required"] else "완료")
            properties["최신학습"] = page["url"]
            material_page = self._row("materials", version["key"], properties,
                                      "최신학습 속성에서 학습 페이지를 열 수 있습니다. 이전 버전은 학습 페이지 DB에 보존됩니다.")
        return {"artifact_page_id": (material_page or page)["id"], "artifact_url": page["url"],
                "version_page_id": page["id"], "material_url": (material_page or page)["url"]}

    def _record_link(self, version_id):
        page = self.store.get_meta("artifact:row:records:version:" + version_id)
        if page:
            return page["url"]
        stage = self.store.get_stage(version_id, "published") or {}
        if stage.get("artifact_url"):
            return stage["artifact_url"]
        raise PublicationError("study_page_not_published")

    def publish_reviews(self, version_id):
        from .study_artifact_review import publish_reviews
        outcome = publish_reviews(self, version_id)
        self.store.set_meta("artifact:review_publish:" + version_id, outcome)
        if outcome.get("failures"):
            raise PublicationError("review_publication_failed")
        return outcome

    def sync_reviews(self):
        from .study_artifact_review import sync_reviews
        return sync_reviews(self)

    def briefing(self, report):
        from .study_render import render_briefing
        key = "briefing:" + report["date"]
        body = render_briefing(report)
        _, managed = _managed(key, body)
        page = self._row("records", key, {
            "제목": report["date"] + " 학습 브리핑", "분류": "일일브리핑",
            "date:기준일:start": report["date"], "date:기준일:is_datetime": 0,
            "검토필요": "__YES__" if report.get("failures") else "__NO__",
        }, managed)
        self._set_body(page, key, body)
        return {"artifact_page_id": page["id"], "artifact_url": page["url"]}

    def publish_exam(self, exam, key):
        from .study_render import render_exam, safe_text
        selected = {item["source_key"] for item in exam["sources"]}
        latest = {source_key: self.store.current_version(source_key) for source_key in selected}
        if any(version is None for version in latest.values()):
            raise PublicationError("exam_current_source_missing")
        sources = {key: {"title": version["source"]["title"], "url": self._record_link(version["id"])}
                   for key, version in latest.items()}
        body = render_exam(exam, sources)
        unique_key = "exam:" + key
        _, managed = _managed(unique_key, body)
        page = self._row("records", unique_key, {
            "제목": safe_text(exam["title"]), "분류": "시험대비",
            "date:기준일:start": datetime.now(UTC).date().isoformat(),
            "date:기준일:is_datetime": 0,
            "검토필요": "__YES__" if exam["review_required"] else "__NO__",
        }, managed)
        self._set_body(page, unique_key, body)
        return {"artifact_page_id": page["id"], "artifact_url": page["url"]}
