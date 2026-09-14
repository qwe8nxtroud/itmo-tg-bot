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
class LLMResponse:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


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
    ) -> None:
        self._session = session
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.model = model
        self._timeout = timeout
        self._max_tokens = max_tokens

    async def complete(self, messages: list[dict[str, str]], *, temperature: float) -> LLMResponse:
        request_id = uuid.uuid4().hex[:8]
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self._max_tokens > 0:
            payload["max_tokens"] = self._max_tokens
        logger.info(
            "LLM %s: запрос к модели %s, сообщений %d, temperature %.1f",
            request_id,
            self.model,
            len(messages),
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

        result = _parse_response(body)
        if result is None:
            logger.warning("LLM %s: пустой или неожиданный ответ", request_id)
            raise LLMEmptyResponseError("ответ модели пуст")
        logger.info(
            "LLM %s: ответ за %.2f с, токены запрос/ответ: %s/%s",
            request_id,
            time.monotonic() - started,
            result.prompt_tokens,
            result.completion_tokens,
        )
        return result


def _parse_response(body: object) -> LLMResponse | None:
    """Достаёт текст первого варианта ответа; None — если структура неожиданная."""
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        return None
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return LLMResponse(
        text=content.strip(),
        prompt_tokens=_as_int(usage.get("prompt_tokens")),
        completion_tokens=_as_int(usage.get("completion_tokens")),
    )


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
