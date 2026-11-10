from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import feedparser
import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.bond_document import MoexReference

logger = get_logger(__name__)


@dataclass
class DisclosureItem:
    """Сообщение из RSS / mock-источника о потенциально релевантном документе."""

    guid: str
    title: str
    link: str
    published_at: datetime | None = None
    summary: str = ""
    enclosure_url: str | None = None
    source: str = "RSS"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StoredDocument:
    """Файл, сохранённый в локальное storage/."""

    path: Path
    original_filename: str
    file_hash: str
    mime_type: str
    size_bytes: int


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


class EDisclosureRSSClient:
    """Парсер RSS Интерфакс-ЦРКИ (e-disclosure.ru) с фильтрацией по ключевым словам.

    Если лента недоступна или RSS_USE_MOCK=true — отдаёт детерминированные
    тестовые события (удобно для локальной разработки без сети).
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        keywords: list[str] | None = None,
        use_mock: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        self.url = url or settings.rss_url
        self.keywords = [k.lower() for k in (keywords or settings.rss_keywords)]
        self.use_mock = settings.rss_use_mock if use_mock is None else use_mock
        self.timeout = timeout if timeout is not None else settings.http_timeout_seconds

    async def fetch(self) -> list[DisclosureItem]:
        if self.use_mock:
            logger.info("RSS: using mock generator (RSS_USE_MOCK=true)")
            return self.build_mock_items()

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(
                    self.url,
                    headers={"User-Agent": "BondEmissionParser/0.1 (+mvp)"},
                )
                response.raise_for_status()
                return self._parse_feed(response.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RSS fetch failed (%s) — falling back to mock items", exc)
            items = self.build_mock_items()
            for item in items:
                item.source = "MOCK"
                item.raw["fallback_reason"] = str(exc)
            return items

    def _parse_feed(self, payload: str) -> list[DisclosureItem]:
        parsed = feedparser.parse(payload)
        items: list[DisclosureItem] = []
        for entry in parsed.entries:
            title = (entry.get("title") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            haystack = f"{title}\n{summary}".lower()
            if not any(kw in haystack for kw in self.keywords):
                continue

            link = (entry.get("link") or "").strip()
            guid = (entry.get("id") or entry.get("guid") or link or title).strip()
            published = self._parse_date(entry)
            enclosure = None
            for enc in entry.get("enclosures") or []:
                href = enc.get("href") or enc.get("url")
                if href:
                    enclosure = href
                    break

            items.append(
                DisclosureItem(
                    guid=guid,
                    title=title,
                    link=link,
                    published_at=published,
                    summary=summary,
                    enclosure_url=enclosure,
                    source="RSS",
                    raw={"title": title, "link": link},
                )
            )

        logger.info("RSS: %d relevant items (of %d total)", len(items), len(parsed.entries))
        return items

    @staticmethod
    def _parse_date(entry: dict[str, Any]) -> datetime | None:
        if entry.get("published_parsed"):
            try:
                return datetime(*entry.published_parsed[:6], tzinfo=UTC)  # type: ignore[index]
            except Exception:  # noqa: BLE001
                pass
        raw = entry.get("published") or entry.get("updated")
        if not raw:
            return None
        try:
            dt = parsedate_to_datetime(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def build_mock_items() -> list[DisclosureItem]:
        now = datetime.now(UTC)
        return [
            DisclosureItem(
                guid="mock-disclosure-sber-001p-01",
                title="ПАО Сбербанк — решение о выпуске биржевых облигаций серии 001P-01",
                link="https://www.e-disclosure.ru/mock/sber-001p-01",
                published_at=now,
                summary=(
                    "Раскрыто решение о выпуске биржевых облигаций. "
                    "Регистрационный номер 4B02-01-36400-R-001P. "
                    "Номинал 1000 RUB. ИНН 7707083893."
                ),
                enclosure_url=None,
                source="MOCK",
            ),
            DisclosureItem(
                guid="mock-disclosure-unrelated-dividends",
                title="ООО Пример — сообщение о выплате дивидендов",
                link="https://www.e-disclosure.ru/mock/dividends",
                published_at=now,
                summary="Дивиденды по акциям за 2024 год.",
                source="MOCK",
            ),
        ]


# ---------------------------------------------------------------------------
# MOEX ISS
# ---------------------------------------------------------------------------


class MoexIssClient:
    """Клиент публичного MOEX ISS API для эталонных данных по бумаге."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or settings.moex_iss_base_url).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.http_timeout_seconds
        self._headers = {
            "User-Agent": "BondEmissionParser/0.1 (research; +https://localhost)",
            "Accept": "application/json",
        }

    async def lookup(self, *, isin: str | None = None, secid: str | None = None) -> MoexReference:
        if not isin and not secid:
            return MoexReference(found=False)

        try:
            if secid:
                return await self._fetch_by_secid(secid)
            assert isin is not None
            resolved = await self._resolve_secid_by_isin(isin)
            if not resolved:
                logger.info("MOEX: ISIN %s not found", isin)
                return MoexReference(found=False, isin=isin)
            return await self._fetch_by_secid(resolved, isin=isin)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MOEX lookup failed for isin=%s secid=%s: %s", isin, secid, exc)
            return MoexReference(found=False, isin=isin, secid=secid, raw={"error": str(exc)})

    async def _resolve_secid_by_isin(self, isin: str) -> str | None:
        url = f"{self.base_url}/securities.json"
        params = {"iss.meta": "off", "q": isin}
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()

        securities = payload.get("securities") or {}
        columns: list[str] = securities.get("columns") or []
        rows: list[list[Any]] = securities.get("data") or []
        if not columns or not rows:
            return None

        col = {name: idx for idx, name in enumerate(columns)}
        for row in rows:
            row_isin = row[col["isin"]] if "isin" in col else None
            primary = row[col["primary_boardid"]] if "primary_boardid" in col else None
            # Предпочитаем бумаги с основным режимом торгов
            if row_isin and str(row_isin).upper() == isin.upper():
                secid = str(row[col["secid"]])
                if primary:
                    return secid
        # fallback — первая попавшаяся с этим ISIN
        for row in rows:
            row_isin = row[col.get("isin", -1)] if "isin" in col else None
            if row_isin and str(row_isin).upper() == isin.upper():
                return str(row[col["secid"]])
        return None

    async def _fetch_by_secid(self, secid: str, *, isin: str | None = None) -> MoexReference:
        url = f"{self.base_url}/securities/{secid}.json"
        params = {"iss.meta": "off", "iss.only": "description"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()

        description = payload.get("description") or {}
        columns: list[str] = description.get("columns") or []
        rows: list[list[Any]] = description.get("data") or []
        if not columns:
            return MoexReference(found=False, secid=secid, isin=isin)

        col = {name: idx for idx, name in enumerate(columns)}
        fields: dict[str, Any] = {}
        for row in rows:
            name = row[col["name"]] if "name" in col else None
            value = row[col["value"]] if "value" in col else None
            if name:
                fields[str(name)] = value

        maturity = self._parse_moex_date(fields.get("MATDATE") or fields.get("MATURITYDATE"))
        nominal_raw = fields.get("FACEVALUE") or fields.get("INITIALFACEVALUE")
        try:
            nominal = float(str(nominal_raw).replace(",", ".")) if nominal_raw not in (None, "") else None
        except ValueError:
            nominal = None

        currency = fields.get("FACEUNIT") or fields.get("CURRENCYID")
        if isinstance(currency, str) and currency.upper() in {"SUR", "RUR"}:
            currency = "RUB"

        return MoexReference(
            found=True,
            isin=(fields.get("ISIN") or isin),
            secid=secid,
            shortname=fields.get("SHORTNAME") or fields.get("NAME"),
            nominal_value=nominal,
            currency=str(currency).upper() if currency else None,
            maturity_date=maturity,
            issuer_name=fields.get("EMITENT_TITLE") or fields.get("ISSUERNAME"),
            raw=fields,
        )

    @staticmethod
    def _parse_moex_date(value: Any) -> Any:
        if value in (None, "", "0000-00-00"):
            return None
        if isinstance(value, datetime):
            return value.date()
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None


# ---------------------------------------------------------------------------
# Local storage / manual upload
# ---------------------------------------------------------------------------


_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


class DocumentStorage:
    """Сохранение загруженных/скачанных файлов в STORAGE_DIR/documents."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or settings.uploads_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def store_bytes(
        self,
        content: bytes,
        *,
        original_filename: str,
        subdir: str | None = None,
    ) -> StoredDocument:
        suffix = Path(original_filename).suffix.lower() or ".bin"
        digest = hashlib.sha256(content).hexdigest()
        folder = self.root / (subdir or datetime.now(UTC).strftime("%Y/%m/%d"))
        folder.mkdir(parents=True, exist_ok=True)

        safe_stem = self._safe_stem(Path(original_filename).stem)[:80] or "document"
        filename = f"{safe_stem}_{digest[:12]}{suffix}"
        path = folder / filename
        if not path.exists():
            path.write_bytes(content)

        return StoredDocument(
            path=path,
            original_filename=original_filename,
            file_hash=digest,
            mime_type=_MIME_BY_SUFFIX.get(suffix, "application/octet-stream"),
            size_bytes=len(content),
        )

    async def download(self, url: str, *, original_filename: str | None = None) -> StoredDocument:
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "BondEmissionParser/0.1"})
            response.raise_for_status()
            content = response.content

        name = original_filename or self._filename_from_response(url, response)
        return self.store_bytes(content, original_filename=name)

    @staticmethod
    def _safe_stem(stem: str) -> str:
        cleaned = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
        return cleaned or uuid4().hex[:8]

    @staticmethod
    def _filename_from_response(url: str, response: httpx.Response) -> str:
        cd = response.headers.get("content-disposition") or ""
        match = re.search(r'filename="?([^";]+)"?', cd)
        if match:
            return match.group(1)
        name = Path(httpx.URL(url).path).name
        return name or f"download_{uuid4().hex[:8]}.pdf"


# ---------------------------------------------------------------------------
# Orchestrator facade used by scheduler / API
# ---------------------------------------------------------------------------


class CollectorService:
    """Фасад: RSS → список DisclosureItem; MOEX lookup; сохранение файлов."""

    def __init__(self) -> None:
        self.rss = EDisclosureRSSClient()
        self.moex = MoexIssClient()
        self.storage = DocumentStorage()

    async def poll_disclosures(self) -> list[DisclosureItem]:
        items = await self.rss.fetch()
        # Фильтруем mock-шум (дивиденды и т.п.), если вдруг проскочил
        keywords = [k.lower() for k in settings.rss_keywords]
        relevant = [
            item
            for item in items
            if any(kw in f"{item.title}\n{item.summary}".lower() for kw in keywords)
        ]
        logger.info("Collector poll: %d relevant disclosures", len(relevant))
        return relevant

    async def lookup_moex(self, *, isin: str | None = None, secid: str | None = None) -> MoexReference:
        return await self.moex.lookup(isin=isin, secid=secid)

    def store_upload(self, content: bytes, filename: str) -> StoredDocument:
        max_bytes = settings.max_upload_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise ValueError(
                f"File too large: {len(content)} bytes (limit {settings.max_upload_size_mb} MB)"
            )
        return self.storage.store_bytes(content, original_filename=filename)


def parse_rss_xml_string(xml: str, keywords: list[str] | None = None) -> list[DisclosureItem]:
    """Утилита для unit-тестов: разобрать RSS из строки без сети."""
    client = EDisclosureRSSClient(use_mock=False, keywords=keywords)
    return client._parse_feed(xml)  # noqa: SLF001 - test helper
