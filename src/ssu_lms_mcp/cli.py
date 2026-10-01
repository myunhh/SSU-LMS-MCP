"""웹앱 없이 로그인하고 MCP를 실행하는 명령행 인터페이스."""

import argparse
import asyncio
import getpass
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from loguru import logger

from . import __version__
from .config import Settings
from .server import LOCAL_HOSTS, create_server
from .session import SessionManager, SessionRequiredError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="숭실대학교 LMS 독립 MCP 서버")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", help="세션·자료 저장 폴더 (기본: ~/.ssu-lms-mcp)")
    parser.add_argument("--materials-dir", help="자료 전용 저장 폴더 (세션 저장 위치와 분리)")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="MCP 서버 실행 (기본 동작)")
    serve.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    serve.add_argument("--host", choices=sorted(LOCAL_HOSTS), default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    login = sub.add_parser("login", help="학번·비밀번호를 입력해 LMS 로그인")
    login.add_argument("--username", help="학번 (생략 시 입력)")
    login.add_argument("--headed", action="store_true", help="로그인 브라우저 표시")
    refresh = sub.add_parser("refresh", help="저장된 LMS 세션 유효성 확인·갱신")
    status = sub.add_parser("status", help="로컬 세션 상태 표시 (네트워크 호출 없음)")
    config = sub.add_parser("config", help="현재 설치·저장 경로가 반영된 MCP 연결 설정 출력")
    config.add_argument("--format", choices=["json", "codex", "vscode"], default="json")
    doctor = sub.add_parser("doctor", help="저장 폴더·Chromium·로컬 세션 진단 (LMS 접속 없음)")
    doctor.add_argument("--json", action="store_true", help="진단 결과를 JSON으로 출력")
    for command in [serve, login, refresh, status, config, doctor]:
        command.add_argument("--data-dir", default=argparse.SUPPRESS, help="세션·자료 저장 폴더")
        command.add_argument(
            "--materials-dir", default=argparse.SUPPRESS, help="자료 전용 저장 폴더"
        )
    return parser


def _configure_logging() -> None:
    # stdout은 MCP JSON-RPC 전용이다. HTTP/브라우저의 상세 주소도 로그에 남기지 않는다.
    logger.remove()
    logger.add(
        sys.stderr, level="WARNING", format="{level}: {message}", diagnose=False, backtrace=False
    )


async def _login(args, settings: Settings) -> dict:
    username = args.username or os.getenv("SSU_LMS_USERNAME")
    password = os.getenv("SSU_LMS_PASSWORD")
    if not sys.stdin.isatty() and (not username or not password):
        raise ValueError(
            "터미널에서 login을 실행해 학번·비밀번호를 입력하세요. 자동 실행은 SSU_LMS_USERNAME/SSU_LMS_PASSWORD 환경 변수를 사용하세요."
        )
    username = username or input("학번: ").strip()
    password = password or getpass.getpass("LMS 비밀번호: ")
    if not username or not password:
        raise ValueError("학번과 비밀번호를 입력하세요.")
    if args.headed:
        from dataclasses import replace

        settings = replace(settings, headless=False)
    return await SessionManager(settings).login(username, password)


def _client_config(settings: Settings, output_format: str) -> str:
    entry = {
        # resolve() would dereference a Unix venv's Python symlink into the base interpreter.
        "command": os.path.abspath(sys.executable),
        "args": [
            "-m",
            "ssu_lms_mcp",
            "serve",
            "--data-dir",
            str(settings.data_dir),
            "--materials-dir",
            str(settings.materials_dir),
        ],
        "env": {
            "PYTHONUTF8": "1",
            "SSU_LMS_HEADLESS": str(settings.headless).lower(),
            "SSU_LMS_SESSION_REFRESH_SECONDS": str(settings.session_refresh_seconds),
        },
    }
    browser_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if browser_path:
        entry["env"]["PLAYWRIGHT_BROWSERS_PATH"] = (
            "0" if browser_path == "0" else str(Path(browser_path).expanduser().absolute())
        )
    if output_format == "codex":
        return "\n".join(
            [
                "[mcp_servers.ssu-lms]",
                f"command = {json.dumps(entry['command'], ensure_ascii=False)}",
                f"args = {json.dumps(entry['args'], ensure_ascii=False)}",
                "",
                "[mcp_servers.ssu-lms.env]",
                *(f"{key} = {json.dumps(value)}" for key, value in entry["env"].items()),
            ]
        )
    if output_format == "vscode":
        config = {"servers": {"ssu-lms": {"type": "stdio", **entry}}}
    else:
        config = {"mcpServers": {"ssu-lms": entry}}
    return json.dumps(config, ensure_ascii=False, indent=2)


def _command_text(arguments: list[str]) -> str:
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in arguments)
    return shlex.join(arguments)


def _storage_check(directory: Path) -> dict:
    probe = directory
    try:
        while not probe.exists() and not probe.is_symlink() and probe != probe.parent:
            probe = probe.parent
        with tempfile.TemporaryFile(dir=probe):
            pass
    except OSError:
        return {
            "status": "error",
            "message": "저장 폴더 또는 상위 경로에 쓸 수 없습니다. 폴더 경로와 권한을 확인하세요.",
        }
    return {"status": "ok", "message": "저장 폴더 또는 기존 상위 폴더에 쓰기 가능"}


async def _doctor(settings: Settings) -> dict:
    from playwright.async_api import async_playwright

    python = os.path.abspath(sys.executable)
    command = [python, "-m", "ssu_lms_mcp"]
    paths = ["--data-dir", str(settings.data_dir), "--materials-dir", str(settings.materials_dir)]
    checks = {
        "data_dir": _storage_check(settings.data_dir),
        "materials_dir": _storage_check(settings.materials_dir),
    }
    next_steps = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, timeout=15000)
            try:
                page = await browser.new_page()
                await page.goto("about:blank")
            finally:
                await browser.close()
        checks["chromium"] = {"status": "ok", "message": "로컬 headless Chromium 실행 성공"}
    except Exception as exc:
        checks["chromium"] = {
            "status": "error",
            "message": f"Chromium을 실행하지 못했습니다 ({type(exc).__name__}). 브라우저 설치와 OS 의존성을 확인하세요.",
        }
        next_steps.append([python, "-m", "playwright", "install", "chromium"])

    session = SessionManager(settings).status()
    if not session["authenticated"]:
        checks["session"] = {
            "status": "warning",
            "message": "사용 가능한 로컬 세션 없음. LMS 조회 전 로그인하세요.",
        }
        next_steps.append([*command, "login", *paths])
    elif session["needs_refresh"]:
        checks["session"] = {
            "status": "warning",
            "message": "로컬 세션 갱신 필요. refresh로 실제 유효성을 확인하세요.",
        }
        next_steps.append([*command, "refresh", *paths])
    else:
        checks["session"] = {
            "status": "ok",
            "message": "로컬 세션 있음. 현재 LMS 인증 유효성은 확인하지 않았습니다.",
        }
    ok = all(check["status"] != "error" for check in checks.values())
    if ok:
        next_steps.append([*command, "config", *paths])
    return {
        "ok": ok,
        "version": __version__,
        "python": sys.version.split()[0],
        "executable": python,
        "data_dir": str(settings.data_dir),
        "materials_dir": str(settings.materials_dir),
        "checks": checks,
        "session": session,
        "next_steps": next_steps,
    }


def _print_doctor(result: dict) -> None:
    print(f"SSU LMS MCP {result['version']} / Python {result['python']}")
    for key in ("executable", "data_dir", "materials_dir"):
        print(f"{key}: {result[key]}")
    for name, check in result["checks"].items():
        print(f"[{check['status'].upper()}] {name}: {check['message']}")
    print(
        "환경 진단 통과 (LMS 접속 검증 아님)."
        if result["ok"]
        else "환경 오류를 해결한 뒤 doctor를 다시 실행하세요."
    )
    if result["next_steps"]:
        print("\n다음 명령 (PowerShell):" if os.name == "nt" else "\n다음 명령:")
        for command in result["next_steps"]:
            print(_command_text(command))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _configure_logging()
    try:
        settings = Settings.from_env(data_dir=args.data_dir, materials_dir=args.materials_dir)
        if args.command == "config":
            print(_client_config(settings, args.format))
            return 0
        if args.command == "doctor":
            result = asyncio.run(_doctor(settings))
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                _print_doctor(result)
            return 0 if result["ok"] else 1
        if args.command in {None, "serve"}:
            server = create_server(
                settings, host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 8765)
            )
            server.run(transport=getattr(args, "transport", "stdio"))
            return 0
        if args.command == "login":
            result = asyncio.run(_login(args, settings))
        elif args.command == "refresh":
            result = asyncio.run(SessionManager(settings).refresh())
        else:
            result = SessionManager(settings).status()
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except (SessionRequiredError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            f"실행 환경 오류 ({type(exc).__name__}). 네트워크와 Chromium 설치를 확인하세요: python -m playwright install chromium",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
