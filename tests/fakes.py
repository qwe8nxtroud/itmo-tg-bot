"""Тестовые замены хранилища и клиента модели: без PostgreSQL и без сети."""

import asyncio
from collections import deque

from app.llm import LLMResponse
from app.storage import HistoryMessage, UserSettings


class FakeStorage:
    """In-memory реализация интерфейса app.storage.Storage с раздельными данными по chat_id."""

    def __init__(self) -> None:
        self.settings: dict[int, UserSettings] = {}
        self.history: dict[int, list[HistoryMessage]] = {}

    async def init_schema(self) -> None:
        pass

    async def get_settings(self, chat_id: int) -> UserSettings | None:
        return self.settings.get(chat_id)

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


class FakeLLM:
    """Отдаёт заранее заданные ответы или исключения и запоминает переданные запросы."""

    model = "test-model"

    def __init__(self, *responses: str | Exception, delay: float = 0.0) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[list[dict[str, str]], float]] = []
        self.delay = delay

    async def complete(self, messages: list[dict[str, str]], *, temperature: float) -> LLMResponse:
        self.calls.append(([dict(message) for message in messages], temperature))
        await asyncio.sleep(self.delay)  # уступаем цикл событий, как настоящий сетевой вызов
        response = self.responses.popleft() if self.responses else "ответ модели"
        if isinstance(response, Exception):
            raise response
        return LLMResponse(text=response, prompt_tokens=10, completion_tokens=5)
