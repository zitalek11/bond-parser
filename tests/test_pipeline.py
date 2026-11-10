from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.enums import TaskStatus
from app.models import Base, BondIssue, ParsingTask
from app.schemas.bond_document import MoexReference
from app.services.collector import CollectorService
from app.services.llm_parser import MockLLMBondParser
from app.services.pipeline import BondPipeline, dashboard_stats
from app.services.validator import BondDataValidator


@pytest_asyncio.fixture
async def session(tmp_path: Path):
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BondPipeline:
    from app.core.config import settings
    from app.services.collector import DocumentStorage

    storage_root = tmp_path / "storage"
    docs = storage_root / "documents"
    docs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "storage_dir", storage_root)

    collector = CollectorService()
    collector.storage = DocumentStorage(root=docs)

    async def _no_moex(*, isin=None, secid=None):
        return MoexReference(found=False, isin=isin, secid=secid)

    collector.lookup_moex = _no_moex  # type: ignore[method-assign]
    return BondPipeline(
        collector=collector,
        llm=MockLLMBondParser(),
        validator=BondDataValidator(),
    )


@pytest.mark.asyncio
async def test_ingest_demo_markdown_auto_approved(
    session: AsyncSession, pipeline: BondPipeline
) -> None:
    demo = Path(__file__).resolve().parents[1] / "samples" / "demo_emission.md"
    content = demo.read_bytes()

    task = await pipeline.ingest_upload(
        session, content=content, filename="demo_emission.md", run_pipeline=True
    )
    await session.commit()

    assert task.status == TaskStatus.APPROVED
    assert task.llm_output is not None
    assert task.llm_output["reg_number"] == "4B02-01-36400-R-001P"
    assert task.llm_output["issuer_inn"] == "7707083893"
    assert len(task.llm_output.get("coupons") or []) == 6

    bond = await session.get(BondIssue, 1)
    # may not be id=1 if empty — fetch by reg
    from sqlalchemy import select

    bond = await session.scalar(
        select(BondIssue).where(BondIssue.reg_number == "4B02-01-36400-R-001P")
    )
    assert bond is not None
    assert bond.issuer_inn == "7707083893"
    assert float(bond.nominal_value) == 1000.0
    assert len(bond.coupons) == 6

    stats = await dashboard_stats(session)
    assert stats["approved"] >= 1
    assert stats["bonds"] >= 1


@pytest.mark.asyncio
async def test_duplicate_upload_returns_same_task(
    session: AsyncSession, pipeline: BondPipeline
) -> None:
    content = (
        "# test\nИНН 7707083893\nномер 4B02-01-36400-R-001P\n"
        "Номинальная стоимость 1000"
    ).encode("utf-8")
    t1 = await pipeline.ingest_upload(session, content=content, filename="a.md")
    t2 = await pipeline.ingest_upload(session, content=content, filename="b.md")
    assert t1.id == t2.id


@pytest.mark.asyncio
async def test_hitl_reject(session: AsyncSession, pipeline: BondPipeline) -> None:
    content = (
        "FLOATER demo\nИНН 7707083893\n4B02-01-36400-R-001P\n"
        "Номинальная стоимость 1000"
    ).encode("utf-8")
    # Force NEEDS_REVIEW via floater payload
    pipeline.llm = MockLLMBondParser(
        {
            "reg_number": "4B02-01-36400-R-001P",
            "issuer_name": "Test",
            "issuer_inn": "7707083893",
            "nominal_value": 1000,
            "coupon_type": "FLOATER",
            "floater_formula": {
                "benchmark": "OTHER",
                "spread_percent": None,
                "averaging_type": "UNKNOWN",
                "raw_formula_text": "неясная формула",
            },
            "quotes": {
                "reg_number": "4B02-01-36400-R-001P",
                "issuer_inn": "7707083893",
                "nominal_value": "1000",
            },
        }
    )
    task = await pipeline.ingest_upload(session, content=content, filename="floater.md")
    assert task.status == TaskStatus.NEEDS_REVIEW

    await pipeline.reject_task(session, task, reviewer="vitaly", comment="bad doc")
    assert task.status == TaskStatus.REJECTED
    assert task.reviewed_by == "vitaly"
