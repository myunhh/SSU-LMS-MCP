"""JSON command-line interface for the private SSU study pipeline."""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path


class StudyUsageError(ValueError):
    """Arguments are invalid; never retain the rejected argument text."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise StudyUsageError("invalid_arguments")

    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if parsed.command == "exam" and not (parsed.course_id or parsed.key):
            raise StudyUsageError("exam_scope_required")
        return parsed


def _positive_id(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("positive integer required") from None
    if result <= 0:
        raise argparse.ArgumentTypeError("positive integer required")
    return result


def _nonempty(value: str) -> str:
    if not value.strip() or any(ord(char) < 32 for char in value):
        raise argparse.ArgumentTypeError("nonempty single-line value required")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="ssu-study",
        description="숭실대학교 학습 자동화: JSON 결과, 원본 보존, 학습자 주도 복습",
        epilog=(
            "종료 코드: 0 성공/건너뜀, 1 부분 실패/실행 실패, 2 설정/인수 오류. "
            "Notion 인증은 기존 허브를 이관할 때만 node .omp/study/cloud.mjs --login; "
            "일일 실행은 아티팩트 저장소를 사용하며 로그인이나 동의를 자동 실행하지 않습니다."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--config", type=Path, default=Path.cwd() / ".omp" / "study" / "settings.json",
        help="설정 JSON 경로 (기본: 현재 작업공간/.omp/study/settings.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="과목·개인 아티팩트 허브와 기존 자료 기준선 초기화", allow_abbrev=False)
    run = sub.add_parser("run", help="실제 LMS → OneDrive → 학습 분석 → 아티팩트 실행", allow_abbrev=False)
    run.add_argument("--key", action="append", type=_nonempty, help="명시적으로 학습할 자료 키 (반복 가능)")
    run.add_argument("--scheduled", action="store_true", help="서울 07:30 이후 당일 성공 여부를 확인해 실행")
    status = sub.add_parser("status", help="네트워크 없이 로컬 학습 상태와 실제 Windows 예약 상태 조회", allow_abbrev=False)
    review = sub.add_parser("review", help="학습자가 직접 선택한 평가와 답안 기록", allow_abbrev=False)
    review.add_argument("--key", required=True, type=_nonempty, help="복습 항목 키 (자료 키와 구분)")
    review.add_argument("--rating", required=True, choices=("understood", "uncertain", "unknown"))
    review.add_argument("--answer", default="", help="학습자가 직접 작성한 답안; 생략하면 빈 답안")
    exam = sub.add_parser("exam", help="명시한 과목/자료 범위만 시험 대비 연습 묶음 생성", allow_abbrev=False)
    exam.add_argument("--course-id", action="append", type=_positive_id, help="포함할 과목 ID (반복 가능)")
    exam.add_argument("--key", action="append", type=_nonempty, help="포함할 자료 키 (반복 가능)")
    exam.add_argument("--title", required=True, type=_nonempty, help="학습자가 지정한 묶음 제목")
    schedule = sub.add_parser("schedule", help="현재 사용자 전용 Windows 작업 예약 (최초에는 --disable)", allow_abbrev=False)
    choices = schedule.add_mutually_exclusive_group(required=True)
    choices.add_argument("--enable", action="store_true", help="비활성 등록 및 실제 검증을 마친 작업 활성화")
    choices.add_argument("--disable", action="store_true", help="소유권 확인 후 비활성 상태로 등록/갱신")
    choices.add_argument("--status", action="store_true", help="실제 작업의 활성화·실행·트리거 상태 조회")
    library = sub.add_parser("library", help="OneDrive 학습 저장소의 비공개 PC 편집 화면", allow_abbrev=False)
    library_mode = library.add_mutually_exclusive_group(required=True)
    library_mode.add_argument("--open", action="store_true", help="편집 서버를 시작하고 브라우저로 열기")
    library_mode.add_argument("--serve", action="store_true", help="127.0.0.1 전용 편집 서버 실행")
    library_mode.add_argument("--status", action="store_true", help="현재 PC의 편집 서버 상태 조회")
    migrate = sub.add_parser("migrate-notion", help="기존 SSU Notion 허브 전체를 보존·검증하여 아티팩트로 이관", allow_abbrev=False)
    for command in (init, run, status, review, exam, schedule, library, migrate):
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="설정 JSON 경로")
    return parser


def _dispatch(args) -> dict:
    from . import study_runner, study_schedule

    config = args.config.expanduser().absolute()
    if args.command in {"library", "migrate-notion"}:
        from .study_library import (
            install_application,
            launch_library,
            library_status,
            serve_library,
        )

        settings = study_runner.load_config(config)
        if args.command == "migrate-notion":
            from .study_migrate import migrate_notion
            from .study_store import StudyStore

            with study_runner._lock(settings), StudyStore(study_runner._database(settings)) as store:
                report = migrate_notion(settings, store)
            if report.get("status") == "done":
                report["application"] = install_application(settings, config)
            return report
        if args.status:
            return library_status(settings)
        if args.serve:
            install_application(settings, config)
            serve_library(settings)
            return {"status": "stopped"}
        return launch_library(settings, config)
    if args.command == "init":
        return asyncio.run(study_runner.initialize_study(config))
    if args.command == "run":
        return asyncio.run(study_runner.run_study(config, keys=args.key, scheduled=args.scheduled))
    if args.command == "status":
        report = study_runner.study_status(config)
        try:
            report["schedule"] = study_schedule.schedule_status(config)
            if _exit_code(report["schedule"]):
                report["status"] = "partial"
        except study_schedule.ScheduleError as exc:
            report["schedule"] = {"status": "failed", "error_type": exc.code}
            report["status"] = "partial"
        return report
    if args.command == "review":
        return asyncio.run(study_runner.review_study(
            config, review_key=args.key, rating=args.rating, answer=args.answer,
        ))
    if args.command == "exam":
        return asyncio.run(study_runner.exam_study(
            config, course_ids=args.course_id or [], source_keys=args.key or [], title=args.title,
        ))
    if args.status:
        return study_schedule.schedule_status(config)
    return study_schedule.configure_schedule(config, enabled=args.enable)


def _exit_code(report: dict) -> int:
    status = report.get("status", "ok")
    if status in {"invalid_config", "invalid_scope", "invalid_arguments"}:
        return 2
    if status in {"partial", "failed", "error", "review_required", "blocked"}:
        return 1
    return 0


def _write_json(report: dict) -> None:
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        args = _parser().parse_args(argv)
    except StudyUsageError as exc:
        _write_json({"status": "invalid_arguments", "error_type": str(exc), "help": "ssu-study --help"})
        return 2
    except SystemExit as exc:
        return int(exc.code)

    # Third-party HTTP/model logs must not expose cookies, signed URLs or model text.
    from loguru import logger

    logger.remove()
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        from .study_artifacts import ArtifactError
        from .study_library import LibraryError
        from .study_publish import PublicationError
        from .study_runner import StudyConfigError, StudyScopeError
        from .study_schedule import ScheduleError

        try:
            # CLI output is structured only; accidental library prints are discarded.
            with open(os.devnull, "w", encoding="utf-8") as sink:
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    report = _dispatch(args)
            code = _exit_code(report)
        except (StudyConfigError, StudyScopeError) as exc:
            report = {"status": "invalid_config" if isinstance(exc, StudyConfigError) else "invalid_scope", "error_type": exc.code}
            code = 2
        except (ScheduleError, LibraryError, ArtifactError, PublicationError) as exc:
            report = {"status": "failed", "error_type": exc.code}
            code = 1
    except KeyboardInterrupt:
        report, code = {"status": "failed", "error_type": "interrupted"}, 1
    except Exception:
        report, code = {"status": "failed", "error_type": "study_execution_failed"}, 1
    finally:
        logging.disable(previous_logging)
    _write_json(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
