"""Сервис ассистента: настройки пользователя, сборка контекста и обращение к модели."""

from dataclasses import replace

from app.llm import LLMClient
from app.prompts import DEFAULT_MODE, MODES, Mode
from app.storage import HistoryMessage, Storage, UserSettings

TEMPERATURES = (0.0, 0.3, 0.7, 1.0)
DEFAULT_TEMPERATURE = 0.7


def build_messages(
    mode: Mode,
    history: list[HistoryMessage],
    user_text: str,
    *,
    max_messages: int,
    max_chars: int,
) -> list[dict[str, str]]:
    """Инструкция режима, few-shot-примеры, усечённая история и текущий запрос — по порядку."""
    fixed = [{"role": "system", "content": mode.system_prompt}]
    for question, answer in mode.few_shot:
        fixed.append({"role": "user", "content": question})
        fixed.append({"role": "assistant", "content": answer})
    reserved = sum(len(message["content"]) for message in fixed) + len(user_text)
    trimmed = trim_history(
        history, max_messages=max_messages, max_chars=max(0, max_chars - reserved)
    )
    return [
        *fixed,
        *({"role": message.role, "content": message.content} for message in trimmed),
        {"role": "user", "content": user_text},
    ]


def trim_history(
    history: list[HistoryMessage], *, max_messages: int, max_chars: int
) -> list[HistoryMessage]:
    """Оставляет самые новые сообщения: не больше max_messages и не длиннее max_chars.

    Порядок сохраняется. Объём оценивается по числу символов. Если после усечения
    история начинается с ответа ассистента без вопроса, этот ответ тоже отбрасывается.
    """
    kept = history[-max_messages:] if max_messages > 0 else []
    total = sum(len(message.content) for message in kept)
    while kept and total > max_chars:
        total -= len(kept[0].content)
        kept = kept[1:]
    while kept and kept[0].role != "user":
        kept = kept[1:]
    return kept


class Assistant:
    """Связывает хранилище и клиент модели; обработчики Telegram работают только с ним."""

    def __init__(
        self,
        storage: Storage,
        llm: LLMClient,
        *,
        history_max_messages: int,
        history_max_chars: int,
    ) -> None:
        self._storage = storage
        self._llm = llm
        self._history_max_messages = history_max_messages
        self._history_max_chars = history_max_chars

    @property
    def model(self) -> str:
        return self._llm.model

    async def get_settings(self, chat_id: int) -> UserSettings:
        settings = await self._storage.get_settings(chat_id)
        if settings is None:
            return UserSettings(mode=DEFAULT_MODE, temperature=DEFAULT_TEMPERATURE)
        if settings.mode not in MODES:
            # Режим мог быть переименован в коде после сохранения; не ломаем диалог.
            return replace(settings, mode=DEFAULT_MODE)
        return settings

    async def switch_mode(self, chat_id: int, mode: str) -> Mode:
        if mode not in MODES:
            raise ValueError(f"неизвестный режим: {mode}")
        settings = await self.get_settings(chat_id)
        await self._storage.switch_mode(chat_id, replace(settings, mode=mode))
        return MODES[mode]

    async def set_temperature(self, chat_id: int, temperature: float) -> None:
        if temperature not in TEMPERATURES:
            raise ValueError(f"недопустимое значение temperature: {temperature}")
        settings = await self.get_settings(chat_id)
        await self._storage.save_settings(chat_id, replace(settings, temperature=temperature))

    async def reset(self, chat_id: int) -> None:
        await self._storage.clear_history(chat_id)

    async def answer(self, chat_id: int, user_text: str) -> str:
        """Отвечает в активном режиме; при ошибке модели история не изменяется."""
        settings = await self.get_settings(chat_id)
        history = await self._storage.get_history(chat_id, limit=self._history_max_messages)
        messages = build_messages(
            MODES[settings.mode],
            history,
            user_text,
            max_messages=self._history_max_messages,
            max_chars=self._history_max_chars,
        )
        response = await self._llm.complete(messages, temperature=settings.temperature)
        await self._storage.append_exchange(
            chat_id, user_text, response.text, keep=self._history_max_messages
        )
        return response.text
