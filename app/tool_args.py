"""Проверка решения модели до MCP-вызова: структура по схеме сервера и смысл значений.

Схема берётся из обнаружения MCP (`ToolSpec.input_schema`) и здесь не дублируется.
Поверх схемы проверяются правила, которых JSON-схема не выражает: календарь, будущее
время, часовой пояс пользователя, переходы на летнее время.
"""

import json
import re
from datetime import date, datetime, timedelta

import jsonschema

from app.mcp_client import ToolSpec
from app.timezones import TIMEZONE_HINT, LocalTimeError, format_local, localize, zone

MAX_SCHEDULE_DAYS = 400
MAX_REMINDER_AHEAD = timedelta(days=366)
_LABELS = {"city": "город", "date": "дата", "text": "текст", "remind_at": "время"}

# Признаки текста, обращённого к модели. Это эвристика для данных инструментов, а не
# разбор ответа модели: найденные строки скрываются, действия с эффектом блокируются.
_INSTRUCTION_LIKE = re.compile(
    r"игнорир\w*|забудь\w*|ignore\s+(?:all|any|previous|the|prior)|disregard"
    r"|system\s*prompt|системн\w*\s+(?:промпт|инструкц)|ты\s+теперь|you\s+are\s+now"
    r"|вызов\w*\s+(?:инструмент|функци)|call\s+the\s+tool|new\s+instructions"
    r"|нов\w+\s+инструкц|developer\s+message|add_reminder|get_weather|get_schedule",
    re.IGNORECASE,
)
HIDDEN_TEXT = "[скрыто: текст похож на инструкцию]"


class ArgumentError(ValueError):
    """Аргументы не прошли проверку; message можно показать пользователю."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_arguments(raw: object) -> dict:
    """Аргументы tool_call: JSON-строка (OpenAI) или уже объект."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raise ArgumentError(
                "bad_json", "Модель передала аргументы в неверном формате."
            ) from None
    if not isinstance(raw, dict):
        raise ArgumentError("bad_json", "Модель передала аргументы в неверном формате.")
    return raw


def validate_arguments(
    spec: ToolSpec, arguments: dict, *, timezone: str | None, now: datetime
) -> dict:
    """Возвращает нормализованные аргументы или бросает ArgumentError."""
    allowed = set(spec.input_schema.get("properties", {}))
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise ArgumentError("extra_arguments", f"Лишние аргументы: {', '.join(extra)}.")
    try:
        jsonschema.validate(arguments, spec.input_schema)
    except jsonschema.ValidationError as exc:
        field = ".".join(str(part) for part in exc.absolute_path) or "аргументы"
        raise ArgumentError("schema", f"Поле «{field}» не прошло проверку схемы.") from None
    normalized = {
        key: value.strip() if isinstance(value, str) else value for key, value in arguments.items()
    }
    if spec.name == "get_schedule":
        normalized["date"] = _schedule_date(normalized["date"], now)
    elif spec.name == "add_reminder":
        normalized = _reminder(normalized, timezone, now)
    return normalized


def _schedule_date(value: str, now: datetime) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ArgumentError("invalid_date", f"Такой даты нет в календаре: {value}.") from None
    if abs((parsed - now.date()).days) > MAX_SCHEDULE_DAYS:
        raise ArgumentError("date_out_of_range", "Дата слишком далеко от сегодняшней.")
    return parsed.isoformat()


def _reminder(arguments: dict, timezone: str | None, now: datetime) -> dict:
    if timezone is None:
        raise ArgumentError(
            "timezone_not_set",
            "Сначала задайте часовой пояс — без него я не знаю, какое время вы имеете в виду. "
            + TIMEZONE_HINT,
        )
    text = arguments["text"]
    if not text:
        raise ArgumentError("empty_text", "Текст напоминания пустой.")
    try:
        moment = datetime.fromisoformat(arguments["remind_at"])
    except ValueError:
        raise ArgumentError(
            "invalid_time", "Не удалось разобрать дату и время напоминания. Уточните их."
        ) from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ArgumentError("time_without_zone", "У времени напоминания нет часового пояса.")
    try:
        # Локальное время толкуется только в зоне, сохранённой пользователем.
        local = localize(moment.replace(tzinfo=None), timezone)
    except LocalTimeError as exc:
        raise ArgumentError(exc.code, exc.message) from None
    # Модель знает только текущее смещение, а на дату после перехода на летнее или
    # зимнее время действует другое: допустимы оба, время всё равно берётся по зоне.
    allowed = {local.utcoffset(), now.astimezone(zone(timezone)).utcoffset()}
    if moment.utcoffset() not in allowed:
        raise ArgumentError(
            "timezone_mismatch",
            f"Время указано не в вашем часовом поясе ({timezone}). Уточните время.",
        )
    if local <= now:
        raise ArgumentError(
            "past_time",
            f"Это время уже прошло: {format_local(local, timezone)}. Укажите время в будущем.",
        )
    if local - now > MAX_REMINDER_AHEAD:
        raise ArgumentError("too_far", "Напоминание можно поставить не дальше чем на год вперёд.")
    return {"text": text, "remind_at": local.isoformat()}


def summarize(arguments: dict, timezone: str | None) -> str:
    """Безопасное резюме для аудита и /why: без внутренних идентификаторов."""
    parts = []
    for key, value in arguments.items():
        label = _LABELS.get(key, key)
        if key == "remind_at" and timezone:
            try:
                value = format_local(datetime.fromisoformat(value), timezone)
            except (TypeError, ValueError):
                pass
        elif key == "date":
            try:
                value = date.fromisoformat(value).strftime("%d.%m.%Y")
            except (TypeError, ValueError):
                pass
        text = str(value)
        parts.append(f"{label} «{text[:80]}{'…' if len(text) > 80 else ''}»")
    return ", ".join(parts) or "без аргументов"


def neutralize(data: object) -> tuple[object, bool]:
    """Заменяет строки, похожие на инструкции модели; второй элемент — найдено ли такое."""
    if isinstance(data, str):
        return (HIDDEN_TEXT, True) if _INSTRUCTION_LIKE.search(data) else (data, False)
    if isinstance(data, list):
        items = [neutralize(item) for item in data]
        return [item for item, _ in items], any(flag for _, flag in items)
    if isinstance(data, dict):
        pairs = {key: neutralize(value) for key, value in data.items()}
        return {key: value for key, (value, _) in pairs.items()}, any(
            flag for _, flag in pairs.values()
        )
    return data, False
