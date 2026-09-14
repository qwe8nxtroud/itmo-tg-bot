"""RUN_INTEGRATION=1 python -m pytest -m integration -v.

Тест создаёт собственные контейнер и volume; пользовательскую БД не использует.
"""

import asyncio
import json
import os
import secrets
import socket
import time
from dataclasses import replace

import asyncpg
import pytest

from app.config import Settings
from app.db import create_pool
from app.health import HealthState, health_result
from scripts.common import CommandError, run_command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
    ),
]


@pytest.fixture
def database():
    name = "itmo-test-" + secrets.token_hex(6)
    volume = name + "-data"
    password = secrets.token_urlsafe(24)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def docker(*args):
        return run_command(["docker", *args], label="Интеграционный Docker-тест", timeout=180)

    def wait_until_ready():
        deadline = time.monotonic() + 60
        while True:
            try:
                docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "bot", "-d", "bot")
                return
            except CommandError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    docker("info")
    docker("volume", "create", volume)
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            "POSTGRES_DB=bot",
            "-e",
            "POSTGRES_USER=bot",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "--mount",
            f"type=volume,source={volume},target=/var/lib/postgresql/data",
            "postgres:16-bookworm",
        )
        info = json.loads(docker("inspect", name))[0]
        assert int(info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]) == port
        wait_until_ready()
        yield (
            Settings(
                bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                postgres_password=password,
                postgres_port=port,
            ),
            docker,
            wait_until_ready,
            name,
        )
    finally:
        try:
            docker("rm", "-f", name)
        finally:
            docker("volume", "rm", volume)


async def test_real_db_password_restart_health_and_persistence(database):
    # Arrange
    settings, docker, wait_until_ready, name = database
    pool = await create_pool(settings)
    task = asyncio.create_task(asyncio.Event().wait())
    state = HealthState(pool=pool, initialized=True, polling_task=task)
    try:
        await pool.execute("CREATE TABLE integration_probe (value text NOT NULL)")
        await pool.execute("INSERT INTO integration_probe VALUES ($1)", "Сохранилось 👋")
        # Act / Assert: реальное подключение и ошибка пароля.
        assert (await health_result(state))[0] == 200
        with pytest.raises(asyncpg.InvalidPasswordError):
            await create_pool(replace(settings, postgres_password="wrong-password"))
        # Act / Assert: потеря соединения и сохранность данных после запуска.
        await asyncio.to_thread(docker, "stop", name)
        assert (await health_result(state))[0] == 503
        await asyncio.to_thread(docker, "start", name)
        await asyncio.to_thread(wait_until_ready)
        for _ in range(60):
            if (await health_result(state))[0] == 200:
                break
            await asyncio.sleep(1)
        assert (await health_result(state))[0] == 200
        assert await pool.fetchval("SELECT value FROM integration_probe") == "Сохранилось 👋"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close()


async def test_storage_settings_history_isolation_and_pruning(database):
    # Arrange
    from app.storage import Storage, UserSettings

    settings, *_ = database
    pool = await create_pool(settings)
    storage = Storage(pool)
    try:
        await storage.init_schema()
        await storage.init_schema()  # повторный запуск безопасен
        # Act / Assert: настройки
        assert await storage.get_settings(1) is None
        await storage.save_settings(1, UserSettings("study", 0.3))
        await storage.save_settings(1, UserSettings("study", 1.0))
        assert await storage.get_settings(1) == UserSettings("study", 1.0)
        # Act / Assert: история, порядок и окно последних сообщений
        for index in range(3):
            await storage.append_exchange(1, f"q{index}", f"a{index}", keep=4)
            await storage.append_exchange(2, f"чужой {index}", f"ответ {index}", keep=10)
        history = await storage.get_history(1, limit=10)
        assert [(m.role, m.content) for m in history] == [
            ("user", "q1"),
            ("assistant", "a1"),
            ("user", "q2"),
            ("assistant", "a2"),
        ]
        assert [m.content for m in await storage.get_history(1, limit=2)] == ["q2", "a2"]
        assert len(await storage.get_history(2, limit=10)) == 6
        # Act / Assert: смена режима чистит историю только своего чата
        await storage.switch_mode(1, UserSettings("quiz", 1.0))
        assert await storage.get_settings(1) == UserSettings("quiz", 1.0)
        assert await storage.get_history(1, limit=10) == []
        assert len(await storage.get_history(2, limit=10)) == 6
        await storage.clear_history(2)
        assert await storage.get_history(2, limit=10) == []
        assert await storage.get_settings(2) is None
    finally:
        await pool.close()
