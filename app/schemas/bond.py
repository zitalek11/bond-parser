from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from app.core.enums import CouponType

BenchmarkType = Literal["KEY_RATE_CBR", "RUONIA", "RUSFAR", "CPI", "OTHER"]
AveragingType = Literal["POINT_IN_TIME", "AVERAGE_IN_ARREARS", "COMPOUNDED", "UNKNOWN"]


def _coerce_date(value: Any) -> Any:
    """ISO/RU date strings → date (нужно при instructor strict / MD_JSON)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if "T" in text:
            text = text.split("T", 1)[0]
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return value


DateIn = Annotated[date, BeforeValidator(_coerce_date)]
OptionalDateIn = Annotated[date | None, BeforeValidator(_coerce_date)]


class CouponItem(BaseModel):
    """Один купонный период облигации."""

    model_config = ConfigDict(extra="forbid")

    coupon_number: int = Field(
        ...,
        ge=1,
        description="Порядковый номер купонного периода, начиная с 1. Пример: 6",
    )
    start_date: DateIn = Field(
        ...,
        description="Дата начала купонного периода в формате YYYY-MM-DD. Пример: 2025-01-15",
    )
    end_date: DateIn = Field(
        ...,
        description=(
            "Дата окончания купонного периода / дата выплаты купона в формате YYYY-MM-DD. "
            "Пример: 2025-07-15"
        ),
    )
    rate_percent: float | None = Field(
        None,
        description=(
            "Ставка купона в процентах годовых для FIXED. "
            "Число с точкой: 15.5 означает 15,5%. Для FLOATER обычно null."
        ),
    )
    payment_amount: float | None = Field(
        None,
        description=(
            "Размер купонной выплаты на одну облигацию в валюте номинала. "
            "Пример: 77.5"
        ),
    )

    @model_validator(mode="after")
    def _period_positive(self) -> CouponItem:
        if self.end_date <= self.start_date:
            raise ValueError(
                f"coupon #{self.coupon_number}: end_date must be after start_date"
            )
        return self


class AmortizationItem(BaseModel):
    """Одно частичное погашение (амортизация) номинала.

    В российских эмиссионных документах дата часто задаётся через номер купона:
    «в дату окончания 6-го купонного периода погашается 25% номинальной стоимости».
    """

    model_config = ConfigDict(extra="forbid")

    payment_date: OptionalDateIn = Field(
        None,
        description=(
            "Календарная дата амортизации YYYY-MM-DD, если явно указана в таблице. "
            "Если указан только номер купона — оставь null."
        ),
    )
    coupon_number: int | None = Field(
        None,
        ge=1,
        description=(
            "Номер купона, в дату выплаты которого происходит амортизация. "
            "Из фразы «в дату окончания 6-го купонного периода» извлеки 6."
        ),
    )
    amortization_percent: float = Field(
        ...,
        gt=0,
        le=100,
        description=(
            "Процент погашения от первоначальной номинальной стоимости в эту дату. "
            "Пример: 25.0 означает погашение 25% номинала. "
            "Устаревший алиас поля: percent."
        ),
    )
    residual_nominal_percent: float | None = Field(
        None,
        ge=0,
        le=100,
        description=(
            "Остаточная непогашенная доля номинала в % после данного платежа, "
            "если есть столбец «остаток номинала» / «непогашенная часть». "
            "После полного погашения должно быть 0.0."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_percent_key(cls, data: object) -> object:
        if isinstance(data, dict) and "percent" in data:
            data = dict(data)
            if "amortization_percent" not in data:
                data["amortization_percent"] = data["percent"]
            data.pop("percent", None)
        return data

    @model_validator(mode="after")
    def _require_date_or_coupon(self) -> AmortizationItem:
        if self.payment_date is None and self.coupon_number is None:
            raise ValueError(
                "AmortizationItem requires payment_date or coupon_number (or both)"
            )
        return self


class FloaterFormulaSchema(BaseModel):
    """Структурированная формула плавающей ставки (флоатер)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    benchmark: BenchmarkType = Field(
        ...,
        description=(
            "Базовый индикатор ставки. Маппинг из текста документа: "
            "«Ключевая ставка Банка России» / «КС ЦБ» / «ключевая ставка» → KEY_RATE_CBR; "
            "«ставка RUONIA» / «RUONIA» → RUONIA; "
            "«RUSFAR» → RUSFAR; "
            "«индекс потребительских цен» / «ИПЦ» / «CPI» → CPI; "
            "иной индикатор → OTHER."
        ),
    )
    spread_percent: float | None = Field(
        None,
        description=(
            "Спред в процентах годовых к индикатору. "
            "«плюс 2.25%» / «+ 2,25 п.п.» → 2.25; "
            "«минус 0.5%» / «− 0,50» → -0.5; "
            "если спред не упомянут явно — 0.0. "
            "Разделитель дробной части — точка."
        ),
    )
    observation_shift_days: int | None = Field(
        None,
        ge=0,
        description=(
            "Лаг наблюдения в днях. Из фразы "
            "«по состоянию на 5-й рабочий день, предшествующий дате начала купонного периода» "
            "извлеки 5. Если лаг не указан — null."
        ),
    )
    averaging_type: AveragingType = Field(
        "UNKNOWN",
        description=(
            "Способ определения ставки: "
            "POINT_IN_TIME — значение на конкретную дату (точку); "
            "AVERAGE_IN_ARREARS — среднее арифметическое за купонный период; "
            "COMPOUNDED — сложный процент / капитализация (compounded in arrears); "
            "UNKNOWN — если из текста однозначно не следует."
        ),
    )
    cap_rate: float | None = Field(
        None,
        description=(
            "Максимальное ограничение (cap) итоговой ставки купона в % годовых, "
            "если указано. Пример: 25.0. Иначе null."
        ),
    )
    floor_rate: float | None = Field(
        None,
        description=(
            "Минимальное ограничение (floor) итоговой ставки купона в % годовых, "
            "если указано. Пример: 0.0. Иначе null."
        ),
    )
    raw_formula_text: str = Field(
        ...,
        min_length=1,
        description=(
            "Точная дословная цитата абзаца (или строки таблицы) из документа, "
            "где описана формула расчёта купонного дохода. Не пересказывай своими словами."
        ),
    )


class BondDocumentSchema(BaseModel):
    """Структурированный результат LLM-парсинга эмиссионного документа (Qwen 2.5).

    Поле `quotes` содержит дословные цитаты из исходного текста для антигаллюцинаций.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    isin: str | None = Field(
        None,
        description="ISIN код ценной бумаги из 12 символов, если указан. Пример: RU000A107T19",
        examples=["RU000A107T19"],
    )
    reg_number: str = Field(
        ...,
        min_length=1,
        description=(
            "Государственный регистрационный номер выпуска. "
            "Пример: 4B02-01-36400-R-001P"
        ),
        examples=["4B02-01-36400-R-001P"],
    )
    issuer_name: str = Field(
        ...,
        min_length=1,
        description="Полное фирменное наименование эмитента. Пример: ПАО Сбербанк",
    )
    issuer_inn: str = Field(
        ...,
        description="ИНН эмитента — только 10 или 12 цифр без пробелов и дефисов. Пример: 7707083893",
        examples=["7707083893"],
    )
    nominal_value: float = Field(
        ...,
        gt=0,
        description=(
            "Номинальная стоимость одной ценной бумаги. Число с точкой. "
            "Пример: 1000.0 для «1 000 рублей»"
        ),
    )
    currency: str = Field(
        default="RUB",
        min_length=3,
        max_length=3,
        description="Валюта номинала ISO 4217. По умолчанию RUB. RUR/SUR нормализуй в RUB.",
    )
    issue_volume: int | None = Field(
        None,
        gt=0,
        description="Количество ценных бумаг в выпуске (штук). Пример: 10000000",
    )
    placement_start_date: OptionalDateIn = Field(
        None,
        description="Дата начала размещения YYYY-MM-DD. Пример: 2025-01-15",
    )
    maturity_date: OptionalDateIn = Field(
        None,
        description="Дата погашения выпуска YYYY-MM-DD. Пример: 2028-01-15",
    )
    coupon_type: CouponType = Field(
        ...,
        description=(
            "Тип купона: FIXED — фиксированная ставка; "
            "FLOATER — плавающая (КС ЦБ, RUONIA и т.п.); "
            "ZERO — дисконтная / без купона; "
            "OTHER — комбинированные и прочие нестандартные условия."
        ),
    )
    floater_formula: FloaterFormulaSchema | None = Field(
        None,
        description=(
            "Структурированная формула плавающей ставки. "
            "Обязательно заполняй при coupon_type=FLOATER. Для FIXED/ZERO — null."
        ),
    )
    coupons: list[CouponItem] = Field(
        default_factory=list,
        description=(
            "Полный график купонных периодов без сокращений. "
            "Запрещено использовать «...», комментарии или пропускать строки посередине."
        ),
    )
    amortization: list[AmortizationItem] = Field(
        default_factory=list,
        description=(
            "График частичного погашения номинала. "
            "Сумма amortization_percent по всем шагам должна равняться 100.0. "
            "Не сокращай таблицу."
        ),
    )
    quotes: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Дословные цитаты из документа по ключам: reg_number, issuer_name, issuer_inn, "
            "nominal_value, maturity_date, coupon_type, floater_formula, isin."
        ),
    )

    @field_validator("isin", mode="before")
    @classmethod
    def _empty_isin_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("isin")
    @classmethod
    def _isin_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if len(cleaned) != 12 or not cleaned.isalnum():
            raise ValueError("ISIN must be exactly 12 alphanumeric characters")
        return cleaned

    @field_validator("issuer_inn")
    @classmethod
    def _inn_digits_only(cls, value: str) -> str:
        digits = value.strip()
        if not digits.isdigit() or len(digits) not in (10, 12):
            raise ValueError("INN must contain 10 or 12 digits")
        return digits

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if cleaned in {"SUR", "RUR"}:
            return "RUB"
        return cleaned

    @field_validator("floater_formula", mode="before")
    @classmethod
    def _coerce_floater_string(cls, value: object) -> object:
        """Допускаем legacy-строку «КС ЦБ + 2.5%» → минимальная структура."""
        if value is None or isinstance(value, (dict, FloaterFormulaSchema)):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            benchmark: BenchmarkType = "OTHER"
            lower = text.casefold()
            if "ruonia" in lower:
                benchmark = "RUONIA"
            elif "rusfar" in lower:
                benchmark = "RUSFAR"
            elif "ипц" in lower or "cpi" in lower or "потребительск" in lower:
                benchmark = "CPI"
            elif (
                "ключев" in lower
                or "кс цб" in lower
                or "кс%" in lower
                or "банка россии" in lower
                or "key rate" in lower
            ):
                benchmark = "KEY_RATE_CBR"

            import re

            spread = 0.0
            m = re.search(
                r"([+\-−–]?)\s*(\d+[.,]\d+|\d+)\s*%?",
                text.replace("плюс", "+").replace("минус", "-"),
            )
            if m:
                sign = -1.0 if m.group(1) in {"-", "−", "–"} or "минус" in lower else 1.0
                # If explicit minus in text before number
                if m.group(1) in {"-", "−", "–"}:
                    sign = -1.0
                elif "+" in (m.group(1) or ""):
                    sign = 1.0
                elif "минус" in lower:
                    sign = -1.0
                spread = sign * float(m.group(2).replace(",", "."))

            return {
                "benchmark": benchmark,
                "spread_percent": spread,
                "averaging_type": "UNKNOWN",
                "raw_formula_text": text,
            }
        return value

    @field_validator("coupons")
    @classmethod
    def _sort_coupons(cls, value: list[CouponItem]) -> list[CouponItem]:
        return sorted(value, key=lambda c: c.coupon_number)

    @field_validator("amortization")
    @classmethod
    def _sort_amortization(cls, value: list[AmortizationItem]) -> list[AmortizationItem]:
        def _key(item: AmortizationItem) -> tuple:
            return (
                item.payment_date.toordinal() if item.payment_date else 10**9,
                item.coupon_number or 10**9,
            )

        return sorted(value, key=_key)

    @model_validator(mode="after")
    def _floater_requires_formula(self) -> BondDocumentSchema:
        if self.coupon_type == CouponType.FLOATER and self.floater_formula is None:
            raise ValueError("floater_formula is required when coupon_type is FLOATER")
        return self


class MoexReference(BaseModel):
    """Эталонные поля с MOEX ISS для кросс-валидации."""

    model_config = ConfigDict(extra="ignore")

    found: bool = False
    isin: str | None = None
    secid: str | None = None
    shortname: str | None = None
    nominal_value: float | None = None
    currency: str | None = None
    maturity_date: date | None = None
    issuer_name: str | None = None
    raw: dict | None = None


class ValidationIssue(BaseModel):
    """Одна ошибка/предупреждение Data Quality Gate."""

    model_config = ConfigDict(extra="forbid")

    field: str
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"


class ValidationResult(BaseModel):
    """Итог Data Quality Gate."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["APPROVED", "NEEDS_REVIEW"]
    issues: list[ValidationIssue] = Field(default_factory=list)
    # Документ после автоподстановки дат амортизации из купонов
    resolved_document: BondDocumentSchema | None = None

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_storage(self) -> list[dict]:
        return [issue.model_dump() for issue in self.issues]

    def failed_fields(self) -> set[str]:
        return {issue.field for issue in self.errors}
