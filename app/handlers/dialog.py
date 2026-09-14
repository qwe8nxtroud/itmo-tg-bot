"""Обработчики Telegram: команды режимов и настроек, текстовые сообщения в личном чате."""

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message
from aiogram.utils.chat_action import ChatActionSender

from app.assistant import TEMPERATURES, Assistant
from app.llm import LLMEmptyResponseError, LLMError, LLMTimeoutError
from app.prompts import DEFAULT_MODE, MODES

logger = logging.getLogger("app.handlers")

TELEGRAM_MESSAGE_LIMIT = 4096

router = Router(name="dialog")
# Бот отвечает только в личных чатах: групповые сообщения не обрабатываются.
router.message.filter(F.chat.type == "private")

BOT_COMMANDS = [
    BotCommand(command="start", description="Что умеет бот"),
    *(BotCommand(command=m.command, description=m.summary.capitalize()) for m in MODES.values()),
    BotCommand(command="settings", description="Режим, модель и temperature"),
    BotCommand(command="reset", description="Очистить историю диалога"),
]

_MODE_LIST = "\n".join(f"/{m.command} — {m.summary}" for m in MODES.values())
_TEMPERATURE_LIST = ", ".join(f"{t:.1f}" for t in TEMPERATURES)
START_TEXT = (
    "Привет! Я AI-ассистент студента.\n\n"
    f"Режимы (по умолчанию включён /{DEFAULT_MODE}):\n{_MODE_LIST}\n\n"
    "Команды:\n"
    "/settings — режим, модель и temperature\n"
    "/reset — очистить историю диалога\n\n"
    "Просто напиши сообщение — отвечу в активном режиме."
)
UNKNOWN_COMMAND_TEXT = "Такой команды нет. Список команд: /start"
NOT_TEXT_MESSAGE = "Я понимаю только текстовые сообщения."
RESET_TEXT = "История диалога очищена. Режим и temperature сохранены."
BAD_TEMPERATURE_TEXT = (
    f"Недопустимое значение. Выберите одно из: {_TEMPERATURE_LIST}. Пример: /settings 0.3"
)
ERROR_TIMEOUT_TEXT = "Модель не ответила за отведённое время. Попробуйте ещё раз чуть позже."
ERROR_EMPTY_TEXT = "Модель вернула пустой ответ. Переформулируйте запрос или повторите попытку."
ERROR_UNAVAILABLE_TEXT = "Сервис модели сейчас недоступен. Попробуйте позже."
ERROR_INTERNAL_TEXT = "Не получилось обработать сообщение. Попробуйте ещё раз позже."


def split_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Режет текст на части не длиннее limit, предпочитая границы строк и слов, без потерь."""
    parts: list[str] = []
    while len(text) > limit:
        # Индекс последнего разделителя, который ещё помещается в часть вместе с ним.
        cut = text.rfind("\n", 1, limit)
        if cut == -1:
            cut = text.rfind(" ", 1, limit)
        if cut == -1:
            cut = limit - 1
        parts.append(text[: cut + 1])
        text = text[cut + 1 :]
    parts.append(text)
    return parts


def error_text(error: LLMError) -> str:
    if isinstance(error, LLMTimeoutError):
        return ERROR_TIMEOUT_TEXT
    if isinstance(error, LLMEmptyResponseError):
        return ERROR_EMPTY_TEXT
    return ERROR_UNAVAILABLE_TEXT


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(START_TEXT, parse_mode=None)


@router.message(Command(*MODES))
async def on_mode(message: Message, command: CommandObject, assistant: Assistant) -> None:
    mode = await assistant.switch_mode(message.chat.id, command.command)
    await message.answer(
        f"Режим «{mode.title}» включён. История очищена. {mode.hint}", parse_mode=None
    )


@router.message(Command("settings"))
async def on_settings(message: Message, command: CommandObject, assistant: Assistant) -> None:
    argument = (command.args or "").strip().replace(",", ".")
    if not argument:
        settings = await assistant.get_settings(message.chat.id)
        mode = MODES[settings.mode]
        await message.answer(
            f"Режим: {mode.title} (/{mode.command})\n"
            f"Модель: {assistant.model}\n"
            f"Temperature: {settings.temperature:.1f}\n\n"
            f"Изменить temperature: /settings <значение>, где значение — {_TEMPERATURE_LIST}",
            parse_mode=None,
        )
        return
    try:
        temperature = float(argument)
    except ValueError:
        temperature = None
    if temperature not in TEMPERATURES:
        await message.answer(BAD_TEMPERATURE_TEXT, parse_mode=None)
        return
    temperature = TEMPERATURES[TEMPERATURES.index(temperature)]  # «-0.0» и «1» → 0.0 и 1.0
    await assistant.set_temperature(message.chat.id, temperature)
    await message.answer(
        f"Temperature: {temperature:.1f}. Применится начиная со следующего запроса.",
        parse_mode=None,
    )


@router.message(Command("reset"))
async def on_reset(message: Message, assistant: Assistant) -> None:
    await assistant.reset(message.chat.id)
    await message.answer(RESET_TEXT, parse_mode=None)


@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message) -> None:
    # Неизвестные команды не отправляются модели.
    await message.answer(UNKNOWN_COMMAND_TEXT, parse_mode=None)


@router.message(F.text)
async def on_text(message: Message, assistant: Assistant) -> None:
    # Пока ответ формируется, пользователь видит статус «печатает…».
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            answer = await assistant.answer(message.chat.id, message.text)
        except LLMError as error:
            await message.answer(error_text(error), parse_mode=None)
            return
        except Exception:
            # Трассировка попадает в лог через фильтр секретов, пользователю — короткий текст.
            logger.exception("Не удалось ответить на сообщение в чате %s", message.chat.id)
            await message.answer(ERROR_INTERNAL_TEXT, parse_mode=None)
            return
    for part in split_text(answer):
        await message.answer(part, parse_mode=None)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(NOT_TEXT_MESSAGE, parse_mode=None)
