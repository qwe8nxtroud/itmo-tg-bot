"""Чистые функции сборки контекста, усечения истории и нарезки длинных ответов."""

import pytest

from app.assistant import build_messages, trim_history
from app.handlers.dialog import split_text
from app.prompts import STUDY, TRANSLATE
from app.storage import HistoryMessage


def exchange(count: int, size: int = 1) -> list[HistoryMessage]:
    history = []
    for index in range(count):
        history.append(HistoryMessage("user", f"q{index}" + "?" * (size - 2)))
        history.append(HistoryMessage("assistant", f"a{index}" + "." * (size - 2)))
    return history


def test_trim_keeps_newest_messages_in_order():
    # Arrange
    history = exchange(5)
    # Act
    trimmed = trim_history(history, max_messages=4, max_chars=1000)
    # Assert
    assert trimmed == history[-4:]
    assert [m.role for m in trimmed] == ["user", "assistant", "user", "assistant"]


def test_trim_by_chars_drops_oldest_first():
    # Arrange
    history = exchange(3, size=10)
    # Act
    trimmed = trim_history(history, max_messages=100, max_chars=45)
    # Assert
    assert trimmed == history[-4:]
    assert sum(len(m.content) for m in trimmed) <= 45


def test_trim_never_starts_with_assistant_reply():
    # Arrange
    history = exchange(3)
    # Act
    trimmed = trim_history(history, max_messages=3, max_chars=1000)
    # Assert
    assert trimmed == history[-2:]
    assert trimmed[0].role == "user"


@pytest.mark.parametrize("max_messages", [0, 1])
def test_trim_edge_limits(max_messages):
    # Act / Assert
    assert trim_history(exchange(2), max_messages=max_messages, max_chars=1000) == []
    assert trim_history([], max_messages=10, max_chars=1000) == []


def test_build_messages_places_instruction_history_and_request_in_order():
    # Arrange
    history = exchange(2)
    # Act
    messages = build_messages(STUDY, history, "новый", max_messages=10, max_chars=10_000)
    # Assert
    assert messages[0] == {"role": "system", "content": STUDY.system_prompt}
    assert messages[1:-1] == [{"role": m.role, "content": m.content} for m in history]
    assert messages[-1] == {"role": "user", "content": "новый"}


def test_build_messages_puts_few_shot_right_after_system():
    # Act
    messages = build_messages(TRANSLATE, [], "текст", max_messages=10, max_chars=10_000)
    # Assert
    pairs = messages[1:-1]
    assert len(pairs) == 2 * len(TRANSLATE.few_shot)
    for index, (question, answer) in enumerate(TRANSLATE.few_shot):
        assert pairs[2 * index] == {"role": "user", "content": question}
        assert pairs[2 * index + 1] == {"role": "assistant", "content": answer}


def test_build_messages_reserves_space_for_instruction_and_request():
    # Arrange: бюджет покрывает инструкцию, запрос и ровно одну пару истории.
    history = exchange(3, size=10)
    budget = len(STUDY.system_prompt) + len("запрос") + 20
    # Act
    messages = build_messages(STUDY, history, "запрос", max_messages=10, max_chars=budget)
    # Assert
    assert [m["content"] for m in messages[1:-1]] == [history[-2].content, history[-1].content]
    assert sum(len(m["content"]) for m in messages) <= budget


def test_build_messages_keeps_request_when_it_exceeds_budget():
    # Arrange
    request = "х" * 500
    # Act
    messages = build_messages(STUDY, exchange(2), request, max_messages=10, max_chars=100)
    # Assert
    assert messages == [
        {"role": "system", "content": STUDY.system_prompt},
        {"role": "user", "content": request},
    ]


@pytest.mark.parametrize(
    ("text", "expected_parts"),
    [("а" * 4096, 1), ("а" * 4097, 2), ("б" * 10_000, 3), ("", 1)],
)
def test_split_text_hard_cut(text, expected_parts):
    # Act
    parts = split_text(text)
    # Assert
    assert len(parts) == expected_parts
    assert "".join(parts) == text
    assert all(len(part) <= 4096 for part in parts)


def test_split_text_prefers_newlines_then_spaces():
    # Arrange
    text = "строка один\nстрока два\n" + "слово " * 800 + "конец"
    # Act
    parts = split_text(text, limit=100)
    # Assert
    assert "".join(parts) == text
    assert all(len(part) <= 100 for part in parts)
    assert parts[0] == "строка один\nстрока два\n"
    assert all(part.endswith((" ", "\n")) for part in parts[:-1])
