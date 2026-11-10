from __future__ import annotations

from app.services.extractor import extract_relevant_sections, stitch_broken_markdown_tables


def test_stitch_tables_across_pages() -> None:
    md = """
## Страница 1

| № | Дата | % |
| --- | --- | --- |
| 1 | 15.01.2026 | 25 |

## Страница 2

| 2 | 15.07.2026 | 25 |
| 3 | 15.01.2027 | 50 |
""".strip()
    stitched = stitch_broken_markdown_tables(md)
    assert stitched.count("| --- | --- | --- |") == 1
    assert "| 1 | 15.01.2026 | 25 |" in stitched
    assert "| 2 | 15.07.2026 | 25 |" in stitched
    assert "| 3 | 15.01.2027 | 50 |" in stitched
    # Маркеры страниц между склеенными частями таблицы удаляются
    assert stitched.count("| 2 |") == 1


def test_stitch_keeps_separate_tables_with_different_width() -> None:
    md = """
| A | B |
| --- | --- |
| 1 | 2 |

| X | Y | Z |
| --- | --- | --- |
| 9 | 8 | 7 |
""".strip()
    stitched = stitch_broken_markdown_tables(md)
    assert "| A | B |" in stitched
    assert "| X | Y | Z |" in stitched


def test_extract_relevant_sections_keeps_income_and_redemption() -> None:
    md = """
# Введение
Общие слова о компании и рынке.

## Порядок определения дохода
Купонный доход определяется как Ключевая ставка Банка России плюс 2.25%.

## История компании
Длинный нерелевантный текст про основание в 1991 году и филиальную сеть.

## Порядок и условия погашения
В дату окончания 6-го купонного периода погашается 25% номинальной стоимости.
""".strip()
    focused = extract_relevant_sections(md)
    assert "Ключевая ставка" in focused
    assert "6-го купонного" in focused
    assert "филиальную сеть" not in focused


def test_extract_relevant_sections_fallback_when_no_markers() -> None:
    md = "Просто текст без заголовков разделов и таблица | a | b |"
    assert extract_relevant_sections(md) == md
