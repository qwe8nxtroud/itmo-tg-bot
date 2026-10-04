"""ЛР2 на настоящем PostgreSQL: миграции, защита от дублей и изоляция пользователей.

Фиктивное хранилище не доказывает работу ограничений и атомарных UPDATE в БД, поэтому
эти проверки идут на PostgreSQL. Два способа запуска:

    RUN_INTEGRATION=1 python -m pytest -m integration -v      # свой контейнер Docker
    TEST_POSTGRES_DSN=postgresql://user:pass@127.0.0.1:5432/postgres \\
        python -m pytest -m integration -v                     # готовый сервер, временная БД
"""

import asyncio
import os
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import asyncpg
import pytest

from app.agent import Agent, AgentRequest
from app.assistant import Assistant
from app.mcp_client import McpGateway
from app.storage import AgentEvent, Storage, apply_migrations
from mcp_server.errors import ToolFailure
from mcp_server.reminders import PgConfig, PgReminderStore, insert_reminder
from tests.conftest import ALICE, BOB
from tests.fakes import TRUST_SECRET, Clock, FakeLLM, build_test_server, call

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1" and not os.environ.get("TEST_POSTGRES_DSN"),
        reason="Включите RUN_INTEGRATION=1 (Docker) или задайте TEST_POSTGRES_DSN",
    ),
]

LAB1_SCHEMA = """
CREATE TABLE user_settings (
    chat_id BIGINT PRIMARY KEY, mode TEXT NOT NULL, temperature DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE dialog_messages (
    id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')), content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@pytest.fixture
async def pg_config(request) -> PgConfig:
    """Пустая база: временная на готовом сервере или в собственном контейнере."""
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        settings, *_ = request.getfixturevalue("database")
        yield PgConfig(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.postgres_user,
            settings.postgres_password,
        )
        return
    parts = urlsplit(dsn)
    name = "itmo_lab2_" + secrets.token_hex(4)
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield PgConfig(
            parts.hostname, parts.port or 5432, name, parts.username, parts.password or ""
        )
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def pool(pg_config):
    pool = await asyncpg.create_pool(
        host=pg_config.host,
        port=pg_config.port,
        database=pg_config.database,
        user=pg_config.user,
        password=pg_config.password,
        min_size=1,
        max_size=10,
    )
    try:
        yield pool
    finally:
        await pool.close()


async def test_migrations_upgrade_lab1_database_and_are_idempotent(pool):
    # Arrange: база в состоянии ЛР1 (таблицы без schema_migrations) с данными.
    await pool.execute(LAB1_SCHEMA)
    await pool.execute(
        "INSERT INTO user_settings (chat_id, mode, temperature) VALUES (1, 'quiz', 0.3)"
    )
    # Act
    first = await apply_migrations(pool)
    second = await apply_migrations(pool)
    # Assert
    assert (first, second) == (["001_lab1", "002_lab2"], [])
    assert await pool.fetchval("SELECT mode FROM user_settings WHERE chat_id = 1") == "quiz"
    tables = {
        row["tablename"]
        for row in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert {"user_profiles", "pending_actions", "reminders", "agent_events"} <= tables


async def test_reminder_idempotency_pg(pool):
    # Arrange
    await apply_migrations(pool)
    key = uuid.uuid4()
    when = Clock()() + timedelta(days=1)
    # Act
    first = await insert_reminder(
        pool, owner_id=ALICE, key=key, text="сдать отчёт", remind_at=when, timezone="Europe/Moscow"
    )
    second = await insert_reminder(
        pool, owner_id=ALICE, key=key, text="сдать отчёт", remind_at=when, timezone="Europe/Moscow"
    )
    # Assert
    assert (first.created, second.created, first.id) == (True, False, second.id)
    assert await pool.fetchval("SELECT count(*) FROM reminders") == 1
    with pytest.raises(ToolFailure) as error:
        await insert_reminder(
            pool, owner_id=BOB, key=key, text="чужое", remind_at=when, timezone="Europe/Moscow"
        )
    assert error.value.code == "untrusted_context"


async def test_concurrent_repeated_update_prepares_one_action_pg(pool):
    # Arrange: один и тот же update обрабатывается пять раз одновременно.
    await apply_migrations(pool)
    storage = Storage(pool)
    clock = Clock()

    async def prepare():
        return await storage.prepare_action(
            user_id=ALICE,
            chat_id=ALICE,
            source_message_id=555,
            tool="add_reminder",
            arguments={"text": "сдать отчёт", "remind_at": "2026-10-13T18:30:00+03:00"},
            timezone="Europe/Moscow",
            now=clock(),
            ttl=timedelta(minutes=5),
        )

    # Act
    results = await asyncio.gather(*(prepare() for _ in range(5)))
    # Assert
    assert len({action.id for action, _ in results}) == 1
    assert [created for _, created in results].count(True) == 1
    assert await pool.fetchval("SELECT count(*) FROM pending_actions") == 1


async def test_concurrent_double_confirmation_creates_one_reminder_pg(pool, pg_config):
    # Arrange: весь путь — агент, настоящий MCP-сервер и PgReminderStore на одной БД.
    await apply_migrations(pool)
    storage = Storage(pool)
    clock = Clock()
    store = PgReminderStore(pg_config)
    server, _ = build_test_server(reminders=store, clock=clock)
    gateway = McpGateway(server, trust_secret=TRUST_SECRET)
    await gateway.start(wait=10)
    llm = FakeLLM(
        call("add_reminder", {"text": "сдать отчёт", "remind_at": "2026-10-13T18:30:00+03:00"})
    )
    assistant = Assistant(storage, llm, history_max_messages=20, history_max_chars=12000)  # type: ignore[arg-type]
    agent = Agent(
        assistant,
        storage,
        llm,  # type: ignore[arg-type]
        gateway,
        temperature=0.2,
        history_max_messages=20,
        history_max_chars=12000,
        clock=clock,
    )
    try:
        await storage.set_timezone(ALICE, "Europe/Moscow")
        reply = await agent.reply(
            AgentRequest(user_id=ALICE, chat_id=ALICE, message_id=1, text="Напомни…")
        )
        # Act: пять одновременных нажатий «Подтвердить»
        presses = await asyncio.gather(
            *(
                agent.confirm(reply.action.id, user_id=ALICE, chat_id=ALICE, request_id=str(i))
                for i in range(5)
            )
        )
        # Assert
        created = [p.text for p in presses if p.text.startswith("Напоминание создано: #")]
        assert len(created) == 1
        assert await pool.fetchval("SELECT count(*) FROM reminders") == 1
        row = await pool.fetchrow("SELECT owner_id, idempotency_key, text FROM reminders")
        assert (row["owner_id"], row["idempotency_key"], row["text"]) == (
            ALICE,
            reply.action.id,
            "сдать отчёт",
        )
        status = await pool.fetchval(
            "SELECT status FROM pending_actions WHERE id = $1", reply.action.id
        )
        assert status == "done"
    finally:
        await gateway.close()
        await store.close()


async def test_users_are_isolated_pg(pool):
    # Arrange
    await apply_migrations(pool)
    storage = Storage(pool)
    clock = Clock()
    await storage.set_timezone(ALICE, "Europe/Moscow")
    action, _ = await storage.prepare_action(
        user_id=ALICE,
        chat_id=ALICE,
        source_message_id=1,
        tool="add_reminder",
        arguments={"text": "личное", "remind_at": "2026-10-13T18:30:00+03:00"},
        timezone="Europe/Moscow",
        now=clock(),
        ttl=timedelta(minutes=5),
    )
    await storage.add_event(
        ALICE,
        AgentEvent(
            request_id="r1",
            kind="message",
            action="prepare_action",
            validation="пройдена",
            execution="ждёт подтверждения",
            duration_ms=5,
            created_at=clock(),
        ),
    )
    # Act / Assert: Боб не видит, не подтверждает и не отменяет действие Алисы.
    assert await storage.get_action(action.id, user_id=BOB) is None
    assert await storage.claim_action(action.id, user_id=BOB, chat_id=BOB, now=clock()) is None
    assert await storage.cancel_action(action.id, user_id=BOB, chat_id=BOB) is False
    assert await storage.last_message_event(BOB) is None
    assert await storage.get_timezone(BOB) is None
    assert (await storage.get_action(action.id, user_id=ALICE)).status == "pending"
    # Act / Assert: просрочка через подменяемые часы
    later = clock() + timedelta(minutes=6)
    assert await storage.claim_action(action.id, user_id=ALICE, chat_id=ALICE, now=later) is None
    assert await storage.expire_action(action.id, user_id=ALICE, now=later) is True
    assert (await storage.get_action(action.id, user_id=ALICE)).status == "expired"


async def test_one_pending_action_per_user_under_concurrency_pg(pool):
    # Два разных сообщения одного пользователя обрабатываются одновременно.
    await apply_migrations(pool)
    storage = Storage(pool)
    clock = Clock()

    async def prepare(message_id: int):
        return await storage.prepare_action(
            user_id=ALICE,
            chat_id=ALICE,
            source_message_id=message_id,
            tool="add_reminder",
            arguments={"text": f"дело {message_id}", "remind_at": "2026-10-13T18:30:00+03:00"},
            timezone="Europe/Moscow",
            now=clock(),
            ttl=timedelta(minutes=5),
        )

    await asyncio.gather(*(prepare(message_id) for message_id in range(1, 6)))
    statuses = [row["status"] for row in await pool.fetch("SELECT status FROM pending_actions")]
    assert sorted(statuses) == ["cancelled"] * 4 + ["pending"]


async def test_confirmed_and_stale_actions_can_be_claimed_again_pg(pool):
    # Arrange
    await apply_migrations(pool)
    storage = Storage(pool)
    clock = Clock()
    action, _ = await storage.prepare_action(
        user_id=ALICE,
        chat_id=ALICE,
        source_message_id=1,
        tool="add_reminder",
        arguments={"text": "отчёт", "remind_at": "2026-10-13T18:30:00+03:00"},
        timezone="Europe/Moscow",
        now=clock(),
        ttl=timedelta(minutes=5),
    )
    claimed = await storage.claim_action(action.id, user_id=ALICE, chat_id=ALICE, now=clock())
    assert claimed.confirmed_at == clock()
    # Act / Assert: свежее «выполняется» второй раз не захватывается
    assert await storage.claim_action(action.id, user_id=ALICE, chat_id=ALICE, now=clock()) is None
    # Act / Assert: после временного сбоя и истечения 5 минут подтверждённое можно повторить
    await storage.finish_action(action.id, status="pending")
    later = clock() + timedelta(minutes=10)
    assert await storage.expire_action(action.id, user_id=ALICE, now=later) is False
    again = await storage.claim_action(action.id, user_id=ALICE, chat_id=ALICE, now=later)
    assert again is not None and again.status == "executing"
    # Act / Assert: зависшее «выполняется» старше минуты захватывается снова
    stale = await storage.claim_action(
        action.id, user_id=ALICE, chat_id=ALICE, now=later + timedelta(seconds=61)
    )
    assert stale is not None
