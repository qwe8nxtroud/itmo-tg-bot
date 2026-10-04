"""Единственное место чтения и проверки настроек приложения."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values

from app.timezones import normalize_timezone


class ConfigError(ValueError):
    """Ошибка настройки без секретных значений в сообщении."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    postgres_password: str = field(repr=False)
    telegram_proxy_url: str = field(default="", repr=False)
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "bot"
    postgres_user: str = "bot"
    log_level: str = "INFO"
    health_port: int = 8080
    # Языковая модель: OpenAI-совместимый API (chat completions).
    llm_api_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    llm_proxy_url: str = field(default="", repr=False)
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 1024
    llm_api_project: str = ""
    # Ограничения истории диалога, передаваемой модели.
    history_max_messages: int = 20
    history_max_chars: int = 12000
    # ЛР2: агент и MCP-сервер.
    schedule_path: Path = Path("data/schedule.json")
    weather_timeout_seconds: float = 5.0
    mcp_call_timeout_seconds: float = 30.0
    agent_temperature: float = 0.2
    default_timezone: str = "Europe/Moscow"

    @classmethod
    def load(
        cls, env_file: Path | str = ".env", *, environ: Mapping[str, str] | None = None
    ) -> "Settings":
        values = {
            **dotenv_values(env_file, interpolate=False),
            **(os.environ if environ is None else environ),
        }

        def value(key: str, default: str = "") -> str:
            return values.get(key) or default

        def port(key: str, default: str) -> int:
            try:
                result = int(value(key, default))
                if not 1 <= result <= 65535:
                    raise ValueError
                return result
            except ValueError:
                raise ConfigError(f"{key}: нужен номер порта от 1 до 65535.") from None

        def positive_int(key: str, default: str) -> int:
            try:
                result = int(value(key, default))
                if result <= 0:
                    raise ValueError
                return result
            except ValueError:
                raise ConfigError(f"{key}: нужно целое число больше нуля.") from None

        def non_negative_int(key: str, default: str) -> int:
            try:
                result = int(value(key, default))
                if result < 0:
                    raise ValueError
                return result
            except ValueError:
                raise ConfigError(f"{key}: нужно целое число не меньше нуля.") from None

        def positive_float(key: str, default: str) -> float:
            try:
                result = float(value(key, default))
                if result <= 0:
                    raise ValueError
                return result
            except ValueError:
                raise ConfigError(f"{key}: нужно число больше нуля.") from None

        def proxy_url(key: str) -> str:
            proxy = value(key)
            if proxy:
                try:
                    parsed = urlsplit(proxy)
                    if (
                        parsed.scheme not in {"http", "socks5"}
                        or not parsed.hostname
                        or not parsed.port
                        or parsed.path not in {"", "/"}
                        or parsed.query
                        or parsed.fragment
                    ):
                        raise ValueError
                except ValueError:
                    raise ConfigError(
                        f"{key}: нужен http://host:port или socks5://host:port; "
                        "при необходимости добавьте user:password@."
                    ) from None
            return proxy

        token = value("BOT_TOKEN")
        try:
            validate_token(token)
        except TokenValidationError:
            raise ConfigError("BOT_TOKEN: укажите токен, полученный у BotFather.") from None
        password = value("POSTGRES_PASSWORD")
        if not password:
            raise ConfigError("POSTGRES_PASSWORD: пароль базы данных не задан.")
        proxy = proxy_url("TELEGRAM_PROXY_URL")
        level = value("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("LOG_LEVEL: используйте DEBUG, INFO, WARNING, ERROR или CRITICAL.")
        postgres_port = port("POSTGRES_PORT", "5432")
        health_port = port("HEALTH_PORT", "8080")

        base_url = value("LLM_API_BASE_URL").rstrip("/")
        parsed_base = urlsplit(base_url)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.hostname:
            raise ConfigError(
                "LLM_API_BASE_URL: укажите адрес OpenAI-совместимого API, "
                "например https://api.openai.com/v1."
            )
        api_key = value("LLM_API_KEY")
        if not api_key:
            raise ConfigError(
                "LLM_API_KEY: ключ доступа к API модели не задан "
                "(для локальной модели без авторизации подойдёт любое непустое значение)."
            )
        model = value("LLM_MODEL")
        if not model:
            raise ConfigError("LLM_MODEL: укажите имя модели у выбранного провайдера.")

        default_zone = normalize_timezone(value("DEFAULT_TIMEZONE", "Europe/Moscow"))
        if default_zone is None:
            raise ConfigError("DEFAULT_TIMEZONE: укажите зону IANA, например Europe/Moscow.")

        def unit_interval(key: str, default: str) -> float:
            try:
                result = float(value(key, default))
                if not 0.0 <= result <= 1.0:
                    raise ValueError
                return result
            except ValueError:
                raise ConfigError(f"{key}: нужно число от 0 до 1.") from None

        return cls(
            bot_token=token,
            postgres_password=password,
            telegram_proxy_url=proxy,
            postgres_host=value("POSTGRES_HOST", "127.0.0.1"),
            postgres_port=postgres_port,
            postgres_db=value("POSTGRES_DB", "bot"),
            postgres_user=value("POSTGRES_USER", "bot"),
            log_level=level,
            health_port=health_port,
            llm_api_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            llm_proxy_url=proxy_url("LLM_PROXY_URL"),
            llm_timeout_seconds=positive_float("LLM_TIMEOUT_SECONDS", "60"),
            llm_max_tokens=non_negative_int("LLM_MAX_TOKENS", "1024"),
            history_max_messages=positive_int("HISTORY_MAX_MESSAGES", "20"),
            history_max_chars=positive_int("HISTORY_MAX_CHARS", "12000"),
            llm_api_project=value("LLM_API_PROJECT").strip(),
            schedule_path=Path(value("SCHEDULE_PATH", "data/schedule.json")),
            weather_timeout_seconds=positive_float("WEATHER_TIMEOUT_SECONDS", "5"),
            mcp_call_timeout_seconds=positive_float("MCP_CALL_TIMEOUT_SECONDS", "30"),
            agent_temperature=unit_interval("AGENT_TEMPERATURE", "0.2"),
            default_timezone=default_zone,
        )
