from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.core.config import settings
from app.core.enums import CouponType, TaskStatus
from app.core.logging import get_logger
from app.schemas.bond import (
    AmortizationItem,
    BondDocumentSchema,
    FloaterFormulaSchema,
    MoexReference,
    ValidationIssue,
    ValidationResult,
)

logger = get_logger(__name__)

_QUOTE_REQUIRED_FIELDS: tuple[str, ...] = (
    "reg_number",
    "nominal_value",
    "issuer_inn",
)

_COUPON_GAP_TOLERANCE_DAYS = 3


def validate_inn_checksum(inn: str) -> bool:
    """Проверка контрольной суммы ИНН (10 или 12 цифр). Алгоритм ФНС РФ."""
    if not inn or not inn.isdigit() or len(inn) not in (10, 12):
        return False

    digits = [int(c) for c in inn]

    def _checksum(weights: list[int], payload: list[int]) -> int:
        total = sum(w * d for w, d in zip(weights, payload, strict=True))
        return (total % 11) % 10

    if len(digits) == 10:
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return _checksum(weights, digits[:9]) == digits[9]

    weights_11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    weights_12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    return (
        _checksum(weights_11, digits[:10]) == digits[10]
        and _checksum(weights_12, digits[:11]) == digits[11]
    )


def _normalize_for_quote_match(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, float):
        as_int = int(value) if value == int(value) else None
        variants = {f"{value:g}", f"{value:.2f}", f"{value:.4f}".rstrip("0").rstrip(".")}
        if as_int is not None:
            variants.add(str(as_int))
            variants.add(f"{as_int:,}".replace(",", " "))
            variants.add(f"{as_int:,}".replace(",", "\u00a0"))
        return [v for v in variants if v]
    if isinstance(value, date):
        return [
            value.isoformat(),
            value.strftime("%d.%m.%Y"),
            value.strftime("%d/%m/%Y"),
            value.strftime("%d-%m-%Y"),
        ]
    text = str(value).strip()
    return [text] if text else []


def _quote_contains_value(quote: str, value: Any) -> bool:
    if not quote:
        return False
    haystack = re.sub(r"\s+", " ", quote)
    for candidate in _normalize_for_quote_match(value):
        needle = re.sub(r"\s+", " ", candidate)
        if needle and needle.casefold() in haystack.casefold():
            return True
    return False


class BondDataValidator:
    """Data Quality Gate: бизнес-правила + кросс-валидация с MOEX + флоатеры/амортизация."""

    def __init__(
        self,
        *,
        amortization_tolerance: float | None = None,
        nominal_tolerance: float | None = None,
        coupon_gap_tolerance_days: int = _COUPON_GAP_TOLERANCE_DAYS,
        require_quotes: bool = True,
    ) -> None:
        self.amortization_tolerance = (
            amortization_tolerance
            if amortization_tolerance is not None
            else settings.amortization_tolerance
        )
        self.nominal_tolerance = (
            nominal_tolerance if nominal_tolerance is not None else settings.nominal_tolerance
        )
        self.coupon_gap_tolerance_days = coupon_gap_tolerance_days
        self.require_quotes = require_quotes

    def validate(
        self,
        document: BondDocumentSchema | dict[str, Any],
        *,
        source_text: str | None = None,
        moex: MoexReference | dict[str, Any] | None = None,
    ) -> ValidationResult:
        data = (
            document
            if isinstance(document, BondDocumentSchema)
            else BondDocumentSchema.model_validate(document)
        )
        moex_ref = self._coerce_moex(moex)
        issues: list[ValidationIssue] = []

        data, resolve_issues = self.resolve_amortization_dates(data)
        issues.extend(resolve_issues)

        issues.extend(self._check_inn(data))
        issues.extend(self._check_dates(data))
        issues.extend(self._check_coupon_sequence(data))
        issues.extend(self._check_amortization(data))
        issues.extend(self._check_floater(data))
        issues.extend(self._check_complexity(data))
        issues.extend(self._check_moex(data, moex_ref))
        if self.require_quotes:
            issues.extend(self._check_quotes(data, source_text or ""))

        has_errors = any(i.severity == "error" for i in issues)
        status = TaskStatus.NEEDS_REVIEW.value if has_errors else TaskStatus.APPROVED.value

        result = ValidationResult(
            status=status,  # type: ignore[arg-type]
            issues=issues,
            resolved_document=data,
        )
        logger.info(
            "DQ Gate: status=%s errors=%d warnings=%d reg=%s",
            result.status,
            len(result.errors),
            len(result.warnings),
            data.reg_number,
        )
        return result

    # ------------------------------------------------------------------ resolve

    def resolve_amortization_dates(
        self, data: BondDocumentSchema
    ) -> tuple[BondDocumentSchema, list[ValidationIssue]]:
        """Подтягивает payment_date из coupons по coupon_number, если даты нет."""
        if not data.amortization:
            return data, []

        by_number = {c.coupon_number: c for c in data.coupons}
        issues: list[ValidationIssue] = []
        resolved: list[AmortizationItem] = []

        for item in data.amortization:
            payment_date = item.payment_date
            if payment_date is None and item.coupon_number is not None:
                coupon = by_number.get(item.coupon_number)
                if coupon is None:
                    issues.append(
                        ValidationIssue(
                            field="amortization",
                            code="AMORTIZATION_COUPON_MISSING",
                            message=(
                                f"Амортизация ссылается на купон #{item.coupon_number}, "
                                f"которого нет в графике coupons"
                            ),
                        )
                    )
                else:
                    payment_date = coupon.end_date
            resolved.append(
                item.model_copy(update={"payment_date": payment_date})
            )

        return data.model_copy(update={"amortization": resolved}), issues

    # ------------------------------------------------------------------ rules

    def _check_inn(self, data: BondDocumentSchema) -> list[ValidationIssue]:
        if not validate_inn_checksum(data.issuer_inn):
            return [
                ValidationIssue(
                    field="issuer_inn",
                    code="INN_CHECKSUM",
                    message=f"ИНН {data.issuer_inn} не проходит проверку контрольной суммы",
                )
            ]
        return []

    def _check_dates(self, data: BondDocumentSchema) -> list[ValidationIssue]:
        if (
            data.maturity_date is not None
            and data.placement_start_date is not None
            and data.maturity_date <= data.placement_start_date
        ):
            return [
                ValidationIssue(
                    field="maturity_date",
                    code="DATE_ORDER",
                    message=(
                        f"maturity_date ({data.maturity_date}) должна быть позже "
                        f"placement_start_date ({data.placement_start_date})"
                    ),
                )
            ]
        return []

    def _check_coupon_sequence(self, data: BondDocumentSchema) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        coupons = data.coupons
        if not coupons:
            return issues

        numbers = [c.coupon_number for c in coupons]
        if len(numbers) != len(set(numbers)):
            issues.append(
                ValidationIssue(
                    field="coupons",
                    code="COUPON_DUPLICATE",
                    message="В графике купонов есть дублирующиеся номера",
                )
            )

        expected = list(range(1, len(coupons) + 1))
        if sorted(numbers) != expected:
            issues.append(
                ValidationIssue(
                    field="coupons",
                    code="COUPON_NUMBERING",
                    message=(
                        f"Номера купонов должны идти подряд с 1; "
                        f"получено {sorted(numbers)}, ожидалось {expected}"
                    ),
                )
            )

        ordered = sorted(coupons, key=lambda c: (c.coupon_number, c.start_date))
        for prev, curr in zip(ordered, ordered[1:], strict=False):
            gap = (curr.start_date - prev.end_date).days
            if gap < -self.coupon_gap_tolerance_days:
                issues.append(
                    ValidationIssue(
                        field="coupons",
                        code="COUPON_OVERLAP",
                        message=(
                            f"Купоны #{prev.coupon_number} и #{curr.coupon_number} "
                            f"пересекаются: end={prev.end_date}, start={curr.start_date}"
                        ),
                    )
                )
            elif gap > self.coupon_gap_tolerance_days:
                issues.append(
                    ValidationIssue(
                        field="coupons",
                        code="COUPON_GAP",
                        message=(
                            f"Разрыв между купонами #{prev.coupon_number} и "
                            f"#{curr.coupon_number}: {gap} дн. "
                            f"(допуск {self.coupon_gap_tolerance_days})"
                        ),
                    )
                )

        if data.placement_start_date and ordered[0].start_date < data.placement_start_date - timedelta(
            days=self.coupon_gap_tolerance_days
        ):
            issues.append(
                ValidationIssue(
                    field="coupons",
                    code="COUPON_BEFORE_PLACEMENT",
                    message=(
                        f"Первый купон ({ordered[0].start_date}) раньше "
                        f"даты размещения ({data.placement_start_date})"
                    ),
                    severity="warning",
                )
            )
        if data.maturity_date and ordered[-1].end_date > data.maturity_date + timedelta(
            days=self.coupon_gap_tolerance_days
        ):
            issues.append(
                ValidationIssue(
                    field="coupons",
                    code="COUPON_AFTER_MATURITY",
                    message=(
                        f"Последний купон ({ordered[-1].end_date}) позже "
                        f"даты погашения ({data.maturity_date})"
                    ),
                    severity="warning",
                )
            )
        return issues

    def _check_amortization(self, data: BondDocumentSchema) -> list[ValidationIssue]:
        if not data.amortization:
            return []

        issues: list[ValidationIssue] = []
        total = sum(item.amortization_percent for item in data.amortization)
        if abs(total - 100.0) > self.amortization_tolerance:
            issues.append(
                ValidationIssue(
                    field="amortization",
                    code="AMORTIZATION_SUM",
                    message=(
                        f"Сумма amortization_percent = {total:.4f}, "
                        f"ожидалось 100.0 ± {self.amortization_tolerance}"
                    ),
                )
            )

        # Сверка остатка номинала, если столбец residual заполнен хотя бы у одного шага
        residuals = [item.residual_nominal_percent for item in data.amortization]
        if any(r is not None for r in residuals):
            issues.extend(self._check_residual_chain(data.amortization))

        # После резолва дат — все шаги должны иметь payment_date или явную ошибку купона
        for item in data.amortization:
            if item.payment_date is None and item.coupon_number is not None:
                # уже отмечено в resolve; дублировать не нужно
                continue
            if item.payment_date is None:
                issues.append(
                    ValidationIssue(
                        field="amortization",
                        code="AMORTIZATION_DATE_MISSING",
                        message="Шаг амортизации без payment_date и без coupon_number",
                    )
                )
        return issues

    def _check_residual_chain(
        self, items: list[AmortizationItem]
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        # Сортируем по дате / номеру купона
        ordered = sorted(
            items,
            key=lambda a: (
                a.payment_date.toordinal() if a.payment_date else 10**9,
                a.coupon_number or 10**9,
            ),
        )
        prev_residual = 100.0
        for idx, item in enumerate(ordered):
            if item.residual_nominal_percent is None:
                issues.append(
                    ValidationIssue(
                        field="amortization",
                        code="AMORTIZATION_RESIDUAL_GAP",
                        message=(
                            f"Шаг #{idx + 1}: residual_nominal_percent не заполнен, "
                            f"хотя в таблице есть остатки номинала"
                        ),
                    )
                )
                continue
            expected = prev_residual - item.amortization_percent
            if abs(expected - item.residual_nominal_percent) > self.amortization_tolerance:
                issues.append(
                    ValidationIssue(
                        field="amortization",
                        code="AMORTIZATION_RESIDUAL_MISMATCH",
                        message=(
                            f"Шаг #{idx + 1}: ожидался остаток {expected:.4f} "
                            f"(= {prev_residual:.4f} − {item.amortization_percent:.4f}), "
                            f"в таблице {item.residual_nominal_percent:.4f}"
                        ),
                    )
                )
            prev_residual = item.residual_nominal_percent

        if ordered and ordered[-1].residual_nominal_percent is not None:
            if abs(ordered[-1].residual_nominal_percent - 0.0) > self.amortization_tolerance:
                issues.append(
                    ValidationIssue(
                        field="amortization",
                        code="AMORTIZATION_RESIDUAL_NOT_ZERO",
                        message=(
                            f"После последнего шага остаток номинала = "
                            f"{ordered[-1].residual_nominal_percent}, ожидалось 0.0"
                        ),
                    )
                )
        return issues

    def _check_floater(self, data: BondDocumentSchema) -> list[ValidationIssue]:
        coupon_type = CouponType(str(data.coupon_type))
        if coupon_type != CouponType.FLOATER:
            return []

        issues: list[ValidationIssue] = []
        formula = data.floater_formula
        if formula is None:
            issues.append(
                ValidationIssue(
                    field="floater_formula",
                    code="FLOATER_PARSE_INCOMPLETE",
                    message="coupon_type=FLOATER, но floater_formula не извлечена",
                )
            )
            return issues
        if isinstance(formula, dict):
            formula = FloaterFormulaSchema.model_validate(formula)

        if formula.benchmark == "OTHER":
            issues.append(
                ValidationIssue(
                    field="floater_formula.benchmark",
                    code="FLOATER_PARSE_INCOMPLETE",
                    message=(
                        "Индикатор флоатера не распознан (benchmark=OTHER). "
                        "Ожидались KEY_RATE_CBR / RUONIA / RUSFAR / CPI"
                    ),
                )
            )
        if formula.spread_percent is None:
            issues.append(
                ValidationIssue(
                    field="floater_formula.spread_percent",
                    code="FLOATER_PARSE_INCOMPLETE",
                    message="Спред флоатера (spread_percent) не извлечён — нужна проверка оператора",
                )
            )
        if not (formula.raw_formula_text or "").strip():
            issues.append(
                ValidationIssue(
                    field="floater_formula.raw_formula_text",
                    code="FLOATER_PARSE_INCOMPLETE",
                    message="Отсутствует дословная цитата формулы (raw_formula_text)",
                )
            )
        return issues

    def _check_complexity(self, data: BondDocumentSchema) -> list[ValidationIssue]:
        coupon_type = CouponType(str(data.coupon_type))
        # OTHER всегда HITL. Хорошо распарсенный FLOATER может пройти авто (smart filter).
        if coupon_type == CouponType.OTHER:
            return [
                ValidationIssue(
                    field="coupon_type",
                    code="COMPLEX_COUPON",
                    message=(
                        "Тип купона OTHER требует обязательной ручной проверки оператором (HITL)"
                    ),
                )
            ]
        return []

    def _check_moex(
        self, data: BondDocumentSchema, moex: MoexReference | None
    ) -> list[ValidationIssue]:
        if moex is None or not moex.found:
            return []

        issues: list[ValidationIssue] = []
        if moex.nominal_value is not None and data.nominal_value is not None:
            if abs(moex.nominal_value - data.nominal_value) > self.nominal_tolerance:
                issues.append(
                    ValidationIssue(
                        field="nominal_value",
                        code="MOEX_NOMINAL_MISMATCH",
                        message=(
                            f"Номинал в документе {data.nominal_value} ≠ "
                            f"номинал на MOEX {moex.nominal_value}"
                        ),
                    )
                )

        if moex.maturity_date is not None and data.maturity_date is not None:
            if moex.maturity_date != data.maturity_date:
                issues.append(
                    ValidationIssue(
                        field="maturity_date",
                        code="MOEX_MATURITY_MISMATCH",
                        message=(
                            f"Дата погашения в документе {data.maturity_date} ≠ "
                            f"дата на MOEX {moex.maturity_date}"
                        ),
                    )
                )

        if (
            moex.currency
            and data.currency
            and moex.currency.upper() != data.currency.upper()
        ):
            issues.append(
                ValidationIssue(
                    field="currency",
                    code="MOEX_CURRENCY_MISMATCH",
                    message=(
                        f"Валюта в документе {data.currency} ≠ валюта на MOEX {moex.currency}"
                    ),
                    severity="warning",
                )
            )
        return issues

    def _check_quotes(
        self, data: BondDocumentSchema, source_text: str
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        corpus = source_text or ""

        for field in _QUOTE_REQUIRED_FIELDS:
            value = getattr(data, field)
            quote = (data.quotes or {}).get(field, "")

            if not quote:
                issues.append(
                    ValidationIssue(
                        field=field,
                        code="QUOTE_MISSING",
                        message=f"Отсутствует цитата (quotes['{field}']) для проверки значения",
                    )
                )
                continue

            if _quote_contains_value(quote, value):
                continue

            if corpus and _quote_contains_value(corpus, value):
                issues.append(
                    ValidationIssue(
                        field=field,
                        code="QUOTE_MISMATCH",
                        message=(
                            f"Цитата для '{field}' не содержит извлечённое значение "
                            f"'{value}', хотя оно найдено в исходном тексте"
                        ),
                        severity="warning",
                    )
                )
            else:
                issues.append(
                    ValidationIssue(
                        field=field,
                        code="QUOTE_NOT_IN_SOURCE",
                        message=(
                            f"Значение '{value}' поля '{field}' не подтверждено "
                            f"цитатой и не найдено в исходном тексте — возможна галлюцинация"
                        ),
                    )
                )
        return issues

    @staticmethod
    def _coerce_moex(moex: MoexReference | dict[str, Any] | None) -> MoexReference | None:
        if moex is None:
            return None
        if isinstance(moex, MoexReference):
            return moex
        return MoexReference.model_validate(moex)
