"""명령행 실행이 로그인 요청과 프로토콜 실행을 분리하는지 확인한다."""

import json
import os
import subprocess
import sys

import pytest

from ssu_lms_mcp import cli
from ssu_lms_mcp.config import Settings
from ssu_lms_mcp.server import create_server


def test_status_runs_outside_repo_without_web_dependencies(tmp_path):
    env = {**os.environ, "SSU_LMS_DATA_DIR": str(tmp_path / "state"), "PYTHONUTF8": "1"}
    process = subprocess.run(
        [sys.executable, "-m", "ssu_lms_mcp", "status"], cwd=tmp_path,
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["authenticated"] is False
    assert not (tmp_path / "state" / "session_state.json").exists()


def test_login_noninteractive_never_prompts_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SSU_LMS_USERNAME", raising=False)
    monkeypatch.delenv("SSU_LMS_PASSWORD", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["login", "--data-dir", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "터미널" in output.err


def test_login_password_is_not_an_argument():
    with pytest.raises(SystemExit) as error:
        cli._parser().parse_args(["login", "--password", "do-not-store-password"])
    assert error.value.code == 2


def test_cli_credential_failure_does_not_print_password(tmp_path, monkeypatch, capsys):
    async def failed_login(*args):
        raise RuntimeError("dangerous-password-canary")

    monkeypatch.setattr(cli, "_login", failed_login)
    assert cli.main(["login", "--data-dir", str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert "dangerous-password-canary" not in output.out + output.err


def test_serve_does_not_login_and_accepts_global_data_dir(tmp_path, monkeypatch):
    invoked = []

    class FakeServer:
        def run(self, transport):
            invoked.append(transport)

    def factory(settings, **kwargs):
        assert settings.data_dir == tmp_path.resolve()
        return FakeServer()

    monkeypatch.setattr(cli, "create_server", factory)
    assert cli.main(["--data-dir", str(tmp_path), "serve"]) == 0
    assert invoked == ["stdio"]


@pytest.mark.parametrize("placement", ["before", "after", "implicit"])
def test_serve_materials_dir_option(tmp_path, monkeypatch, placement):
    state, materials = tmp_path / "state", tmp_path / "external notes"
    monkeypatch.setenv("SSU_LMS_MATERIALS_DIR", str(tmp_path / "env-unused"))
    invoked = []

    class FakeServer:
        def run(self, transport):
            invoked.append(transport)

    def factory(settings, **kwargs):
        assert settings.materials_dir == materials
        assert settings.session_file == state / "session_state.json"
        return FakeServer()

    monkeypatch.setattr(cli, "create_server", factory)
    args = ["--data-dir", str(state), "--materials-dir", str(materials)]
    args = args + ["serve"] if placement == "before" else ["serve", *args] if placement == "after" else args
    assert cli.main(args) == 0
    assert invoked == ["stdio"]


def test_http_rejects_public_binding(tmp_path):
    with pytest.raises(ValueError, match="로컬"):
        create_server(Settings(data_dir=tmp_path), host="0.0.0.0")


def test_import_does_not_load_web_app():
    assert "app" not in sys.modules
    assert "litellm" not in sys.modules
    assert "fastapi" not in sys.modules
