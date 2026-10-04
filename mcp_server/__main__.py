"""Запуск MCP-сервера по stdio: `python -m mcp_server`.

Обычно сервер запускает бот как дочерний процесс и передаёт ему переменные окружения.
Для независимой проверки его можно запустить MCP Inspector'ом или scripts/mcp_check.py.
Логи пишутся в stderr: stdout занят протоколом MCP.
"""

import logging
import os
import sys
import zoneinfo
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from mcp_server import trust
from mcp_server.reminders import PgConfig, PgReminderStore
from mcp_server.schedule import ScheduleSource
from mcp_server.server import ServerDeps, build_server
from mcp_server.weather import HttpJsonFetcher, OpenMeteoWeather

DEFAULT_SCHEDULE = Path(__file__).resolve().parent.parent / "data" / "schedule.json"


class _RedactSecrets(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        for secret in self._secrets:
            text = text.replace(secret, "[скрыто]")
        record.msg, record.args = text, ()
        return True


def _number(environ: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(environ.get(key) or default)
    except ValueError:
        value = default
    return value if value > 0 else default


def create_deps(environ: Mapping[str, str]) -> tuple[ServerDeps, list]:
    schedule_path = Path(environ.get("SCHEDULE_PATH") or DEFAULT_SCHEDULE)
    fetcher = HttpJsonFetcher(timeout=_number(environ, "WEATHER_TIMEOUT_SECONDS", 5.0), retries=1)
    store = PgReminderStore(
        PgConfig(
            host=environ.get("POSTGRES_HOST") or "127.0.0.1",
            port=int(_number(environ, "POSTGRES_PORT", 5432)),
            database=environ.get("POSTGRES_DB") or "bot",
            user=environ.get("POSTGRES_USER") or "bot",
            password=environ.get("POSTGRES_PASSWORD") or "",
        )
    )
    cache: dict[str, ScheduleSource] = {}

    def schedule() -> ScheduleSource:
        # Файл читается при первом обращении; ошибка формата не мешает погоде.
        if "source" not in cache:
            cache["source"] = ScheduleSource.load(schedule_path)
        return cache["source"]

    deps = ServerDeps(
        weather=OpenMeteoWeather(fetcher),
        schedule=schedule,
        reminders=store,
        trust_secret=(environ.get(trust.SECRET_ENV) or "").encode("utf-8"),
        clock=lambda: datetime.now(UTC),
    )
    return deps, [fetcher, store]


def main() -> None:
    # Как и в боте, правила часовых поясов берутся из пакета tzdata на любой ОС.
    zoneinfo.reset_tzpath(to=[])
    environ = os.environ
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(
        _RedactSecrets([environ.get("POSTGRES_PASSWORD", ""), environ.get(trust.SECRET_ENV, "")])
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=environ.get("LOG_LEVEL", "INFO"), handlers=[handler], force=True)
    deps, closeables = create_deps(environ)

    @asynccontextmanager
    async def lifespan(_server) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            for item in closeables:
                await item.close()

    build_server(deps, lifespan=lifespan).run("stdio")


if __name__ == "__main__":
    main()
