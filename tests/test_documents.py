import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from ssu_lms_mcp import documents
from ssu_lms_mcp.documents import list_files, read_file, search_files


@dataclass
class Settings:
    materials_dir: Path


def test_list_read_search_and_path_confinement(tmp_path):
    settings = Settings(tmp_path / "materials")
    folder = settings.materials_dir / "123"
    folder.mkdir(parents=True)
    source_text = "운영체제 수업 자료입니다. 프로세스 스케줄링"
    (folder / "sample.txt").write_text(source_text, encoding="utf-8")
    (folder / "notes.md").write_text("# 네트워크\nTCP 흐름 제어", encoding="utf-8")

    assert [item["path"] for item in list_files(settings)] == ["123/notes.md", "123/sample.txt"]
    page = read_file(settings, "123/sample.txt", offset=0, limit=10)
    assert page["text"] == source_text[:10]
    assert page["truncated"] is True
    assert read_file(settings, "123/sample.txt", offset=page["next_offset"])["text"] == source_text[10:]
    assert search_files(settings, "프로세스")["results"][0]["path"] == "123/sample.txt"
    with pytest.raises(ValueError):
        read_file(settings, "../secret.txt")


def test_extracts_generated_docx_pptx_and_notebook(tmp_path):
    from docx import Document
    from pptx import Presentation

    settings = Settings(tmp_path / "materials")
    folder = settings.materials_dir / "7"
    folder.mkdir(parents=True)
    document = Document()
    document.add_paragraph("문서 본문")
    document.save(folder / "a.docx")
    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[5]).shapes.add_textbox(0, 0, 1, 1).text_frame.text = "슬라이드 본문"
    presentation.save(folder / "b.pptx")
    (folder / "c.ipynb").write_text('{"cells":[{"source":["print(1)"]}]}', encoding="utf-8")

    assert "문서 본문" in read_file(settings, "7/a.docx")["text"]
    assert "슬라이드 본문" in read_file(settings, "7/b.pptx")["text"]
    assert "print(1)" in read_file(settings, "7/c.ipynb")["text"]


def test_rejects_unsupported_corrupt_and_oversize_files(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "materials")
    folder = settings.materials_dir / "9"
    folder.mkdir(parents=True)
    (folder / "binary.exe").write_bytes(b"x")
    (folder / "broken.pdf").write_bytes(b"not a pdf")
    (folder / "huge.txt").write_bytes(b"x")
    with pytest.raises(ValueError):
        read_file(settings, "9/binary.exe")
    with pytest.raises(ValueError):
        read_file(settings, "9/broken.pdf")
    monkeypatch.setattr("ssu_lms_mcp.documents.MAX_EXTRACT_BYTES", 0)
    with pytest.raises(ValueError):
        read_file(settings, "9/huge.txt")


def test_extracts_real_generated_pdf(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    settings = Settings(tmp_path / "materials")
    folder = settings.materials_dir / "1"
    folder.mkdir(parents=True)
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })}),
    })
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 50 700 Td (process scheduling) Tj ET")
    page[NameObject("/Contents")] = content
    writer.write(folder / "lecture.pdf")
    assert "process scheduling" in read_file(settings, "1/lecture.pdf")["text"]


def test_pdf_page_and_text_limits_stop_extraction(monkeypatch):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 1)
    with pytest.raises(ValueError, match="페이지"):
        documents._extract_text("lecture.pdf", output.getvalue())
    monkeypatch.setattr(documents, "MAX_EXTRACT_CHARS", 4)

    def parts():
        yield "first exceeds limit"
        raise AssertionError("길이 상한 이후의 본문은 추출하면 안 됩니다.")

    with pytest.raises(ValueError, match="길이"):
        documents._bounded_text(parts())


def test_zip_expansion_limit_before_document_parser(monkeypatch):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 2048)
    monkeypatch.setattr(documents, "MAX_EXTRACT_BYTES", 512)
    with pytest.raises(ValueError, match="압축 해제"):
        documents._extract_text("compressed.docx", output.getvalue())


def test_oversize_file_is_rejected_before_opening(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "materials")
    folder = settings.materials_dir / "1"
    folder.mkdir(parents=True)
    (folder / "large.txt").write_text("12345", encoding="utf-8")
    monkeypatch.setattr(documents, "MAX_EXTRACT_BYTES", 3)

    def forbidden_read(*args, **kwargs):
        raise AssertionError("크기 상한보다 큰 파일은 열면 안 됩니다.")

    with monkeypatch.context() as local:
        local.setattr(Path, "open", forbidden_read)
        with pytest.raises(ValueError, match="크기"):
            read_file(settings, "1/large.txt")


def test_real_empty_text_is_distinct_from_unsupported_file(tmp_path):
    settings = Settings(tmp_path / "materials")
    folder = settings.materials_dir / "1"
    folder.mkdir(parents=True)
    (folder / "empty.txt").write_bytes(b"")
    assert read_file(settings, "1/empty.txt")["text"] == ""


def test_external_root_existing_documents_and_absolute_paths(tmp_path):
    from ssu_lms_mcp.config import Settings as AppSettings

    root = tmp_path / "external vault"
    settings = AppSettings(data_dir=tmp_path / "private state", materials_dir=root)
    (root / "수업노트").mkdir(parents=True)
    (root / "123").mkdir()
    (root / ".private").mkdir()
    for rel in ["intro.txt", "수업노트/notes.md", "123/download.txt", ".private/hidden.txt"]:
        (root / rel).write_text("운영체제 교안", encoding="utf-8")
    listing = list_files(settings)
    assert {f["path"] for f in listing} == {"intro.txt", "수업노트/notes.md", "123/download.txt"}
    assert next(f for f in listing if f["path"] == "intro.txt")["course_id"] is None
    assert [f["path"] for f in list_files(settings, 123)] == ["123/download.txt"]
    assert read_file(settings, str(root / "intro.txt"))["text"] == "운영체제 교안"
    assert read_file(settings, "수업노트/notes.md")["absolute_path"] == str(root / "수업노트/notes.md")
    assert search_files(settings, "운영체제")["matched"] == 3
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    with pytest.raises(ValueError, match="허용되지"):
        read_file(settings, str(outside))
    with pytest.raises(ValueError, match="허용되지"):
        read_file(settings, "../private.txt")
    assert not settings.data_dir.exists()
