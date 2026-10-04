import argparse
import asyncio
import logging

import aiohttp
from aiogram import Dispatcher
from aiohttp_socks import ProxyConnector

from app.agent import Agent
from app.assistant import Assistant
from app.config import ConfigError, Settings
from app.db import create_pool
from app.handlers.agent import router as agent_router
from app.handlers.dialog import BOT_COMMANDS
from app.handlers.dialog import router as dialog_router
from app.health import HealthState, start_health_server
from app.llm import LLMClient
from app.logging_setup import configure_logging
from app.mcp_client import McpGateway
from app.storage import Storage
from app.telegram import create_bot

logger = logging.getLogger("app")


def create_llm_session(settings: Settings) -> aiohttp.ClientSession:
    # Отдельная HTTP-сессия для модели: у Telegram свой прокси и свои таймауты.
    connector = ProxyConnector.from_url(settings.llm_proxy_url) if settings.llm_proxy_url else None
    return aiohttp.ClientSession(connector=connector)


async def run(settings: Settings) -> None:
    state = HealthState()
    bot = create_bot(settings)
    runner = None
    llm_session = None
    gateway = None
    try:
        state.pool = await create_pool(settings)
        logger.info("PostgreSQL подключён: SELECT 1 выполнен.")
        # Начальная проверка токена и маршрута через прокси ограничена по времени.
        async with asyncio.timeout(30):
            me = await bot.get_me()
            webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ConfigError(
                "У бота установлен webhook. Удалите его перед запуском polling "
                "или используйте отдельного учебного бота."
            )
        logger.info("Telegram доступен. Бот @%s запускает polling.", me.username)
        storage = Storage(state.pool)
        await storage.init_schema()
        llm_session = create_llm_session(settings)
        llm = LLMClient(
            llm_session,
            base_url=settings.llm_api_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            project=settings.llm_api_project,
        )
        assistant = Assistant(
            storage,
            llm,
            history_max_messages=settings.history_max_messages,
            history_max_chars=settings.history_max_chars,
        )
        # MCP-сервер запускается дочерним процессом; если он не поднялся, бот работает
        # без инструментов и пытается переподключиться в фоне.
        gateway = McpGateway.for_settings(settings)
        await gateway.start()
        agent = Agent(
            assistant,
            storage,
            llm,
            gateway,
            temperature=settings.agent_temperature,
            history_max_messages=settings.history_max_messages,
            history_max_chars=settings.history_max_chars,
            default_timezone=settings.default_timezone,
        )
        async with asyncio.timeout(30):
            await bot.set_my_commands(BOT_COMMANDS)
        dispatcher = Dispatcher()
        # Команды агента раньше общего обработчика неизвестных команд и текста.
        dispatcher.include_router(agent_router)
        dispatcher.include_router(dialog_router)
        runner = await start_health_server(state, settings.health_port)
        state.polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                db=state.pool,
                assistant=assistant,
                agent=agent,
                allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
        )
        state.initialized = True
        await state.polling_task
    finally:
        state.initialized = False
        if state.polling_task and not state.polling_task.done():
            state.polling_task.cancel()
            await asyncio.gather(state.polling_task, return_exceptions=True)
        if runner:
            await runner.cleanup()
        if gateway:
            await gateway.close()
        if llm_session:
            await llm_session.close()
        await bot.session.close()
        if state.pool:
            try:
                async with asyncio.timeout(10):
                    await state.pool.close()
            except TimeoutError:
                state.pool.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-ассистент студента в Telegram")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    try:
        settings = Settings.load(args.env_file)
    except ConfigError as exc:
        print(str(exc))
        return 1
    configure_logging(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    except Exception:
        logger.exception(
            "Не удалось запустить бот. Проверьте БД, токен, прокси и настройки модели."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
