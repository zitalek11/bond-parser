from __future__ import annotations

from typing import Any

import instructor
from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.bond import BondDocumentSchema
from app.services.extractor import extract_relevant_sections

logger = get_logger(__name__)

SYSTEM_PROMPT = """\
Ты — эксперт по российскому рынку облигаций и эмиссионной документации \
(решения о выпуске, проспекты ценных бумаг, условия выпуска биржевых облигаций). \
Ты работаешь как локальная модель Qwen 2.5 и извлекаешь данные строго в JSON-схему BondDocumentSchema.

Жёсткие правила:
1. Не выдумывай данные. Если поле в документе отсутствует — верни null или пустой список.
2. Ответ — ТОЛЬКО валидный JSON (внутри markdown-блока ```json ... ```), без пояснений вне JSON.
3. Числа пиши с точкой как десятичным разделителем: 15.5, а не 15,5.
4. Даты — строго YYYY-MM-DD.
5. ИНН — только цифры (10 или 12). ISIN — ровно 12 символов.
6. ЗАПРЕЩЕНО сокращать длинные таблицы купонов и амортизации: не используй «...», \
«и так далее», комментарии внутри JSON и не пропускай строки посередине графика.
7. coupon_type:
   - FIXED — фиксированная ставка / известный график ставок;
   - FLOATER — плавающая ставка (Ключевая ставка ЦБ, RUONIA, RUSFAR, ИПЦ и т.п.);
   - ZERO — дисконтные / без купона;
   - OTHER — комбинированные и прочие нестандартные условия.
8. Для FLOATER обязательно заполни объект floater_formula:
   - benchmark: KEY_RATE_CBR | RUONIA | RUSFAR | CPI | OTHER;
   - spread_percent: число («плюс 2.25%» → 2.25, «минус 0.5%» → -0.5, нет спреда → 0.0);
   - observation_shift_days: лаг в днях, если указан;
   - averaging_type: POINT_IN_TIME | AVERAGE_IN_ARREARS | COMPOUNDED | UNKNOWN;
   - cap_rate / floor_rate при наличии;
   - raw_formula_text: дословная цитата формулы из документа.
9. amortization: у каждого шага amortization_percent; payment_date и/или coupon_number; \
при наличии столбца остатка — residual_nominal_percent. Сумма amortization_percent = 100.
10. quotes: дословные цитаты, подтверждающие reg_number, issuer_inn, nominal_value и др.
11. Строго следуй описаниям полей схемы (description) — они содержат примеры маппинга.
"""

USER_PROMPT_TEMPLATE = """\
Извлеки параметры облигационного выпуска из следующего фрагмента эмиссионного документа.
Верни полный JSON по схеме BondDocumentSchema. Не сокращай таблицы.

--- DOCUMENT START ---
{document}
--- DOCUMENT END ---
"""


def _resolve_instructor_mode(provider: str, explicit: str | None) -> instructor.Mode:
    if explicit:
        return getattr(instructor.Mode, explicit.upper(), instructor.Mode.MD_JSON)
    # Qwen 2.5: MD_JSON — максимальная стабильность на вложенных схемах
    if provider in {"ollama", "vllm", "openrouter"}:
        return instructor.Mode.MD_JSON
    return instructor.Mode.MD_JSON


class LLMBondParser:
    """Извлечение BondDocumentSchema через Qwen 2.5 (Ollama / vLLM / OpenRouter) + instructor.

    Трафик идёт на settings.llm_base_url (локально или OpenRouter).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
        instructor_mode: str | None = None,
        max_input_chars: int | None = None,
        provider: str | None = None,
    ) -> None:
        self.provider = (provider or settings.llm_provider).lower()
        self.model = model or settings.resolved_llm_model
        self.temperature = (
            temperature if temperature is not None else settings.llm_temperature
        )
        self.max_tokens = settings.llm_max_tokens
        self.context_window = settings.llm_context_window
        self.repeat_penalty = settings.llm_repeat_penalty
        self.max_input_chars = max_input_chars or settings.llm_max_input_chars
        self.max_retries = (
            max_retries if max_retries is not None else settings.llm_max_retries
        )

        mode = _resolve_instructor_mode(
            self.provider, instructor_mode or settings.llm_instructor_mode
        )
        self.instructor_mode = mode

        endpoint = (base_url or settings.llm_base_url).rstrip("/")
        if "api.openai.com" in endpoint:
            raise ValueError(
                "LLM_BASE_URL указывает на api.openai.com — используйте "
                "Ollama/vLLM локально или OpenRouter (https://openrouter.ai/api/v1)."
            )

        raw_client = AsyncOpenAI(
            api_key=api_key or settings.llm_api_key or "not-needed",
            base_url=endpoint,
            timeout=timeout if timeout is not None else settings.llm_timeout_seconds,
            max_retries=0,  # сетевые ретраи отключаем — ретраи валидации делает instructor
        )
        # max_retries не кладём в from_openai.kwargs — иначе конфликт с create(max_retries=...)
        self.client = instructor.from_openai(raw_client, mode=mode)
        self.base_url = endpoint
        logger.info(
            "LLMBondParser ready provider=%s model=%s base_url=%s mode=%s ctx=%s",
            self.provider,
            self.model,
            self.base_url,
            mode,
            self.context_window,
        )

    def _extra_body(self) -> dict[str, Any]:
        if self.provider == "ollama":
            return {
                "options": {
                    "num_ctx": self.context_window,
                    "repeat_penalty": self.repeat_penalty,
                    "temperature": self.temperature,
                }
            }
        if self.provider == "openrouter":
            # OpenRouter: только стандартные OpenAI-поля; без provider-specific extras
            return {}
        # vLLM / OpenAI-compatible local servers
        return {"repetition_penalty": self.repeat_penalty}

    async def parse(
        self,
        markdown: str,
        *,
        extra_context: str | None = None,
        use_relevant_sections: bool = True,
    ) -> BondDocumentSchema:
        focused = (
            extract_relevant_sections(markdown) if use_relevant_sections else markdown
        )
        document = self._truncate(focused)
        user_content = USER_PROMPT_TEMPLATE.format(document=document)
        if extra_context:
            user_content = f"{extra_context.strip()}\n\n{user_content}"

        logger.info(
            "Qwen parse start model=%s provider=%s chars_in=%d chars_llm=%d retries=%d",
            self.model,
            self.provider,
            len(markdown),
            len(document),
            self.max_retries,
        )

        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "response_model": BondDocumentSchema,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "max_retries": self.max_retries,
            # strict=True ломает coerce date-строк из MD_JSON ответа Qwen
            "strict": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        extra = self._extra_body()
        if extra:
            create_kwargs["extra_body"] = extra
        result = await self.client.chat.completions.create(**create_kwargs)
        assert isinstance(result, BondDocumentSchema)
        logger.info(
            "Qwen parse done reg=%s type=%s coupons=%d amort=%d",
            result.reg_number,
            result.coupon_type,
            len(result.coupons),
            len(result.amortization),
        )
        return result

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_input_chars:
            return text
        head = int(self.max_input_chars * 0.7)
        tail = self.max_input_chars - head
        logger.warning(
            "Document truncated for LLM: %d → %d chars (head=%d tail=%d)",
            len(text),
            self.max_input_chars,
            head,
            tail,
        )
        return text[:head] + "\n\n...[TRUNCATED]...\n\n" + text[-tail:]


class MockLLMBondParser:
    """Детерминированный парсер для офлайн-тестов / демо без Ollama."""

    def __init__(self, payload: dict[str, Any] | BondDocumentSchema | None = None) -> None:
        self.payload = payload
        self.model = "mock-qwen2.5"

    async def parse(
        self,
        markdown: str,
        *,
        extra_context: str | None = None,
        use_relevant_sections: bool = True,
    ) -> BondDocumentSchema:
        if self.payload is not None:
            if isinstance(self.payload, BondDocumentSchema):
                return self.payload
            return BondDocumentSchema.model_validate(self.payload)

        from datetime import date
        import re

        # Шапка с ISIN/рег.номером часто отбрасывается extract_relevant_sections —
        # идентичность берём из полного текста, таблицы — из релевантных секций.
        full = markdown
        text = extract_relevant_sections(markdown) if use_relevant_sections else markdown

        def _ru_date(label: str, haystack: str = full) -> date | None:
            m = re.search(
                rf"{label}\s*:\s*(\d{{2}})[.](\d{{2}})[.](\d{{4}})",
                haystack,
                re.IGNORECASE,
            )
            if not m:
                return None
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                return None

        def _parse_ddmmyyyy(value: str) -> date | None:
            value = value.strip()
            if len(value) >= 10 and value[2] == "." and value[5] == ".":
                try:
                    return date(int(value[6:10]), int(value[3:5]), int(value[0:2]))
                except ValueError:
                    return None
            return None

        inn_match = re.search(
            r"(?:ИНН(?:\s+эмитента)?)\s*:\s*(\d{10}|\d{12})\b", full, re.IGNORECASE
        ) or re.search(r"\b(\d{10}|\d{12})\b", full)
        reg_match = re.search(
            r"(?:регистрационн\w*\s+номер[^\n:]{0,40}:\s*)([0-9A-Za-z][0-9A-Za-z/-]{5,})",
            full,
            re.IGNORECASE,
        ) or re.search(
            r"\b(\d[A-Z0-9]{0,4}-\d{1,2}-\d{5}[A-Z0-9-]*)\b",
            full,
            re.IGNORECASE,
        )
        isin_match = re.search(
            r"(?:ISIN(?:\s*код)?)\s*:\s*([A-Z]{2}[A-Z0-9]{10})\b", full, re.IGNORECASE
        ) or re.search(r"\b([A-Z]{2}[A-Z0-9]{10})\b", full)
        issuer_match = re.search(
            r"(?:Полное наименование эмитента|Эмитент)\s*:\s*([^\n]+)",
            full,
            re.IGNORECASE,
        )
        nominal_match = re.search(
            r"номинальн\w*\s+стоимост\w*[^\d]{0,60}?"
            r"(\d[\d\s\u00a0]*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)",
            full,
            re.IGNORECASE,
        )
        nominal = 1000.0
        if nominal_match:
            nominal = float(
                nominal_match.group(1)
                .replace(" ", "")
                .replace("\u00a0", "")
                .replace(",", ".")
            )

        inn = inn_match.group(1) if inn_match else "7707083893"
        reg = (reg_match.group(1) if reg_match else "4B02-01-00000-R-001P").strip().upper()
        issuer = (
            issuer_match.group(1).strip().strip("«»\"") if issuer_match else "Mock Issuer"
        )

        coupons = []
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4 and cells[0].isdigit():
                try:
                    start = _parse_ddmmyyyy(cells[1])
                    end = _parse_ddmmyyyy(cells[2])
                    if not start or not end:
                        continue
                    rate = float(cells[3].replace(",", ".").replace(" ", "")) if cells[3] else None
                    pay = None
                    if len(cells) > 4 and cells[4]:
                        pay = float(cells[4].replace(",", ".").replace(" ", ""))
                    coupons.append(
                        {
                            "coupon_number": int(cells[0]),
                            "start_date": start.isoformat(),
                            "end_date": end.isoformat(),
                            "rate_percent": rate,
                            "payment_amount": pay,
                        }
                    )
                except (ValueError, IndexError):
                    continue

        amortization = []
        residual = 100.0
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            pay_date = _parse_ddmmyyyy(cells[0])
            if pay_date is None:
                continue
            try:
                pct = float(cells[1].replace(",", ".").replace(" ", ""))
            except ValueError:
                continue
            residual = round(residual - pct, 4)
            try:
                resid = float(cells[2].replace(",", ".").replace(" ", "")) if cells[2] else residual
            except ValueError:
                resid = residual
            amortization.append(
                {
                    "payment_date": pay_date.isoformat(),
                    "amortization_percent": pct,
                    "residual_nominal_percent": resid,
                }
            )

        lower = full.casefold()
        is_floater = any(
            token in lower
            for token in ("ключев", "ruonia", "rusfar", "флоатер", "плавающ", "кс цб", "key_rate")
        )
        floater = None
        coupon_type = "FIXED"
        if is_floater:
            coupon_type = "FLOATER"
            if "ruonia" in lower:
                benchmark = "RUONIA"
            elif "rusfar" in lower:
                benchmark = "RUSFAR"
            elif "ипц" in lower or "cpi" in lower:
                benchmark = "CPI"
            elif "ключев" in lower or "кс цб" in lower or "key_rate_cbr" in lower:
                benchmark = "KEY_RATE_CBR"
            else:
                benchmark = "OTHER"
            spread_match = re.search(
                r"[Сс]пред[^\d]{0,40}?(\d+(?:[.,]\d+)?)\s*%?",
                full,
            )
            spread = float(spread_match.group(1).replace(",", ".")) if spread_match else 0.0
            lag_match = re.search(r"[Лл]аг наблюдения:\s*(\d+)", full)
            lag = int(lag_match.group(1)) if lag_match else 5
            floater = {
                "benchmark": benchmark,
                "spread_percent": spread,
                "observation_shift_days": lag,
                "averaging_type": "POINT_IN_TIME",
                "raw_formula_text": f"{benchmark} + {spread}%",
            }

        placement = _ru_date("Дата начала размещения")
        maturity = _ru_date("Дата погашения")

        return BondDocumentSchema(
            isin=isin_match.group(1).upper() if isin_match else None,
            reg_number=reg,
            issuer_name=issuer,
            issuer_inn=inn,
            nominal_value=nominal,
            currency="RUB",
            placement_start_date=placement or date(2025, 1, 15),
            maturity_date=maturity or date(2028, 1, 15),
            coupon_type=coupon_type,
            floater_formula=floater,
            coupons=coupons,
            amortization=amortization,
            quotes={
                "reg_number": f"Государственный регистрационный номер: {reg}",
                "issuer_inn": f"ИНН {inn}",
                "nominal_value": f"Номинальная стоимость одной облигации: {int(nominal)}",
                "issuer_name": f"Эмитент: {issuer}",
            },
        )
