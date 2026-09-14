"""Сквозные тесты обработчиков: aiogram Dispatcher с подменённой сессией бота."""

import itertools
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendChatAction, SendMessage
from aiogram.types import Chat, Message, PhotoSize, Update, User

from app.assistant import Assistant
from app.handlers.dialog import (
    BAD_TEMPERATURE_TEXT,
    ERROR_EMPTY_TEXT,
    ERROR_INTERNAL_TEXT,
    ERROR_TIMEOUT_TEXT,
    ERROR_UNAVAILABLE_TEXT,
    NOT_TEXT_MESSAGE,
    RESET_TEXT,
    START_TEXT,
    UNKNOWN_COMMAND_TEXT,
    router,
)
from app.llm import LLMEmptyResponseError, LLMTimeoutError, LLMUnavailableError
from app.prompts import QUIZ, STUDY, TRANSLATE
from app.storage import UserSettings
from tests.fakes import FakeLLM, FakeStorage

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
ALICE, BOB = 1001, 1002

# Роутер может быть подключён только к одному диспетчеру, поэтому он общий для модуля.
dispatcher = Dispatcher()
dispatcher.include_router(router)
counter = itertools.count(1)


class Harness:
    """Подаёт обновления в диспетчер и собирает отправленные ботом сообщения."""

    def __init__(
        self, llm: FakeLLM | None = None, *, max_messages: int = 20, max_chars: int = 12000
    ):
        self.bot = Bot(TOKEN)
        self.bot.session = AsyncMock()
        self.storage = FakeStorage()
        self.llm = llm or FakeLLM()
        self.assistant = Assistant(
            self.storage,  # type: ignore[arg-type]
            self.llm,  # type: ignore[arg-type]
            history_max_messages=max_messages,
            history_max_chars=max_chars,
        )

    async def send(self, chat_id: int, text: str | None = None, **fields) -> list[str]:
        message = Message(
            message_id=next(counter),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=fields.pop("chat_type", "private")),
            from_user=User(id=chat_id, is_bot=False, first_name="Студент"),
            text=text,
            **fields,
        )
        before = len(self.bot.session.call_args_list)
        await dispatcher.feed_update(
            self.bot, Update(update_id=next(counter), message=message), assistant=self.assistant
        )
        sent = [
            call.args[1]
            for call in self.bot.session.call_args_list[before:]
            if isinstance(call.args[1], SendMessage)
        ]
        assert all(method.chat_id == chat_id and method.parse_mode is None for method in sent)
        return [method.text for method in sent]


@pytest.fixture
def harness():
    return Harness()


async def test_start_describes_commands(harness):
    # Act
    replies = await harness.send(ALICE, "/start")
    # Assert
    assert replies == [START_TEXT]
    assert harness.llm.calls == []


async def test_text_goes_to_model_with_default_mode_and_temperature(harness):
    # Arrange
    harness.llm.responses.append("Список изменяем, кортеж — нет.")
    # Act
    replies = await harness.send(ALICE, "Чем список отличается от кортежа?")
    # Assert
    assert replies == ["Список изменяем, кортеж — нет."]
    messages, temperature = harness.llm.calls[0]
    assert temperature == 0.7
    assert messages[0] == {"role": "system", "content": STUDY.system_prompt}
    assert messages[-1] == {"role": "user", "content": "Чем список отличается от кортежа?"}
    assert [m["role"] for m in messages] == ["system", "user"]


async def test_history_is_passed_in_order_and_stored(harness):
    # Arrange
    harness.llm.responses.extend(["ответ 1", "ответ 2"])
    await harness.send(ALICE, "вопрос 1")
    # Act
    await harness.send(ALICE, "вопрос 2")
    # Assert
    messages, _ = harness.llm.calls[1]
    assert [(m["role"], m["content"]) for m in messages[1:]] == [
        ("user", "вопрос 1"),
        ("assistant", "ответ 1"),
        ("user", "вопрос 2"),
    ]


async def test_histories_of_two_users_do_not_mix(harness):
    # Arrange
    harness.llm.responses.extend(["ответ Алисе", "ответ Бобу", "снова Алисе"])
    await harness.send(ALICE, "секрет Алисы")
    await harness.send(BOB, "секрет Боба")
    # Act
    await harness.send(ALICE, "что я говорила?")
    # Assert
    messages, _ = harness.llm.calls[2]
    contents = [m["content"] for m in messages]
    assert "секрет Алисы" in contents and "ответ Алисе" in contents
    assert all("Боб" not in content for content in contents)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]


async def test_history_limits_keep_current_request_and_order():
    # Arrange: лимит на 4 сообщения истории и небольшой объём.
    harness = Harness(max_messages=4, max_chars=len(STUDY.system_prompt) + 60)
    for index in range(5):
        harness.llm.responses.append(f"ответ {index}")
        await harness.send(ALICE, f"вопрос {index}")
    harness.llm.responses.append("итог")
    # Act
    await harness.send(ALICE, "последний вопрос")
    # Assert
    messages, _ = harness.llm.calls[-1]
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "последний вопрос"}
    history = messages[1:-1]
    assert len(history) <= 4
    assert [m["role"] for m in history] == ["user", "assistant"] * (len(history) // 2)
    assert sum(len(m["content"]) for m in messages) <= len(STUDY.system_prompt) + 60
    numbers = [int(m["content"].split()[-1]) for m in history]
    assert numbers == sorted(numbers)
    assert all(number >= 3 for number in numbers), "старые сообщения отброшены первыми"


@pytest.mark.parametrize("mode", [TRANSLATE, QUIZ, STUDY])
async def test_mode_switch_clears_history_and_saves_mode(harness, mode):
    # Arrange
    await harness.send(ALICE, "/settings 0.3")
    harness.llm.responses.append("старый ответ")
    await harness.send(ALICE, "старый вопрос")
    calls_before = len(harness.llm.calls)
    # Act
    replies = await harness.send(ALICE, f"/{mode.command}")
    harness.llm.responses.append("новый ответ")
    await harness.send(ALICE, "новый вопрос")
    # Assert
    assert replies == [f"Режим «{mode.title}» включён. История очищена. {mode.hint}"]
    assert harness.storage.settings[ALICE] == UserSettings(mode=mode.command, temperature=0.3)
    assert len(harness.llm.calls) == calls_before + 1, "сама команда модели не отправляется"
    messages, temperature = harness.llm.calls[-1]
    assert temperature == 0.3
    assert messages[0] == {"role": "system", "content": mode.system_prompt}
    assert "старый вопрос" not in [m["content"] for m in messages]
    few_shot = [(m["role"], m["content"]) for m in messages[1 : 1 + 2 * len(mode.few_shot)]]
    expected = [
        pair
        for question, answer in mode.few_shot
        for pair in (("user", question), ("assistant", answer))
    ]
    assert few_shot == expected
    assert messages[-1] == {"role": "user", "content": "новый вопрос"}


async def test_reset_clears_only_caller_history(harness):
    # Arrange
    await harness.send(ALICE, "/translate")
    await harness.send(ALICE, "/settings 1.0")
    harness.llm.responses.extend(["a", "b"])
    await harness.send(ALICE, "привет")
    await harness.send(BOB, "hello")
    # Act
    replies = await harness.send(ALICE, "/reset")
    # Assert
    assert replies == [RESET_TEXT]
    assert harness.storage.history.get(ALICE, []) == []
    assert len(harness.storage.history[BOB]) == 2
    assert harness.storage.settings[ALICE] == UserSettings(mode="translate", temperature=1.0)


async def test_settings_shows_mode_model_and_temperature(harness):
    # Arrange
    await harness.send(ALICE, "/quiz")
    # Act
    replies = await harness.send(ALICE, "/settings")
    # Assert
    assert replies == [
        "Режим: Тренажёр (/quiz)\nМодель: test-model\nTemperature: 0.7\n\n"
        "Изменить temperature: /settings <значение>, где значение — 0.0, 0.3, 0.7, 1.0"
    ]


@pytest.mark.parametrize(
    ("argument", "expected"), [("0.3", 0.3), ("0,3", 0.3), ("1", 1.0), ("0.0", 0.0), ("-0.0", 0.0)]
)
async def test_settings_accepts_allowed_values(harness, argument, expected):
    # Act
    replies = await harness.send(ALICE, f"/settings {argument}")
    await harness.send(ALICE, "вопрос")
    # Assert
    assert replies == [f"Temperature: {expected:.1f}. Применится начиная со следующего запроса."]
    assert harness.storage.settings[ALICE].temperature == expected
    assert str(harness.storage.settings[ALICE].temperature) == str(expected)
    assert harness.llm.calls[-1][1] == expected


@pytest.mark.parametrize("argument", ["0.5", "2", "abc", "1.0.0", "nan", "0.3 0.7"])
async def test_settings_rejects_invalid_values_before_api_call(harness, argument):
    # Arrange
    await harness.send(ALICE, "/settings 0.3")
    # Act
    replies = await harness.send(ALICE, f"/settings {argument}")
    # Assert
    assert replies == [BAD_TEMPERATURE_TEXT]
    assert harness.storage.settings[ALICE].temperature == 0.3
    assert harness.llm.calls == []


async def test_settings_of_one_user_do_not_affect_another(harness):
    # Arrange
    await harness.send(ALICE, "/settings 0.0")
    # Act
    await harness.send(BOB, "вопрос Боба")
    await harness.send(ALICE, "вопрос Алисы")
    # Assert
    assert harness.llm.calls[0][1] == 0.7
    assert harness.llm.calls[1][1] == 0.0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMTimeoutError("нет ответа за 60 с"), ERROR_TIMEOUT_TEXT),
        (LLMUnavailableError("сервис вернул HTTP 500 secret-key-123"), ERROR_UNAVAILABLE_TEXT),
        (LLMEmptyResponseError("ответ модели пуст"), ERROR_EMPTY_TEXT),
        (RuntimeError("secret-key-123"), ERROR_INTERNAL_TEXT),
    ],
)
async def test_llm_error_becomes_safe_message_and_is_not_stored(harness, error, expected):
    # Arrange
    harness.llm.responses.extend([error, "после ошибки"])
    # Act
    replies = await harness.send(ALICE, "сломай")
    await harness.send(ALICE, "ещё раз")
    # Assert
    assert replies == [expected]
    assert "secret-key-123" not in expected
    assert harness.storage.history.get(ALICE, [])[0].content == "ещё раз"
    messages, _ = harness.llm.calls[1]
    assert "сломай" not in [m["content"] for m in messages]


async def test_long_answer_is_split_in_order_without_loss(harness):
    # Arrange
    answer = "\n".join(f"строка {index}: " + "ж" * 90 for index in range(120))
    assert len(answer) > 2 * 4096
    harness.llm.responses.append(answer)
    # Act
    replies = await harness.send(ALICE, "расскажи подробно")
    # Assert
    assert len(replies) >= 3
    assert all(len(part) <= 4096 for part in replies)
    assert "".join(replies) == answer
    assert harness.storage.history[ALICE][-1].content == answer


async def test_unknown_command_is_not_sent_to_model(harness):
    # Act
    replies = await harness.send(ALICE, "/foo bar")
    # Assert
    assert replies == [UNKNOWN_COMMAND_TEXT]
    assert harness.llm.calls == []


async def test_photo_gets_text_only_reply(harness):
    # Act
    replies = await harness.send(
        ALICE, photo=[PhotoSize(file_id="a", file_unique_id="b", width=1, height=1)]
    )
    # Assert
    assert replies == [NOT_TEXT_MESSAGE]
    assert harness.llm.calls == []


async def test_group_chat_is_ignored(harness):
    # Act
    replies = await harness.send(ALICE, "привет всем", chat_type="group")
    # Assert
    assert replies == []
    assert harness.llm.calls == []


async def test_typing_status_is_sent_while_answering(harness):
    # Arrange: «сетевой» вызов длиннее разрешения таймера Windows (~16 мс).
    harness.llm.delay = 0.2
    # Act
    await harness.send(ALICE, "вопрос")
    # Assert
    actions = [
        c.args[1]
        for c in harness.bot.session.call_args_list
        if isinstance(c.args[1], SendChatAction)
    ]
    assert actions and actions[0].action == "typing" and actions[0].chat_id == ALICE
