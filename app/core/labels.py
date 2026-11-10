"""Русские подписи для статусов и справочников в UI."""

from __future__ import annotations

STATUS_RU: dict[str, str] = {
    "PENDING": "Ожидает",
    "DOWNLOADED": "Скачан",
    "EXTRACTED": "Текст извлечён",
    "PARSED": "Распознан",
    "NEEDS_REVIEW": "На проверке",
    "APPROVED": "Одобрен",
    "REJECTED": "Отклонён",
    "FAILED": "Ошибка",
}

SOURCE_RU: dict[str, str] = {
    "RSS": "Лента e-disclosure",
    "MANUAL_UPLOAD": "Ручная загрузка",
    "MOCK": "Тестовый источник",
}

COUPON_TYPE_RU: dict[str, str] = {
    "FIXED": "Фиксированный",
    "FLOATER": "Плавающий",
    "ZERO": "Бескупонный",
    "OTHER": "Прочий",
}

APPROVAL_MODE_RU: dict[str, str] = {
    "AUTO": "Автоматически",
    "MANUAL": "Вручную",
}

SEVERITY_RU: dict[str, str] = {
    "error": "ошибка",
    "warning": "предупреждение",
}


def ru_label(mapping: dict[str, str], value: object, fallback: str | None = None) -> str:
    if value is None:
        return fallback or "—"
    key = getattr(value, "value", value)
    key_s = str(key)
    return mapping.get(key_s, fallback or key_s)
