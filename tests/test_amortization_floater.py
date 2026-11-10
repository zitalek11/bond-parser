from __future__ import annotations

from datetime import date

from app.schemas.bond import AmortizationItem, BondDocumentSchema, FloaterFormulaSchema
from app.services.validator import BondDataValidator


def test_amortization_resolves_date_from_coupon(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(
        update={
            "amortization": [
                AmortizationItem(coupon_number=2, amortization_percent=40.0),
                AmortizationItem(coupon_number=4, amortization_percent=30.0),
                AmortizationItem(coupon_number=6, amortization_percent=30.0),
            ]
        }
    )
    result = BondDataValidator().validate(doc, source_text=source_text)
    resolved = result.resolved_document
    assert resolved is not None
    assert resolved.amortization[0].payment_date == date(2026, 1, 15)
    assert resolved.amortization[1].payment_date == date(2027, 1, 15)
    assert resolved.amortization[2].payment_date == date(2028, 1, 15)
    assert result.status == "APPROVED"


def test_amortization_missing_coupon_number_errors(
    valid_document: BondDocumentSchema, source_text: str
) -> None:
    doc = valid_document.model_copy(
        update={
            "amortization": [
                AmortizationItem(coupon_number=99, amortization_percent=100.0),
            ]
        }
    )
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert result.status == "NEEDS_REVIEW"
    assert any(i.code == "AMORTIZATION_COUPON_MISSING" for i in result.errors)


def test_residual_nominal_chain(
    amortizing_document: BondDocumentSchema, source_text: str
) -> None:
    result = BondDataValidator().validate(amortizing_document, source_text=source_text)
    assert result.status == "APPROVED"
    assert not any(i.code.startswith("AMORTIZATION_RESIDUAL") for i in result.issues)


def test_residual_mismatch_needs_review(
    amortizing_document: BondDocumentSchema, source_text: str
) -> None:
    items = list(amortizing_document.amortization)
    items[1] = items[1].model_copy(update={"residual_nominal_percent": 50.0})  # should be 40
    doc = amortizing_document.model_copy(update={"amortization": items})
    result = BondDataValidator().validate(doc, source_text=source_text)
    assert any(i.code == "AMORTIZATION_RESIDUAL_MISMATCH" for i in result.errors)


def test_legacy_percent_alias_and_string_floater() -> None:
    doc = BondDocumentSchema.model_validate(
        {
            "reg_number": "4B02-01-36400-R-001P",
            "issuer_name": "Test",
            "issuer_inn": "7707083893",
            "nominal_value": 1000,
            "coupon_type": "FLOATER",
            "floater_formula": "Ключевая ставка Банка России + 2.25%",
            "amortization": [{"percent": 100.0, "payment_date": "2028-01-15"}],
            "quotes": {
                "reg_number": "4B02-01-36400-R-001P",
                "issuer_inn": "7707083893",
                "nominal_value": "1000",
            },
        }
    )
    assert isinstance(doc.floater_formula, FloaterFormulaSchema)
    assert doc.floater_formula.benchmark == "KEY_RATE_CBR"
    assert doc.floater_formula.spread_percent == 2.25
    assert doc.amortization[0].amortization_percent == 100.0
