"""Offline collection and repeat-run acceptance tests."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from ssu_lms_mcp import automation
from ssu_lms_mcp.config import Settings
from ssu_lms_mcp.session import SessionRequiredError


class _Manager:
    def __init__(self, settings):
        self.settings = settings

    @asynccontextmanager
    async def client(self):
        yield object()


def _wire(monkeypatch, *, notices=None):
    monkeypatch.setattr(automation, "SessionManager", _Manager)

    async def courses(client):
        return [{"id": 2, "name": "two"}, {"id": 1, "name": "one"}]

    async def empty(client, course_id):
        return []

    monkeypatch.setattr(automation, "list_courses", courses)
    monkeypatch.setattr(automation, "list_notices", notices or empty)
    monkeypatch.setattr(automation, "list_assignments", empty)
    monkeypatch.setattr(automation, "list_materials", empty)

    async def boards(client, settings, course_id):
        return []

    monkeypatch.setattr(automation, "list_board_posts", boards)


@pytest.mark.asyncio
async def test_collect_stable_ids_and_retains_pending_on_unchanged_run(tmp_path, monkeypatch):
    async def notices(client, cid):
        return [{"id": 10, "course_id": cid, "title": f"공지 {cid}", "message_text": "실제 내용"}]
    _wire(monkeypatch, notices=notices)
    settings = Settings(data_dir=tmp_path)
    first = await automation.collect(settings, download=False)
    pending_path = tmp_path / "automation/pending.json"
    first_pending = json.loads(pending_path.read_text("utf-8"))["items"]
    assert {item["key"] for item in first_pending} == {"notice:1:10", "notice:2:10"}
    second = await automation.collect(settings, download=False)
    assert [row["id"] for row in first["courses"]] == [1, 2]
    pending = json.loads((tmp_path / "automation" / "pending.json").read_text("utf-8"))
    assert pending["items"] == first_pending
    assert second["status"] == "ok"
    pending_path.write_text('{"items":[]}', encoding="utf-8")
    await automation.collect(settings, download=False)
    assert json.loads(pending_path.read_text("utf-8"))["items"] == []


@pytest.mark.asyncio
async def test_collect_partial_preserves_prior_and_sanitizes_exception(tmp_path, monkeypatch):
    async def original(client, cid):
        return [{"id": 10, "course_id": cid, "title": f"old-{cid}"}]
    _wire(monkeypatch, notices=original)
    settings = Settings(data_dir=tmp_path)
    await automation.collect(settings, download=False)

    async def broken(client, course_id):
        if course_id == 1:
            raise RuntimeError("password-canary https://signed.example/secret")
        return [{"id": 10, "course_id": 2, "title": "updated-2"}]

    _wire(monkeypatch, notices=broken)
    result = await automation.collect(settings, download=False)
    assert result["status"] == "partial"
    assert [(row["course_id"], row["title"]) for row in result["notices"]] == [(1, "old-1"), (2, "updated-2")]
    assert "canary" not in json.dumps(result)
    assert result["errors"][0]["type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_collect_auth_error_is_preserved(tmp_path, monkeypatch):
    _wire(monkeypatch)
    settings = Settings(data_dir=tmp_path)

    async def auth(client, course_id):
        raise SessionRequiredError("credential-canary")

    _wire(monkeypatch, notices=auth)
    with pytest.raises(SessionRequiredError) as error:
        await automation.collect(settings, download=False)
    assert "canary" not in str(error.value)


async def test_filtered_successful_empty_replaces_only_requested_partition(tmp_path, monkeypatch):
    async def original(client, cid):
        return [{"id": 10, "course_id": cid, "title": f"old-{cid}"}]
    settings = Settings(data_dir=tmp_path)
    _wire(monkeypatch, notices=original)
    await automation.collect(settings, download=False)
    _wire(monkeypatch)
    result = await automation.collect(settings, course_ids=[1], download=False)
    assert [row["course_id"] for row in result["notices"]] == [2]
    assert [row["id"] for row in result["courses"]] == [1, 2]


async def test_collection_cancellation_propagates_without_snapshot(tmp_path, monkeypatch):
    async def cancelled(client, cid):
        raise asyncio.CancelledError
    _wire(monkeypatch, notices=cancelled)
    with pytest.raises(asyncio.CancelledError):
        await automation.collect(Settings(data_dir=tmp_path), download=False)
    assert not (tmp_path / "automation/latest.json").exists()


async def test_download_failure_retry_and_exists_do_not_requeue(tmp_path, monkeypatch):
    _wire(monkeypatch)
    async def materials(client, cid):
        return [{"course_id": cid, "id": 20, "item_type": "File", "title": "교안"}] if cid == 1 else []
    responses = iter([{"status": "failed", "reason": "network"}, {"status": "saved", "path": "1/lecture.pdf"}, {"status": "exists", "path": "1/lecture.pdf"}])
    async def download(settings, client, cid, mid, **kwargs):
        assert (cid, mid) == (1, 20)
        return next(responses)
    monkeypatch.setattr(automation, "list_materials", materials)
    monkeypatch.setattr(automation, "download_material", download)
    settings = Settings(data_dir=tmp_path)
    first = await automation.collect(settings)
    assert first["status"] == "partial" and first["errors"][0]["type"] == "DownloadFailed"
    pending_path = tmp_path / "automation/pending.json"
    pending_path.write_text('{"items":[]}', encoding="utf-8")
    second = await automation.collect(settings)
    assert second["status"] == "ok"
    assert [x["key"] for x in json.loads(pending_path.read_text("utf-8"))["items"]] == ["download:material:1:20"]
    pending_path.write_text('{"items":[]}', encoding="utf-8")
    await automation.collect(settings)
    assert json.loads(pending_path.read_text("utf-8"))["items"] == []
