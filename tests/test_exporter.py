from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.enums import ApprovalMode, CouponType
from app.models import AmortizationSchedule, Base, BondIssue, CouponSchedule
from app.services.exporter import BondExporter


@pytest_asyncio.fixture
async def session(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'export.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
def sample_bonds() -> list[BondIssue]:
    fixed = BondIssue(
        isin="RU000A108X21",
        reg_number="4B02-03-65116-D-001P",
        issuer_name="ПАО Тест Фикс",
        issuer_inn="7707083893",
        nominal_value=Decimal("1000"),
        currency="RUB",
        issue_volume=1_000_000,
        placement_start_date=date(2025, 1, 15),
        maturity_date=date(2028, 1, 15),
        coupon_type=CouponType.FIXED,
        approval_mode=ApprovalMode.AUTO,
        approved_by="system",
        approved_at=datetime(2025, 2, 1, 12, 0, tzinfo=UTC),
    )
    fixed.coupons = [
        CouponSchedule(
            coupon_number=1,
            start_date=date(2025, 1, 15),
            end_date=date(2025, 7, 15),
            rate_percent=Decimal("15.5"),
            payment_amount=Decimal("77.5"),
        )
    ]
    fixed.amortizations = [
        AmortizationSchedule(
            payment_date=date(2026, 1, 15),
            coupon_number=2,
            percent=Decimal("40"),
            residual_nominal_percent=Decimal("60"),
        ),
        AmortizationSchedule(
            payment_date=date(2028, 1, 15),
            coupon_number=4,
            percent=Decimal("60"),
            residual_nominal_percent=Decimal("0"),
        ),
    ]

    floater = BondIssue(
        isin="RU000A109K55",
        reg_number="4B02-07-00739-A-001P",
        issuer_name="АО Тест Флоатер",
        issuer_inn="7729355614",
        nominal_value=Decimal("1000"),
        currency="RUB",
        coupon_type=CouponType.FLOATER,
        floater_formula=(
            '{"benchmark":"KEY_RATE_CBR","spread_percent":1.75,'
            '"observation_shift_days":7,"averaging_type":"POINT_IN_TIME",'
            '"cap_rate":null,"floor_rate":null,'
            '"raw_formula_text":"КС ЦБ + 1.75%"}'
        ),
        approval_mode=ApprovalMode.MANUAL,
        approved_by="operator",
        approved_at=datetime(2025, 3, 1, 10, 0, tzinfo=UTC),
        exported_at=None,
    )
    return [fixed, floater]


def test_xlsx_has_four_sheets_and_formatting(sample_bonds: list[BondIssue]) -> None:
    raw = BondExporter().to_xlsx(sample_bonds)
    wb = load_workbook(filename=__import__("io").BytesIO(raw))
    assert wb.sheetnames == ["Выпуски", "Купоны", "Амортизация", "Плавающие"]

    issues = wb["Выпуски"]
    assert issues["A1"].value == "ISIN"
    assert issues.freeze_panes == "A2"
    assert issues.auto_filter.ref.startswith("A1:")
    assert issues["B2"].value == "4B02-03-65116-D-001P"
    assert issues["E2"].number_format == "#,##0.00"
    assert issues["J2"].value == "Фиксированный"

    coupons = wb["Купоны"]
    assert coupons["C2"].value == 1
    assert coupons["F2"].value == 15.5

    amort = wb["Амортизация"]
    assert amort["C2"].value == 2
    assert amort["E2"].value == 40.0
    assert amort["F2"].value == 400.0  # 40% of 1000
    assert amort["G2"].value == 60.0

    floaters = wb["Плавающие"]
    assert floaters["C2"].value == "KEY_RATE_CBR"
    assert floaters["D2"].value == 1.75
    assert floaters["E2"].value == 7


def test_csv_utf8_bom_and_semicolon(sample_bonds: list[BondIssue]) -> None:
    raw = BondExporter().to_csv(sample_bonds)
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert ";" in text.splitlines()[0]
    assert "4B02-03-65116-D-001P" in text
    assert "KEY_RATE_CBR" in text
    assert "Фиксированный" in text
    assert "Плавающий" in text


@pytest.mark.asyncio
async def test_only_new_filter(session: AsyncSession, sample_bonds: list[BondIssue]) -> None:
    sample_bonds[0].exported_at = datetime.now(UTC)
    session.add_all(sample_bonds)
    await session.commit()

    exporter = BondExporter()
    all_bonds = await exporter.fetch_bonds(session, only_new=False)
    new_bonds = await exporter.fetch_bonds(session, only_new=True)
    assert len(all_bonds) == 2
    assert len(new_bonds) == 1
    assert new_bonds[0].reg_number == "4B02-07-00739-A-001P"

    await exporter.mark_exported(session, new_bonds)
    await session.commit()
    assert (await exporter.fetch_bonds(session, only_new=True)) == []
