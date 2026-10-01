import copy
import json
from pathlib import Path

import pymupdf
import pytest

from ssu_lms_mcp import study_content as content


def _pdf(path, texts, *, color=(0, 0, 0)):
    with pymupdf.open() as document:
        for text in texts:
            page = document.new_page(width=300, height=400)
            if text:
                page.insert_text((30, 40), text)
            page.draw_circle((100, 120), 20, color=color, fill=color)
        document.save(path)
    return path


@pytest.fixture
def package():
    return {
        "schema_version": 1,
        "title": "프로세스 학습",
        "source_sha256": "a" * 64,
        "page_count": 2,
        "summary": [{"text": "프로세스는 실행 중인 프로그램이다.", "pages": [1, 2], "evidence": "source"}],
        "concepts": [{"name": "프로세스", "definition": "실행 중인 프로그램", "intuition": "작업 단위",
                      "example": "[보충] 실행 중인 편집기", "pages": [1]}],
        "formulas": [],
        "misconceptions": [{"misconception": "프로그램과 프로세스는 같다.",
                            "correction": "프로세스에는 실행 상태가 있다.", "pages": [1]}],
        "guides": {"quick": [{"step": "정의를 회상한다.", "minutes": 10, "pages": [1]}],
                   "deep": [{"step": "상태 전이를 설명한다.", "minutes": 20, "pages": [1, 2]}]},
        "quizzes": [{"id": "q1", "prompt": "[AI 생성] 프로세스란?", "answer": "실행 중인 프로그램",
                     "explanation": "실행 상태를 포함한다.", "kind": "회상", "concept": "프로세스", "pages": [1]}],
        "practices": [{"task": "[AI 생성] 대기 상태의 예를 제시하라.",
                       "solution": "입출력 완료를 기다리는 경우이다.", "pages": [2]}],
        "prerequisites": [{"concept": "프로그램", "why": "실행과 저장된 명령을 구분한다.",
                           "pages": [1], "related_keys": []}],
        "changes": {"summary": ["처음 발견한 자료이다."], "added_pages": [1, 2],
                    "changed_pages": [], "removed_pages": []},
        "coverage": [1, 2], "review_required": False, "review_reasons": [],
    }


def _validate(package, *, related=None, diff=None, readable=None):
    bundle = {"source_sha256": "a" * 64, "page_count": 2}
    initial = {"added_pages": [1, 2], "changed_pages": [], "removed_pages": []}
    return content._validate_package(package, bundle, related or [], diff or initial, readable)


def _event(final, *, stop="stop"):
    return json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": stop,
        "content": [{"type": "text", "text": final}],
    }})


def test_extracts_all_pages_and_visual_only_evidence(tmp_path):
    path = _pdf(tmp_path / "lecture.pdf", ["Process scheduling", ""])
    bundle = content.extract_document(path, tmp_path / "rendered")
    assert bundle["complete"] is True
    assert bundle["page_count"] == 2
    assert [page["page"] for page in bundle["pages"]] == [1, 2]
    assert "Process scheduling" in bundle["pages"][0]["text"]
    assert bundle["pages"][1]["text"] == ""
    assert "no_extracted_text" in bundle["pages"][1]["flags"]
    for page in bundle["pages"]:
        image = pymupdf.Pixmap(page["image_path"])
        assert (image.width, image.height) == (600, 800)
        # The circle remains visible even on the page without extractable text.
        assert image.pixel(200, 240) == (0, 0, 0)


def test_unsupported_corrupt_and_truncated_documents_are_review_required(tmp_path):
    unsupported = tmp_path / "lecture.pptx"
    unsupported.write_bytes(b"preserved original")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a PDF")
    truncated = _pdf(tmp_path / "truncated.pdf", ["A real page"])
    data = truncated.read_bytes()
    truncated.write_bytes(data[:data.rfind(b"%%EOF")])
    for source, reason in ((unsupported, "unsupported_format"), (broken, "pdf_open_failed"),
                           (truncated, "pdf_missing_eof")):
        bundle = content.extract_document(source, tmp_path / "images")
        assert bundle["complete"] is False
        assert reason in bundle["review_reasons"]
        assert len(bundle["source_sha256"]) == 64
    assert unsupported.read_bytes() == b"preserved original"


def test_page_and_text_caps_cannot_become_complete_material(tmp_path, monkeypatch):
    source = _pdf(tmp_path / "lecture.pdf", ["First page", "Second page"])
    monkeypatch.setattr(content, "MAX_PDF_PAGES", 1)
    bundle = content.extract_document(source, tmp_path / "limited")
    assert bundle["page_count"] == 2
    assert bundle["complete"] is False
    assert "pdf_page_limit" in bundle["review_reasons"]
    monkeypatch.setattr(content, "MAX_PDF_PAGES", 500)
    monkeypatch.setattr(content, "MAX_PAGE_TEXT", 3)
    bundle = content.extract_document(source, tmp_path / "text-limited")
    assert bundle["complete"] is False
    assert all("text_limit" in page["flags"] for page in bundle["pages"])
    assert all(page["image_path"] for page in bundle["pages"])
    with pytest.raises(content.ContentError, match="document_review_required"):
        content.build_package(bundle, {}, None, [], {}, tmp_path)


def test_diff_aligns_insertions_and_detects_visual_only_changes(tmp_path):
    before = content.extract_document(_pdf(tmp_path / "before.pdf", ["A", "B"]), tmp_path / "images")
    inserted = content.extract_document(_pdf(tmp_path / "inserted.pdf", ["X", "A", "B"]), tmp_path / "images")
    diff = content.compare_documents(before, inserted)
    assert diff["added_pages"] == [1]
    assert diff["changed_pages"] == []
    assert diff["removed_pages"] == []
    visual = content.extract_document(_pdf(tmp_path / "visual.pdf", ["A", "B"], color=(1, 0, 0)), tmp_path / "images")
    assert [p["text"] for p in before["pages"]] == [p["text"] for p in visual["pages"]]
    diff = content.compare_documents(before, visual)
    assert diff["changed_pages"] == [1, 2]
    assert diff["added_pages"] == diff["removed_pages"] == []
    removed = content.compare_documents(inserted, before)
    assert removed["removed_pages"] == [1]
    assert removed["changed_pages"] == []


def test_diff_missing_image_is_not_treated_as_unchanged(tmp_path):
    bundle = content.extract_document(_pdf(tmp_path / "lecture.pdf", ["A"]), tmp_path / "images")
    Path(bundle["pages"][0]["image_path"]).unlink()
    with pytest.raises(content.ContentError, match="document_image_missing"):
        content.compare_documents(bundle, bundle)


@pytest.mark.parametrize("pages", [[0], [3], [-1], [True], [1, 1], ["1"], []])
def test_package_rejects_invalid_page_citations(package, pages):
    package["concepts"][0]["pages"] = pages
    with pytest.raises(content.ContentError):
        _validate(package)


def test_package_requires_complete_coverage_and_actual_related_keys(package):
    package["coverage"] = [1]
    with pytest.raises(content.ContentError, match="coverage"):
        _validate(package)
    package["coverage"] = [1, 2]
    package["prerequisites"][0]["related_keys"] = ["invented"]
    with pytest.raises(content.ContentError, match="related_key"):
        _validate(package)
    _validate(package, related=[{"key": "invented"}])


@pytest.mark.parametrize("section", ["summary", "concepts", "misconceptions", "quizzes", "practices", "prerequisites"])
def test_package_cannot_publish_empty_required_sections(package, section):
    package[section] = []
    with pytest.raises(content.ContentError, match="schema"):
        _validate(package)


def test_package_requires_source_identity_labels_and_consistent_review_state(package):
    for mutate in (
        lambda value: value.update(source_sha256="b" * 64),
        lambda value: value["quizzes"][0].update(prompt="프로세스란?"),
        lambda value: value["practices"][0].update(task="교수님 출제 예상 문제"),
        lambda value: value["quizzes"][0].update(id="q2"),
        lambda value: value["guides"]["quick"][0].update(minutes=True),
        lambda value: value.update(review_reasons=["그림 판독 불확실"]),
        lambda value: value["concepts"][0].update(definition=" "),
    ):
        invalid = copy.deepcopy(package)
        mutate(invalid)
        with pytest.raises(content.ContentError):
            _validate(invalid)


@pytest.mark.parametrize("minutes", [9, 11, 20])
def test_quick_guide_cannot_mislabel_other_durations_as_ten_minutes(package, minutes):
    package["guides"]["quick"][0]["minutes"] = minutes
    with pytest.raises(content.ContentError, match="quick_guide_duration"):
        _validate(package)


def test_removed_citations_use_previous_version_numbering(package):
    diff = {"added_pages": [], "changed_pages": [2], "removed_pages": [5]}
    package["changes"].update(diff)
    _validate(package, diff=diff)
    package["changes"]["removed_pages"] = [2]
    with pytest.raises(content.ContentError, match="change_metadata"):
        _validate(package, diff=diff)


def test_final_parser_ignores_thinking_and_deltas_but_requires_completed_json():
    event = {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
             "content": [{"type": "thinking", "thinking": "private reasoning"},
                         {"type": "text", "text": '{"title":"실제 '},
                         {"type": "text", "text": '출력"}'}]}}
    stream = json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "ignored"}})
    result = content._parse_jsonl(stream + "\n" + json.dumps(event))
    assert result == {"title": "실제 출력"}
    with pytest.raises(content.ContentError, match="final_missing"):
        content._parse_jsonl(stream)


@pytest.mark.parametrize("final", ['{"title":', '```json\n{"title":"x"}\n```',
                                   '{"x":1,"x":2}', '{"x":NaN}', '[]'])
def test_malformed_model_output_is_not_recovered_or_published(final):
    with pytest.raises(content.ContentError):
        content._parse_jsonl(_event(final))


def test_empty_final_message_cannot_reuse_an_earlier_answer():
    with pytest.raises(content.ContentError, match="final_missing"):
        content._parse_jsonl(_event('{"title":"earlier"}') + "\n" + _event(""))


def test_tool_invocations_and_truncated_model_responses_fail_closed():
    for event in (
        {"type": "tool_execution_start", "toolName": "bash"},
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_start"}},
        {"type": "message_start", "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "read"}]}},
    ):
        with pytest.raises(content.ContentError, match="tool_invocation"):
            content._parse_jsonl(json.dumps(event) + "\n" + _event('{"title":"x"}'))
    for stop in ("length", "error", "aborted", None, "end_turn"):
        with pytest.raises(content.ContentError, match="incomplete_response"):
            content._parse_jsonl(_event('{"title":"x"}', stop=stop))


@pytest.fixture
def exam():
    refs = [{"source_key": "material:7:1", "pages": [1]}]
    return {
        "title": "운영체제 시험 대비", "scope": {"course_id": 7},
        "comparisons": [{"topic": "프로그램과 프로세스", "explanation": "실행 상태를 구분한다.", "sources": copy.deepcopy(refs)}],
        "weak_topics": [],
        "questions": [{"id": "e1", "prompt": "[AI 생성] 프로세스란?", "kind": "회상", "topic": "프로세스", "sources": copy.deepcopy(refs)}],
        "answers": [{"question_id": "e1", "answer": "실행 중인 프로그램", "explanation": "상태를 포함한다.", "sources": copy.deepcopy(refs)}],
        "sources": copy.deepcopy(refs), "review_required": False, "review_reasons": [],
    }


def test_exam_rejects_invented_sources_out_of_bounds_and_unpaired_answers(package, exam):
    sources = {"material:7:1": package}
    scope = {"course_id": 7}
    content._validate_exam(exam, sources, scope, [])
    for mutate in (
        lambda value: value["questions"][0]["sources"][0].update(source_key="material:7:999"),
        lambda value: value["answers"][0]["sources"][0].update(pages=[3]),
        lambda value: value["answers"][0].update(question_id="e2"),
        lambda value: value.update(scope={"course_id": 8}),
        lambda value: value["questions"][0]["sources"][0].update(pages=[2]),
    ):
        invalid = copy.deepcopy(exam)
        mutate(invalid)
        with pytest.raises(content.ContentError):
            content._validate_exam(invalid, sources, scope, [])


def test_exam_weak_topics_must_match_actual_scoped_review_history(package, exam):
    sources = {"material:7:1": package}
    reviews = [{"key": "v1:q1", "material_key": "material:7:1", "rating": "unknown", "answer": "모름"},
               {"key": "v2:q1", "material_key": "material:8:1", "rating": "unknown", "answer": "모름"}]
    with pytest.raises(content.ContentError, match="weak_coverage"):
        content._validate_exam(exam, sources, {"course_id": 7}, reviews)
    exam["weak_topics"] = [{"topic": "프로세스", "reason": "모름으로 답했다.", "review_keys": ["v1:q1"],
                           "sources": [{"source_key": "material:7:1", "pages": [1]}]}]
    content._validate_exam(exam, sources, {"course_id": 7}, reviews)
    exam["weak_topics"][0]["review_keys"] = ["v2:q1"]
    with pytest.raises(content.ContentError, match="weak_review_unknown"):
        content._validate_exam(exam, sources, {"course_id": 7}, reviews)
