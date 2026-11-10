from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from docx import Document

from app.services.extractor import DocumentExtractor


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "decision.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Решение о выпуске биржевых облигаций\n"
        "Регистрационный номер: 4B02-01-36400-R-001P\n"
        "ИНН 7707083893\n"
        "Номинал 1000 рублей",
        fontsize=11,
    )
    # Простая таблица купонов через text (pdfplumber tables optional)
    page.insert_text((72, 160), "Купон | Начало | Конец | Ставка", fontsize=10)
    page.insert_text((72, 175), "1 | 15.01.2025 | 15.07.2025 | 15.5", fontsize=10)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def sample_docx(tmp_path: Path) -> Path:
    path = tmp_path / "prospect.docx"
    document = Document()
    document.add_paragraph("Проспект ценных бумаг")
    document.add_paragraph("Эмитент ПАО Тест, ИНН 7707083893")
    table = document.add_table(rows=2, cols=3)
    table.rows[0].cells[0].text = "№"
    table.rows[0].cells[1].text = "Дата"
    table.rows[0].cells[2].text = "%"
    table.rows[1].cells[0].text = "1"
    table.rows[1].cells[1].text = "15.01.2028"
    table.rows[1].cells[2].text = "100"
    document.save(path)
    return path


def test_extract_pdf_pymupdf(sample_pdf: Path) -> None:
    result = DocumentExtractor(backend="pymupdf").extract(sample_pdf)
    assert result.backend == "pymupdf"
    assert result.page_count == 1
    assert "4B02-01-36400-R-001P" in result.markdown
    assert "7707083893" in result.markdown
    assert "## Страница 1" in result.markdown


def test_extract_docx(sample_docx: Path) -> None:
    result = DocumentExtractor().extract(sample_docx)
    assert result.backend == "docx"
    assert "Проспект ценных бумаг" in result.markdown
    assert "7707083893" in result.markdown
    assert "| № | Дата | % |" in result.markdown
    assert "| 1 | 15.01.2028 | 100 |" in result.markdown


def test_extract_txt(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("# Hello\n\nНоминал 1000", encoding="utf-8")
    result = DocumentExtractor().extract(path)
    assert result.backend == "text"
    assert "Номинал 1000" in result.markdown


def test_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "x.xlsx"
    path.write_bytes(b"PK")
    with pytest.raises(ValueError, match="Unsupported"):
        DocumentExtractor().extract(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        DocumentExtractor().extract(tmp_path / "nope.pdf")


def test_table_to_markdown_helper() -> None:
    md = DocumentExtractor._table_to_markdown(
        [["A", "B"], ["1", "2"], ["3", None]]
    )
    assert md.splitlines()[0] == "| A | B |"
    assert "| 3 |  |" in md
