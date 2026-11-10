from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.bond import (
    AmortizationItem,
    BondDocumentSchema,
    CouponItem,
    MoexReference,
)
from app.services.validator import BondDataValidator, validate_inn_checksum


# ---------------------------------------------------------------------------
# INN checksum (unit of the helper itself)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inn,expected",
    [
        ("7707083893", True),   # Сбербанк (10)
        ("7736207543", True),   # Яндекс (10)
        ("500100732259", True), # физлицо (12), валидный контрольный
        ("7707083890", False),  # битая контрольная
        ("123", False),
        ("abcdefghij", False),
        ("", False),
    ],
)
def test_inn_checksum(inn: str, expected: bool) -> None:
    assert validate_inn_checksum(inn) is expected


# ---------------------------------------------------------------------------
# Schema guards
# ---------------------------------------------------------------------------


def test_schema_rejects_bad_isin() -> None:
    with pytest.raises(ValidationError):
        BondDocumentSchema(
            isin="SHORT",
            reg_number="X",
            issuer_name="Y",
            issuer_inn="7707083893",
            nominal_value=1000,
            coupon_type="FIXED",
        )


def test_schema_floater_requires_formula() -> None:
    with pytest.raises(ValidationError, match="floater_formula"):
        BondDocumentSchema(
            reg_number="X",
            issuer_name="Y",
            issuer_inn="7707083893",
            nominal_value=1000,
            coupon_type="FLOATER",
        )


def test_schema_coupon_period_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        CouponItem(
            coupon_number=1,
            start_date=date(2025, 7, 1),
            end_date=date(2025, 1, 1),
            rate_percent=10,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_document_auto_approved(
    valid_document: BondDocumentSchema, source_text: str, moex_match: MoexReference
) -> None:
    result = BondDataValidator().validate(
        valid_document, source_text=source_text, moex=moex_match
    )
    assert result.status == "APPROVED"
    assert result.errors == []
    assert result.failed_fields() == set()


def test_amortization_sum_100_approved(
    amortizing_document: BondDocumentSchema, source_text: str
) -> None:
    result = BondDataValidator().validate(amortizing_document, source_text=source_text)
    assert result.status == "APPROVED"
    assert not any(i.code == "AMORTIZATION_SUM" for i in result.issues)


# ---------------------------------------------------------------------------
# INN
# ---------------------------------------------------------------------------


def test_bad_inn_needs_review(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    # Меняем ИНН на формально 10-значный, но с битой контрольной;
    # цитату тоже правим, чтобы сработала именно INN_CHECKSUM, а не QUOTE_*.
    doc = valid_document.model_copy(
        update={
            "issuer_inn": "7707083890",
            "quotes": {
                **valid_document.quotes,
                "issuer_inn": "ИНН 7707083890",
            },
        }
    )
    # Чтобы quote-check не падал на отсутствии значения в source_text —
    # отключаем quotes для этого кейса через require_quotes=False, либо
    # передаём текст, где ИНН уже подменён.
    patched_text = source_text.replace("7707083893", "7707083890")
    result = BondDataValidator().validate(doc, source_text=patched_text)
    assert result.status == "NEEDS_REVIEW"
    assert any(i.code == "INN_CHECKSUM" and i.field == "issuer_inn" for i in result.errors)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def test_maturity_before_placement_needs_review(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(
        update={
            "placement_start_date": date(2028, 1, 15),
            "maturity_date": date(2025, 1, 15),
            "coupons": [],  # иначе дополнительно сыплются COUPON_* — нам нужна DATE_ORDER
        }
    )
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert result.status == "NEEDS_REVIEW"
    assert any(i.code == "DATE_ORDER" for i in result.errors)


# ---------------------------------------------------------------------------
# Coupons sequence
# ---------------------------------------------------------------------------


def test_coupon_gap_needs_review(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    coupons = list(valid_document.coupons)
    # Разрыв между 1 и 2: вместо 2025-07-15 ставим 2025-10-01
    coupons[1] = coupons[1].model_copy(update={"start_date": date(2025, 10, 1)})
    doc = valid_document.model_copy(update={"coupons": coupons})
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert result.status == "NEEDS_REVIEW"
    assert any(i.code == "COUPON_GAP" for i in result.errors)


def test_coupon_overlap_needs_review(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    coupons = list(valid_document.coupons)
    coupons[1] = coupons[1].model_copy(update={"start_date": date(2025, 6, 1)})
    doc = valid_document.model_copy(update={"coupons": coupons})
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert any(i.code == "COUPON_OVERLAP" for i in result.errors)


def test_coupon_numbering_gap(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    coupons = [
        c.model_copy(update={"coupon_number": c.coupon_number + (1 if c.coupon_number >= 3 else 0)})
        for c in valid_document.coupons
    ]
    # Получим номера 1,2,4,5,6,7 — пропуск 3
    doc = valid_document.model_copy(update={"coupons": coupons})
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert any(i.code == "COUPON_NUMBERING" for i in result.errors)


def test_small_weekend_gap_allowed(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    """Разрыв 2 дня (выходные) укладывается в допуск 3 дня."""
    from datetime import timedelta

    coupons = list(valid_document.coupons)
    coupons[1] = coupons[1].model_copy(
        update={"start_date": coupons[0].end_date + timedelta(days=2)}
    )
    doc = valid_document.model_copy(update={"coupons": coupons})
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert not any(i.code == "COUPON_GAP" for i in result.issues)


# ---------------------------------------------------------------------------
# Amortization math
# ---------------------------------------------------------------------------


def test_amortization_not_100_needs_review(
    amortizing_document: BondDocumentSchema, source_text: str
) -> None:
    items = list(amortizing_document.amortization)
    items[-1] = AmortizationItem(payment_date=items[-1].payment_date, amortization_percent=39.0)
    doc = amortizing_document.model_copy(update={"amortization": items})
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert result.status == "NEEDS_REVIEW"
    assert any(i.code == "AMORTIZATION_SUM" and i.field == "amortization" for i in result.errors)


def test_amortization_within_tolerance(
    amortizing_document: BondDocumentSchema, source_text: str
) -> None:
    items = [
        AmortizationItem(payment_date=date(2026, 1, 15), amortization_percent=33.333),
        AmortizationItem(payment_date=date(2027, 1, 15), amortization_percent=33.333),
        AmortizationItem(payment_date=date(2028, 1, 15), amortization_percent=33.334),
    ]
    doc = amortizing_document.model_copy(update={"amortization": items})
    result = BondDataValidator(amortization_tolerance=0.01).validate(
        doc, source_text=source_text
    )
    assert not any(i.code == "AMORTIZATION_SUM" for i in result.issues)


# ---------------------------------------------------------------------------
# Complexity → always HITL
# ---------------------------------------------------------------------------


def test_other_coupon_always_needs_review(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(update={"coupon_type": "OTHER"})
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert result.status == "NEEDS_REVIEW"
    assert any(i.code == "COMPLEX_COUPON" for i in result.errors)


def test_well_parsed_floater_can_auto_approve(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(
        update={
            "coupon_type": "FLOATER",
            "floater_formula": {
                "benchmark": "KEY_RATE_CBR",
                "spread_percent": 2.5,
                "observation_shift_days": 5,
                "averaging_type": "POINT_IN_TIME",
                "raw_formula_text": "Ключевая ставка Банка России + 2.5%",
            },
        }
    )
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert result.status == "APPROVED"
    assert not any(i.code.startswith("FLOATER_") for i in result.errors)


def test_floater_other_benchmark_needs_review(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(
        update={
            "coupon_type": "FLOATER",
            "floater_formula": {
                "benchmark": "OTHER",
                "spread_percent": 1.0,
                "averaging_type": "UNKNOWN",
                "raw_formula_text": "некая ставка + 1%",
            },
        }
    )
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert result.status == "NEEDS_REVIEW"
    assert any(i.code == "FLOATER_PARSE_INCOMPLETE" for i in result.errors)


# ---------------------------------------------------------------------------
# MOEX cross-check
# ---------------------------------------------------------------------------


def test_moex_nominal_mismatch(
    valid_document: BondDocumentSchema, source_text: str, moex_match: MoexReference
) -> None:
    moex = moex_match.model_copy(update={"nominal_value": 1500.0})
    result = BondDataValidator().validate(
        valid_document, source_text=source_text, moex=moex
    )
    assert result.status == "NEEDS_REVIEW"
    assert any(i.code == "MOEX_NOMINAL_MISMATCH" for i in result.errors)


def test_moex_maturity_mismatch(
    valid_document: BondDocumentSchema, source_text: str, moex_match: MoexReference
) -> None:
    moex = moex_match.model_copy(update={"maturity_date": date(2030, 1, 1)})
    result = BondDataValidator().validate(
        valid_document, source_text=source_text, moex=moex
    )
    assert any(i.code == "MOEX_MATURITY_MISMATCH" for i in result.errors)


def test_moex_not_found_is_ok(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    result = BondDataValidator().validate(
        valid_document,
        source_text=source_text,
        moex=MoexReference(found=False),
    )
    assert result.status == "APPROVED"
    assert not any(i.code.startswith("MOEX_") for i in result.issues)


def test_moex_accepts_dict(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    result = BondDataValidator().validate(
        valid_document,
        source_text=source_text,
        moex={
            "found": True,
            "nominal_value": 1000.0,
            "maturity_date": "2028-01-15",
            "currency": "RUB",
        },
    )
    assert result.status == "APPROVED"


# ---------------------------------------------------------------------------
# Quotes / hallucination guard
# ---------------------------------------------------------------------------


def test_missing_quote_needs_review(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    quotes = dict(valid_document.quotes)
    del quotes["reg_number"]
    doc = valid_document.model_copy(update={"quotes": quotes})
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert any(i.code == "QUOTE_MISSING" and i.field == "reg_number" for i in result.errors)


def test_hallucinated_value_needs_review(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(
        update={
            "reg_number": "FAKE-REG-999",
            "quotes": {
                **valid_document.quotes,
                "reg_number": "какая-то цитата без номера",
            },
        }
    )
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert any(i.code == "QUOTE_NOT_IN_SOURCE" and i.field == "reg_number" for i in result.errors)


def test_quote_mismatch_but_present_in_source_is_warning(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(
        update={
            "quotes": {
                **valid_document.quotes,
                "nominal_value": "цитата без числа",
            }
        }
    )
    result = BondDataValidator().validate(doc, source_text=source_text)
    # значение 1000 / "1 000" есть в source_text → warning, не error
    assert any(i.code == "QUOTE_MISMATCH" and i.severity == "warning" for i in result.issues)
    assert not any(i.code == "QUOTE_NOT_IN_SOURCE" and i.field == "nominal_value" for i in result.errors)
    # Только warning по quotes → статус всё ещё APPROVED (нет error)
    assert result.status == "APPROVED"


def test_require_quotes_can_be_disabled(
    valid_document: BondDocumentSchema,
) -> None:
    doc = valid_document.model_copy(update={"quotes": {}})
    result = BondDataValidator(require_quotes=False).validate(doc, source_text="")
    assert not any(i.code.startswith("QUOTE_") for i in result.issues)
    assert result.status == "APPROVED"


# ---------------------------------------------------------------------------
# Storage helper & failed_fields (for HITL red highlighting)
# ---------------------------------------------------------------------------


def test_to_storage_and_failed_fields(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(
        update={
            "issuer_inn": "7707083890",
            "quotes": {**valid_document.quotes, "issuer_inn": "ИНН 7707083890"},
            "coupon_type": "OTHER",
        }
    )
    result = BondDataValidator().validate(
        doc, source_text=source_text.replace("7707083893", "7707083890")
    )
    payload = result.to_storage()
    assert isinstance(payload, list) and payload
    assert {"field", "code", "message", "severity"} <= payload[0].keys()
    assert "issuer_inn" in result.failed_fields()
    assert "coupon_type" in result.failed_fields() or "issuer_inn" in result.failed_fields()
