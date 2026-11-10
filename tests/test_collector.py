from __future__ import annotations

from pathlib import Path

import pytest

from app.services.collector import (
    CollectorService,
    DocumentStorage,
    EDisclosureRSSClient,
    MoexIssClient,
    parse_rss_xml_string,
)


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>e-disclosure mock</title>
    <item>
      <title>ПАО Пример — решение о выпуске биржевых облигаций</title>
      <link>https://e-disclosure.ru/portal/event.aspx?id=1</link>
      <guid>guid-bond-1</guid>
      <pubDate>Mon, 10 Nov 2025 10:00:00 GMT</pubDate>
      <description>Проспект ценных бумаг, номинал 1000</description>
      <enclosure url="https://e-disclosure.ru/files/1.pdf" type="application/pdf"/>
    </item>
    <item>
      <title>ООО Пример — сообщение о дивидендах</title>
      <link>https://e-disclosure.ru/portal/event.aspx?id=2</link>
      <guid>guid-div-2</guid>
      <description>Выплата дивидендов</description>
    </item>
  </channel>
</rss>
"""


def test_rss_keyword_filter() -> None:
    items = parse_rss_xml_string(SAMPLE_RSS)
    assert len(items) == 1
    assert items[0].guid == "guid-bond-1"
    assert items[0].enclosure_url.endswith("1.pdf")
    assert items[0].published_at is not None


@pytest.mark.asyncio
async def test_rss_mock_generator_filters_noise() -> None:
    client = EDisclosureRSSClient(use_mock=True)
    raw = client.build_mock_items()
    assert len(raw) == 2  # включая «дивиденды»

    service = CollectorService()
    service.rss = EDisclosureRSSClient(use_mock=True)
    items = await service.poll_disclosures()
    assert len(items) == 1
    assert "облигац" in items[0].title.lower() or "решение о выпуске" in items[0].title.lower()


def test_document_storage_dedup(tmp_path: Path) -> None:
    storage = DocumentStorage(root=tmp_path / "docs")
    a = storage.store_bytes(b"%PDF-1.4 mock", original_filename="Решение о выпуске.pdf")
    b = storage.store_bytes(b"%PDF-1.4 mock", original_filename="other.pdf")
    assert a.file_hash == b.file_hash
    assert a.path.exists()
    assert a.mime_type == "application/pdf"
    assert a.size_bytes == len(b"%PDF-1.4 mock")


def test_collector_store_upload_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_upload_size_mb", 0)  # 0 MB → любой файл велик
    # uploads_dir зависит от storage_dir; подменим storage сервиса напрямую
    service = CollectorService()
    service.storage = DocumentStorage(root=tmp_path)
    with pytest.raises(ValueError, match="too large"):
        service.store_upload(b"x", "a.pdf")


def test_moex_parse_date() -> None:
    assert MoexIssClient._parse_moex_date("2028-01-15").isoformat() == "2028-01-15"
    assert MoexIssClient._parse_moex_date("15.01.2028").isoformat() == "2028-01-15"
    assert MoexIssClient._parse_moex_date("0000-00-00") is None
    assert MoexIssClient._parse_moex_date(None) is None


@pytest.mark.asyncio
async def test_mock_llm_parser_extracts_from_text() -> None:
    from app.services.llm_parser import MockLLMBondParser

    text = (
        "Решение о выпуске. Рег. номер 4B02-01-36400-R-001P. "
        "ИНН 7707083893. Номинальная стоимость 1 000 рублей."
    )
    doc = await MockLLMBondParser().parse(text)
    assert doc.reg_number == "4B02-01-36400-R-001P"
    assert doc.issuer_inn == "7707083893"
    assert doc.nominal_value == 1000.0
    assert doc.quotes["reg_number"]
