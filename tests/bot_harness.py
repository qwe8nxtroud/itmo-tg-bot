"""Общий диспетчер aiogram для тестов: роутер можно подключить только к одному диспетчеру."""

from aiogram import Dispatcher

from app.handlers.agent import router as agent_router
from app.handlers.dialog import router as dialog_router

dispatcher = Dispatcher()
# Тот же порядок, что в app/__main__.py: команды агента раньше общего обработчика.
dispatcher.include_router(agent_router)
dispatcher.include_router(dialog_router)
