"""MCP-сервер через настоящий MCP-клиент (в памяти): схемы, ошибки, доверенный контекст."""

import json
import uuid

import pytest
from mcp import Client

from mcp_server import trust
from mcp_server.errors import ToolFailure
from tests.fakes import TRUST_SECRET, Clock, FakeWeather, MemoryReminderStore, build_test_server

OWNER = 1001
FUTURE = "2026-10-13T18:30:00+03:00"


def error_of(result) -> dict:
    assert result.is_error
    text = result.content[0].text
    return json.loads(text[text.index("{") :])


def signed_meta(arguments: dict, *, owner=OWNER, action_id=None, clock=None, secret=TRUST_SECRET):
    clock = clock or Clock()
    token = trust.sign(
        secret.encode(),
        trust.TrustedContext(
            owner_id=owner, action_id=action_id or uuid.uuid4(), timezone="Europe/Moscow"
        ),
        arguments,
        expires_at=int(clock().timestamp()) + 60,
    )
    return {trust.META_KEY: token}


async def test_discovery_lists_three_tools_with_strict_schemas_and_resource():
    server, _ = build_test_server()
    async with Client(server) as client:
        # Act
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        resources = (await client.list_resources()).resources
    # Assert
    assert set(tools) == {"get_weather", "get_schedule", "add_reminder"}
    for tool in tools.values():
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["type"] == "object"
        assert tool.description
    assert tools["get_weather"].input_schema["properties"]["city"]["maxLength"] == 100
    assert tools["get_schedule"].input_schema["required"] == ["date"]
    assert set(tools["add_reminder"].input_schema["properties"]) == {"text", "remind_at"}
    assert tools["add_reminder"].input_schema["properties"]["text"]["maxLength"] == 500
    assert tools["get_weather"].annotations.read_only_hint is True
    assert tools["add_reminder"].annotations.read_only_hint is False
    assert [(str(r.uri), r.mime_type) for r in resources] == [
        ("schedule://current-week", "application/json")
    ]


async def test_weather_result_is_structured():
    server, deps = build_test_server()
    async with Client(server) as client:
        result = await client.call_tool("get_weather", {"city": "  Санкт-Петербург  "})
    assert not result.is_error
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["current"]["temperature_c"] == 12.5
    assert deps.weather.queries == ["Санкт-Петербург"], "крайние пробелы удалены"


async def test_weather_failure_has_uniform_error():
    server, _ = build_test_server(
        weather=FakeWeather(ToolFailure("weather_timeout", "Погодный сервис не ответил вовремя."))
    )
    async with Client(server) as client:
        result = await client.call_tool("get_weather", {"city": "Казань"})
    assert error_of(result) == {
        "code": "weather_timeout",
        "message": "Погодный сервис не ответил вовремя.",
    }


@pytest.mark.parametrize("city", ["", "   ", "x" * 101])
async def test_weather_rejects_bad_city(city):
    server, deps = build_test_server()
    async with Client(server) as client:
        result = await client.call_tool("get_weather", {"city": city})
    assert result.is_error
    assert deps.weather.queries == []


async def test_schedule_for_date():
    server, _ = build_test_server()
    async with Client(server) as client:
        result = await client.call_tool("get_schedule", {"date": "2026-10-12"})
    assert result.structured_content["lessons"][0]["title"] == "Матанализ"


@pytest.mark.parametrize("value", ["12.10.2026", "2026-13-01", "завтра"])
async def test_schedule_rejects_bad_date(value):
    server, _ = build_test_server()
    async with Client(server) as client:
        result = await client.call_tool("get_schedule", {"date": value})
    assert result.is_error


async def test_schedule_unavailable_when_file_broken():
    server, _ = build_test_server(
        schedule=ToolFailure("schedule_unavailable", "Расписание сейчас недоступно.")
    )
    async with Client(server) as client:
        result = await client.call_tool("get_schedule", {"date": "2026-10-12"})
    assert error_of(result)["code"] == "schedule_unavailable"


async def test_extra_arguments_are_rejected_by_server():
    # Arrange: попытка передать владельца аргументом модели.
    server, deps = build_test_server()
    arguments = {"city": "Казань", "owner_id": 42}
    async with Client(server) as client:
        result = await client.call_tool("get_weather", arguments)
    assert error_of(result) == {
        "code": "invalid_arguments",
        "message": "Лишние аргументы: owner_id.",
    }
    assert deps.weather.queries == []


async def test_week_resource_returns_current_week():
    server, _ = build_test_server()
    async with Client(server) as client:
        result = await client.read_resource("schedule://current-week")
    week = json.loads(result.contents[0].text)
    assert (week["week_start"], week["week_end"]) == ("2026-10-12", "2026-10-18")
    assert [d["weekday"] for d in week["days"]][:2] == ["понедельник", "вторник"]


# --- add_reminder: доверенный контекст и идемпотентность ----------------------------------


async def test_add_reminder_without_trusted_context_is_rejected():
    # Посторонний клиент (как MCP Inspector) не может создать напоминание.
    store = MemoryReminderStore()
    server, _ = build_test_server(reminders=store)
    async with Client(server) as client:
        result = await client.call_tool("add_reminder", {"text": "тест", "remind_at": FUTURE})
    assert error_of(result)["code"] == "untrusted_context"
    assert store.rows == {}


async def test_add_reminder_with_trusted_context_creates_record():
    # Arrange
    store = MemoryReminderStore()
    server, _ = build_test_server(reminders=store)
    arguments = {"text": "отправить отчёт", "remind_at": FUTURE}
    async with Client(server) as client:
        # Act
        result = await client.call_tool("add_reminder", arguments, meta=signed_meta(arguments))
    # Assert
    assert result.structured_content == {
        "reminder_id": 1,
        "text": "отправить отчёт",
        "remind_at": FUTURE,
        "timezone": "Europe/Moscow",
        "created": True,
    }
    (row,) = store.rows.values()
    assert row["owner_id"] == OWNER


async def test_add_reminder_is_idempotent():
    # Arrange: один и тот же подтверждённый action_id дважды.
    store = MemoryReminderStore()
    server, _ = build_test_server(reminders=store)
    arguments = {"text": "сделать перерыв", "remind_at": FUTURE}
    action_id = uuid.uuid4()
    async with Client(server) as client:
        first = await client.call_tool(
            "add_reminder", arguments, meta=signed_meta(arguments, action_id=action_id)
        )
        second = await client.call_tool(
            "add_reminder", arguments, meta=signed_meta(arguments, action_id=action_id)
        )
    # Assert
    assert first.structured_content["created"] is True
    assert second.structured_content["created"] is False
    assert second.structured_content["reminder_id"] == first.structured_content["reminder_id"]
    assert len(store.rows) == 1


@pytest.mark.parametrize(
    "tamper",
    ["other_arguments", "wrong_secret", "expired", "garbage"],
)
async def test_add_reminder_rejects_forged_context(tamper):
    # Arrange
    store = MemoryReminderStore()
    clock = Clock()
    server, _ = build_test_server(reminders=store, clock=clock)
    arguments = {"text": "оплатить общежитие", "remind_at": FUTURE}
    meta = signed_meta(arguments, clock=clock)
    if tamper == "other_arguments":
        arguments = {**arguments, "text": "чужой текст"}
    elif tamper == "wrong_secret":
        meta = signed_meta(arguments, secret="угаданный секрет")
    elif tamper == "expired":
        clock.advance(minutes=2)
    else:
        meta = {trust.META_KEY: "abc.def"}
    async with Client(server) as client:
        # Act
        result = await client.call_tool("add_reminder", arguments, meta=meta)
    # Assert
    assert error_of(result)["code"] == "untrusted_context"
    assert store.rows == {}


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"text": "поздно", "remind_at": "2026-10-11T18:30:00+03:00"}, "past_time"),
        ({"text": "без зоны", "remind_at": "2026-10-13T18:30:00"}, "invalid_time"),
    ],
)
async def test_add_reminder_validates_time(arguments, code):
    store = MemoryReminderStore()
    server, _ = build_test_server(reminders=store)
    async with Client(server) as client:
        result = await client.call_tool("add_reminder", arguments, meta=signed_meta(arguments))
    assert error_of(result)["code"] == code
    assert store.rows == {}


async def test_add_reminder_rejects_empty_text():
    store = MemoryReminderStore()
    server, _ = build_test_server(reminders=store)
    arguments = {"text": "   ", "remind_at": FUTURE}
    async with Client(server) as client:
        result = await client.call_tool("add_reminder", arguments, meta=signed_meta(arguments))
    assert result.is_error
    assert store.rows == {}
