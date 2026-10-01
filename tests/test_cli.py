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
        [sys.executable, "-m", "ssu_lms_mcp", "status"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
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
    assert output.err


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


def test_http_rejects_public_binding(tmp_path):
    with pytest.raises(ValueError):
        create_server(Settings(data_dir=tmp_path), host="0.0.0.0")


def test_doctor_reports_runtime_failures_without_modifying_private_data(
    tmp_path, monkeypatch, capsys
):
    blocked = tmp_path / "not-a-directory"
    canary = "private-diagnostic-canary"
    blocked.write_text(canary, encoding="utf-8")
    materials = tmp_path / "missing-materials"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))
    monkeypatch.setenv("SSU_LMS_PASSWORD", canary)
    assert (
        cli.main(
            [
                "doctor",
                "--json",
                "--data-dir",
                str(blocked),
                "--materials-dir",
                str(materials),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["ok"] is False
    assert report["checks"]["data_dir"]["status"] == "error"
    assert report["checks"]["chromium"]["status"] == "error"
    assert report["session"]["authenticated"] is False
    assert any(step[-3:] == ["playwright", "install", "chromium"] for step in report["next_steps"])
    assert canary not in output.out + output.err
    assert blocked.read_text(encoding="utf-8") == canary
    assert not materials.exists()
