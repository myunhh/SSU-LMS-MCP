"""로컬 강의자료의 안전한 목록·본문 추출·검색 도우미."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

MAX_EXTRACT_BYTES = 20 * 1024 * 1024
MAX_TEXT = 16_000
MAX_EXTRACT_CHARS = 1_000_000
MAX_PDF_PAGES = 500
SEARCH_FILE_CAP = 50
SNIPPET_PAD = 80


def _bounded_text(parts) -> str:
    """합치기 전에 누적 길이를 제한해 압축 문서의 거대한 본문을 방지한다."""
    chunks = []
    length = 0
    for part in parts:
        length += len(part) + bool(chunks)
        if length > MAX_EXTRACT_CHARS:
            raise ValueError("자료 본문 추출 길이 상한(100만 자)을 초과했습니다.")
        chunks.append(part)
    return "\n".join(chunks)


def _materials_root(settings) -> Path:
    return Path(settings.materials_dir).resolve()


def _safe_path(settings, path: str) -> Path:
    """지정 자료 폴더 내부의 상대·절대 경로만 허용한다(링크 탈출 포함)."""
    root = _materials_root(settings)
    if not isinstance(path, str) or not path:
        raise ValueError("자료 파일 경로를 지정하세요.")
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("허용되지 않은 자료 경로입니다.") from exc
    return candidate


def _extract_text(filename: str, data: bytes) -> str:
    """지원 파일에서 텍스트를 추출한다. 지원하지 않거나 손상되면 명시적으로 실패한다."""
    ext = Path(filename).suffix.lower()
    if ext not in {".pdf", ".pptx", ".docx", ".ipynb", ".txt", ".md"}:
        raise ValueError("지원하지 않는 자료 형식입니다.")
    try:
        if ext in {".pptx", ".docx"}:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(entry.file_size for entry in archive.infolist()) > MAX_EXTRACT_BYTES:
                    raise ValueError("압축 해제 크기 상한을 초과했습니다.")
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            if len(reader.pages) > MAX_PDF_PAGES:
                raise ValueError("PDF 페이지 상한(500쪽)을 초과했습니다.")
            return _bounded_text(page.extract_text() or "" for page in reader.pages)
        if ext == ".pptx":
            from pptx import Presentation

            presentation = Presentation(io.BytesIO(data))
            return _bounded_text(
                shape.text_frame.text
                for slide in presentation.slides
                for shape in slide.shapes
                if shape.has_text_frame and shape.text_frame.text.strip()
            )
        if ext == ".docx":
            from docx import Document

            document = Document(io.BytesIO(data))

            def paragraphs_and_tables():
                yield from (p.text for p in document.paragraphs if p.text.strip())
                for table in document.tables:
                    for row in table.rows:
                        yield " | ".join(cell.text for cell in row.cells)

            return _bounded_text(paragraphs_and_tables())
        if ext == ".ipynb":
            notebook = json.loads(data.decode("utf-8", errors="replace"))
            return _bounded_text(
                "".join(cell.get("source", []))
                if isinstance(cell.get("source", []), list)
                else str(cell.get("source", ""))
                for cell in notebook.get("cells", [])
            )
        return _bounded_text([data.decode("utf-8", errors="strict")])
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("자료 본문을 추출할 수 없습니다.") from exc


def list_files(settings, course_id: int | None = None) -> list[dict]:
    root = _materials_root(settings)
    if not root.exists():
        return []
    folders = [root / str(course_id)] if course_id is not None else [root]
    files: list[dict] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        if not folder.resolve().is_relative_to(root):
            continue
        for item in folder.rglob("*"):
            if not item.is_file() or item.name.startswith(".") or item.suffix == ".part":
                continue
            try:
                resolved = item.resolve()
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in item.relative_to(root).parts):
                continue
            cid = int(relative.parts[0]) if len(relative.parts) > 1 and relative.parts[0].isdigit() else None
            files.append({"path": relative.as_posix(), "absolute_path": str(resolved), "size": resolved.stat().st_size, "course_id": cid})
    return sorted(files, key=lambda item: item["path"])


def read_file(settings, path: str, offset: int = 0, limit: int = MAX_TEXT) -> dict:
    if offset < 0 or offset > MAX_EXTRACT_CHARS or limit < 1:
        raise ValueError("offset은 0~100만, limit은 1 이상이어야 합니다.")
    limit = min(limit, MAX_TEXT)
    source = _safe_path(settings, path)
    if not source.is_file():
        raise FileNotFoundError("자료 파일을 찾을 수 없습니다.")
    if source.stat().st_size > MAX_EXTRACT_BYTES:
        raise ValueError("자료 파일 크기 상한을 초과했습니다.")
    with source.open("rb") as handle:
        data = handle.read(MAX_EXTRACT_BYTES + 1)
    if len(data) > MAX_EXTRACT_BYTES:
        raise ValueError("자료 파일 크기 상한을 초과했습니다.")
    text = _extract_text(source.name, data)
    chunk = text[offset : offset + limit]
    next_offset = offset + len(chunk) if offset + len(chunk) < len(text) else None
    return {
        "path": source.relative_to(_materials_root(settings)).as_posix(),
        "absolute_path": str(source),
        "text": chunk,
        "offset": offset,
        "next_offset": next_offset,
        "truncated": next_offset is not None,
        "total_chars": len(text),
    }


def _snippets(text: str, query: str) -> list[str]:
    out: list[str] = []
    start = 0
    lower_text, lower_query = text.lower(), query.lower()
    while len(out) < 3:
        found = lower_text.find(lower_query, start)
        if found < 0:
            break
        end = min(len(text), found + len(query) + SNIPPET_PAD)
        out.append(" ".join(text[max(0, found - SNIPPET_PAD) : end].split()))
        start = max(end, found + len(query))
    return out


def search_files(settings, query: str, course_id: int | None = None) -> dict:
    query = (query or "").strip()
    if not query:
        raise ValueError("검색어를 입력하세요.")
    files = list_files(settings, course_id)
    selected = files[:SEARCH_FILE_CAP]
    results = []
    for item in selected:
        try:
            text = read_file(settings, item["path"], limit=MAX_TEXT)["text"]
        except (FileNotFoundError, OSError, ValueError):
            continue
        snippets = _snippets(text, query)
        if snippets:
            results.append({**item, "snippets": snippets})
    result = {
        "query": query, "matched": len(results), "scanned": len(selected), "results": results,
        "note": f"파일 {len(files)}개 중 최대 {SEARCH_FILE_CAP}개, 파일당 앞부분 {MAX_TEXT}자를 검색합니다.",
    }
    return result
