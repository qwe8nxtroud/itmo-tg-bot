"""Запись напоминаний в PostgreSQL с защитой от повторов по ключу идемпотентности."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import asyncpg
from pydantic import BaseModel, Field

from mcp_server.errors import ToolFailure

logger = logging.getLogger("mcp_server.reminders")


class ReminderResult(BaseModel):
    reminder_id: int = Field(description="Идентификатор напоминания")
    text: str
    remind_at: str = Field(description="Время напоминания, ISO 8601 в зоне пользователя")
    timezone: str = Field(description="Часовой пояс пользователя (IANA)")
    created: bool = Field(description="false — запись уже была создана этим же действием")


@dataclass(frozen=True)
class StoredReminder:
    id: int
    owner_id: int
    remind_at: datetime
    created: bool


class ReminderStore(Protocol):
    async def create(
        self, *, owner_id: int, key: uuid.UUID, text: str, remind_at: datetime, timezone: str
    ) -> StoredReminder: ...


@dataclass(frozen=True)
class PgConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


class PgReminderStore:
    """Пул создаётся при первом обращении: инструменты чтения работают и без БД."""

    def __init__(self, config: PgConfig) -> None:
        self._config = config
        self._pool: asyncpg.Pool | None = None
        # Два первых одновременных вызова не должны создать два пула.
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def _get_pool(self) -> asyncpg.Pool:
        async with self._lock:
            if self._pool is None:
                self._pool = await self._create_pool()
        return self._pool

    async def _create_pool(self) -> asyncpg.Pool:
        return await asyncpg.create_pool(
            host=self._config.host,
            port=self._config.port,
            database=self._config.database,
            user=self._config.user,
            password=self._config.password,
            min_size=1,
            max_size=2,
            timeout=5,
            command_timeout=5,
        )

    async def create(
        self, *, owner_id: int, key: uuid.UUID, text: str, remind_at: datetime, timezone: str
    ) -> StoredReminder:
        try:
            pool = await self._get_pool()
            return await insert_reminder(
                pool, owner_id=owner_id, key=key, text=text, remind_at=remind_at, timezone=timezone
            )
        except ToolFailure:
            raise
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            logger.warning("Напоминание не сохранено: %s", type(exc).__name__)
            raise ToolFailure(
                "storage_unavailable", "Хранилище напоминаний сейчас недоступно."
            ) from None


async def insert_reminder(
    pool: asyncpg.Pool,
    *,
    owner_id: int,
    key: uuid.UUID,
    text: str,
    remind_at: datetime,
    timezone: str,
) -> StoredReminder:
    """INSERT … ON CONFLICT по ключу: повтор того же действия возвращает прежнюю запись."""
    row = await pool.fetchrow(
        "INSERT INTO reminders (owner_id, idempotency_key, text, remind_at, timezone) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (idempotency_key) DO NOTHING "
        "RETURNING id, owner_id, remind_at",
        owner_id,
        key,
        text,
        remind_at,
        timezone,
    )
    created = row is not None
    if row is None:
        row = await pool.fetchrow(
            "SELECT id, owner_id, remind_at FROM reminders WHERE idempotency_key = $1", key
        )
    if row is None or row["owner_id"] != owner_id:
        # Ключ занят другим владельцем: такого не бывает при честной подписи, но проверяем.
        raise ToolFailure("untrusted_context", "Действие принадлежит другому пользователю.")
    return StoredReminder(
        id=row["id"], owner_id=row["owner_id"], remind_at=row["remind_at"], created=created
    )
