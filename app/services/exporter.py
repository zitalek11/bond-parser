from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import Select, and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import CouponType
from app.core.labels import APPROVAL_MODE_RU, COUPON_TYPE_RU, ru_label
from app.core.logging import get_logger
from app.models import AmortizationSchedule, BondIssue, CouponSchedule

logger = get_logger(__name__)

HEADER_FILL = PatternFill("solid", fgColor="D9EAD3")
HEADER_FONT = Font(bold=True, color="1F4E3D")
THIN = Border(
    left=Side(style="thin", color="CCCCCC"),
    right=Side(style="thin", color="CCCCCC"),
    top=Side(style="thin", color="CCCCCC"),
    bottom=Side(style="thin", color="CCCCCC"),
)


def _parse_floater(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"raw_formula_text": raw}
    except json.JSONDecodeError:
        return {"raw_formula_text": raw}


def _num(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _money_from_percent(nominal: Decimal | float | None, percent: Decimal | float | None) -> float | None:
    if nominal is None or percent is None:
        return None
    return round(float(nominal) * float(percent) / 100.0, 4)


class BondExporter:
    """Экспорт одобренных выпусков в многостраничный Excel и плоский CSV."""

    async def fetch_bonds(
        self,
        session: AsyncSession,
        *,
        date_from: date | datetime | None = None,
        date_to: date | datetime | None = None,
        only_new: bool = False,
    ) -> list[BondIssue]:
        stmt: Select[Any] = (
            select(BondIssue)
            .options(
                selectinload(BondIssue.coupons),
                selectinload(BondIssue.amortizations),
            )
            .order_by(BondIssue.approved_at.desc().nulls_last(), BondIssue.id.desc())
        )
        filters = []
        if only_new:
            filters.append(BondIssue.exported_at.is_(None))
        if date_from is not None:
            start = (
                date_from
                if isinstance(date_from, datetime)
                else datetime.combine(date_from, datetime.min.time(), tzinfo=UTC)
            )
            filters.append(BondIssue.approved_at >= start)
        if date_to is not None:
            end = (
                date_to
                if isinstance(date_to, datetime)
                else datetime.combine(date_to, datetime.max.time(), tzinfo=UTC)
            )
            filters.append(BondIssue.approved_at <= end)
        if filters:
            stmt = stmt.where(and_(*filters))
        return list(await session.scalars(stmt))

    async def mark_exported(self, session: AsyncSession, bonds: Sequence[BondIssue]) -> None:
        now = datetime.now(UTC)
        for bond in bonds:
            bond.exported_at = now
        await session.flush()

    def to_xlsx(self, bonds: Sequence[BondIssue]) -> bytes:
        wb = Workbook()
        ws_issues = wb.active
        ws_issues.title = "Выпуски"
        ws_coupons = wb.create_sheet("Купоны")
        ws_amort = wb.create_sheet("Амортизация")
        ws_floaters = wb.create_sheet("Плавающие")

        self._write_issues(ws_issues, bonds)
        self._write_coupons(ws_coupons, bonds)
        self._write_amortization(ws_amort, bonds)
        self._write_floaters(ws_floaters, bonds)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def to_csv(self, bonds: Sequence[BondIssue]) -> bytes:
        """Плоский реестр выпусков (utf-8-sig + `;`) для Excel в РФ."""
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(
            [
                "ISIN",
                "Рег. номер",
                "Эмитент",
                "ИНН",
                "Номинал",
                "Валюта",
                "Объём выпуска",
                "Дата размещения",
                "Дата погашения",
                "Тип купона",
                "Индикатор флоатера",
                "Спред %",
                "Дата утверждения",
                "Режим утверждения",
            ]
        )
        for b in bonds:
            floater = _parse_floater(b.floater_formula)
            writer.writerow(
                [
                    b.isin or "",
                    b.reg_number,
                    b.issuer_name,
                    b.issuer_inn,
                    _num(b.nominal_value),
                    b.currency,
                    b.issue_volume or "",
                    b.placement_start_date.strftime("%d.%m.%Y") if b.placement_start_date else "",
                    b.maturity_date.strftime("%d.%m.%Y") if b.maturity_date else "",
                    ru_label(COUPON_TYPE_RU, b.coupon_type),
                    floater.get("benchmark") or "",
                    floater.get("spread_percent") if floater.get("spread_percent") is not None else "",
                    b.approved_at.strftime("%d.%m.%Y %H:%M") if b.approved_at else "",
                    ru_label(APPROVAL_MODE_RU, b.approval_mode),
                ]
            )
        return buf.getvalue().encode("utf-8-sig")

    # ------------------------------------------------------------------ sheets

    def _style_header(self, ws: Worksheet, headers: list[str]) -> None:
        for col, title in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=title)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = THIN
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"
        ws.row_dimensions[1].height = 28

    def _autosize(self, ws: Worksheet, min_width: int = 10, max_width: int = 48) -> None:
        for col_cells in ws.columns:
            letter = get_column_letter(col_cells[0].column)
            length = 0
            for cell in col_cells:
                if cell.value is None:
                    continue
                length = max(length, len(str(cell.value)))
            ws.column_dimensions[letter].width = max(min_width, min(max_width, length + 2))

    def _write_issues(self, ws: Worksheet, bonds: Sequence[BondIssue]) -> None:
        headers = [
            "ISIN",
            "Рег. номер",
            "Эмитент",
            "ИНН",
            "Номинал",
            "Валюта",
            "Объём выпуска",
            "Дата начала размещения",
            "Дата погашения",
            "Тип купона",
            "Дата утверждения",
        ]
        self._style_header(ws, headers)
        for row_idx, b in enumerate(bonds, start=2):
            values = [
                b.isin,
                b.reg_number,
                b.issuer_name,
                b.issuer_inn,
                _num(b.nominal_value),
                b.currency,
                b.issue_volume,
                b.placement_start_date,
                b.maturity_date,
                ru_label(COUPON_TYPE_RU, b.coupon_type),
                b.approved_at.replace(tzinfo=None) if b.approved_at else None,
            ]
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=row_idx, column=col, value=value)
                cell.border = THIN
                if col in {8, 9} and isinstance(value, date):
                    cell.number_format = "DD.MM.YYYY"
                if col == 11 and isinstance(value, datetime):
                    cell.number_format = "DD.MM.YYYY HH:MM"
                if col == 5 and value is not None:
                    cell.number_format = "#,##0.00"
        self._autosize(ws)

    def _write_coupons(self, ws: Worksheet, bonds: Sequence[BondIssue]) -> None:
        headers = [
            "Рег. номер",
            "ISIN",
            "Номер купона",
            "Дата начала",
            "Дата окончания",
            "Ставка (% годовых)",
            "Выплата на 1 бумагу (руб.)",
        ]
        self._style_header(ws, headers)
        row = 2
        for b in bonds:
            for c in sorted(b.coupons, key=lambda x: x.coupon_number):
                values = [
                    b.reg_number,
                    b.isin,
                    c.coupon_number,
                    c.start_date,
                    c.end_date,
                    _num(c.rate_percent),
                    _num(c.payment_amount),
                ]
                for col, value in enumerate(values, start=1):
                    cell = ws.cell(row=row, column=col, value=value)
                    cell.border = THIN
                    if col in {4, 5} and isinstance(value, date):
                        cell.number_format = "DD.MM.YYYY"
                    if col in {6, 7} and value is not None:
                        cell.number_format = "0.00"
                row += 1
        self._autosize(ws)

    def _write_amortization(self, ws: Worksheet, bonds: Sequence[BondIssue]) -> None:
        headers = [
            "Рег. номер",
            "ISIN",
            "Номер купона",
            "Дата погашения",
            "Процент погашения (%)",
            "Сумма погашения на 1 бумагу (руб.)",
            "Остаток номинала (%)",
        ]
        self._style_header(ws, headers)
        row = 2
        for b in bonds:
            running = 100.0
            for a in sorted(b.amortizations, key=lambda x: (x.payment_date, x.coupon_number or 0)):
                pct = _num(a.percent)
                residual = _num(a.residual_nominal_percent)
                if residual is None and pct is not None:
                    running = round(running - pct, 4)
                    residual = running
                values = [
                    b.reg_number,
                    b.isin,
                    a.coupon_number,
                    a.payment_date,
                    pct,
                    _money_from_percent(b.nominal_value, a.percent),
                    residual,
                ]
                for col, value in enumerate(values, start=1):
                    cell = ws.cell(row=row, column=col, value=value)
                    cell.border = THIN
                    if col == 4 and isinstance(value, date):
                        cell.number_format = "DD.MM.YYYY"
                    if col in {5, 6, 7} and value is not None:
                        cell.number_format = "0.00"
                row += 1
        self._autosize(ws)

    def _write_floaters(self, ws: Worksheet, bonds: Sequence[BondIssue]) -> None:
        headers = [
            "Рег. номер",
            "ISIN",
            "Базовый индикатор",
            "Спред (%)",
            "Лаг наблюдения (дней)",
            "Тип усреднения",
            "Потолок ставки, cap (%)",
            "Пол ставки, floor (%)",
            "Исходная цитата формулы",
        ]
        self._style_header(ws, headers)
        row = 2
        for b in bonds:
            if str(b.coupon_type) != CouponType.FLOATER.value and not b.floater_formula:
                continue
            floater = _parse_floater(b.floater_formula)
            if not floater and str(b.coupon_type) != CouponType.FLOATER.value:
                continue
            values = [
                b.reg_number,
                b.isin,
                floater.get("benchmark"),
                floater.get("spread_percent"),
                floater.get("observation_shift_days"),
                floater.get("averaging_type"),
                floater.get("cap_rate"),
                floater.get("floor_rate"),
                floater.get("raw_formula_text"),
            ]
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=row, column=col, value=value)
                cell.border = THIN
                if col in {4, 7, 8} and isinstance(value, (int, float)):
                    cell.number_format = "0.00"
            row += 1
        self._autosize(ws, max_width=60)
