"""웹앱 없이 로그인하고 MCP를 실행하는 명령행 인터페이스."""

import argparse
import asyncio
import getpass
import json
import os
import sys

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
    for command in [serve, login, refresh, status]:
        command.add_argument("--data-dir", default=argparse.SUPPRESS, help="세션·자료 저장 폴더")
        command.add_argument("--materials-dir", default=argparse.SUPPRESS, help="자료 전용 저장 폴더")
    return parser


def _configure_logging() -> None:
    # stdout은 MCP JSON-RPC 전용이다. HTTP/브라우저의 상세 주소도 로그에 남기지 않는다.
    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="{level}: {message}", diagnose=False, backtrace=False)


async def _login(args, settings: Settings) -> dict:
    username = args.username or os.getenv("SSU_LMS_USERNAME")
    password = os.getenv("SSU_LMS_PASSWORD")
    if not sys.stdin.isatty() and (not username or not password):
        raise ValueError("터미널에서 login을 실행해 학번·비밀번호를 입력하세요. 자동 실행은 SSU_LMS_USERNAME/SSU_LMS_PASSWORD 환경 변수를 사용하세요.")
    username = username or input("학번: ").strip()
    password = password or getpass.getpass("LMS 비밀번호: ")
    if not username or not password:
        raise ValueError("학번과 비밀번호를 입력하세요.")
    if args.headed:
        from dataclasses import replace

        settings = replace(settings, headless=False)
    return await SessionManager(settings).login(username, password)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _configure_logging()
    try:
        settings = Settings.from_env(data_dir=args.data_dir, materials_dir=args.materials_dir)
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
