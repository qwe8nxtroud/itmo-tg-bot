"""Telegram-уровень ЛР2: команды /tools, /timezone, /why, /week и кнопки подтверждения."""

import itertools
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.handlers.agent import (
    TIMEZONE_NOT_SET_TEXT,
    TOOLS_UNAVAILABLE_TEXT,
    UNKNOWN_TIMEZONE_TEXT,
    ActionCallback,
)
from app.handlers.dialog import START_TEXT
from tests.bot_harness import dispatcher
from tests.conftest import ALICE, BOB
from tests.fakes import call, text

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
counter = itertools.count(1000)


class Chatter:
    """Подаёт update в общий диспетчер и собирает вызовы Bot API."""

    def __init__(self, env) -> None:
        self.env = env
        self.bot = Bot(TOKEN)
        self.bot.session = AsyncMock()

    def _methods(self, before: int) -> list:
        return [call_.args[1] for call_ in self.bot.session.call_args_list[before:]]

    async def feed(self, update: Update) -> list:
        before = len(self.bot.session.call_args_list)
        await dispatcher.feed_update(
            self.bot, update, assistant=self.env.agent._assistant, agent=self.env.agent
        )
        return self._methods(before)

    def message(self, text_: str, user: int = ALICE) -> Message:
        return Message(
            message_id=next(counter),
            date=datetime.now(UTC),
            chat=Chat(id=user, type="private"),
            from_user=User(id=user, is_bot=False, first_name="Студент"),
            text=text_,
        )

    async def send(self, text_: str, user: int = ALICE) -> list[SendMessage]:
        methods = await self.feed(
            Update(update_id=next(counter), message=self.message(text_, user))
        )
        return [m for m in methods if isinstance(m, SendMessage)]

    async def press(self, data: str, user: int = ALICE, owner: int = ALICE) -> list:
        query = CallbackQuery(
            id=str(next(counter)),
            from_user=User(id=user, is_bot=False, first_name="Студент"),
            chat_instance="test",
            data=data,
            message=self.message("Создать напоминание?", owner),
        )
        return await self.feed(Update(update_id=next(counter), callback_query=query))


async def test_start_lists_agent_commands():
    for command in ("/tools", "/timezone", "/why", "/week", "/agent"):
        assert command in START_TEXT


async def test_tools_lists_discovered_tools(make_env):
    chat = Chatter(await make_env())
    (reply,) = await chat.send("/tools")
    assert reply.text == (
        "Доступные инструменты:\n"
        "• get_weather — текущая погода в городе (чтение)\n"
        "• get_schedule — занятия на выбранную дату (чтение)\n"
        "• add_reminder — создание напоминания (изменение, нужно подтверждение)\n\n"
        "Ресурсы:\n"
        "• schedule://current-week — занятия с понедельника по воскресенье текущей недели, "
        "по датам (/week)"
    )


async def test_tools_when_server_unavailable(make_env):
    chat = Chatter(await make_env(connect=False))
    (reply,) = await chat.send("/tools")
    assert reply.text == TOOLS_UNAVAILABLE_TEXT


async def test_timezone_set_show_and_reject(make_env):
    # Arrange
    env = await make_env(timezone=None)
    chat = Chatter(env)
    # Act / Assert: не задана
    assert (await chat.send("/timezone"))[0].text == TIMEZONE_NOT_SET_TEXT
    # Act / Assert: неизвестная зона не сохраняется
    assert (await chat.send("/timezone Mars/Olympus"))[0].text == UNKNOWN_TIMEZONE_TEXT
    assert env.storage.timezones == {}
    # Act / Assert: сохранение с нормализацией регистра и показом местного времени
    (saved,) = await chat.send("/timezone europe/moscow")
    assert saved.text == (
        "Часовой пояс сохранён: Europe/Moscow. "
        "Местное время: 12.10.2026 09:00 (Europe/Moscow, UTC+03:00)."
    )
    assert env.storage.timezones == {ALICE: "Europe/Moscow"}
    (shown,) = await chat.send("/timezone")
    assert shown.text.startswith("Часовой пояс: Europe/Moscow. Сейчас там 12.10.2026 09:00")


async def test_why_before_and_after_request(make_env):
    # Arrange
    env = await make_env(
        call("get_weather", {"city": "Санкт-Петербург"}, comment="нужны актуальные данные"),
        text("12,5 °C"),
    )
    chat = Chatter(env)
    assert (await chat.send("/why"))[0].text == "Вы ещё не отправляли запросов агенту."
    # Act
    await chat.send("Какая сейчас погода в Санкт-Петербурге?")
    (why,) = await chat.send("/why")
    # Assert
    lines = why.text.splitlines()
    assert lines[0] == "Последний запрос: 12.10.2026 09:00 (Europe/Moscow, UTC+03:00)"
    assert lines[1:5] == [
        "Действие: вызов инструмента get_weather",
        "Аргументы: город «Санкт-Петербург»",
        "Проверка: пройдена",
        "Выполнение: успешно",
    ]
    assert lines[-1] == "Причина: нужны актуальные данные"
    assert "Роль:" not in why.text and str(ALICE) not in why.text


async def test_week_uses_resource(make_env):
    chat = Chatter(await make_env())
    (reply,) = await chat.send("/week")
    assert reply.text.startswith("Расписание на неделю 12.10–18.10.2026 (Europe/Moscow):")
    assert "Понедельник, 12.10:\n  10:00–11:30 Матанализ (ауд. 101)" in reply.text
    assert "Воскресенье, 18.10:\n  занятий нет" in reply.text


async def test_reminder_card_and_confirm_button(make_env):
    # Arrange
    env = await make_env(
        call("add_reminder", {"text": "сдать лабу", "remind_at": "2026-10-13T10:00:00+03:00"})
    )
    chat = Chatter(env)
    # Act: сообщение → карточка с кнопками
    (card,) = await chat.send("Напомни завтра в 10 сдать лабу")
    buttons = card.reply_markup.inline_keyboard[0]
    assert [b.text for b in buttons] == ["✅ Подтвердить", "✖️ Отменить"]
    confirm_data = buttons[0].callback_data
    assert ActionCallback.unpack(confirm_data).decision == "ok"
    assert len(confirm_data) <= 64
    # Act: чужое нажатие ничего не создаёт
    foreign = await chat.press(confirm_data, user=BOB, owner=BOB)
    assert [m.text for m in foreign if isinstance(m, SendMessage)] == [
        "Действие не найдено или уже недоступно."
    ]
    assert env.reminders.rows == {}
    # Act: нажатие владельца
    methods = await chat.press(confirm_data)
    # Assert
    assert any(isinstance(m, AnswerCallbackQuery) for m in methods)
    assert any(isinstance(m, EditMessageReplyMarkup) for m in methods), "кнопки убраны"
    (done,) = [m for m in methods if isinstance(m, SendMessage)]
    assert done.text.startswith("Напоминание создано: #1 — «сдать лабу» на 13.10.2026 10:00")
    assert len(env.reminders.rows) == 1


async def test_cancel_button(make_env):
    env = await make_env(
        call("add_reminder", {"text": "сдать лабу", "remind_at": "2026-10-13T10:00:00+03:00"})
    )
    chat = Chatter(env)
    (card,) = await chat.send("Напомни завтра в 10 сдать лабу")
    cancel_data = card.reply_markup.inline_keyboard[0][1].callback_data
    methods = await chat.press(cancel_data)
    assert [m.text for m in methods if isinstance(m, SendMessage)] == [
        "Отменено. Напоминание не создано."
    ]
    assert env.reminders.rows == {}
