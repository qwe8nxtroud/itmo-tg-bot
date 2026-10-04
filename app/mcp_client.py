"""MCP-клиент приложения: подключение к серверу, обнаружение возможностей и вызовы.

Соединением владеет отдельная задача-супервизор: она запускает сервер (stdio), получает
списки инструментов и ресурсов и переподключается с паузой, если сервер упал. Все ошибки
MCP и внешних сервисов приводятся к одному виду — `ToolCallError(code, message)`.
"""

import asyncio
import contextlib
import json
import logging
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import jsonschema
from mcp import Client, MCPError, StdioServerParameters
from mcp.types import CONNECTION_CLOSED

from app.config import Settings
from mcp_server import trust

logger = logging.getLogger("app.mcp")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RETRYABLE = frozenset({"mcp_unavailable", "timeout", "storage_unavailable"})
# Ошибки транспорта: процесс сервера умер или поток закрыт — нужно переподключение.
_TRANSPORT_ERRORS = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    EOFError,
    OSError,
)


class ToolCallError(Exception):
    """Ошибка вызова: код для программы и безопасный текст для пользователя."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


@dataclass(frozen=True)
class ToolSpec:
    """Инструмент в том виде, в каком его сообщил сервер."""

    name: str
    description: str
    input_schema: dict
    output_schema: dict | None
    read_only: bool

    @property
    def summary(self) -> str:
        first = self.description.strip().splitlines()[0] if self.description.strip() else ""
        return first.rstrip(".") or self.name

    def as_openai_function(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": self.input_schema,
            },
        }


@dataclass(frozen=True)
class ResourceSpec:
    uri: str
    name: str
    description: str


def server_environment(settings: Settings, trust_secret: str) -> dict[str, str]:
    """Только то, что нужно серверу: БД для напоминаний, источник расписания, тайм-ауты."""
    return {
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": settings.postgres_db,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password,
        "SCHEDULE_PATH": str(settings.schedule_path),
        "WEATHER_TIMEOUT_SECONDS": str(settings.weather_timeout_seconds),
        "LOG_LEVEL": settings.log_level,
        "PYTHONUNBUFFERED": "1",
        trust.SECRET_ENV: trust_secret,
    }


def stdio_parameters(settings: Settings, trust_secret: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        env=server_environment(settings, trust_secret),
        cwd=str(PROJECT_ROOT),
    )


class McpGateway:
    """Единственное место работы с MCP-сессией."""

    def __init__(
        self,
        target: Any,
        *,
        call_timeout: float = 30.0,
        connect_timeout: float = 20.0,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
        trust_secret: str = "",
    ) -> None:
        # target — StdioServerParameters, URL или объект сервера (в тестах — в памяти).
        self._target = target
        self._call_timeout = call_timeout
        self._connect_timeout = connect_timeout
        self._delay = reconnect_delay
        self._max_delay = max_reconnect_delay
        self.trust_secret = trust_secret.encode("utf-8")
        self._client: Client | None = None
        self._ready = asyncio.Event()
        # Первая попытка подключения завершилась (успешно или нет): старт бота не ждёт дольше.
        self._attempted = asyncio.Event()
        self._broken = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.tools: dict[str, ToolSpec] = {}
        self.resources: dict[str, ResourceSpec] = {}
        self.last_error: str | None = None

    @classmethod
    def for_settings(cls, settings: Settings) -> "McpGateway":
        secret = secrets.token_hex(32)
        return cls(
            stdio_parameters(settings, secret),
            call_timeout=settings.mcp_call_timeout_seconds,
            trust_secret=secret,
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    async def start(self, *, wait: float = 15.0) -> None:
        """Запускает супервизор и ждёт первое подключение не дольше `wait` секунд."""
        self._task = asyncio.create_task(self._supervise(), name="mcp-supervisor")
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait):
                await self._attempted.wait()
        if not self.available:
            logger.warning("MCP: бот работает без инструментов: %s", self.last_error)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _supervise(self) -> None:
        delay = self._delay
        while True:
            try:
                async with contextlib.AsyncExitStack() as stack:
                    async with asyncio.timeout(self._connect_timeout):
                        client = await stack.enter_async_context(Client(self._target))
                        await self._discover(client)
                    self._client = client
                    self.last_error = None
                    self._ready.set()
                    self._attempted.set()
                    delay = self._delay
                    logger.info(
                        "MCP: подключено, инструменты: %s; ресурсы: %s",
                        ", ".join(self.tools),
                        ", ".join(self.resources),
                    )
                    await self._broken.wait()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 — сбой транспорта не должен ронять бота
                # Отмену anyio может завернуть в группу исключений: её нельзя проглотить.
                task = asyncio.current_task()
                if isinstance(exc, KeyboardInterrupt | SystemExit) or (task and task.cancelling()):
                    raise
                self.last_error = _describe(exc)
                logger.warning("MCP: сервер недоступен (%s)", self.last_error)
                self._attempted.set()
            finally:
                self._client = None
                self._broken.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_delay)

    async def _discover(self, client: Client) -> None:
        listed = await client.list_tools()
        tools = {}
        for tool in listed.tools:
            annotations = tool.annotations
            tools[tool.name] = ToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema),
                output_schema=dict(tool.output_schema) if tool.output_schema else None,
                read_only=bool(annotations and annotations.read_only_hint),
            )
        resources = {
            str(item.uri): ResourceSpec(str(item.uri), item.name, item.description or "")
            for item in (await client.list_resources()).resources
        }
        # Последний известный список остаётся после разрыва: модель может попросить
        # инструмент, и пользователь получит честное «сейчас недоступен».
        self.tools, self.resources = tools, resources

    def _classify(self, exc: Exception, target: str) -> ToolCallError:
        """Транспортный сбой → переподключение; ошибка живого сервера → её код."""
        if isinstance(exc, _TRANSPORT_ERRORS) or (
            isinstance(exc, MCPError) and exc.code == CONNECTION_CLOSED
        ):
            self._mark_broken(exc)
            return ToolCallError("mcp_unavailable", "Инструменты сейчас недоступны.")
        if isinstance(exc, MCPError):
            # Например, битый файл расписания при чтении ресурса.
            return _error_from_text(exc.message)
        # SDK сам проверяет structuredContent по outputSchema и бросает RuntimeError:
        # соединение исправно, неверны данные.
        logger.warning("MCP: ответ %s отклонён проверкой (%s)", target, type(exc).__name__)
        return ToolCallError("bad_result", "Инструмент вернул данные неожиданного вида.")

    def _mark_broken(self, exc: BaseException) -> None:
        self.last_error = _describe(exc)
        logger.warning("MCP: соединение потеряно (%s), переподключаемся", self.last_error)
        self._broken.set()

    async def call_tool(
        self, name: str, arguments: dict, *, meta: Mapping[str, Any] | None = None
    ) -> dict:
        client = self._client
        if client is None:
            raise ToolCallError("mcp_unavailable", "Инструменты сейчас недоступны.")
        spec = self.tools.get(name)
        if spec is None:
            raise ToolCallError("unknown_tool", "Такого инструмента у сервера нет.")
        try:
            async with asyncio.timeout(self._call_timeout):
                result = await client.call_tool(name, arguments, meta=dict(meta) if meta else None)
        except TimeoutError:
            raise ToolCallError("timeout", "Инструмент не ответил вовремя.") from None
        except Exception as exc:
            raise self._classify(exc, name) from None
        if result.is_error:
            raise _error_from_content(result.content)
        data = result.structured_content
        if not isinstance(data, dict):
            raise ToolCallError("bad_result", "Инструмент вернул данные неожиданного вида.")
        if spec.output_schema:
            try:
                jsonschema.validate(data, spec.output_schema)
            except jsonschema.ValidationError:
                logger.warning("MCP: результат %s не соответствует outputSchema", name)
                raise ToolCallError(
                    "bad_result", "Инструмент вернул данные неожиданного вида."
                ) from None
        return data

    async def read_resource(self, uri: str) -> dict:
        client = self._client
        if client is None:
            raise ToolCallError("mcp_unavailable", "Инструменты сейчас недоступны.")
        if uri not in self.resources:
            raise ToolCallError("unknown_resource", "Такого ресурса у сервера нет.")
        try:
            async with asyncio.timeout(self._call_timeout):
                result = await client.read_resource(uri)
        except TimeoutError:
            raise ToolCallError("timeout", "Сервер не ответил вовремя.") from None
        except Exception as exc:
            raise self._classify(exc, uri) from None
        try:
            data = json.loads(result.contents[0].text)  # type: ignore[union-attr]
        except (IndexError, AttributeError, ValueError):
            raise ToolCallError("bad_result", "Ресурс вернул данные неожиданного вида.") from None
        if not isinstance(data, dict):
            raise ToolCallError("bad_result", "Ресурс вернул данные неожиданного вида.")
        return data


def _error_from_content(content: list) -> ToolCallError:
    text = " ".join(getattr(block, "text", "") for block in content)
    return _error_from_text(text)


def _error_from_text(text: str) -> ToolCallError:
    """Сервер кладёт JSON {"code", "message"}; SDK может добавить перед ним префикс."""
    start = text.find("{")
    if start != -1:
        try:
            payload = json.loads(text[start:])
        except ValueError:
            payload = None
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("code"), str)
            and isinstance(payload.get("message"), str)
        ):
            return ToolCallError(payload["code"], payload["message"])
    # Например, ошибка схемы на стороне SDK: детали в журнал не тянем.
    return ToolCallError("tool_error", "Инструмент отклонил запрос.")


def _describe(exc: BaseException) -> str:
    """Причина сбоя для журнала: anyio заворачивает исходную ошибку в группу исключений."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {_short(exc)}"


def _short(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:200] if text else "без описания"
