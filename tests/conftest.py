from __future__ import annotations

from datetime import date

import pytest

from app.schemas.bond import (
    AmortizationItem,
    BondDocumentSchema,
    CouponItem,
    MoexReference,
)


@pytest.fixture
def source_text() -> str:
    return (
        "Решение о выпуске биржевых облигаций серии 001P-01.\n"
        "Государственный регистрационный номер: 4B02-01-36400-R-001P.\n"
        "Эмитент: ПАО «Сбербанк России», ИНН 7707083893.\n"
        "Номинальная стоимость одной облигации: 1 000 рублей.\n"
        "Дата начала размещения: 15.01.2025. Дата погашения: 15.01.2028.\n"
        "Купон фиксированный 15,50% годовых.\n"
    )


@pytest.fixture
def valid_document(source_text: str) -> BondDocumentSchema:
    return BondDocumentSchema(
        isin="RU000A107T19",
        reg_number="4B02-01-36400-R-001P",
        issuer_name="ПАО Сбербанк России",
        issuer_inn="7707083893",
        nominal_value=1000.0,
        currency="RUB",
        issue_volume=10_000_000,
        placement_start_date=date(2025, 1, 15),
        maturity_date=date(2028, 1, 15),
        coupon_type="FIXED",
        coupons=[
            CouponItem(
                coupon_number=1,
                start_date=date(2025, 1, 15),
                end_date=date(2025, 7, 15),
                rate_percent=15.5,
                payment_amount=77.5,
            ),
            CouponItem(
                coupon_number=2,
                start_date=date(2025, 7, 15),
                end_date=date(2026, 1, 15),
                rate_percent=15.5,
                payment_amount=77.5,
            ),
            CouponItem(
                coupon_number=3,
                start_date=date(2026, 1, 15),
                end_date=date(2026, 7, 15),
                rate_percent=15.5,
                payment_amount=77.5,
            ),
            CouponItem(
                coupon_number=4,
                start_date=date(2026, 7, 15),
                end_date=date(2027, 1, 15),
                rate_percent=15.5,
                payment_amount=77.5,
            ),
            CouponItem(
                coupon_number=5,
                start_date=date(2027, 1, 15),
                end_date=date(2027, 7, 15),
                rate_percent=15.5,
                payment_amount=77.5,
            ),
            CouponItem(
                coupon_number=6,
                start_date=date(2027, 7, 15),
                end_date=date(2028, 1, 15),
                rate_percent=15.5,
                payment_amount=77.5,
            ),
        ],
        amortization=[],
        quotes={
            "reg_number": "Государственный регистрационный номер: 4B02-01-36400-R-001P.",
            "issuer_inn": "ИНН 7707083893",
            "nominal_value": "Номинальная стоимость одной облигации: 1 000 рублей.",
            "maturity_date": "Дата погашения: 15.01.2028.",
            "issuer_name": "Эмитент: ПАО «Сбербанк России»",
        },
    )


@pytest.fixture
def moex_match() -> MoexReference:
    return MoexReference(
        found=True,
        isin="RU000A107T19",
        secid="SU26238RMFS4",
        shortname="СберБОк1P1",
        nominal_value=1000.0,
        currency="RUB",
        maturity_date=date(2028, 1, 15),
        issuer_name="Сбербанк России",
    )


@pytest.fixture
def amortizing_document(valid_document: BondDocumentSchema) -> BondDocumentSchema:
    return valid_document.model_copy(
        update={
            "amortization": [
                AmortizationItem(
                    payment_date=date(2026, 1, 15),
                    amortization_percent=30.0,
                    residual_nominal_percent=70.0,
                ),
                AmortizationItem(
                    payment_date=date(2027, 1, 15),
                    amortization_percent=30.0,
                    residual_nominal_percent=40.0,
                ),
                AmortizationItem(
                    payment_date=date(2028, 1, 15),
                    amortization_percent=40.0,
                    residual_nominal_percent=0.0,
                ),
            ]
        }
    )
