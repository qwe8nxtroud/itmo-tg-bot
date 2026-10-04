"""MCP-клиент приложения: обнаружение, ошибки, проверка результата, переподключение."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import MCPError, StdioServerParameters
from mcp.types import CONNECTION_CLOSED

from app.mcp_client import McpGateway, ToolCallError
from mcp_server.errors import ToolFailure
from tests.fakes import TRUST_SECRET, FakeWeather, build_test_server

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def gateway_for():
    started: list[McpGateway] = []

    async def start(target, **options) -> McpGateway:
        gateway = McpGateway(target, trust_secret=TRUST_SECRET, **options)
        await gateway.start(wait=10)
        started.append(gateway)
        return gateway

    yield start
    for gateway in started:
        await gateway.close()


async def test_discovery_builds_specs_from_server(gateway_for):
    server, _ = build_test_server()
    gateway = await gateway_for(server)
    assert gateway.available
    assert {name: spec.read_only for name, spec in gateway.tools.items()} == {
        "get_weather": True,
        "get_schedule": True,
        "add_reminder": False,
    }
    function = gateway.tools["get_schedule"].as_openai_function()
    assert function["function"]["name"] == "get_schedule"
    assert function["function"]["parameters"]["additionalProperties"] is False
    assert gateway.tools["get_weather"].summary == "Текущая погода в городе"
    assert list(gateway.resources) == ["schedule://current-week"]


async def test_call_returns_structured_data(gateway_for):
    server, _ = build_test_server()
    gateway = await gateway_for(server)
    data = await gateway.call_tool("get_schedule", {"date": "2026-10-18"})
    assert data == {"date": "2026-10-18", "timezone": "Europe/Moscow", "lessons": []}


async def test_tool_error_is_mapped_to_code_and_safe_message(gateway_for):
    server, _ = build_test_server(
        weather=FakeWeather(ToolFailure("city_not_found", "Город не найден. Проверьте название."))
    )
    gateway = await gateway_for(server)
    with pytest.raises(ToolCallError) as error:
        await gateway.call_tool("get_weather", {"city": "Нетгорода"})
    assert (error.value.code, error.value.message) == (
        "city_not_found",
        "Город не найден. Проверьте название.",
    )


async def test_unknown_tool_is_not_sent_to_server(gateway_for):
    server, _ = build_test_server()
    gateway = await gateway_for(server)
    with pytest.raises(ToolCallError) as error:
        await gateway.call_tool("drop_database", {})
    assert error.value.code == "unknown_tool"


async def test_result_of_unexpected_structure_is_rejected(gateway_for):
    # Arrange: схема результата требует поле, которого сервер не возвращает.
    server, _ = build_test_server()
    gateway = await gateway_for(server)
    spec = gateway.tools["get_schedule"]
    strict = {**spec.output_schema, "required": [*spec.output_schema["required"], "week"]}
    gateway.tools["get_schedule"] = replace(spec, output_schema=strict)
    # Act / Assert
    with pytest.raises(ToolCallError) as error:
        await gateway.call_tool("get_schedule", {"date": "2026-10-12"})
    assert error.value.code == "bad_result"


async def test_server_that_does_not_start_leaves_bot_without_tools(gateway_for):
    # Arrange: «сервер» сразу завершается с ошибкой.
    target = StdioServerParameters(command=sys.executable, args=["-c", "raise SystemExit(3)"])
    # Act
    gateway = await gateway_for(target, connect_timeout=5, reconnect_delay=60)
    # Assert
    assert not gateway.available
    assert gateway.last_error
    with pytest.raises(ToolCallError) as error:
        await gateway.call_tool("get_weather", {"city": "Казань"})
    assert error.value.code == "mcp_unavailable"
    with pytest.raises(ToolCallError):
        await gateway.read_resource("schedule://current-week")


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionResetError("сервер разорвал соединение"),
        # Так SDK сообщает о смерти дочернего процесса сервера (stdio закрыт).
        MCPError(CONNECTION_CLOSED, "Connection closed"),
    ],
)
async def test_broken_connection_reconnects(gateway_for, failure):
    # Arrange
    server, _ = build_test_server()
    gateway = await gateway_for(server, reconnect_delay=0.05)
    client = gateway._client

    async def broken(*args, **kwargs):
        raise failure

    client.call_tool = broken  # type: ignore[method-assign]
    # Act
    with pytest.raises(ToolCallError) as error:
        await gateway.call_tool("get_schedule", {"date": "2026-10-12"})
    # Assert: ошибка честная, затем супервизор подключается заново.
    assert error.value.code == "mcp_unavailable"
    for _ in range(100):
        if gateway.available and gateway._client is not client:
            break
        await asyncio.sleep(0.02)
    data = await gateway.call_tool("get_schedule", {"date": "2026-10-12"})
    assert data["lessons"][0]["title"] == "Матанализ"


async def test_real_stdio_server_process(gateway_for):
    # Настоящий дочерний процесс `python -m mcp_server` с расписанием из репозитория.
    target = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        env={"SCHEDULE_PATH": str(ROOT / "data" / "schedule.json"), "LOG_LEVEL": "WARNING"},
        cwd=str(ROOT),
    )
    gateway = await gateway_for(target)
    assert set(gateway.tools) == {"get_weather", "get_schedule", "add_reminder"}
    week = await gateway.read_resource("schedule://current-week")
    assert len(week["days"]) == 7
    with pytest.raises(ToolCallError) as error:
        await gateway.call_tool(
            "add_reminder", {"text": "тест", "remind_at": "2030-01-01T10:00:00+03:00"}
        )
    assert error.value.code == "untrusted_context"


async def test_sdk_schema_rejection_is_bad_result_without_reconnect(gateway_for):
    # SDK сам проверяет structuredContent по outputSchema и бросает RuntimeError.
    server, _ = build_test_server()
    gateway = await gateway_for(server, reconnect_delay=0.05)
    client = gateway._client

    async def invalid(*args, **kwargs):
        raise RuntimeError("Invalid structured content returned by tool get_schedule")

    client.call_tool = invalid  # type: ignore[method-assign]
    with pytest.raises(ToolCallError) as error:
        await gateway.call_tool("get_schedule", {"date": "2026-10-12"})
    assert error.value.code == "bad_result"
    assert not error.value.retryable
    assert gateway.available and gateway._client is client, "соединение не пересоздаётся"
