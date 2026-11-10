from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import Form, Request

from app.schemas.bond import BondDocumentSchema


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _parse_date(value: str | None) -> date | None:
    text = _empty_to_none(value)
    if not text:
        return None
    return date.fromisoformat(text)


def _parse_float(value: str | None) -> float | None:
    text = _empty_to_none(value)
    if text is None:
        return None
    return float(text.replace(",", ".").replace(" ", "").replace("\u2212", "-"))


def _parse_int(value: str | None) -> int | None:
    text = _empty_to_none(value)
    if text is None:
        return None
    return int(text.replace(" ", ""))


def _floater_from_form(form: Any) -> dict[str, Any] | None:
    coupon_type = str(form.get("coupon_type") or "").strip().upper()
    benchmark = _empty_to_none(str(form.get("floater_benchmark") or ""))
    raw_text = _empty_to_none(str(form.get("floater_raw_formula_text") or ""))
    # Если оператор не трогал блок флоатера и тип не FLOATER — null
    if coupon_type != "FLOATER" and not benchmark and not raw_text:
        legacy = _empty_to_none(str(form.get("floater_formula") or ""))
        return legacy  # type: ignore[return-value]

    if coupon_type != "FLOATER" and not benchmark:
        return None

    return {
        "benchmark": benchmark or "OTHER",
        "spread_percent": _parse_float(str(form.get("floater_spread_percent") or "0")),
        "observation_shift_days": _parse_int(str(form.get("floater_observation_shift_days") or "")),
        "averaging_type": _empty_to_none(str(form.get("floater_averaging_type") or ""))
        or "UNKNOWN",
        "cap_rate": _parse_float(str(form.get("floater_cap_rate") or "")),
        "floor_rate": _parse_float(str(form.get("floater_floor_rate") or "")),
        "raw_formula_text": raw_text or benchmark or "формула не указана",
    }


async def bond_document_from_form(request: Request) -> BondDocumentSchema:
    """Собрать BondDocumentSchema из HTML-формы экрана верификации."""
    form = await request.form()

    coupons_raw = _empty_to_none(str(form.get("coupons_json") or "")) or "[]"
    amort_raw = _empty_to_none(str(form.get("amortization_json") or "")) or "[]"
    quotes_raw = _empty_to_none(str(form.get("quotes_json") or "")) or "{}"

    payload: dict[str, Any] = {
        "isin": _empty_to_none(str(form.get("isin") or "")),
        "reg_number": str(form.get("reg_number") or "").strip(),
        "issuer_name": str(form.get("issuer_name") or "").strip(),
        "issuer_inn": str(form.get("issuer_inn") or "").strip(),
        "nominal_value": _parse_float(str(form.get("nominal_value") or "")),
        "currency": (_empty_to_none(str(form.get("currency") or "")) or "RUB").upper(),
        "issue_volume": _parse_int(str(form.get("issue_volume") or "")),
        "placement_start_date": _parse_date(str(form.get("placement_start_date") or "")),
        "maturity_date": _parse_date(str(form.get("maturity_date") or "")),
        "coupon_type": str(form.get("coupon_type") or "FIXED").strip().upper(),
        "floater_formula": _floater_from_form(form),
        "coupons": json.loads(coupons_raw),
        "amortization": json.loads(amort_raw),
        "quotes": json.loads(quotes_raw),
    }
    return BondDocumentSchema.model_validate(payload)


ReviewerForm = Form(default="operator")
CommentForm = Form(default=None)
