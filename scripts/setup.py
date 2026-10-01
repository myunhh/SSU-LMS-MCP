"""프로젝트 전용 가상환경에 독립 MCP를 설치한다. Python 표준 라이브러리만 사용."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="SSU LMS MCP 설치 (전역 Python·클라이언트 설정 변경 없음)"
    )
    parser.add_argument("--venv", type=Path, default=root / ".venv", help="설치할 가상환경 폴더")
    parser.add_argument(
        "--dev", action="store_true", help="개발 의존성과 함께 편집 가능 모드로 설치"
    )
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Chromium 다운로드 생략 (설치된 브라우저는 진단)",
    )
    parser.add_argument("--data-dir", help="세션·다운로드 이력 저장 폴더")
    parser.add_argument("--materials-dir", help="자료 전용 저장 폴더")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        print("Python 3.11 이상으로 실행하세요.", file=sys.stderr)
        return 1

    target = args.venv.expanduser().absolute()
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {**os.environ, "PYTHONUTF8": "1"}
    # 설치·진단·설정 생성에는 LMS 자격증명이 필요하지 않다.
    env.pop("SSU_LMS_USERNAME", None)
    env.pop("SSU_LMS_PASSWORD", None)
    paths = []
    for option, value in (("--data-dir", args.data_dir), ("--materials-dir", args.materials_dir)):
        if value:
            paths.extend([option, str(Path(value).expanduser().absolute())])
    # 상대 환경 변수 경로도 사용자가 명령을 실행한 위치 기준으로 고정한다.
    for key in ("SSU_LMS_DATA_DIR", "SSU_LMS_MATERIALS_DIR", "PLAYWRIGHT_BROWSERS_PATH"):
        if env.get(key) and not (key == "PLAYWRIGHT_BROWSERS_PATH" and env[key] == "0"):
            env[key] = str(Path(env[key]).expanduser().absolute())

    stage = "가상환경 준비"
    try:
        print(f"[1/4] {stage}: {target}", flush=True)
        if target.exists():
            if not (target / "pyvenv.cfg").is_file() or not python.is_file():
                print(
                    "대상 경로가 정상적인 가상환경이 아닙니다. 빈 새 경로를 --venv로 지정하세요. 기존 경로는 삭제하지 않습니다.",
                    file=sys.stderr,
                )
                return 1
        else:
            venv.EnvBuilder(with_pip=True).create(target)

        stage = "패키지 설치"
        print(f"[2/4] {stage}", flush=True)
        install = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
        install += ["-e", f"{root}[dev]"] if args.dev else [str(root)]
        subprocess.run(install, cwd=root, env=env, check=True)

        stage = "Chromium 설치"
        print(
            f"[3/4] {stage}" if not args.skip_browser else "[3/4] Chromium 다운로드 생략",
            flush=True,
        )
        if not args.skip_browser:
            subprocess.run(
                [str(python), "-m", "playwright", "install", "chromium"],
                cwd=root,
                env=env,
                check=True,
            )

        stage = "실행 환경 진단"
        print(f"[4/4] {stage}", flush=True)
        subprocess.run(
            [str(python), "-m", "ssu_lms_mcp", "doctor", *paths], cwd=root, env=env, check=True
        )
        print("\n클라이언트에 넣을 stdio 설정 (기존 설정과 직접 병합하세요):", flush=True)
        subprocess.run(
            [str(python), "-m", "ssu_lms_mcp", "config", *paths], cwd=root, env=env, check=True
        )
        print(
            "\n설치 완료. 세션 경고가 있으면 안내된 login/refresh 명령을 실행하고 클라이언트를 연결하세요.",
            flush=True,
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"{stage} 실패 ({type(exc).__name__}). 위 오류를 해결한 뒤 같은 설치 명령을 다시 실행하세요.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
