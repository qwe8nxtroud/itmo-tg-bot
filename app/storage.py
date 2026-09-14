"""Хранение настроек пользователей и истории диалогов в PostgreSQL."""

from dataclasses import dataclass

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    chat_id     BIGINT PRIMARY KEY,
    mode        TEXT NOT NULL,
    temperature DOUBLE PRECISION NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dialog_messages (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dialog_messages_chat_id_id_idx ON dialog_messages (chat_id, id);
"""


@dataclass(frozen=True)
class UserSettings:
    mode: str
    temperature: float


@dataclass(frozen=True)
class HistoryMessage:
    role: str
    content: str


class Storage:
    """Все SQL-запросы приложения; данные разделены по идентификатору личного чата."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def init_schema(self) -> None:
        # Повторный запуск безопасен: таблицы создаются только при отсутствии.
        await self._pool.execute(SCHEMA)

    async def get_settings(self, chat_id: int) -> UserSettings | None:
        row = await self._pool.fetchrow(
            "SELECT mode, temperature FROM user_settings WHERE chat_id = $1", chat_id
        )
        return None if row is None else UserSettings(row["mode"], row["temperature"])

    async def save_settings(self, chat_id: int, settings: UserSettings) -> None:
        await self._pool.execute(_UPSERT_SETTINGS, chat_id, settings.mode, settings.temperature)

    async def switch_mode(self, chat_id: int, settings: UserSettings) -> None:
        # Новый режим и очистка истории фиксируются одной транзакцией.
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(_UPSERT_SETTINGS, chat_id, settings.mode, settings.temperature)
            await connection.execute("DELETE FROM dialog_messages WHERE chat_id = $1", chat_id)

    async def get_history(self, chat_id: int, *, limit: int) -> list[HistoryMessage]:
        rows = await self._pool.fetch(
            "SELECT role, content FROM dialog_messages WHERE chat_id = $1 "
            "ORDER BY id DESC LIMIT $2",
            chat_id,
            limit,
        )
        return [HistoryMessage(row["role"], row["content"]) for row in reversed(rows)]

    async def append_exchange(
        self, chat_id: int, user_text: str, assistant_text: str, *, keep: int
    ) -> None:
        # Пара сообщений сохраняется атомарно; в базе остаются последние `keep` записей чата.
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO dialog_messages (chat_id, role, content) "
                "VALUES ($1, 'user', $2), ($1, 'assistant', $3)",
                chat_id,
                user_text,
                assistant_text,
            )
            await connection.execute(
                "DELETE FROM dialog_messages WHERE chat_id = $1 AND id NOT IN "
                "(SELECT id FROM dialog_messages WHERE chat_id = $1 ORDER BY id DESC LIMIT $2)",
                chat_id,
                keep,
            )

    async def clear_history(self, chat_id: int) -> None:
        await self._pool.execute("DELETE FROM dialog_messages WHERE chat_id = $1", chat_id)


_UPSERT_SETTINGS = (
    "INSERT INTO user_settings (chat_id, mode, temperature) VALUES ($1, $2, $3) "
    "ON CONFLICT (chat_id) DO UPDATE SET mode = EXCLUDED.mode, "
    "temperature = EXCLUDED.temperature, updated_at = now()"
)
