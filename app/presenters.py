"""Человекочитаемые тексты из данных инструментов, аудита и списка инструментов."""

from datetime import date, datetime

from app.mcp_client import ResourceSpec, ToolSpec
from app.storage import AgentEvent, PendingAction
from app.timezones import format_local

ACTION_TITLES = {
    "respond": "ответ без инструмента",
    "clarify": "уточнение",
    "call_tool": "вызов инструмента",
    "prepare_action": "подготовка действия (ждёт подтверждения)",
    "rejected": "вызов отклонён проверкой",
    "limit_exceeded": "остановка по лимиту вызовов",
    "error": "ошибка",
}


def _number(value: float) -> str:
    return f"{value:.1f}".replace(".", ",").replace(",0", "")


def _hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


def weather_text(data: dict) -> str:
    if data.get("status") == "ambiguous":
        lines = [
            f"{index}. {item['name']} — {item['country']}"
            + (f", {item['admin1']}" if item.get("admin1") else "")
            for index, item in enumerate(data.get("candidates", []), start=1)
        ]
        example = data["candidates"][0]
        hint = f"{example['name']}, {example.get('admin1') or example['country']}"
        return (
            "Нашлось несколько городов с таким названием:\n"
            + "\n".join(lines)
            + f"\n\nУточните, какой нужен, например: «погода в {hint}»."
        )
    place, now = data["location"], data["current"]
    observed = datetime.fromisoformat(now["observed_at"])
    return (
        f"{place['name']}, {place['country']}: {_number(now['temperature_c'])} °C, "
        f"{now['condition']}, ветер {_number(now['wind_speed'])} м/с. "
        f"Данные {data['source']} на {observed:%d.%m.%Y %H:%M} ({now['timezone']})."
    )


def lesson_lines(lessons: list[dict]) -> list[str]:
    return [
        f"{_hhmm(item['start'])}–{_hhmm(item['end'])} {item['title']}"
        + (f" ({item['location']})" if item.get("location") else "")
        for item in lessons
    ]


def schedule_text(data: dict) -> str:
    day = date.fromisoformat(data["date"]).strftime("%d.%m.%Y")
    if not data["lessons"]:
        return f"На {day} занятий нет."
    return f"Занятия на {day} ({data['timezone']}):\n" + "\n".join(lesson_lines(data["lessons"]))


def week_text(data: dict) -> str:
    start = date.fromisoformat(data["week_start"]).strftime("%d.%m")
    end = date.fromisoformat(data["week_end"]).strftime("%d.%m.%Y")
    blocks = [f"Расписание на неделю {start}–{end} ({data['timezone']}):"]
    for day in data["days"]:
        title = f"{day['weekday'].capitalize()}, {date.fromisoformat(day['date']):%d.%m}"
        lines = lesson_lines(day["lessons"]) or ["занятий нет"]
        blocks.append(title + ":\n" + "\n".join(f"  {line}" for line in lines))
    return "\n\n".join(blocks)


def tool_result_text(tool: str, data: dict) -> str | None:
    """Ответ без модели — если модель недоступна после успешного вызова инструмента."""
    if tool == "get_weather":
        return weather_text(data)
    if tool == "get_schedule":
        return schedule_text(data)
    return None


def tools_text(tools: dict[str, ToolSpec], resources: dict[str, ResourceSpec]) -> str:
    lines = ["Доступные инструменты:"]
    for spec in tools.values():
        kind = "чтение" if spec.read_only else "изменение, нужно подтверждение"
        lines.append(f"• {spec.name} — {spec.summary.lower()} ({kind})")
    if resources:
        lines.append("\nРесурсы:")
        lines.extend(
            f"• {item.uri} — {item.description.rstrip('.').lower() or item.name} (/week)"
            for item in resources.values()
        )
    return "\n".join(lines)


def confirmation_text(action: PendingAction) -> str:
    moment = datetime.fromisoformat(action.arguments["remind_at"])
    return (
        "Создать напоминание?\n"
        f"Текст: {action.arguments['text']}\n"
        f"Когда: {format_local(moment, action.timezone)}\n\n"
        "Подтверждение действует 5 минут."
    )


def reminder_created_text(result: dict) -> str:
    moment = datetime.fromisoformat(result["remind_at"])
    head = "Напоминание создано" if result.get("created", True) else "Это напоминание уже создано"
    return (
        f"{head}: #{result['reminder_id']} — «{result['text']}» на "
        f"{format_local(moment, result['timezone'])}."
    )


def why_text(event: AgentEvent | None, timezone: str | None) -> str:
    if event is None:
        return "Вы ещё не отправляли запросов агенту."
    zone = timezone or "UTC"
    action = ACTION_TITLES.get(event.action, event.action)
    if event.tools and event.action in {"call_tool", "prepare_action", "rejected", "error"}:
        action += f" {event.tools}"
    lines = [
        f"Последний запрос: {format_local(event.created_at, zone)}",
        f"Действие: {action}",
    ]
    if event.args_summary:
        lines.append(f"Аргументы: {event.args_summary}")
    lines += [
        f"Проверка: {event.validation}",
        f"Выполнение: {event.execution}",
        f"Вызовов MCP: {event.tool_calls}, время обработки: {event.duration_ms / 1000:.1f} с",
    ]
    if event.reason:
        lines.append(f"Причина: {event.reason}")
    return "\n".join(lines)
