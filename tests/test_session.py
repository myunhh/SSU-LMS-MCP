import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ssu_lms_mcp import auth, session
from ssu_lms_mcp.config import Settings
from ssu_lms_mcp.session import SessionManager, SessionRequiredError


def _session_data(*, stale=False):
    cookie = {
        "name": "_normandy_session", "value": "test-session-cookie", "domain": "canvas.ssu.ac.kr",
        "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax",
    }
    return {
        "saved_at": (datetime.now(UTC) - timedelta(seconds=6000 if stale else 0)).isoformat(),
        "cookies": [cookie],
        "storage_state": {
            "cookies": [cookie],
            "origins": [{"origin": "https://lms.ssu.ac.kr", "localStorage": [
                {"name": "xn_api_token", "value": "test-learningx-token"},
            ]}],
        },
    }


def _write_session(settings, *, stale=False):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.session_file.write_text(json.dumps(_session_data(stale=stale)), encoding="utf-8")


def test_status_is_local_and_hides_session_content(tmp_path: Path):
    manager = SessionManager(Settings(tmp_path))
    assert manager.status() == {"authenticated": False, "saved_at": None, "needs_refresh": False}
    manager.settings.session_file.write_text("{broken", encoding="utf-8")
    assert manager.status()["authenticated"] is False


@pytest.mark.asyncio
async def test_missing_session_requires_relogin(tmp_path: Path):
    manager = SessionManager(Settings(tmp_path))
    with pytest.raises(SessionRequiredError, match="다시 로그인"):
        async with manager.client():
            pass


def test_status_marks_stale_session_without_secrets(tmp_path: Path):
    settings = Settings(tmp_path, session_refresh_seconds=1)
    settings.session_file.parent.mkdir(parents=True, exist_ok=True)
    settings.session_file.write_text(
        json.dumps(
            {
                **_session_data(),
                "saved_at": (datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    status = SessionManager(settings).status()
    assert status["authenticated"] is True and status["needs_refresh"] is True
    assert "cookies" not in status and "secret" not in repr(status)


@pytest.mark.parametrize("raw", [[], {"saved_at": "2026-01-01T00:00:00Z"}, {"cookies": []}])
def test_status_rejects_malformed_session_shape(tmp_path: Path, raw: object):
    settings = Settings(tmp_path)
    settings.session_file.write_text(json.dumps(raw), encoding="utf-8")
    assert SessionManager(settings).status()["authenticated"] is False


@pytest.mark.parametrize("broken", [
    {"cookies": [], "storage_state": {"cookies": [], "origins": []}},
    {"cookies": [{}], "storage_state": {"cookies": [{}], "origins": []}},
    {"cookies": [], "storage_state": {"cookies": [], "origins": [
        {"origin": "https://lms.ssu.ac.kr", "localStorage": ["not-an-item"]},
    ]}},
    {"cookies": _session_data()["cookies"], "storage_state": {"origins": []}},
    {"cookies": _session_data()["cookies"], "storage_state": {"cookies": [{}], "origins": []}},
])
async def test_invalid_auth_material_never_reaches_playwright(tmp_path, monkeypatch, broken):
    settings = Settings(tmp_path)
    broken["saved_at"] = datetime.now(UTC).isoformat()
    settings.session_file.write_text(json.dumps(broken), encoding="utf-8")

    def forbidden_browser():
        raise AssertionError("손상된 세션으로 브라우저를 실행하면 안 됩니다.")

    monkeypatch.setattr(auth, "async_playwright", forbidden_browser)
    assert not SessionManager(settings).status()["authenticated"]
    assert not await auth.SSULMSAuthPlaywright(settings.session_file).load_session()


@pytest.mark.parametrize("error", [ValueError("자료 파싱 실패"), FileNotFoundError("자료 없음")])
async def test_adapter_errors_are_not_misreported_as_session_errors(tmp_path, monkeypatch, error):
    settings = Settings(tmp_path)
    _write_session(settings)
    fake = SimpleNamespace(init=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(session, "CanvasClient", lambda path: fake)
    with pytest.raises(type(error), match=str(error)):
        async with SessionManager(settings).client():
            raise error
    fake.close.assert_awaited_once()


@pytest.mark.parametrize("status", [401, 403, 419])
async def test_adapter_auth_failure_requires_login_and_closes_client(tmp_path, monkeypatch, status):
    settings = Settings(tmp_path)
    _write_session(settings)
    fake = SimpleNamespace(init=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(session, "CanvasClient", lambda path: fake)
    request = httpx.Request("GET", "https://canvas.ssu.ac.kr/api/v1/courses")
    with pytest.raises(SessionRequiredError, match="login"):
        async with SessionManager(settings).client():
            raise httpx.HTTPStatusError("인증 실패", request=request, response=httpx.Response(status, request=request))
    fake.close.assert_awaited_once()


@pytest.mark.parametrize("error,expected", [
    (ValueError("손상"), SessionRequiredError),
    (RuntimeError("인프라 오류"), RuntimeError),
])
async def test_init_failure_cleanup_and_error_classification(tmp_path, monkeypatch, error, expected):
    settings = Settings(tmp_path)
    _write_session(settings)
    fake = SimpleNamespace(init=AsyncMock(side_effect=error), close=AsyncMock())
    monkeypatch.setattr(session, "CanvasClient", lambda path: fake)
    with pytest.raises(expected) as raised:
        async with SessionManager(settings).client():
            pytest.fail("실패한 클라이언트가 반환되었습니다.")
    if expected is RuntimeError:
        assert not isinstance(raised.value, SessionRequiredError)
    fake.close.assert_awaited_once()


async def test_login_and_refresh_create_parent_before_lock(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "missing" / "state")

    async def success(*args):
        assert settings.data_dir.exists()
        _write_session(settings)
        return True

    fake = SimpleNamespace(login=success, load_session=success)
    monkeypatch.setattr(SessionManager, "_auth", lambda self: fake)
    assert (await SessionManager(settings).login("test-student", "test-password"))["authenticated"]
    assert (await SessionManager(settings).refresh())["authenticated"]


async def test_concurrent_stale_sessions_refresh_once(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    _write_session(settings, stale=True)
    refreshes = 0

    async def refresh():
        nonlocal refreshes
        refreshes += 1
        await asyncio.sleep(0.03)
        _write_session(settings)
        return True

    monkeypatch.setattr(SessionManager, "_auth", lambda self: SimpleNamespace(load_session=refresh))
    monkeypatch.setattr(session, "CanvasClient", lambda path: SimpleNamespace(init=AsyncMock(), close=AsyncMock()))

    async def query():
        async with SessionManager(settings).client():
            pass

    await asyncio.gather(*(query() for _ in range(5)))
    assert refreshes == 1


@asynccontextmanager
async def _value_context(value):
    yield value


async def test_sso_waits_for_navigation_with_all_original_login_selectors(tmp_path, monkeypatch):
    navigation = False
    page = SimpleNamespace(url="https://smartid.ssu.ac.kr/login", fill=AsyncMock(), wait_for_selector=AsyncMock())

    @asynccontextmanager
    async def expect_navigation(**kwargs):
        nonlocal navigation
        assert kwargs == {"timeout": 15000, "wait_until": "networkidle"}
        navigation = True
        yield
        navigation = False

    async def click(selector):
        assert navigation
        assert 'a:has-text("로그인")' in selector
        assert 'input[type="image"]' in selector
        page.url = "https://lms.ssu.ac.kr/main.php"

    page.expect_navigation = expect_navigation
    page.click = click
    page.goto = AsyncMock()
    data = _session_data()
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page), cookies=AsyncMock(return_value=data["cookies"]),
        storage_state=AsyncMock(return_value=data["storage_state"]),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    play = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    monkeypatch.setattr(auth, "async_playwright", lambda: _value_context(play))
    settings = Settings(tmp_path)
    assert await auth.SSULMSAuthPlaywright(settings.session_file).login("fake-student", "password-canary")
    assert SessionManager(settings).status()["authenticated"]
    assert "password-canary" not in settings.session_file.read_text("utf-8")
    browser.close.assert_awaited_once()


async def test_missing_chromium_is_infrastructure_error(tmp_path, monkeypatch):
    play = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(side_effect=RuntimeError("Chromium 없음"))))
    monkeypatch.setattr(auth, "async_playwright", lambda: _value_context(play))
    with pytest.raises(RuntimeError, match="Chromium"):
        await auth.SSULMSAuthPlaywright(tmp_path / "state.json").login("fake", "not-real")
