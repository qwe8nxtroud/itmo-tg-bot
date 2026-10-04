"""Единый формат ошибок инструментов: код для программы и текст для пользователя."""

import json


class ToolFailure(Exception):
    """Ожидаемая ошибка инструмента. Сообщение не содержит секретов и стека вызовов."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_json(self) -> str:
        # Клиент разбирает этот JSON из текстового блока результата с isError: true.
        return json.dumps({"code": self.code, "message": self.message}, ensure_ascii=False)
