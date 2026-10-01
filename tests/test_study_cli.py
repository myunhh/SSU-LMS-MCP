"""Public study CLI rejects incomplete scope without echoing private input."""

import json
import os
import subprocess
import sys

import pytest

from ssu_lms_mcp import study_cli


@pytest.mark.parametrize("arguments", [
    [],
    ["run", "--key", ""],
    ["run", "--key", " \t "],
    ["run", "--unknown", "private-argument-canary"],
    ["review", "--key", "review:1"],
    ["review", "--key", "review:1", "--rating", "private-argument-canary"],
    ["review", "--key", "", "--rating", "understood"],
    ["exam", "--title", "중간고사"],
    ["exam", "--course-id", "0", "--title", "중간고사"],
    ["exam", "--course-id", "-1", "--title", "중간고사"],
    ["exam", "--course-id", "private-argument-canary", "--title", "중간고사"],
    ["exam", "--key", "material:1:2"],
    ["exam", "--key", "material:1:2", "--title", "  "],
    ["schedule"],
    ["schedule", "--enable", "--disable"],
    ["schedule", "--status", "--enable"],
])
def test_invalid_request_is_safe_json_and_usage_exit(arguments, capsys):
    assert study_cli.main(arguments) == 2
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "invalid_arguments"
    assert report["error_type"] in {"invalid_arguments", "exam_scope_required"}
    assert "private-argument-canary" not in output.out + output.err
    assert not output.err


def test_exam_scope_preserves_exact_explicit_selection():
    args = study_cli._parser().parse_args([
        "exam", "--course-id", "12", "--course-id", "24", "--key", "board:12:1:2:3",
        "--key", "material:24:5", "--title", "범위 한정 연습",
    ])
    assert args.course_id == [12, 24]
    assert args.key == ["board:12:1:2:3", "material:24:5"]
    assert args.title == "범위 한정 연습"


def test_review_keeps_learners_answer_without_normalizing():
    answer = '  제 생각은 "관성"입니다.\n아직 단위는 모르겠습니다.  '
    args = study_cli._parser().parse_args([
        "review", "--key", "version:quiz", "--rating", "uncertain", "--answer", answer,
    ])
    assert args.answer == answer
    assert args.rating == "uncertain"


def test_module_entry_rejects_missing_scope_outside_workspace(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "ssu_lms_mcp.study_cli", "exam", "--title", "시험 대비"],
        cwd=tmp_path, env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True,
        text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error_type"] == "exam_scope_required"
    assert not result.stderr


def test_missing_config_does_not_expose_path_or_traceback(tmp_path, capsys):
    path = tmp_path / "private-argument-canary" / "missing.json"
    assert study_cli.main(["--config", str(path), "status"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "invalid_config"
    assert "private-argument-canary" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
