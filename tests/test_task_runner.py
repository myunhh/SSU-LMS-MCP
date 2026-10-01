"""Offline background runner and curation acknowledgement tests."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ssu_lms_mcp.config import Settings

spec = importlib.util.spec_from_file_location(
    "automation_task", Path(__file__).parents[1] / "scripts/automation-task.py"
)
task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task)


@pytest.fixture
def curation_skill(tmp_path, monkeypatch):
    skill = tmp_path / "curation-skill.md"
    skill.write_text("Summarize the supplied batch and acknowledge only completed items.", encoding="utf-8")
    monkeypatch.setattr(task, "SKILL", skill)


def test_export_reuses_user_file_and_never_overwrites_collision(tmp_path):
    settings = Settings(data_dir=tmp_path / "private")
    source = settings.materials_dir / "10/original.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"PDF content")
    export = tmp_path / "iCloud/2026-2"
    course = export / "운영체제_교수"
    course.mkdir(parents=True)
    user_file = course / "existing-name.pdf"
    user_file.write_bytes(source.read_bytes())
    snapshot = {"courses": [{"id": 10, "name": "운영체제 (123)", "professor": "교수"}], "downloads": [{"course_id": 10, "path": str(source)}]}
    config = {"export_root": str(export)}
    assert task.export_materials(settings, snapshot, config)[0]["path"] == str(user_file)
    source.write_bytes(b"Updated content")
    collision = course / "original.pdf"
    collision.write_bytes(b"user notes")
    exported = task.export_materials(settings, snapshot, config)
    assert Path(exported[0]["path"]).read_bytes() == b"Updated content"
    assert collision.read_bytes() == b"user notes"
    assert user_file.read_bytes() == b"PDF content"
    assert len(list(course.iterdir())) == 3
    assert task.export_materials(settings, snapshot, config) == exported


def test_export_rejects_escape(tmp_path):
    settings = Settings(data_dir=tmp_path / "private")
    export = tmp_path / "exports"
    export.mkdir()
    source = tmp_path / "outside.pdf"
    source.write_bytes(b"outside")
    with pytest.raises(ValueError, match="outside"):
        task.export_materials(settings, {"downloads": [{"path": str(source)}]}, {"export_root": str(export)})


async def test_acknowledgement_preserves_new_revision(tmp_path):
    settings = Settings(data_dir=tmp_path)
    path = tmp_path / "automation/pending.json"
    batch = {"items": [{"key": "notice:1:2", "hash": "old", "data": {}}]}
    task.write_json(path, {"items": [{"key": "notice:1:2", "hash": "new", "data": {}}]})
    assert await task.acknowledge(settings, batch, {"acknowledged": [{"key": "notice:1:2", "hash": "old", "evidence": "summarized"}]}) == 1
    assert task.read_json(path)["items"][0]["hash"] == "new"
    with pytest.raises(ValueError, match="outside"):
        await task.acknowledge(settings, batch, {"acknowledged": [{"key": "unknown", "hash": "old", "evidence": "x"}]})


async def test_no_pending_does_not_start_codex_and_failure_keeps_pending(tmp_path, monkeypatch, curation_skill):
    settings = Settings(data_dir=tmp_path)
    root = tmp_path / "automation"
    root.mkdir()
    config = {"curation_enabled": True, "codex_command": "codex.exe"}
    monkeypatch.setattr(task, "run_process", lambda *a, **k: pytest.fail("Unexpected AI invocation"))
    assert (await task.curate(settings, config))["status"] == "unchanged"
    item = {"key": "notice:1:2", "kind": "notice", "hash": "h", "data": {"title": "notice"}}
    task.write_json(root / "pending.json", {"items": [item]})
    def fail(command, **kwargs):
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "shell" not in kwargs
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(task, "run_process", fail)
    assert (await task.curate(settings, config))["status"] == "error"
    assert task.read_json(root / "pending.json")["items"] == [item]


async def test_curation_success_acknowledges_exact_batch(tmp_path, monkeypatch, curation_skill):
    settings = Settings(data_dir=tmp_path)
    root = tmp_path / "automation"
    item = {"key": "notice:1:2", "kind": "notice", "hash": "h", "data": {"title": "notice"}}
    task.write_json(root / "pending.json", {"items": [item]})
    def success(command, **kwargs):
        assert 'mcp_servers.notion.tools.notion-create-pages.approval_mode="approve"' in command
        assert 'mcp_servers.notion.tools.notion-update-page.approval_mode="approve"' in command
        allowlist = next(arg for arg in command if arg.startswith("mcp_servers.notion.enabled_tools="))
        assert json.loads(allowlist.split("=", 1)[1]) == ["notion-fetch", "notion-search", "notion-create-pages", "notion-update-page"]
        output = Path(command[command.index("-o") + 1])
        task.write_json(output, {"summary": "새 공지", "acknowledged": [{"key": item["key"], "hash": item["hash"], "evidence": "새 공지"}], "errors": []})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(task, "run_process", success)
    assert (await task.curate(settings, {"curation_enabled": True, "codex_command": "codex.exe"}))["processed"] == 1
    assert not task.read_json(root / "pending.json")["items"]


def test_dry_run_does_not_create_state(tmp_path, capsys):
    state = tmp_path / "not-created"
    assert task.main(["collect", "--data-dir", str(state), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert not state.exists()


def test_read_credentials_accepts_literal_special_characters_and_bom(tmp_path):
    settings = Settings(data_dir=tmp_path)
    before = os.environ.copy()
    (tmp_path / ".env").write_text(
        "SSU_LMS_USERNAME=user# $=\\\\name\n"
        "SSU_LMS_PASSWORD='pass# $=\\\\word'\n",
        encoding="utf-8-sig",
    )

    assert task.read_credentials(settings) == ("user# $=\\\\name", "pass# $=\\\\word")
    assert os.environ == before


async def test_collect_with_relogin_healthy_skips_credentials_and_login(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    snapshot = {"status": "ok"}

    async def healthy(*args, **kwargs):
        return snapshot

    monkeypatch.setattr(task, "collect", healthy)
    monkeypatch.setattr(task, "read_credentials", lambda settings: pytest.fail("credentials read"))
    monkeypatch.setattr(task, "SessionManager", lambda settings: pytest.fail("login attempted"))

    assert await task.collect_with_relogin(settings, course_ids=[1], download=True) == snapshot


async def test_collect_with_relogin_logs_in_once_and_preserves_kwargs(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    calls, lifecycle = [], []

    async def collect(settings, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise task.SessionRequiredError()
        return {"status": "ok"}

    class Manager:
        def __init__(self, received):
            assert received is settings

        async def login(self, username, password):
            lifecycle.append(("login", username, password))

        async def refresh(self):
            lifecycle.append(("refresh",))

    monkeypatch.setattr(task, "collect", collect)
    monkeypatch.setattr(task, "read_credentials", lambda settings: ("student", "password"))
    monkeypatch.setattr(task, "SessionManager", Manager)
    kwargs = {"course_ids": [1, 2], "download": True, "allow_lti": False}

    assert await task.collect_with_relogin(settings, **kwargs) == {"status": "ok"}
    assert calls == [kwargs, kwargs]
    assert lifecycle == [("login", "student", "password"), ("refresh",)]


async def test_collect_with_relogin_retry_auth_failure_is_not_retried(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    logins = []

    async def expired(*args, **kwargs):
        raise task.SessionRequiredError()

    class Manager:
        def __init__(self, settings):
            pass

        async def login(self, username, password):
            logins.append((username, password))

        async def refresh(self):
            pass

    monkeypatch.setattr(task, "collect", expired)
    monkeypatch.setattr(task, "read_credentials", lambda settings: ("student", "password"))
    monkeypatch.setattr(task, "SessionManager", Manager)

    with pytest.raises(task.SessionRequiredError):
        await task.collect_with_relogin(settings)
    assert logins == [("student", "password")]


async def test_collect_with_relogin_empty_credentials_never_logs_in(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)

    async def expired(*args, **kwargs):
        raise task.SessionRequiredError()

    monkeypatch.setattr(task, "collect", expired)
    (tmp_path / ".env").write_text("SSU_LMS_USERNAME=\nSSU_LMS_PASSWORD=\n", encoding="utf-8")
    monkeypatch.setattr(task, "SessionManager", lambda settings: pytest.fail("login attempted"))

    with pytest.raises(task.SessionRequiredError):
        await task.collect_with_relogin(settings)


@pytest.mark.parametrize("failed_step", ["login", "refresh"])
async def test_collect_with_relogin_sanitizes_rejected_login_or_refresh(tmp_path, monkeypatch, failed_step):
    settings = Settings(data_dir=tmp_path)
    username, password = "student-secret", "password-secret"
    collection_attempts, lifecycle = [], []

    async def expired(*args, **kwargs):
        collection_attempts.append(1)
        raise task.SessionRequiredError()

    class Manager:
        def __init__(self, settings):
            pass

        async def login(self, received_username, received_password):
            lifecycle.append("login")
            if failed_step == "login":
                raise RuntimeError(f"rejected {received_username} {received_password}")

        async def refresh(self):
            lifecycle.append("refresh")
            if failed_step == "refresh":
                raise RuntimeError(f"rejected {username} {password}")

    monkeypatch.setattr(task, "collect", expired)
    monkeypatch.setattr(task, "read_credentials", lambda settings: (username, password))
    monkeypatch.setattr(task, "SessionManager", Manager)

    with pytest.raises(task.SessionRequiredError) as error:
        await task.collect_with_relogin(settings)
    assert username not in str(error.value)
    assert password not in str(error.value)
    assert collection_attempts == [1]
    assert lifecycle == (["login"] if failed_step == "login" else ["login", "refresh"])


async def test_collect_with_relogin_cooldown_blocks_then_allows_later_login(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    attempts = []

    async def expired(*args, **kwargs):
        raise task.SessionRequiredError()

    class Manager:
        def __init__(self, settings):
            pass

        async def login(self, username, password):
            attempts.append((username, password))

        async def refresh(self):
            pass

    monkeypatch.setattr(task, "collect", expired)
    monkeypatch.setattr(task, "read_credentials", lambda settings: ("student", "password"))
    monkeypatch.setattr(task, "SessionManager", Manager)

    with pytest.raises(task.SessionRequiredError):
        await task.collect_with_relogin(settings)
    with pytest.raises(task.SessionRequiredError):
        await task.collect_with_relogin(settings)
    assert attempts == [("student", "password")]
    state = task.read_json(tmp_path / "automation/relogin-state.json")
    attempted = task.datetime.fromisoformat(state["attempted_at"])
    task.write_json(tmp_path / "automation/relogin-state.json", {"attempted_at": (attempted - task.timedelta(minutes=15)).isoformat()})
    with pytest.raises(task.SessionRequiredError):
        await task.collect_with_relogin(settings)
    assert attempts == [("student", "password"), ("student", "password")]


async def test_stale_receipt_is_never_acknowledged(tmp_path, monkeypatch, curation_skill):
    settings = Settings(data_dir=tmp_path)
    root = tmp_path / "automation"
    item = {"key": "notice:1:2", "kind": "notice", "hash": "h", "data": {"title": "notice"}}
    task.write_json(root / "pending.json", {"items": [item]})
    batch = await task.prepare_batch(settings, {})
    task.write_json(root / "curation" / (batch["batch_id"] + ".json"), {"summary": "STALE", "acknowledged": [{"key": item["key"], "hash": item["hash"], "evidence": "old receipt"}], "errors": []})
    monkeypatch.setattr(task, "run_process", lambda *a, **kw: SimpleNamespace(returncode=0))
    result = await task.curate(settings, {"curation_enabled": True, "codex_command": "codex.exe"})
    assert result["status"] == "error"
    assert task.read_json(root / "pending.json")["items"] == [item]


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object process containment")
def test_timeout_terminates_descendant_process(tmp_path):
    import ctypes
    import time
    from ctypes import wintypes

    pid_file = tmp_path / "child.pid"
    program = "import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        task.run_process([sys.executable, "-c", program, str(pid_file)], input="", env=os.environ.copy(), timeout=1)
    assert pid_file.exists(), "The descendant must really start for this check"
    pid = int(pid_file.read_text())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    time.sleep(0.05)
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 3000) == 0
        finally:
            kernel.CloseHandle(handle)
