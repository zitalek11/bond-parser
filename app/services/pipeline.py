from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import ApprovalMode, CouponType, TaskSource, TaskStatus
from app.core.logging import get_logger
from app.models import AmortizationSchedule, BondIssue, CouponSchedule, ParsingTask
from app.schemas.bond_document import BondDocumentSchema, MoexReference
from app.services.collector import CollectorService, DisclosureItem
from app.services.extractor import DocumentExtractor
from app.services.llm_parser import LLMBondParser, MockLLMBondParser
from app.services.validator import BondDataValidator

logger = get_logger(__name__)


def _build_llm_parser() -> LLMBondParser | MockLLMBondParser:
    if settings.llm_use_mock:
        logger.warning("LLM_USE_MOCK=true — using MockLLMBondParser")
        return MockLLMBondParser()
    key = (settings.llm_api_key or "").strip().lower()
    # Заглушки только для явного офлайн-режима; "not-needed" — валидный ключ для Ollama/vLLM
    if key in {"", "sk-change-me", "mock", "changeme"}:
        logger.warning("LLM_API_KEY placeholder — using MockLLMBondParser")
        return MockLLMBondParser()
    return LLMBondParser()


class BondPipeline:
    """Оркестратор: файл → extract → LLM → MOEX → DQ Gate → DB / HITL."""

    def __init__(
        self,
        *,
        collector: CollectorService | None = None,
        extractor: DocumentExtractor | None = None,
        llm: LLMBondParser | MockLLMBondParser | None = None,
        validator: BondDataValidator | None = None,
    ) -> None:
        self.collector = collector or CollectorService()
        self.extractor = extractor or DocumentExtractor()
        self.llm = llm or _build_llm_parser()
        self.validator = validator or BondDataValidator()

    # ------------------------------------------------------------------ entry

    async def ingest_upload(
        self,
        session: AsyncSession,
        *,
        content: bytes,
        filename: str,
        run_pipeline: bool = True,
    ) -> ParsingTask:
        stored = self.collector.store_upload(content, filename)

        existing = await session.scalar(
            select(ParsingTask).where(ParsingTask.file_hash == stored.file_hash)
        )
        if existing:
            logger.info("Duplicate upload hash=%s → task #%s", stored.file_hash[:12], existing.id)
            return existing

        task = ParsingTask(
            source=TaskSource.MANUAL_UPLOAD,
            title=filename,
            original_filename=stored.original_filename,
            file_path=str(stored.path),
            file_hash=stored.file_hash,
            mime_type=stored.mime_type,
            status=TaskStatus.DOWNLOADED,
        )
        session.add(task)
        await session.flush()

        if run_pipeline:
            await self.process_task(session, task)
        return task

    async def poll_and_enqueue(self, session: AsyncSession) -> list[ParsingTask]:
        items = await self.collector.poll_disclosures()
        created: list[ParsingTask] = []
        for item in items:
            task = await self._enqueue_disclosure(session, item)
            if task:
                created.append(task)
        await session.flush()
        return created

    async def run_collection_cycle(self, session: AsyncSession) -> dict[str, int]:
        """Полный цикл: poll RSS → enqueue → process PENDING/DOWNLOADED."""
        created = await self.poll_and_enqueue(session)
        processed = 0
        failed = 0

        pending = (
            await session.scalars(
                select(ParsingTask).where(
                    ParsingTask.status.in_(
                        [TaskStatus.PENDING, TaskStatus.DOWNLOADED, TaskStatus.EXTRACTED, TaskStatus.PARSED]
                    )
                )
            )
        ).all()

        for task in pending:
            try:
                await self.process_task(session, task)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.exception("Pipeline failed for task #%s: %s", task.id, exc)
                task.status = TaskStatus.FAILED
                task.error_message = str(exc)[:4000]
                task.attempts = (task.attempts or 0) + 1

        return {"enqueued": len(created), "processed": processed, "failed": failed}

    # ------------------------------------------------------------------ core

    async def process_task(self, session: AsyncSession, task: ParsingTask) -> ParsingTask:
        task.attempts = (task.attempts or 0) + 1
        task.error_message = None

        if not task.file_path or not Path(task.file_path).exists():
            # RSS-событие без файла: сохраняем как PENDING с метаданными
            if task.status == TaskStatus.PENDING:
                logger.info("Task #%s has no file yet — left PENDING", task.id)
                return task
            raise FileNotFoundError(f"Task #{task.id}: file missing at {task.file_path}")

        # 1) Extract
        extraction = self.extractor.extract(task.file_path)
        task.extracted_text = extraction.markdown
        task.extractor_backend = extraction.backend
        task.status = TaskStatus.EXTRACTED
        await session.flush()

        # 2) LLM
        document = await self.llm.parse(extraction.markdown)
        task.llm_output = document.model_dump(mode="json")
        task.llm_model = getattr(self.llm, "model", None) or "mock"
        task.status = TaskStatus.PARSED
        await session.flush()

        # 3) MOEX cross-check
        moex = await self.collector.lookup_moex(isin=document.isin)
        task.moex_reference = moex.model_dump(mode="json")

        # 4) Data Quality Gate (в т.ч. резолв дат амортизации по номеру купона)
        result = self.validator.validate(
            document,
            source_text=extraction.markdown,
            moex=moex,
        )
        if result.resolved_document is not None:
            document = result.resolved_document
            task.llm_output = document.model_dump(mode="json")
        task.validation_errors = result.to_storage()

        if result.status == TaskStatus.APPROVED.value:
            await self._promote_to_production(
                session, task, document, approval_mode=ApprovalMode.AUTO, approved_by="system"
            )
            task.status = TaskStatus.APPROVED
        else:
            task.status = TaskStatus.NEEDS_REVIEW

        await session.flush()
        logger.info("Task #%s → %s (%d issues)", task.id, task.status, len(result.issues))
        return task

    async def approve_task(
        self,
        session: AsyncSession,
        task: ParsingTask,
        *,
        reviewed_data: dict[str, Any],
        reviewer: str = "operator",
        comment: str | None = None,
    ) -> BondIssue:
        document = BondDocumentSchema.model_validate(reviewed_data)
        task.reviewed_data = document.model_dump(mode="json")
        task.reviewed_by = reviewer
        task.reviewed_at = datetime.now(UTC)
        task.review_comment = comment
        task.validation_errors = []

        bond = await self._promote_to_production(
            session,
            task,
            document,
            approval_mode=ApprovalMode.MANUAL,
            approved_by=reviewer,
        )
        task.status = TaskStatus.APPROVED
        await session.flush()
        return bond

    async def reject_task(
        self,
        session: AsyncSession,
        task: ParsingTask,
        *,
        reviewer: str = "operator",
        comment: str | None = None,
    ) -> ParsingTask:
        task.status = TaskStatus.REJECTED
        task.reviewed_by = reviewer
        task.reviewed_at = datetime.now(UTC)
        task.review_comment = comment
        await session.flush()
        return task

    # ------------------------------------------------------------------ helpers

    async def _enqueue_disclosure(
        self, session: AsyncSession, item: DisclosureItem
    ) -> ParsingTask | None:
        existing = await session.scalar(
            select(ParsingTask).where(ParsingTask.source_guid == item.guid)
        )
        if existing:
            return None

        source = TaskSource.MOCK if item.source == "MOCK" else TaskSource.RSS
        task = ParsingTask(
            source=source,
            source_guid=item.guid,
            source_url=item.link or item.enclosure_url,
            title=item.title,
            published_at=item.published_at,
            status=TaskStatus.PENDING,
        )

        if item.enclosure_url:
            try:
                stored = await self.collector.storage.download(
                    item.enclosure_url, original_filename=f"{item.guid}.pdf"
                )
                dup = await session.scalar(
                    select(ParsingTask).where(ParsingTask.file_hash == stored.file_hash)
                )
                if dup:
                    logger.info("Enclosure hash already known → skip guid=%s", item.guid)
                    return None
                task.file_path = str(stored.path)
                task.file_hash = stored.file_hash
                task.original_filename = stored.original_filename
                task.mime_type = stored.mime_type
                task.status = TaskStatus.DOWNLOADED
            except Exception as exc:  # noqa: BLE001
                logger.warning("Download failed for %s: %s", item.enclosure_url, exc)
                task.error_message = f"download_failed: {exc}"[:2000]

        session.add(task)
        return task

    async def _promote_to_production(
        self,
        session: AsyncSession,
        task: ParsingTask,
        document: BondDocumentSchema,
        *,
        approval_mode: ApprovalMode,
        approved_by: str,
    ) -> BondIssue:
        by_reg = await session.scalar(
            select(BondIssue).where(BondIssue.reg_number == document.reg_number)
        )
        by_task = await session.scalar(
            select(BondIssue).where(BondIssue.task_id == task.id)
        )

        # Если по task уже есть запись с другим reg_number — удаляем её,
        # чтобы не словить UNIQUE(task_id) при привязке «правильного» выпуска.
        if by_task is not None and (by_reg is None or by_task.id != by_reg.id):
            await session.delete(by_task)
            await session.flush()
            by_task = None

        if by_reg is not None:
            bond = by_reg
            bond.data_version = (bond.data_version or 1) + 1
            bond.coupons.clear()
            bond.amortizations.clear()
        elif by_task is not None:
            bond = by_task
            bond.data_version = (bond.data_version or 1) + 1
            bond.coupons.clear()
            bond.amortizations.clear()
            bond.reg_number = document.reg_number
        else:
            bond = BondIssue(reg_number=document.reg_number)
            session.add(bond)

        bond.task_id = task.id
        bond.isin = document.isin
        bond.issuer_name = document.issuer_name
        bond.issuer_inn = document.issuer_inn
        bond.nominal_value = Decimal(str(document.nominal_value))
        bond.currency = document.currency
        bond.issue_volume = document.issue_volume
        bond.placement_start_date = document.placement_start_date
        bond.maturity_date = document.maturity_date
        bond.coupon_type = CouponType(str(document.coupon_type))
        if document.floater_formula is None:
            bond.floater_formula = None
        else:
            bond.floater_formula = document.floater_formula.model_dump_json()
        bond.approval_mode = approval_mode
        bond.approved_by = approved_by
        bond.approved_at = datetime.now(UTC)

        for c in document.coupons:
            bond.coupons.append(
                CouponSchedule(
                    coupon_number=c.coupon_number,
                    start_date=c.start_date,
                    end_date=c.end_date,
                    rate_percent=Decimal(str(c.rate_percent)) if c.rate_percent is not None else None,
                    payment_amount=(
                        Decimal(str(c.payment_amount)) if c.payment_amount is not None else None
                    ),
                )
            )
        for a in document.amortization:
            if a.payment_date is None:
                raise ValueError(
                    "Cannot persist amortization step without payment_date "
                    f"(coupon_number={a.coupon_number})"
                )
            bond.amortizations.append(
                AmortizationSchedule(
                    payment_date=a.payment_date,
                    coupon_number=a.coupon_number,
                    percent=Decimal(str(a.amortization_percent)),
                    residual_nominal_percent=(
                        Decimal(str(a.residual_nominal_percent))
                        if a.residual_nominal_percent is not None
                        else None
                    ),
                )
            )

        await session.flush()
        return bond


async def dashboard_stats(session: AsyncSession) -> dict[str, Any]:
    total = await session.scalar(select(func.count()).select_from(ParsingTask)) or 0
    approved = await session.scalar(
        select(func.count()).select_from(ParsingTask).where(ParsingTask.status == TaskStatus.APPROVED)
    ) or 0
    needs_review = await session.scalar(
        select(func.count())
        .select_from(ParsingTask)
        .where(ParsingTask.status == TaskStatus.NEEDS_REVIEW)
    ) or 0
    rejected = await session.scalar(
        select(func.count()).select_from(ParsingTask).where(ParsingTask.status == TaskStatus.REJECTED)
    ) or 0
    failed = await session.scalar(
        select(func.count()).select_from(ParsingTask).where(ParsingTask.status == TaskStatus.FAILED)
    ) or 0
    bonds = await session.scalar(select(func.count()).select_from(BondIssue)) or 0
    auto_rate = round((approved / total) * 100, 1) if total else 0.0
    return {
        "total_tasks": total,
        "approved": approved,
        "needs_review": needs_review,
        "rejected": rejected,
        "failed": failed,
        "bonds": bonds,
        "auto_approved_pct": auto_rate,
    }
