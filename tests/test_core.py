import json
from pathlib import Path

import httpx
import pytest

from ssu_lms_mcp.adapters.canvas_client import CanvasClient
from ssu_lms_mcp.adapters.courses import list_courses
from ssu_lms_mcp.adapters.notices import html_to_text
from ssu_lms_mcp.config import Settings


def test_settings_resolves_all_local_paths(tmp_path: Path):
    settings = Settings(tmp_path / "state")
    assert settings.data_dir.is_absolute()
    assert settings.session_file == settings.data_dir / "session_state.json"
    assert settings.materials_dir == settings.data_dir / "materials"
    assert settings.manifest_file == settings.data_dir / "material_manifest.json"


def test_materials_root_independent_and_cli_overrides_env(tmp_path, monkeypatch):
    state, env_root, arg_root = tmp_path / "state", tmp_path / "env notes", tmp_path / "chosen notes"
    monkeypatch.setenv("SSU_LMS_MATERIALS_DIR", str(env_root))
    settings = Settings.from_env(data_dir=state)
    assert settings.materials_dir == env_root.resolve()
    settings = Settings.from_env(data_dir=state, materials_dir=arg_root)
    assert settings.materials_dir == arg_root.resolve()
    assert settings.session_file == state / "session_state.json"
    assert settings.manifest_file == state / "material_manifest.json"
    assert not arg_root.exists()


def test_materials_root_rejects_file(tmp_path):
    target = tmp_path / "not-a-directory"
    target.write_text("existing document", encoding="utf-8")
    with pytest.raises(ValueError, match="폴더"):
        Settings(materials_dir=target)


@pytest.mark.asyncio
async def test_canvas_does_not_receive_learningx_bearer(tmp_path: Path):
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "cookies": [],
                "storage_state": {
                    "origins": [{"origin": "https://lms.ssu.ac.kr", "localStorage": [{"name": "xn_api_token", "value": "secret"}]}]
                },
            }
        ),
        encoding="utf-8",
    )
    client = CanvasClient(session)
    await client.init()
    assert client._headers_for(False)["Authorization"] == "Bearer secret"
    assert "Authorization" not in client._headers_for(True)
    await client.close()


def test_html_text_removes_script_and_limits():
    assert html_to_text("<p>안녕 <b>세계</b></p><script>secret()</script>") == "안녕 세계"


def test_learningx_token_is_selected_only_from_lms_origin():
    data = {
        "cookies": [{"name": "xn_api_token", "value": "evil-cookie", "domain": "evil.example"}],
        "storage_state": {"origins": [
            {"origin": "https://smartid.ssu.ac.kr", "localStorage": [{"name": "xn_api_token", "value": "sso-secret"}]},
            {"origin": "https://lms.ssu.ac.kr", "localStorage": [{"name": "access_token", "value": "unrelated-token"}]},
        ]},
    }
    assert CanvasClient._find_token(data) is None
    data["storage_state"]["origins"][1]["localStorage"].append({"name": "xn_api_token", "value": "correct-token"})
    assert CanvasClient._find_token(data) == "correct-token"


@pytest.mark.parametrize("target,is_login", [
    ("https://smartid.ssu.ac.kr/login?token=sensitive", True),
    ("https://lms.ssu.ac.kr/login.php", True),
    ("https://attacker.example/steal", False),
])
async def test_redirects_never_forward_secrets_and_login_means_expired(tmp_path, target, is_login):
    visits = []

    def handle(request):
        visits.append(str(request.url))
        assert request.url.host == "lms.ssu.ac.kr"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(302, headers={"Location": target})

    client = CanvasClient(tmp_path / "unused.json")
    client._token = "test-token"
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(httpx.HTTPStatusError if is_login else ValueError) as error:
            await client.get("/courses")
        assert len(visits) == 1
        if is_login:
            assert error.value.response.status_code == 401
        assert "sensitive" not in str(error.value)
    finally:
        await client.close()


async def test_malicious_pagination_link_stops_before_second_request(tmp_path):
    visits = []

    def handle(request):
        visits.append(str(request.url))
        return httpx.Response(200, json=[{"id": 1}], headers={"Link": '<https://attacker.example/steal>; rel="next"'})

    client = CanvasClient(tmp_path / "unused.json")
    client._token = "test-token"
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ValueError, match="허용되지 않은"):
            await client.get_all_pages("/courses")
        assert len(visits) == 1
    finally:
        await client.close()


async def test_canvas_cookie_path_is_preserved_in_http_requests(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [{"name": "scoped", "value": "test-cookie", "domain": "canvas.ssu.ac.kr", "path": "/api/v1/courses"}]}), encoding="utf-8")
    client = CanvasClient(state)
    await client.init()
    cookies = client._client.cookies
    await client.close()
    visits = []

    def handle(request):
        visits.append(request)
        return httpx.Response(200, json=[])

    client._client = httpx.AsyncClient(cookies=cookies, transport=httpx.MockTransport(handle))
    try:
        await client.get("/courses", use_canvas=True)
        await client.get("/users/self", use_canvas=True)
        assert visits[0].headers["cookie"] == "scoped=test-cookie"
        assert "cookie" not in visits[1].headers
        assert all("authorization" not in request.headers for request in visits)
    finally:
        await client.close()


def test_canvas_rejects_cross_host_api_url(tmp_path: Path):
    client = CanvasClient(tmp_path / "session.json")
    with pytest.raises(ValueError, match="허용되지 않은"):
        client._safe_url("https://example.test/steal", use_canvas=False)


def _error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://lms.ssu.ac.kr/learningx/api/v1/courses")
    return httpx.HTTPStatusError(
        "fail", request=request, response=httpx.Response(status, request=request)
    )


class _CourseClient:
    def __init__(self, fail_both: bool = False):
        self.calls = []
        self.fail_both = fail_both

    async def get(self, path, params=None, use_canvas=False):
        self.calls.append(("get", path, use_canvas))
        if path == "/courses" and not use_canvas:
            raise _error(401)
        if path == "/courses" and use_canvas:
            if self.fail_both:
                raise _error(401)
            return [{"id": 7, "name": "테스트"}]
        if path.endswith("/users"):
            return [{"name": "교수"}]
        raise AssertionError(path)

    async def get_all_pages(self, path, params=None, use_canvas=False):
        self.calls.append(("pages", path, use_canvas))
        assert path.endswith("/modules") and use_canvas
        return [{"items": [{"completion_requirement": {"completed": True}}]}]


@pytest.mark.asyncio
async def test_course_host_fallback_keeps_two_extra_calls_per_course():
    client = _CourseClient()
    result = await list_courses(client)
    assert result[0].professor == "교수"
    assert client.calls[:2] == [("get", "/courses", False), ("get", "/courses", True)]
    assert len(client.calls[2:]) == 2


@pytest.mark.asyncio
async def test_course_both_host_session_errors_propagate():
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await list_courses(_CourseClient(fail_both=True))
    assert raised.value.response.status_code == 401
