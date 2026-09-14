import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import ConfigError, Settings
from app.health import HealthState, health_result
from app.logging_setup import SecretFilter
from app.telegram import create_bot

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
# Настройки модели обязательны; значения вымышленные.
LLM_ENV = {
    "LLM_API_BASE_URL": "https://llm.example/v1",
    "LLM_API_KEY": "llm-secret-key",
    "LLM_MODEL": "test-model",
}
LLM_LINES = "".join(f"{key}={value}\n" for key, value in LLM_ENV.items())


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"BOT_TOKEN={TOKEN}\nPOSTGRES_PASSWORD=secret-db\n{LLM_LINES}", encoding="utf-8"
    )
    return Settings.load(path, environ={})


def test_settings_environment_overrides_file(tmp_path):
    # Arrange
    path = tmp_path / ".env"
    path.write_text(
        f"BOT_TOKEN={TOKEN}\nPOSTGRES_PASSWORD='p$a#ss'\nPOSTGRES_PORT=5432\n{LLM_LINES}",
        encoding="utf-8",
    )
    # Act
    config = Settings.load(path, environ={"POSTGRES_PORT": "55432"})
    # Assert
    assert config.postgres_port == 55432
    assert config.postgres_password == "p$a#ss"
    assert "p$a#ss" not in repr(config)
    assert TOKEN not in repr(config)
    assert LLM_ENV["LLM_API_KEY"] not in repr(config)


@pytest.mark.parametrize("value", ["abc", "0", "65536"])
def test_invalid_port_has_safe_error(tmp_path, value):
    # Arrange
    path = tmp_path / ".env"
    # Act / Assert
    with pytest.raises(ConfigError, match="POSTGRES_PORT"):
        Settings.load(
            path,
            environ={
                "BOT_TOKEN": TOKEN,
                "POSTGRES_PASSWORD": "secret",
                "POSTGRES_PORT": value,
                **LLM_ENV,
            },
        )


@pytest.mark.parametrize("proxy", ["http://user:pass@localhost:8080", "socks5://localhost:1080"])
async def test_proxy_is_attached_to_bot_session(settings, proxy):
    # Arrange
    from dataclasses import replace

    config = replace(settings, telegram_proxy_url=proxy)
    # Act
    bot = create_bot(config)
    # Assert
    assert bot.session._proxy == proxy
    await bot.session.close()


def test_bad_proxy_does_not_leak_credentials(tmp_path):
    # Arrange
    proxy = "ftp://user:very-secret@host:123"
    # Act / Assert
    with pytest.raises(ConfigError) as exc:
        Settings.load(
            tmp_path / ".env",
            environ={
                "BOT_TOKEN": TOKEN,
                "POSTGRES_PASSWORD": "db",
                "TELEGRAM_PROXY_URL": proxy,
                **LLM_ENV,
            },
        )
    assert "TELEGRAM_PROXY_URL" in str(exc.value)
    assert "very-secret" not in str(exc.value)


async def test_health_tracks_pool_failure_and_recovery():
    # Arrange
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)
    task = asyncio.create_task(asyncio.Event().wait())
    state = HealthState(pool=pool, initialized=True, polling_task=task)
    try:
        # Act / Assert
        assert (await health_result(state))[0] == 200
        pool.fetchval.assert_awaited_with("SELECT 1", timeout=3)
        pool.fetchval.side_effect = ConnectionError("secret-password")
        status, body = await health_result(state)
        assert status == 503
        assert body == {"status": "not_ready", "database": "unavailable", "polling": "running"}
        pool.fetchval.side_effect = None
        assert (await health_result(state))[0] == 200
        state.initialized = False
        assert (await health_result(state))[0] == 503
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_health_without_polling_is_not_ready():
    # Arrange
    state = HealthState(pool=None)
    # Act
    status, body = await health_result(state)
    # Assert
    assert status == 503
    assert body["polling"] == "stopped"


def test_log_filter_redacts_secrets_and_exception():
    # Arrange
    secret_filter = SecretFilter([TOKEN, "db-secret", "proxy-secret"])
    try:
        raise ValueError("proxy-secret")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test",
            logging.ERROR,
            "",
            1,
            "Ошибка %s https://api.telegram.org/bot%s",
            ("db-secret", TOKEN),
            sys.exc_info(),
        )
    # Act
    secret_filter.filter(record)
    rendered = logging.Formatter().format(record)
    # Assert
    assert all(secret not in rendered for secret in (TOKEN, "db-secret", "proxy-secret"))
    assert "ValueError" in rendered


@pytest.mark.parametrize("missing", ["LLM_API_BASE_URL", "LLM_API_KEY", "LLM_MODEL"])
def test_missing_llm_setting_has_safe_error(tmp_path, missing):
    # Arrange
    environ = {"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "db-secret", **LLM_ENV}
    del environ[missing]
    # Act / Assert
    with pytest.raises(ConfigError, match=missing) as exc:
        Settings.load(tmp_path / ".env", environ=environ)
    assert "db-secret" not in str(exc.value)
    assert LLM_ENV["LLM_API_KEY"] not in str(exc.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LLM_API_BASE_URL", "api.openai.com/v1"),
        ("LLM_PROXY_URL", "ftp://user:very-secret@host:1"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_TIMEOUT_SECONDS", "fast"),
        ("LLM_MAX_TOKENS", "-1"),
        ("HISTORY_MAX_MESSAGES", "0"),
        ("HISTORY_MAX_MESSAGES", "many"),
        ("HISTORY_MAX_CHARS", "-5"),
    ],
)
def test_invalid_llm_and_history_values_name_the_variable(tmp_path, key, value):
    # Arrange
    environ = {"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "db", **LLM_ENV, key: value}
    # Act / Assert
    with pytest.raises(ConfigError, match=key) as exc:
        Settings.load(tmp_path / ".env", environ=environ)
    assert "very-secret" not in str(exc.value)


def test_llm_settings_are_normalized(tmp_path):
    # Arrange
    environ = {
        "BOT_TOKEN": TOKEN,
        "POSTGRES_PASSWORD": "db",
        **LLM_ENV,
        "LLM_API_BASE_URL": "http://127.0.0.1:11434/v1/",
        "LLM_MAX_TOKENS": "0",
        "HISTORY_MAX_MESSAGES": "6",
        "HISTORY_MAX_CHARS": "3000",
        "LLM_TIMEOUT_SECONDS": "2.5",
    }
    # Act
    config = Settings.load(tmp_path / ".env", environ=environ)
    # Assert
    assert config.llm_api_base_url == "http://127.0.0.1:11434/v1"
    assert config.llm_max_tokens == 0
    assert (config.history_max_messages, config.history_max_chars) == (6, 3000)
    assert config.llm_timeout_seconds == 2.5


def test_logging_hides_llm_key_and_proxy_password(settings):
    # Arrange
    from dataclasses import replace

    from app.logging_setup import configure_logging

    config = replace(settings, llm_proxy_url="http://student:proxy-pass@proxy.example:3128")
    root = logging.getLogger()
    previous = (root.handlers[:], root.level)
    try:
        configure_logging(config)
        handler = root.handlers[0]
        record = logging.LogRecord(
            "app.llm",
            logging.WARNING,
            "",
            1,
            "ключ %s прокси %s",
            (config.llm_api_key, config.llm_proxy_url),
            None,
        )
        # Act
        for log_filter in handler.filters:
            log_filter.filter(record)
        rendered = handler.format(record)
        # Assert
        assert config.llm_api_key not in rendered
        assert "proxy-pass" not in rendered
        assert "[скрыто]" in rendered
    finally:
        logging.basicConfig(handlers=previous[0], level=previous[1], force=True)
