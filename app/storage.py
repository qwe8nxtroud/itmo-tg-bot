"""Хранение настроек, истории диалогов, действий агента и аудита в PostgreSQL."""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Произвольная константа: блокировка не даёт двум экземплярам применять миграции одновременно.
_MIGRATION_LOCK = 724_001
# Пространство блокировок «подготовка действия пользователя» (двухключевая форма).
_ACTION_LOCK_SPACE = 7240
EVENTS_PER_USER = 50
# Действие в «выполняется» дольше этого времени считается зависшим и может быть повторено.
STALE_EXECUTION = timedelta(seconds=60)


@dataclass(frozen=True)
class UserSettings:
    mode: str
    temperature: float


@dataclass(frozen=True)
class HistoryMessage:
    role: str
    content: str


@dataclass(frozen=True)
class PendingAction:
    """Подготовленное действие с побочным эффектом и его состояние подтверждения."""

    id: uuid.UUID
    user_id: int
    chat_id: int
    tool: str
    arguments: dict
    timezone: str
    status: str
    expires_at: datetime
    result: dict | None = None
    confirmed_at: datetime | None = None


@dataclass(frozen=True)
class AgentEvent:
    """Запись аудита: что агент решил и чем закончилось, без текста сообщения."""

    request_id: str
    kind: str
    action: str
    validation: str
    execution: str
    duration_ms: int
    created_at: datetime
    tools: str | None = None
    args_summary: str | None = None
    reason: str | None = None
    tool_calls: int = 0


async def apply_migrations(pool: asyncpg.Pool) -> list[str]:
    """Применяет недостающие миграции по порядку номера; возвращает применённые сейчас."""
    applied_now: list[str] = []
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK)
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {
            row["version"]
            for row in await connection.fetch("SELECT version FROM schema_migrations")
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            await connection.execute(path.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1)", path.stem
            )
            applied_now.append(path.stem)
    return applied_now


class Storage:
    """Все SQL-запросы приложения; данные разделены по чату и пользователю Telegram."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def init_schema(self) -> None:
        # Повторный запуск безопасен: применяются только новые миграции.
        await apply_migrations(self._pool)

    # --- ЛР1: настройки и история -------------------------------------------------

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

    # --- ЛР2: часовой пояс ----------------------------------------------------------

    async def get_timezone(self, user_id: int) -> str | None:
        return await self._pool.fetchval(
            "SELECT timezone FROM user_profiles WHERE user_id = $1", user_id
        )

    async def set_timezone(self, user_id: int, timezone: str) -> None:
        await self._pool.execute(
            "INSERT INTO user_profiles (user_id, timezone) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET timezone = EXCLUDED.timezone, updated_at = now()",
            user_id,
            timezone,
        )

    # --- ЛР2: подтверждаемые действия -----------------------------------------------

    async def prepare_action(
        self,
        *,
        user_id: int,
        chat_id: int,
        source_message_id: int,
        tool: str,
        arguments: dict,
        timezone: str,
        now: datetime,
        ttl: timedelta,
    ) -> tuple[PendingAction, bool]:
        """Создаёт действие для сообщения или возвращает уже созданное (повтор update).

        Второй элемент — True, если действие создано сейчас. Новое действие отменяет
        прежние ожидающие действия того же пользователя: подтвердить можно только последнее.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            # Сообщения одного пользователя обрабатываются параллельно: без блокировки два
            # одновременных запроса оставили бы две действующие карточки.
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1, hashtext($2::bigint::text))",
                _ACTION_LOCK_SPACE,
                user_id,
            )
            row = await connection.fetchrow(
                "INSERT INTO pending_actions (id, user_id, chat_id, source_message_id, tool, "
                "arguments, timezone, status, created_at, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'pending', $8, $9) "
                "ON CONFLICT (chat_id, source_message_id) DO NOTHING RETURNING *",
                uuid.uuid4(),
                user_id,
                chat_id,
                source_message_id,
                tool,
                json.dumps(arguments, ensure_ascii=False),
                timezone,
                now,
                now + ttl,
            )
            if row is None:
                row = await connection.fetchrow(
                    "SELECT * FROM pending_actions WHERE chat_id = $1 AND source_message_id = $2",
                    chat_id,
                    source_message_id,
                )
                return _action(row), False
            await connection.execute(
                "UPDATE pending_actions SET status = 'cancelled', updated_at = now() "
                "WHERE user_id = $1 AND status = 'pending' AND id <> $2",
                user_id,
                row["id"],
            )
            return _action(row), True

    async def claim_action(
        self, action_id: uuid.UUID, *, user_id: int, chat_id: int, now: datetime
    ) -> PendingAction | None:
        """Атомарно переводит своё действие в «выполняется»; иначе None.

        Можно: ожидающее подтверждения в течение срока; уже подтверждённое (повтор после
        временного сбоя — в любое время); зависшее в «выполняется» дольше STALE_EXECUTION.
        """
        row = await self._pool.fetchrow(
            "UPDATE pending_actions SET status = 'executing', updated_at = $4, "
            "confirmed_at = COALESCE(confirmed_at, $4) "
            "WHERE id = $1 AND user_id = $2 AND chat_id = $3 AND ("
            "(status = 'pending' AND (expires_at > $4 OR confirmed_at IS NOT NULL)) "
            "OR (status = 'executing' AND updated_at < $5)) RETURNING *",
            action_id,
            user_id,
            chat_id,
            now,
            now - STALE_EXECUTION,
        )
        return None if row is None else _action(row)

    async def get_action(self, action_id: uuid.UUID, *, user_id: int) -> PendingAction | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM pending_actions WHERE id = $1 AND user_id = $2", action_id, user_id
        )
        return None if row is None else _action(row)

    async def expire_action(self, action_id: uuid.UUID, *, user_id: int, now: datetime) -> bool:
        status = await self._pool.fetchval(
            "UPDATE pending_actions SET status = 'expired', updated_at = now() "
            "WHERE id = $1 AND user_id = $2 AND status = 'pending' AND expires_at <= $3 "
            "AND confirmed_at IS NULL RETURNING status",
            action_id,
            user_id,
            now,
        )
        return status is not None

    async def cancel_action(self, action_id: uuid.UUID, *, user_id: int, chat_id: int) -> bool:
        status = await self._pool.fetchval(
            "UPDATE pending_actions SET status = 'cancelled', updated_at = now() "
            "WHERE id = $1 AND user_id = $2 AND chat_id = $3 AND status = 'pending' "
            "RETURNING status",
            action_id,
            user_id,
            chat_id,
        )
        return status is not None

    async def finish_action(
        self, action_id: uuid.UUID, *, status: str, result: dict | None = None
    ) -> None:
        """Завершает выполняемое действие: done/failed — окончательно, pending — для повтора."""
        await self._pool.execute(
            "UPDATE pending_actions SET status = $2, result = $3::jsonb, updated_at = now() "
            "WHERE id = $1 AND status = 'executing'",
            action_id,
            status,
            None if result is None else json.dumps(result, ensure_ascii=False),
        )

    # --- ЛР2: аудит ------------------------------------------------------------------

    async def add_event(self, user_id: int, event: AgentEvent) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO agent_events (user_id, request_id, kind, action, tools, "
                "args_summary, validation, execution, reason, tool_calls, duration_ms, "
                "created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                user_id,
                event.request_id,
                event.kind,
                event.action,
                event.tools,
                event.args_summary,
                event.validation,
                event.execution,
                event.reason,
                event.tool_calls,
                event.duration_ms,
                event.created_at,
            )
            await connection.execute(
                "DELETE FROM agent_events WHERE user_id = $1 AND id NOT IN "
                "(SELECT id FROM agent_events WHERE user_id = $1 ORDER BY id DESC LIMIT $2)",
                user_id,
                EVENTS_PER_USER,
            )

    async def last_message_event(self, user_id: int) -> AgentEvent | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM agent_events WHERE user_id = $1 AND kind = 'message' "
            "ORDER BY id DESC LIMIT 1",
            user_id,
        )
        if row is None:
            return None
        return AgentEvent(
            request_id=row["request_id"],
            kind=row["kind"],
            action=row["action"],
            validation=row["validation"],
            execution=row["execution"],
            duration_ms=row["duration_ms"],
            created_at=row["created_at"],
            tools=row["tools"],
            args_summary=row["args_summary"],
            reason=row["reason"],
            tool_calls=row["tool_calls"],
        )


def _action(row: asyncpg.Record) -> PendingAction:
    result = row["result"]
    return PendingAction(
        id=row["id"],
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        tool=row["tool"],
        arguments=json.loads(row["arguments"]),
        timezone=row["timezone"],
        status=row["status"],
        expires_at=row["expires_at"],
        result=None if result is None else json.loads(result),
        confirmed_at=row["confirmed_at"],
    )


_UPSERT_SETTINGS = (
    "INSERT INTO user_settings (chat_id, mode, temperature) VALUES ($1, $2, $3) "
    "ON CONFLICT (chat_id) DO UPDATE SET mode = EXCLUDED.mode, "
    "temperature = EXCLUDED.temperature, updated_at = now()"
)
