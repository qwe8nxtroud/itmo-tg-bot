"""Тестовые замены хранилища, модели и внешних сервисов: без PostgreSQL и без сети."""

import asyncio
import json
import uuid
from collections import deque
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from app.llm import LLMResponse, ToolCall
from app.storage import STALE_EXECUTION, AgentEvent, HistoryMessage, PendingAction, UserSettings
from mcp_server.errors import ToolFailure
from mcp_server.reminders import StoredReminder
from mcp_server.schedule import ScheduleFile, ScheduleSource
from mcp_server.server import ServerDeps, build_server
from mcp_server.weather import CurrentWeather, Location, WeatherResult

TRUST_SECRET = "test-trust-secret"


class FakeStorage:
    """In-memory реализация интерфейса app.storage.Storage с раздельными данными по chat_id."""

    def __init__(self, default_settings: UserSettings | None = None) -> None:
        self.settings: dict[int, UserSettings] = {}
        self.history: dict[int, list[HistoryMessage]] = {}
        # Настройки «как будто пользователь уже выбрал режим» для тестов режимов ЛР1.
        self.default_settings = default_settings
        self.timezones: dict[int, str] = {}
        self.actions: dict[uuid.UUID, PendingAction] = {}
        self.action_sources: dict[tuple[int, int], uuid.UUID] = {}
        self.events: dict[int, list[AgentEvent]] = {}
        self.updated_at: dict[uuid.UUID, datetime] = {}

    async def init_schema(self) -> None:
        pass

    async def get_settings(self, chat_id: int) -> UserSettings | None:
        return self.settings.get(chat_id, self.default_settings)

    async def save_settings(self, chat_id: int, settings: UserSettings) -> None:
        self.settings[chat_id] = settings

    async def switch_mode(self, chat_id: int, settings: UserSettings) -> None:
        self.settings[chat_id] = settings
        self.history.pop(chat_id, None)

    async def get_history(self, chat_id: int, *, limit: int) -> list[HistoryMessage]:
        return self.history.get(chat_id, [])[-limit:]

    async def append_exchange(
        self, chat_id: int, user_text: str, assistant_text: str, *, keep: int
    ) -> None:
        messages = self.history.setdefault(chat_id, [])
        messages.append(HistoryMessage("user", user_text))
        messages.append(HistoryMessage("assistant", assistant_text))
        del messages[:-keep]

    async def clear_history(self, chat_id: int) -> None:
        self.history.pop(chat_id, None)

    # --- ЛР2 --------------------------------------------------------------------------
    # Методы без await внутри выполняются атомарно в asyncio, как одна SQL-команда.

    async def get_timezone(self, user_id: int) -> str | None:
        return self.timezones.get(user_id)

    async def set_timezone(self, user_id: int, timezone: str) -> None:
        self.timezones[user_id] = timezone

    async def prepare_action(
        self, *, user_id, chat_id, source_message_id, tool, arguments, timezone, now, ttl
    ) -> tuple[PendingAction, bool]:
        existing = self.action_sources.get((chat_id, source_message_id))
        if existing is not None:
            return self.actions[existing], False
        action = PendingAction(
            id=uuid.uuid4(),
            user_id=user_id,
            chat_id=chat_id,
            tool=tool,
            arguments=dict(arguments),
            timezone=timezone,
            status="pending",
            expires_at=now + ttl,
        )
        for other in list(self.actions.values()):
            if other.user_id == user_id and other.status == "pending":
                self.actions[other.id] = replace(other, status="cancelled")
        self.actions[action.id] = action
        self.action_sources[(chat_id, source_message_id)] = action.id
        return action, True

    async def claim_action(self, action_id, *, user_id, chat_id, now) -> PendingAction | None:
        action = self.actions.get(action_id)
        if action is None or action.user_id != user_id or action.chat_id != chat_id:
            return None
        waiting = action.status == "pending" and (action.expires_at > now or action.confirmed_at)
        stale = (
            action.status == "executing"
            and self.updated_at.get(action_id, now) < now - STALE_EXECUTION
        )
        if not (waiting or stale):
            return None
        self.actions[action_id] = replace(
            action, status="executing", confirmed_at=action.confirmed_at or now
        )
        self.updated_at[action_id] = now
        return self.actions[action_id]

    async def get_action(self, action_id, *, user_id) -> PendingAction | None:
        action = self.actions.get(action_id)
        return action if action is not None and action.user_id == user_id else None

    async def expire_action(self, action_id, *, user_id, now) -> bool:
        action = await self.get_action(action_id, user_id=user_id)
        if (
            action is None
            or action.status != "pending"
            or action.expires_at > now
            or action.confirmed_at
        ):
            return False
        self.actions[action_id] = replace(action, status="expired")
        return True

    async def cancel_action(self, action_id, *, user_id, chat_id) -> bool:
        action = self.actions.get(action_id)
        if (
            action is None
            or action.user_id != user_id
            or action.chat_id != chat_id
            or action.status != "pending"
        ):
            return False
        self.actions[action_id] = replace(action, status="cancelled")
        return True

    async def finish_action(self, action_id, *, status, result=None) -> None:
        action = self.actions[action_id]
        if action.status == "executing":
            self.actions[action_id] = replace(action, status=status, result=result)

    async def add_event(self, user_id: int, event: AgentEvent) -> None:
        self.events.setdefault(user_id, []).append(event)

    async def last_message_event(self, user_id: int) -> AgentEvent | None:
        events = [e for e in self.events.get(user_id, []) if e.kind == "message"]
        return events[-1] if events else None


def text(content: str) -> LLMResponse:
    return LLMResponse(text=content, prompt_tokens=10, completion_tokens=5)


def call(name: str, arguments: dict | str | None = None, *, comment: str = "") -> LLMResponse:
    """Ответ модели с одним вызовом инструмента (аргументы — JSON-строка, как в OpenAI)."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
    return LLMResponse(
        text=comment, tool_calls=(ToolCall(id=f"call_{name}", name=name, arguments=raw),)
    )


class FakeLLM:
    """Отдаёт заранее заданные ответы или исключения и запоминает переданные запросы."""

    model = "test-model"

    def __init__(self, *responses: str | LLMResponse | Exception, delay: float = 0.0) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[list[dict], float]] = []
        self.tool_lists: list[list[dict]] = []
        self.delay = delay

    async def _next(self, messages: list[dict], temperature: float) -> LLMResponse:
        self.calls.append(([dict(message) for message in messages], temperature))
        await asyncio.sleep(self.delay)  # уступаем цикл событий, как настоящий сетевой вызов
        response = self.responses.popleft() if self.responses else "ответ модели"
        if isinstance(response, Exception):
            raise response
        return text(response) if isinstance(response, str) else response

    async def complete(self, messages: list[dict[str, str]], *, temperature: float) -> LLMResponse:
        return await self._next(messages, temperature)

    async def chat(
        self, messages: list[dict], *, temperature: float, tools: list[dict]
    ) -> LLMResponse:
        self.tool_lists.append(tools)
        return await self._next(messages, temperature)


# --- MCP-сервер с подменёнными внешними границами --------------------------------------


class FakeWeather:
    """Погода без сети: заданный результат или ошибка; запоминает запросы."""

    def __init__(self, result: WeatherResult | ToolFailure | None = None) -> None:
        self.result = result or weather_ok()
        self.queries: list[str] = []

    async def current(self, query: str) -> WeatherResult:
        self.queries.append(query)
        if isinstance(self.result, ToolFailure):
            raise self.result
        return self.result


def weather_ok(name: str = "Санкт-Петербург", temperature: float = 12.5) -> WeatherResult:
    return WeatherResult(
        status="ok",
        source="Open-Meteo",
        location=Location(
            name=name,
            country="Россия",
            admin1=name,
            latitude=59.94,
            longitude=30.31,
            timezone="Europe/Moscow",
        ),
        current=CurrentWeather(
            temperature_c=temperature,
            condition_code=3,
            condition="пасмурно",
            wind_speed=4.2,
            wind_speed_unit="m/s",
            observed_at="2026-10-12T09:00:00+03:00",
            timezone="Europe/Moscow",
        ),
    )


class MemoryReminderStore:
    """Хранилище напоминаний в памяти с той же семантикой ключа идемпотентности."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, dict] = {}

    async def create(self, *, owner_id, key, text, remind_at, timezone) -> StoredReminder:
        if key in self.rows:
            row = self.rows[key]
            if row["owner_id"] != owner_id:
                raise ToolFailure("untrusted_context", "Действие принадлежит другому пользователю.")
            return StoredReminder(row["id"], owner_id, row["remind_at"], created=False)
        self.rows[key] = {
            "id": len(self.rows) + 1,
            "owner_id": owner_id,
            "text": text,
            "remind_at": remind_at,
            "timezone": timezone,
        }
        return StoredReminder(len(self.rows), owner_id, remind_at, created=True)


def schedule_data(**overrides) -> ScheduleFile:
    """Обезличенное расписание для тестов: две недели, праздник, границы семестра."""
    lesson = {"start": "10:00", "end": "11:30", "title": "Матанализ", "location": "ауд. 101"}
    data = {
        "timezone": "Europe/Moscow",
        "term_start": "2026-09-01",
        "term_end": "2026-12-31",
        "cycle_start": "2026-08-31",
        "weeks": [
            {
                "monday": [lesson],
                "wednesday": [{**lesson, "title": "Алгебра", "start": "08:20", "end": "09:50"}],
            },
            {"monday": [{**lesson, "title": "Физика"}]},
        ],
        "overrides": [{"date": "2026-11-04", "lessons": [], "note": "праздник"}],
    }
    data.update(overrides)
    return ScheduleFile.model_validate(data)


class Clock:
    """Подменяемые часы: тесты двигают время без изменения системных часов."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 10, 12, 6, 0, tzinfo=UTC)  # 09:00 по Москве

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def build_test_server(
    *,
    weather: FakeWeather | None = None,
    schedule: ScheduleSource | Exception | None = None,
    reminders: MemoryReminderStore | None = None,
    clock: Clock | None = None,
    secret: str = TRUST_SECRET,
):
    """Настоящий MCP-сервер с фейковыми погодой, расписанием, хранилищем и часами."""
    source = schedule if schedule is not None else ScheduleSource(schedule_data())

    def get_schedule() -> ScheduleSource:
        if isinstance(source, Exception):
            raise source
        return source

    deps = ServerDeps(
        weather=weather or FakeWeather(),  # type: ignore[arg-type]
        schedule=get_schedule,
        reminders=reminders or MemoryReminderStore(),
        trust_secret=secret.encode(),
        clock=clock or Clock(),
    )
    return build_server(deps), deps


def day(value: str) -> date:
    return date.fromisoformat(value)
