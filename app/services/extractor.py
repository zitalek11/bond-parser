from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

BackendName = Literal["docling", "pymupdf", "docx", "text"]

# Заголовки разделов эмиссионных документов РФ, важные для извлечения параметров выпуска.
RELEVANT_SECTION_MARKERS: tuple[str, ...] = (
    "сведения о размещаемых ценных бумагах",
    "основные сведения о ценных бумагах",
    "условия размещения",
    "порядок определения дохода",
    "порядок определения процентного дохода",
    "порядок и условия погашения",
    "срок и порядок выплаты купонного дохода",
    "размер процентного дохода",
    "график выплат",
    "амортизация",
    "погашение номинальной стоимости",
    "идентификационные признаки",
    "решение о выпуске",
    "проспект ценных бумаг",
)


@dataclass
class ExtractionResult:
    markdown: str
    backend: BackendName
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def extract_relevant_sections(markdown_text: str) -> str:
    """Вырезает целевые главы эмиссионного документа для LLM.

    Ищем заголовки вида `# ...` / `## ...` / жирный текст / строки CAPS и оставляем
    только разделы, связанные с параметрами выпуска, купонами и погашением.
    Если маркеры не найдены — возвращаем исходный текст без изменений.
    """
    if not markdown_text or not markdown_text.strip():
        return markdown_text

    lines = markdown_text.splitlines()
    heading_indexes: list[tuple[int, str]] = []
    heading_re = re.compile(
        r"^(#{1,6}\s+|.+?\n[=-]{3,}\s*$|(?:\*\*|__)?([А-ЯA-Z0-9 «»\"].{8,80})(?:\*\*|__)?\s*$)"
    )

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        is_md_heading = stripped.startswith("#")
        is_short_title = (
            8 <= len(stripped) <= 120
            and not stripped.startswith("|")
            and not stripped.endswith(".")
            and sum(ch.isdigit() for ch in stripped) < 8
        )
        if is_md_heading or (is_short_title and heading_re.match(stripped)):
            heading_indexes.append((i, stripped.lstrip("#").strip(" *_")))

    if not heading_indexes:
        return markdown_text

    selected_ranges: list[tuple[int, int]] = []
    for idx, (start, title) in enumerate(heading_indexes):
        title_l = title.casefold()
        if any(marker in title_l for marker in RELEVANT_SECTION_MARKERS):
            end = heading_indexes[idx + 1][0] if idx + 1 < len(heading_indexes) else len(lines)
            selected_ranges.append((start, end))

    if not selected_ranges:
        return markdown_text

    # Склеиваем выбранные диапазоны без захвата соседних нерелевантных глав
    chunks: list[str] = []
    for start, end in selected_ranges:
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            chunks.append(chunk)

    merged = "\n\n---\n\n".join(chunks).strip()
    # Если вырезка пустая или ничтожно мала относительно исходника — полный текст.
    # Для коротких документов (демо/тесты) порог ниже.
    min_chars = min(500, max(80, int(len(markdown_text) * 0.15)))
    if len(merged) < min_chars:
        logger.info(
            "extract_relevant_sections: selection too small (%d/%d) — using full text",
            len(merged),
            len(markdown_text),
        )
        return markdown_text

    logger.info(
        "extract_relevant_sections: %d chars → %d chars (%d sections)",
        len(markdown_text),
        len(merged),
        len(selected_ranges),
    )
    return merged


def stitch_broken_markdown_tables(markdown: str) -> str:
    """Склеивает Markdown-таблицы, разорванные по страницам PDF.

    Типичный кейс ЦБ/эмиссионки: шапка на стр. N, продолжение без шапки на стр. N+1.
    Если у соседних таблиц одинаковое число столбцов — объединяем строки тела,
    сохраняя шапку первой таблицы и выкидывая повторный separator / дубль шапки.
    """
    if not markdown or "|" not in markdown:
        return markdown

    lines = markdown.splitlines()
    out: list[str] = []
    i = 0
    pending_table: list[str] | None = None
    pending_cols: int | None = None

    def _flush() -> None:
        nonlocal pending_table, pending_cols
        if pending_table:
            out.extend(pending_table)
            if out and out[-1] != "":
                out.append("")
            pending_table = None
            pending_cols = None

    while i < len(lines):
        # Маркер страницы между продолжениями таблицы — пропускаем только
        # если уже есть незакрытая таблица и дальше снова идёт таблица.
        if re.match(r"^##\s+Страница\s+\d+", lines[i].strip(), re.IGNORECASE):
            if pending_table is not None:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                nxt, nxt_cols, _ = _read_table_block(lines, j)
                if nxt is not None and nxt_cols == pending_cols:
                    i = j
                    continue
            out.append(lines[i])
            i += 1
            continue

        table_block, cols, next_i = _read_table_block(lines, i)
        if table_block is None:
            _flush()
            out.append(lines[i])
            i += 1
            continue

        if pending_table is None:
            pending_table = table_block
            pending_cols = cols
            i = next_i
            continue

        # Есть незакрытая таблица — пробуем склеить
        if cols == pending_cols:
            body = _table_body_only(table_block)
            while pending_table and not pending_table[-1].strip():
                pending_table.pop()
            pending_table.extend(body)
            i = next_i
            continue

        _flush()
        pending_table = table_block
        pending_cols = cols
        i = next_i

    _flush()
    # Нормализуем лишние пустые строки
    stitched = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"
    return stitched


def _count_columns(row: str) -> int:
    cells = [c for c in row.strip().strip("|").split("|")]
    return len(cells)


def _is_separator(row: str) -> bool:
    stripped = row.strip().strip("|").replace(" ", "")
    return bool(stripped) and set(stripped) <= {"-", ":", "|"}


def _is_table_row(row: str) -> bool:
    s = row.strip()
    return s.startswith("|") and s.count("|") >= 2


def _read_table_block(
    lines: list[str], start: int
) -> tuple[list[str] | None, int | None, int]:
    if start >= len(lines) or not _is_table_row(lines[start]):
        return None, None, start

    block: list[str] = []
    i = start
    while i < len(lines) and (_is_table_row(lines[i]) or not lines[i].strip()):
        if _is_table_row(lines[i]):
            block.append(lines[i])
        elif block:
            # пустая строка внутри/после таблицы — конец блока
            break
        i += 1
        # после первой пустой — стоп
        if i < len(lines) and not lines[i - 1].strip() and block:
            break

    if len(block) < 1:
        return None, None, start
    cols = _count_columns(block[0])
    return block, cols, i


def _table_body_only(block: list[str]) -> list[str]:
    """Убирает шапку и separator, если продолжение страницы их повторило."""
    if not block:
        return []
    body = list(block)
    # Если первая строка — separator, отбрасываем
    if body and _is_separator(body[0]):
        body = body[1:]
    # Если есть header+separator в начале продолжения — отбрасываем обе
    if len(body) >= 2 and _is_separator(body[1]):
        body = body[2:]
    elif len(body) >= 1 and _is_separator(body[0]):
        body = body[1:]
    return body


class DocumentExtractor:
    """Конвертация PDF/DOCX → Markdown с сохранением таблиц.

    Стратегия выбора бэкенда (`EXTRACTOR_BACKEND`):
      - auto    — docling, если установлен; иначе pymupdf + pdfplumber
      - docling — строго Docling (ImportError, если нет)
      - pymupdf — лёгкий фолбэк (PyMuPDF текст + pdfplumber таблицы)
    """

    SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc", ".txt", ".md"}

    def __init__(self, backend: str | None = None) -> None:
        self.preferred = (backend or settings.extractor_backend).lower()

    def extract(self, path: str | Path) -> ExtractionResult:
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Document not found: {file_path}")

        suffix = file_path.suffix.lower()
        if suffix not in self.SUPPORTED_SUFFIXES:
            raise ValueError(
                f"Unsupported file type '{suffix}'. "
                f"Allowed: {sorted(self.SUPPORTED_SUFFIXES)}"
            )

        if suffix in {".txt", ".md"}:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            text = stitch_broken_markdown_tables(text)
            return ExtractionResult(markdown=text, backend="text", page_count=1)

        if suffix in {".docx", ".doc"}:
            return self._extract_docx(file_path)

        return self._extract_pdf(file_path)

    # ------------------------------------------------------------------ PDF

    def _extract_pdf(self, path: Path) -> ExtractionResult:
        if self.preferred == "docling":
            result = self._extract_with_docling(path)
        elif self.preferred == "pymupdf":
            result = self._extract_with_pymupdf(path)
        else:
            try:
                result = self._extract_with_docling(path)
            except ImportError:
                logger.info("docling not installed — falling back to pymupdf/pdfplumber")
                result = self._extract_with_pymupdf(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("docling failed (%s) — falling back to pymupdf", exc)
                result = self._extract_with_pymupdf(path)
                result.warnings.append(f"docling_fallback: {exc}")

        before = result.markdown
        result.markdown = stitch_broken_markdown_tables(before)
        if result.markdown != before:
            result.warnings.append("tables_stitched")
            result.metadata["stitched"] = True
        return result

    def _extract_with_docling(self, path: Path) -> ExtractionResult:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "docling is not installed. "
                "pip install -r requirements-docling.txt  "
                "or set EXTRACTOR_BACKEND=pymupdf"
            ) from exc

        converter = DocumentConverter()
        converted = converter.convert(str(path))
        markdown = converted.document.export_to_markdown()
        page_count = None
        try:
            page_count = len(converted.document.pages)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            page_count = None

        logger.info("Extracted %s via docling (%d chars)", path.name, len(markdown))
        return ExtractionResult(
            markdown=markdown,
            backend="docling",
            page_count=page_count,
            metadata={"source": str(path)},
        )

    def _extract_with_pymupdf(self, path: Path) -> ExtractionResult:
        import pdfplumber
        import pymupdf

        warnings: list[str] = []
        pages_md: list[str] = []

        with pymupdf.open(path) as doc:
            page_count = doc.page_count
            tables_by_page: dict[int, list[list[list[str | None]]]] = {}
            try:
                with pdfplumber.open(path) as pdf:
                    for i, page in enumerate(pdf.pages):
                        found = page.extract_tables() or []
                        if found:
                            tables_by_page[i] = found
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"pdfplumber_tables: {exc}")

            for i, page in enumerate(doc):
                text = (page.get_text("text") or "").strip()
                chunks: list[str] = [f"## Страница {i + 1}", ""]
                if text:
                    chunks.append(text)
                    chunks.append("")

                for table in tables_by_page.get(i, []):
                    chunks.append(self._table_to_markdown(table))
                    chunks.append("")

                pages_md.append("\n".join(chunks).rstrip())

        markdown = "\n\n".join(pages_md).strip() + "\n"
        if not markdown.strip():
            warnings.append("empty_extraction")

        logger.info(
            "Extracted %s via pymupdf (%d pages, %d chars)",
            path.name,
            page_count,
            len(markdown),
        )
        return ExtractionResult(
            markdown=markdown,
            backend="pymupdf",
            page_count=page_count,
            warnings=warnings,
            metadata={"source": str(path)},
        )

    # ------------------------------------------------------------------ DOCX

    def _extract_docx(self, path: Path) -> ExtractionResult:
        try:
            from docx import Document
        except ImportError as exc:  # pragma: no cover
            raise ImportError("python-docx is required for DOCX extraction") from exc

        if path.suffix.lower() == ".doc":
            raise ValueError(
                "Legacy .doc format is not supported. Convert to .docx or .pdf first."
            )

        document = Document(str(path))
        parts: list[str] = []

        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        body = document.element.body
        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                para = Paragraph(child, document)
                text = para.text.strip()
                if text:
                    parts.append(text)
            elif child.tag == qn("w:tbl"):
                table = Table(child, document)
                grid = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                parts.append(self._table_to_markdown(grid))

        markdown = stitch_broken_markdown_tables("\n\n".join(parts).strip() + "\n")
        logger.info("Extracted %s via python-docx (%d chars)", path.name, len(markdown))
        return ExtractionResult(
            markdown=markdown,
            backend="docx",
            page_count=None,
            metadata={"source": str(path)},
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _table_to_markdown(table: list[list[str | None]]) -> str:
        if not table:
            return ""
        width = max(len(row) for row in table)
        rows = [
            [(cell or "").replace("\n", " ").strip() for cell in row]
            + [""] * (width - len(row))
            for row in table
        ]
        header = rows[0]
        if not any(header):
            header = [f"col_{i + 1}" for i in range(width)]
            body = rows
        else:
            body = rows[1:]

        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        for row in body:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)
