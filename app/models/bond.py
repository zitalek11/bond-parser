from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ApprovalMode, CouponType
from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.parsing_task import ParsingTask


class BondIssue(TimestampMixin, Base):
    """Production ("golden") record of a bond issue."""

    __tablename__ = "bond_issues"
    __table_args__ = (
        CheckConstraint("nominal_value > 0", name="nominal_positive"),
        CheckConstraint(
            "maturity_date IS NULL OR placement_start_date IS NULL "
            "OR maturity_date > placement_start_date",
            name="maturity_after_placement",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("parsing_tasks.id", ondelete="SET NULL"), unique=True
    )

    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    reg_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    issuer_name: Mapped[str] = mapped_column(String(512), nullable=False)
    issuer_inn: Mapped[str] = mapped_column(String(12), index=True, nullable=False)

    nominal_value: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)
    issue_volume: Mapped[int | None] = mapped_column(BigInteger)

    placement_start_date: Mapped[date | None] = mapped_column(Date)
    maturity_date: Mapped[date | None] = mapped_column(Date)

    coupon_type: Mapped[CouponType] = mapped_column(
        Enum(CouponType, native_enum=False, length=16), nullable=False
    )
    floater_formula: Mapped[str | None] = mapped_column(Text)

    approval_mode: Mapped[ApprovalMode] = mapped_column(
        Enum(ApprovalMode, native_enum=False, length=16), nullable=False
    )
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    data_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    task: Mapped[ParsingTask | None] = relationship(back_populates="bond_issue", lazy="selectin")
    coupons: Mapped[list[CouponSchedule]] = relationship(
        back_populates="bond",
        cascade="all, delete-orphan",
        order_by="CouponSchedule.coupon_number",
        lazy="selectin",
    )
    amortizations: Mapped[list[AmortizationSchedule]] = relationship(
        back_populates="bond",
        cascade="all, delete-orphan",
        order_by="AmortizationSchedule.payment_date",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<BondIssue id={self.id} reg={self.reg_number} isin={self.isin}>"


class CouponSchedule(Base):
    __tablename__ = "coupon_schedules"
    __table_args__ = (
        UniqueConstraint("bond_id", "coupon_number", name="uq_coupon_bond_number"),
        CheckConstraint("end_date > start_date", name="coupon_period_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bond_id: Mapped[int] = mapped_column(
        ForeignKey("bond_issues.id", ondelete="CASCADE"), index=True, nullable=False
    )
    coupon_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    payment_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))

    bond: Mapped[BondIssue] = relationship(back_populates="coupons", lazy="raise_on_sql")


class AmortizationSchedule(Base):
    __tablename__ = "amortization_schedules"
    __table_args__ = (
        UniqueConstraint("bond_id", "payment_date", name="uq_amort_bond_date"),
        CheckConstraint("percent > 0 AND percent <= 100", name="amort_percent_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bond_id: Mapped[int] = mapped_column(
        ForeignKey("bond_issues.id", ondelete="CASCADE"), index=True, nullable=False
    )
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    coupon_number: Mapped[int | None] = mapped_column(Integer)
    percent: Mapped[Decimal] = mapped_column(Numeric(9, 4), nullable=False)
    residual_nominal_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))

    bond: Mapped[BondIssue] = relationship(back_populates="amortizations", lazy="raise_on_sql")
