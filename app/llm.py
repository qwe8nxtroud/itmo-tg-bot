"""Клиент OpenAI-совместимого API (chat completions) поверх aiohttp."""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger("app.llm")


class LLMError(Exception):
    """Базовая ошибка обращения к модели; текст безопасен для логов."""


class LLMTimeoutError(LLMError):
    """Модель не ответила за отведённое время."""


class LLMUnavailableError(LLMError):
    """Сервис недоступен, вернул ошибку или соединение не удалось."""


class LLMEmptyResponseError(LLMError):
    """Ответ не содержит текста или имеет неожиданную структуру."""


@dataclass(frozen=True)
class ToolCall:
    """Предложенный моделью вызов функции; аргументы проверяет приложение."""

    id: str
    name: str
    arguments: object


@dataclass(frozen=True)
class LLMResponse:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class LLMClient:
    """Отправляет список сообщений с ролями и возвращает текст ответа модели."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        max_tokens: int = 0,
        project: str = "",
    ) -> None:
        self._session = session
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        if project:
            # Yandex AI Studio: каталог (folder_id), в котором выпущен ключ сервисного аккаунта.
            self._headers["OpenAI-Project"] = project
        self.model = model
        self._timeout = timeout
        self._max_tokens = max_tokens

    async def complete(self, messages: list[dict[str, str]], *, temperature: float) -> LLMResponse:
        """Обычный ответ текстом (режимы ЛР1)."""
        return await self._request(messages, temperature=temperature, tools=None)

    async def chat(
        self, messages: list[dict], *, temperature: float, tools: list[dict]
    ) -> LLMResponse:
        """Ответ с нативным выбором инструмента: текст или tool_calls."""
        return await self._request(messages, temperature=temperature, tools=tools)

    async def _request(
        self, messages: list[dict], *, temperature: float, tools: list[dict] | None
    ) -> LLMResponse:
        request_id = uuid.uuid4().hex[:8]
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self._max_tokens > 0:
            payload["max_tokens"] = self._max_tokens
        logger.info(
            "LLM %s: запрос к модели %s, сообщений %d, инструментов %d, temperature %.1f",
            request_id,
            self.model,
            len(messages),
            len(tools or []),
            temperature,
        )
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session.post(
                    self._url, json=payload, headers=self._headers
                ) as response:
                    status = response.status
                    if status >= 400:
                        raise LLMUnavailableError(f"сервис вернул HTTP {status}")
                    body = await response.json(content_type=None)
        except TimeoutError:
            logger.warning("LLM %s: таймаут через %.1f с", request_id, self._timeout)
            raise LLMTimeoutError(f"нет ответа за {self._timeout:.0f} с") from None
        except LLMError as exc:
            logger.warning("LLM %s: %s", request_id, exc)
            raise
        except ValueError:
            # Тело ответа не является JSON — ожидаемого текста в нём нет.
            logger.warning("LLM %s: ответ не является JSON", request_id)
            raise LLMEmptyResponseError("ответ модели не удалось разобрать") from None
        except (aiohttp.ClientError, OSError) as exc:
            # Текст исключения перед записью проходит через фильтр секретов в логах.
            logger.warning("LLM %s: ошибка запроса %s: %s", request_id, type(exc).__name__, exc)
            raise LLMUnavailableError("сервис недоступен") from None

        result = _parse_response(body, allow_tool_calls=tools is not None)
        if result is None:
            logger.warning("LLM %s: пустой или неожиданный ответ", request_id)
            raise LLMEmptyResponseError("ответ модели пуст")
        logger.info(
            "LLM %s: ответ за %.2f с, вызовов инструментов %d, токены запрос/ответ: %s/%s",
            request_id,
            time.monotonic() - started,
            len(result.tool_calls),
            result.prompt_tokens,
            result.completion_tokens,
        )
        return result


def _parse_response(body: object, *, allow_tool_calls: bool = False) -> LLMResponse | None:
    """Достаёт текст и вызовы инструментов первого варианта; None — структура неожиданная."""
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    text = content.strip() if isinstance(content, str) else ""
    calls = _parse_tool_calls(message.get("tool_calls")) if allow_tool_calls else ()
    if calls is None or (not text and not calls):
        return None
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return LLMResponse(
        text=text,
        prompt_tokens=_as_int(usage.get("prompt_tokens")),
        completion_tokens=_as_int(usage.get("completion_tokens")),
        tool_calls=calls,
    )


def _parse_tool_calls(raw: object) -> tuple[ToolCall, ...] | None:
    """tool_calls в формате OpenAI; None — если поле есть, но устроено неожиданно."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return None
    calls = []
    for index, item in enumerate(raw):
        function = item.get("function") if isinstance(item, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name:
            return None
        call_id = item.get("id")
        calls.append(
            ToolCall(
                id=call_id if isinstance(call_id, str) and call_id else f"call_{index}",
                name=name,
                arguments=function.get("arguments", "{}"),
            )
        )
    return tuple(calls)


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
