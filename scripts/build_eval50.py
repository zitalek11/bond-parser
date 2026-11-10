#!/usr/bin/env python3
"""Сборка eval-набора на 50 выпусках: реестр ЦБ РФ + формат раскрытия Интерфакс-ЦРКИ.

ЦРКИ (e-disclosure.ru) сейчас отдаёт CAPTCHA на автоматические запросы.
Поэтому для половины выборки берём рыночные выпуски MOEX (раскрываются через
Интерфакс-ЦРКИ) и собираем входной текст в стиле эмиссионного решения ЦРКИ;
эталон — атрибуты MOEX ISS (description + bondization).

Для второй половины — реестр ценных бумаг Банка России + обогащение MOEX.

Usage (from bond_parser/):
  .venv/bin/python scripts/build_eval50.py
  .venv/bin/python scripts/build_eval50.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import random
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tests" / "golden_dataset" / "eval50"
STORAGE = ROOT / "storage" / "eval50"
CBR_ZIP_URL = "https://www.cbr.ru/vfs/registers/rcb/list_reestrcb_20260930.zip"
MOEX_TQCB = "https://iss.moex.com/iss/engines/stock/markets/bonds/boards/TQCB/securities.json"
HEADERS = {
    "User-Agent": "BondEmissionParser/0.1 (eval50; research)",
    "Accept": "application/json,application/zip,*/*",
}


def _parse_date(value: Any) -> str | None:
    if value in (None, "", "0000-00-00"):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _fmt_ru(iso: str | None) -> str:
    if not iso:
        return "—"
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def _money_ru(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")


def download_cbr_registry(client: httpx.Client) -> Path:
    STORAGE.mkdir(parents=True, exist_ok=True)
    zip_path = STORAGE / "cbr_reestr.zip"
    if not zip_path.exists() or zip_path.stat().st_size < 1000:
        print("↓ скачиваю реестр ЦБ РФ …")
        resp = client.get(CBR_ZIP_URL, timeout=120)
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        xlsx_name = next(n for n in names if n.endswith(".xlsx"))
        target = STORAGE / Path(xlsx_name).name
        if not target.exists():
            zf.extract(xlsx_name, STORAGE)
            extracted = STORAGE / xlsx_name
            if extracted != target:
                extracted.rename(target)
    return STORAGE / Path(xlsx_name).name


def load_cbr_bonds(xlsx_path: Path, *, limit: int = 2500) -> list[dict[str, Any]]:
    print(f"Читаю реестр ЦБ ({xlsx_path.name}), до {limit} облигаций …", flush=True)
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows)]
    col = {name: idx for idx, name in enumerate(header)}
    out: list[dict[str, Any]] = []
    scanned = 0
    for row in rows:
        scanned += 1
        if scanned % 50000 == 0:
            print(f"  … просмотрено {scanned}, найдено {len(out)}", flush=True)
        isin = row[col.get("ISIN", 1)]
        cfi = str(row[col.get("CFI", 3)] or "")
        if not isin or not str(isin).startswith("RU"):
            continue
        # DB* — долговые инструменты (облигации)
        if not cfi.upper().startswith("DB"):
            continue
        currency = str(row[col.get("Валюта номинала ценной бумаги", 4)] or "RUB")
        if currency.upper() not in {"RUB", "RUR", "SUR"}:
            continue
        out.append(
            {
                "source": "cbr",
                "isin": str(isin).strip().upper(),
                "reg_number": str(row[col.get("Регистрационный номер выпуска ценных бумаг", 2)] or "").strip(),
                "cfi": cfi,
                "currency": "RUB",
                "issuer_inn": str(row[col.get("ИНН эмитента", 5)] or "").strip(),
                "issuer_name": str(
                    row[col.get("Полное наименование эмитента из ЕГРЮЛ", 7)]
                    or row[col.get("Краткое наименование эмитента из ЕГРЮЛ", 8)]
                    or ""
                ).strip(),
            }
        )
        if len(out) >= limit:
            break
    wb.close()
    print(f"  готово: {len(out)} кандидатов ЦБ (скан {scanned} строк)", flush=True)
    return out


def fetch_moex_board(client: httpx.Client) -> list[dict[str, Any]]:
    resp = client.get(MOEX_TQCB, params={"iss.meta": "off", "iss.only": "securities"})
    resp.raise_for_status()
    payload = resp.json()["securities"]
    cols = payload["columns"]
    col = {c: i for i, c in enumerate(cols)}
    items = []
    for row in payload["data"]:
        secid = str(row[col["SECID"]])
        items.append(
            {
                "secid": secid,
                "shortname": row[col.get("SHORTNAME")] if "SHORTNAME" in col else None,
                "facevalue": row[col.get("FACEVALUE")] if "FACEVALUE" in col else None,
                "matdate": row[col.get("MATDATE")] if "MATDATE" in col else None,
                "couponpercent": row[col.get("COUPONPERCENT")] if "COUPONPERCENT" in col else None,
            }
        )
    return items


def fetch_moex_details(client: httpx.Client, secid: str) -> dict[str, Any] | None:
    try:
        desc = client.get(
            f"https://iss.moex.com/iss/securities/{secid}.json",
            params={"iss.meta": "off", "iss.only": "description"},
        )
        desc.raise_for_status()
        fields = {
            row[0]: row[2]
            for row in desc.json()["description"]["data"]
            if row and row[0]
        }
        bond = client.get(
            f"https://iss.moex.com/iss/securities/{secid}/bondization.json",
            params={"iss.meta": "off", "limit": 500},
        )
        bond.raise_for_status()
        bond_payload = bond.json()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! MOEX {secid}: {exc}")
        return None

    isin = fields.get("ISIN") or (secid if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", secid) else None)
    if not isin:
        return None

    currency = fields.get("FACEUNIT") or fields.get("CURRENCYID") or "RUB"
    if str(currency).upper() in {"SUR", "RUR"}:
        currency = "RUB"

    details = str(fields.get("COUPON_DETAILS") or "")
    benchmark_raw = str(fields.get("COUPON_BENCHMARK") or "")
    spread_raw = fields.get("COUPON_BENCHMARK_SPREAD")
    lower = f"{details} {benchmark_raw}".casefold()
    is_floater = any(
        t in lower
        for t in ("ключев", "ruonia", "rusfar", "плавающ", "float", "rrefkeyr", "ипц", "cpi")
    )
    coupon_type = "FLOATER" if is_floater else "FIXED"
    floater = None
    if coupon_type == "FLOATER":
        if "ruonia" in lower:
            bench = "RUONIA"
        elif "rusfar" in lower:
            bench = "RUSFAR"
        elif "ипц" in lower or "cpi" in lower:
            bench = "CPI"
        else:
            bench = "KEY_RATE_CBR"
        try:
            spread = float(str(spread_raw).replace(",", ".")) if spread_raw not in (None, "") else 0.0
        except ValueError:
            spread = 0.0
        floater = {
            "benchmark": bench,
            "spread_percent": spread,
            "observation_shift_days": 5,
            "averaging_type": "POINT_IN_TIME",
            "raw_formula_text": details or f"{bench} + {spread}%",
        }

    coupons_raw = (bond_payload.get("coupons") or {}).get("data") or []
    coupon_cols = (bond_payload.get("coupons") or {}).get("columns") or []
    ccol = {c: i for i, c in enumerate(coupon_cols)}
    coupons: list[dict[str, Any]] = []
    for idx, row in enumerate(coupons_raw[:40], start=1):
        start = _parse_date(row[ccol["startdate"]] if "startdate" in ccol else None)
        end = _parse_date(row[ccol["coupondate"]] if "coupondate" in ccol else None)
        rate = row[ccol["valueprc"]] if "valueprc" in ccol else None
        pay = row[ccol["value"]] if "value" in ccol else None
        try:
            rate_f = float(rate) if rate not in (None, "") else None
        except (TypeError, ValueError):
            rate_f = None
        try:
            pay_f = round(float(pay), 4) if pay not in (None, "") else None
        except (TypeError, ValueError):
            pay_f = None
        if not end:
            continue
        coupons.append(
            {
                "coupon_number": idx,
                "start_date": start,
                "end_date": end,
                "rate_percent": rate_f,
                "payment_amount": pay_f,
            }
        )

    amorts_raw = (bond_payload.get("amortizations") or {}).get("data") or []
    amort_cols = (bond_payload.get("amortizations") or {}).get("columns") or []
    acol = {c: i for i, c in enumerate(amort_cols)}
    amortization: list[dict[str, Any]] = []
    for row in amorts_raw:
        source = str(row[acol["data_source"]] if "data_source" in acol else "")
        if source == "maturity":
            continue
        pct = row[acol["valueprc"]] if "valueprc" in acol else None
        pay_date = _parse_date(row[acol["amortdate"]] if "amortdate" in acol else None)
        try:
            pct_f = float(pct) if pct not in (None, "") else None
        except (TypeError, ValueError):
            pct_f = None
        if pct_f is None:
            continue
        amortization.append(
            {
                "payment_date": pay_date,
                "amortization_percent": pct_f,
            }
        )
    # остатки
    residual = 100.0
    for item in amortization:
        residual = round(residual - float(item["amortization_percent"]), 4)
        item["residual_nominal_percent"] = max(residual, 0.0)

    try:
        nominal = float(str(fields.get("FACEVALUE") or fields.get("INITIALFACEVALUE") or "1000").replace(",", "."))
    except ValueError:
        nominal = 1000.0
    try:
        volume = int(float(fields.get("ISSUESIZE") or 0)) or None
    except (TypeError, ValueError):
        volume = None

    inn = fields.get("EMITENT_INN") or fields.get("INN") or fields.get("ISSUER_INN")
    issuer = (
        fields.get("EMITENT_TITLE")
        or fields.get("ISSUERNAME")
        or fields.get("NAME")
        or fields.get("SHORTNAME")
    )

    return {
        "secid": secid,
        "isin": str(isin).upper(),
        "reg_number": str(fields.get("REGNUMBER") or "").strip() or None,
        "issuer_name": str(issuer or "").strip(),
        "issuer_inn": str(inn).strip() if inn else None,
        "nominal_value": nominal,
        "currency": str(currency).upper(),
        "issue_volume": volume,
        "placement_start_date": _parse_date(fields.get("ISSUEDATE")),
        "maturity_date": _parse_date(fields.get("MATDATE")),
        "coupon_type": coupon_type,
        "floater_formula": floater,
        "coupons": coupons,
        "amortization": amortization,
        "coupon_details": details,
        "shortname": fields.get("SHORTNAME") or fields.get("NAME"),
        "raw_fields": {
            "COUPON_DETAILS": details,
            "COUPON_BENCHMARK": benchmark_raw,
            "COUPONPERCENT": fields.get("COUPONPERCENT"),
        },
    }


def render_cbr_doc(base: dict[str, Any], moex: dict[str, Any]) -> str:
    """Текст в стиле выписки/решения о регистрации выпуска (ЦБ)."""
    issuer = base.get("issuer_name") or moex.get("issuer_name") or "Эмитент"
    inn = base.get("issuer_inn") or moex.get("issuer_inn") or "0000000000"
    reg = base.get("reg_number") or moex.get("reg_number") or "б/н"
    isin = base["isin"]
    nominal = moex.get("nominal_value") or 1000
    volume = moex.get("issue_volume")
    maturity = moex.get("maturity_date")
    placement = moex.get("placement_start_date")
    coupon_type = moex.get("coupon_type") or "FIXED"
    floater = moex.get("floater_formula")

    lines = [
        "БАНК РОССИИ",
        "Сведения из реестра эмиссионных ценных бумаг",
        "",
        f"Полное наименование эмитента: {issuer}",
        f"ИНН эмитента: {inn}",
        f"Государственный регистрационный номер выпуска: {reg}",
        f"ISIN: {isin}",
        f"Валюта номинала ценной бумаги: российский рубль (RUB)",
        f"Номинальная стоимость одной ценной бумаги: {_money_ru(nominal)} руб.",
    ]
    if volume:
        lines.append(f"Количество ценных бумаг в выпуске: {volume} шт.")
    if placement:
        lines.append(f"Дата начала размещения: {_fmt_ru(placement)}")
    if maturity:
        lines.append(f"Дата погашения: {_fmt_ru(maturity)}")
    lines.append("")
    if coupon_type == "FLOATER" and floater:
        lines += [
            "Порядок определения процентной ставки:",
            f"Индикатор: {floater['benchmark']}.",
            f"Спред составляет {floater['spread_percent']} процента годовых.",
            f"Формула: {floater.get('raw_formula_text') or 'плавающая ставка'}.",
            "Купонный доход является плавающим (флоатер).",
        ]
    else:
        rate = None
        if moex.get("coupons"):
            rate = moex["coupons"][0].get("rate_percent")
        rate_s = _money_ru(rate) if rate is not None else "фиксированная"
        lines += [
            "Купонный доход является фиксированным.",
            f"Ставка купона: {rate_s} процента годовых.",
        ]
    lines += _coupon_table(moex.get("coupons") or [])
    lines += _amort_block(moex.get("amortization") or [])
    return "\n".join(lines)


def render_interfax_doc(moex: dict[str, Any]) -> str:
    """Текст в стиле решения о выпуске / сообщения на e-disclosure (Интерфакс-ЦРКИ)."""
    issuer = moex.get("issuer_name") or moex.get("shortname") or "Эмитент"
    inn = moex.get("issuer_inn") or "0000000000"
    reg = moex.get("reg_number") or "б/н"
    isin = moex["isin"]
    nominal = moex.get("nominal_value") or 1000
    volume = moex.get("issue_volume")
    maturity = moex.get("maturity_date")
    placement = moex.get("placement_start_date")
    coupon_type = moex.get("coupon_type") or "FIXED"
    floater = moex.get("floater_formula")

    lines = [
        "Сообщение о существенном факте",
        "Интерфакс — Центр раскрытия корпоративной информации",
        "",
        f"Эмитент: {issuer}",
        f"ИНН: {inn}",
        f"Вид ценных бумаг: биржевые облигации",
        f"Государственный регистрационный номер выпуска: {reg}",
        f"ISIN код: {isin}",
        "",
        "## Сведения о размещаемых ценных бумагах",
        f"Номинальная стоимость одной биржевой облигации составляет {int(nominal)} "
        f"({_money_ru(nominal)}) рублей.",
        "Валюта номинала: российский рубль (RUB).",
    ]
    if volume:
        lines.append(f"Количество ценных бумаг выпуска: {volume} (штук).")
    if placement:
        lines.append(f"Дата начала размещения: {_fmt_ru(placement)}")
    if maturity:
        lines.append(f"Дата погашения: {_fmt_ru(maturity)}")
    lines.append("")
    lines.append("## Срок и порядок выплаты купонного дохода")
    if coupon_type == "FLOATER" and floater:
        lines += [
            "Купонный доход является плавающим.",
            f"Базовый индикатор: {floater['benchmark']} "
            f"({'ключевая ставка Банка России' if floater['benchmark']=='KEY_RATE_CBR' else floater['benchmark']}).",
            f"Спред: {floater['spread_percent']}% годовых.",
            f"Цитата формулы: «{floater.get('raw_formula_text')}».",
            f"Лаг наблюдения: {floater.get('observation_shift_days', 5)} дней.",
        ]
    else:
        rate = None
        if moex.get("coupons"):
            rate = moex["coupons"][0].get("rate_percent")
        lines += [
            "Купонный доход является фиксированным.",
            f"Ставка купона составляет {_money_ru(rate)} процента годовых."
            if rate is not None
            else "Ставка купона фиксированная.",
        ]
    lines += _coupon_table(moex.get("coupons") or [])
    lines += _amort_block(moex.get("amortization") or [])
    lines += [
        "",
        "Источник раскрытия: Интерфакс-ЦРКИ (e-disclosure.ru).",
        "Текст собран по рыночным атрибутам выпуска (сайт ЦРКИ требует CAPTCHA).",
    ]
    return "\n".join(lines)


def _coupon_table(coupons: list[dict[str, Any]]) -> list[str]:
    if not coupons:
        return []
    lines = [
        "",
        "| № купонного периода | Дата начала | Дата окончания (выплаты) | Ставка, % годовых | Размер выплаты, руб. |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in coupons[:12]:
        lines.append(
            f"| {c.get('coupon_number')} | {_fmt_ru(c.get('start_date'))} | "
            f"{_fmt_ru(c.get('end_date'))} | "
            f"{c.get('rate_percent') if c.get('rate_percent') is not None else ''} | "
            f"{c.get('payment_amount') if c.get('payment_amount') is not None else ''} |"
        )
    return lines


def _amort_block(amort: list[dict[str, Any]]) -> list[str]:
    if not amort:
        return []
    lines = [
        "",
        "## Порядок и условия погашения",
        "Погашение номинальной стоимости осуществляется частично (амортизация):",
        "",
        "| Дата | Процент погашения от номинала | Остаток номинала, % |",
        "| --- | --- | --- |",
    ]
    for a in amort:
        lines.append(
            f"| {_fmt_ru(a.get('payment_date'))} | {a.get('amortization_percent')} | "
            f"{a.get('residual_nominal_percent')} |"
        )
    return lines


def _expected_from_moex(
    moex: dict[str, Any],
    *,
    issuer_inn_fallback: str | None = None,
    max_coupons: int = 12,
) -> dict[str, Any]:
    """Эталон только по тем полям/купонам, которые попали во входной текст."""
    coupons = (moex.get("coupons") or [])[:max_coupons]
    amort = moex.get("amortization") or []
    inn = moex.get("issuer_inn") or issuer_inn_fallback
    return {
        "isin": moex["isin"],
        "reg_number": moex["reg_number"],
        "issuer_name": moex["issuer_name"],
        "issuer_inn": inn,
        "nominal_value": moex["nominal_value"],
        "currency": moex["currency"],
        "issue_volume": moex.get("issue_volume"),
        "placement_start_date": moex.get("placement_start_date"),
        "maturity_date": moex.get("maturity_date"),
        "coupon_type": moex["coupon_type"],
        "floater_formula": moex.get("floater_formula"),
        "coupons": coupons,
        "amortization": amort,
        "quotes": {
            "reg_number": f"Государственный регистрационный номер выпуска: {moex['reg_number']}",
            "issuer_inn": f"ИНН: {inn}",
            "nominal_value": f"Номинальная стоимость: {moex['nominal_value']}",
        },
    }


def build_case(
    *,
    case_id: str,
    source: str,
    title: str,
    markdown: str,
    expected: dict[str, Any],
) -> dict[str, Any]:
    critical = [
        "reg_number",
        "isin",
        "nominal_value",
        "currency",
        "maturity_date",
        "coupon_type",
        "coupons_count",
    ]
    inn = str(expected.get("issuer_inn") or "")
    if inn and inn != "0000000000":
        critical.insert(2, "issuer_inn")
    if expected.get("coupon_type") == "FLOATER":
        critical.extend(["benchmark", "spread_percent"])
    if expected.get("amortization"):
        critical.append("amortization_sum")
    return {
        "id": case_id,
        "title": title,
        "source": source,
        "tags": [source, str(expected.get("coupon_type", "")).lower()],
        "input_markdown": markdown,
        "expected": expected,
        "critical_fields": critical,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cbr", type=int, default=25)
    parser.add_argument("--interfax", type=int, default=25)
    args = parser.parse_args()
    random.seed(args.seed)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.json"):
        old.unlink()

    with httpx.Client(timeout=60, follow_redirects=True, headers=HEADERS) as client:
        xlsx = download_cbr_registry(client)
        cbr_bonds = load_cbr_bonds(xlsx)
        print(f"ЦБ: облигаций с ISIN RU / CFI DB*: {len(cbr_bonds)}")
        board = fetch_moex_board(client)
        print(f"MOEX TQCB: {len(board)} бумаг")

        # --- CBR half ---
        cbr_sample = random.sample(cbr_bonds, min(args.cbr * 4, len(cbr_bonds)))
        cbr_cases: list[dict[str, Any]] = []
        for item in cbr_sample:
            if len(cbr_cases) >= args.cbr:
                break
            moex = fetch_moex_details(client, item["isin"])
            if not moex or not moex.get("maturity_date") or not moex.get("reg_number"):
                continue
            if not moex.get("issuer_inn") and item.get("issuer_inn"):
                moex["issuer_inn"] = item["issuer_inn"]
            if item.get("reg_number"):
                moex["reg_number"] = item["reg_number"]
            if item.get("issuer_name"):
                moex["issuer_name"] = item["issuer_name"]
            expected = _expected_from_moex(moex, issuer_inn_fallback=item.get("issuer_inn"))
            case_id = f"cbr_{moex['isin'].lower()}"
            cbr_cases.append(
                build_case(
                    case_id=case_id,
                    source="cbr",
                    title=f"ЦБ: {moex.get('shortname') or moex['isin']}",
                    markdown=render_cbr_doc(item, moex),
                    expected=expected,
                )
            )
            print(f"  + CBR {moex['isin']} {moex.get('shortname')}")

        # --- Interfax-style half: CBR identity + MOEX market attrs, текст в формате ЦРКИ ---
        used_isins = {c["expected"]["isin"] for c in cbr_cases}
        cbr_by_isin = {b["isin"]: b for b in cbr_bonds}
        leftover = [b for b in cbr_bonds if b["isin"] not in used_isins]
        random.shuffle(leftover)
        # дополняем с доски MOEX, если кандидатов ЦБ мало
        moex_extra = [b for b in board if b["secid"].startswith("RU") and b["secid"] not in used_isins]
        random.shuffle(moex_extra)
        ifx_pool: list[dict[str, Any]] = leftover + [
            {"isin": b["secid"], "reg_number": "", "issuer_inn": "", "issuer_name": b.get("shortname") or b["secid"]}
            for b in moex_extra
        ]
        ifx_cases: list[dict[str, Any]] = []
        for item in ifx_pool:
            if len(ifx_cases) >= args.interfax:
                break
            isin = item["isin"]
            if isin in used_isins:
                continue
            moex = fetch_moex_details(client, isin)
            if not moex:
                continue
            if not moex.get("maturity_date") or not moex.get("reg_number"):
                continue
            if moex.get("currency") != "RUB":
                continue
            cbr_row = cbr_by_isin.get(isin) or item
            if not moex.get("issuer_inn") and cbr_row.get("issuer_inn"):
                moex["issuer_inn"] = cbr_row["issuer_inn"]
            if cbr_row.get("issuer_name"):
                moex["issuer_name"] = cbr_row["issuer_name"]
            if cbr_row.get("reg_number"):
                moex["reg_number"] = cbr_row["reg_number"]
            if not moex.get("issuer_inn"):
                # без ИНН кейс всё равно полезен, но critical_fields его пропустит через expected null
                moex["issuer_inn"] = "0000000000"
            expected = _expected_from_moex(moex)
            case_id = f"interfax_{moex['isin'].lower()}"
            ifx_cases.append(
                build_case(
                    case_id=case_id,
                    source="interfax_crki",
                    title=f"ЦРКИ: {moex.get('shortname') or moex['isin']}",
                    markdown=render_interfax_doc(moex),
                    expected=expected,
                )
            )
            used_isins.add(moex["isin"])
            print(f"  + ЦРКИ {moex['isin']} {moex.get('shortname')}", flush=True)

    cases = (cbr_cases + ifx_cases)[: args.limit]
    if len(cases) < args.limit:
        raise SystemExit(f"Собрано только {len(cases)} кейсов из {args.limit}")

    for case in cases:
        path = OUT_DIR / f"{case['id']}.json"
        path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "built_at": datetime.utcnow().isoformat() + "Z",
        "total": len(cases),
        "by_source": {
            "cbr": sum(1 for c in cases if c["source"] == "cbr"),
            "interfax_crki": sum(1 for c in cases if c["source"] == "interfax_crki"),
        },
        "by_coupon_type": {
            "FIXED": sum(1 for c in cases if c["expected"]["coupon_type"] == "FIXED"),
            "FLOATER": sum(1 for c in cases if c["expected"]["coupon_type"] == "FLOATER"),
        },
        "notes": [
            "Эталон: реестр ЦБ РФ + MOEX ISS (description/bondization).",
            "Интерфакс-ЦРКИ: сайт отдаёт CAPTCHA; тексты в формате раскрытия ЦРКИ собраны из рыночных атрибутов.",
        ],
        "case_ids": [c["id"] for c in cases],
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (STORAGE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"\nГотово: {len(cases)} кейсов → {OUT_DIR}\n"
        f"  ЦБ: {manifest['by_source']['cbr']}, ЦРКИ: {manifest['by_source']['interfax_crki']}\n"
        f"  FIXED: {manifest['by_coupon_type']['FIXED']}, FLOATER: {manifest['by_coupon_type']['FLOATER']}"
    )


if __name__ == "__main__":
    main()
