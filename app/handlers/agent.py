"""Команды агента ЛР2 и кнопки подтверждения. Без SQL, MCP-сессий и схем инструментов."""

import contextlib
import logging
import uuid

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.agent import Agent
from app.mcp_client import ToolCallError
from app.presenters import tools_text, week_text, why_text
from app.storage import PendingAction
from app.timezones import TIMEZONE_HINT, format_local

logger = logging.getLogger("app.handlers.agent")

router = Router(name="agent")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

TOOLS_UNAVAILABLE_TEXT = "Инструменты сейчас недоступны. Попробуйте позже."
TIMEZONE_NOT_SET_TEXT = "Часовой пояс не задан. " + TIMEZONE_HINT
UNKNOWN_TIMEZONE_TEXT = "Неизвестный часовой пояс. " + TIMEZONE_HINT
BUTTON_ERROR_TEXT = "Не получилось обработать нажатие. Попробуйте ещё раз позже."


class ActionCallback(CallbackData, prefix="act"):
    decision: str
    action_id: str


def confirmation_keyboard(action: PendingAction) -> InlineKeyboardMarkup:
    action_id = action.id.hex
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    callback_data=ActionCallback(decision="ok", action_id=action_id).pack(),
                ),
                InlineKeyboardButton(
                    text="✖️ Отменить",
                    callback_data=ActionCallback(decision="no", action_id=action_id).pack(),
                ),
            ]
        ]
    )


@router.message(Command("tools"))
async def on_tools(message: Message, agent: Agent) -> None:
    gateway = agent.gateway
    if not gateway.available or not gateway.tools:
        await message.answer(TOOLS_UNAVAILABLE_TEXT, parse_mode=None)
        return
    await message.answer(tools_text(gateway.tools, gateway.resources), parse_mode=None)


@router.message(Command("timezone"))
async def on_timezone(message: Message, command: CommandObject, agent: Agent) -> None:
    user_id = message.from_user.id
    argument = (command.args or "").strip()
    if not argument:
        current = await agent.get_timezone(user_id)
        if current is None:
            await message.answer(TIMEZONE_NOT_SET_TEXT, parse_mode=None)
            return
        await message.answer(
            f"Часовой пояс: {current}. Сейчас там {format_local(agent.now(), current)}.\n"
            "Изменить: /timezone Регион/Город",
            parse_mode=None,
        )
        return
    saved = await agent.set_timezone(user_id, argument)
    if saved is None:
        await message.answer(UNKNOWN_TIMEZONE_TEXT, parse_mode=None)
        return
    await message.answer(
        f"Часовой пояс сохранён: {saved}. Местное время: {format_local(agent.now(), saved)}.",
        parse_mode=None,
    )


@router.message(Command("why"))
async def on_why(message: Message, agent: Agent) -> None:
    user_id = message.from_user.id
    event = await agent.last_event(user_id)
    timezone = await agent.get_timezone(user_id)
    await message.answer(why_text(event, timezone), parse_mode=None)


@router.message(Command("week"))
async def on_week(message: Message, agent: Agent) -> None:
    try:
        data = await agent.current_week()
    except ToolCallError as exc:
        await message.answer(f"Расписание недоступно: {exc.message}", parse_mode=None)
        return
    await message.answer(week_text(data), parse_mode=None)


@router.callback_query(ActionCallback.filter())
async def on_action_button(
    callback: CallbackQuery, callback_data: ActionCallback, agent: Agent
) -> None:
    request_id = uuid.uuid4().hex[:8]
    try:
        action_id = uuid.UUID(hex=callback_data.action_id)
    except ValueError:
        await callback.answer("Кнопка устарела.")
        return
    # Владелец и чат — из самого нажатия (доверенный update), а не из данных кнопки.
    user_id, chat_id = callback.from_user.id, callback.message.chat.id
    handle = agent.confirm if callback_data.decision == "ok" else agent.cancel
    try:
        reply = await handle(action_id, user_id=user_id, chat_id=chat_id, request_id=request_id)
    except Exception:
        logger.exception("Кнопка %s: не удалось обработать нажатие", request_id)
        await callback.answer(BUTTON_ERROR_TEXT, show_alert=True)
        return
    await callback.answer()
    if reply.action is None:
        # Действие завершено: кнопки убираются, чтобы не нажимать их снова.
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text=reply.text, parse_mode=None)
