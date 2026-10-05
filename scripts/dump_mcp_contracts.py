"""Справочник контрактов MCP из работающего сервера: `python -m scripts.dump_mcp_contracts`.

Документ `docs/mcp-contracts.md` не пишется руками: скрипт поднимает настоящий MCP-сервер
(в памяти, с детерминированными погодой и часами из тестовых фикстур), проходит
обнаружение и записывает схемы и примеры ответов. Тест `tests/test_docs.py` проверяет,
что документ совпадает с кодом.
"""

import asyncio
import json
from pathlib import Path

from mcp import Client

from tests.fakes import build_test_server

OUT = Path(__file__).resolve().parent.parent / "docs" / "mcp-contracts.md"


def _json(value: object) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


async def render() -> str:
    server, _ = build_test_server()
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        examples = {
            "get_weather": (
                await client.call_tool("get_weather", {"city": "Санкт-Петербург"})
            ).structured_content,
            "get_schedule": (
                await client.call_tool("get_schedule", {"date": "2026-10-12"})
            ).structured_content,
        }
        week = json.loads((await client.read_resource("schedule://current-week")).contents[0].text)
        refused = await client.call_tool(
            "add_reminder", {"text": "проверка", "remind_at": "2026-10-13T18:30:00+03:00"}
        )
    lines = [
        "# Контракты MCP-сервера",
        "",
        "> Документ сгенерирован `python -m scripts.dump_mcp_contracts` из работающего сервера",
        "> (`tools/list`, `resources/list`, вызовы с тестовыми данными на 12.10.2026). Не правьте",
        "> вручную: тест `tests/test_docs.py` сверяет его с кодом.",
        "",
        "Сервер: `itmo-student-assistant`, транспорт `stdio` (`python -m mcp_server`).",
        "",
        "## Инструменты",
        "",
        "| Инструмент | Назначение | Аннотации |",
        "|---|---|---|",
    ]
    for tool in tools:
        hints = tool.annotations.model_dump(exclude_none=True, by_alias=True)
        rendered = ", ".join(
            f"`{key}: {json.dumps(value, ensure_ascii=False)}`" for key, value in hints.items()
        )
        summary = tool.description.strip().splitlines()[0]
        lines.append(f"| `{tool.name}` | {summary} | {rendered} |")
    for tool in tools:
        lines += [
            "",
            f"### `{tool.name}`",
            "",
            tool.description.strip(),
            "",
            "**inputSchema**",
            "",
            _json(tool.input_schema),
            "",
            "**outputSchema**",
            "",
            _json(tool.output_schema),
        ]
        if tool.name in examples:
            lines += ["", "**Пример `structuredContent`**", "", _json(examples[tool.name])]
    lines += [
        "",
        "Пример отказа без доверенного контекста (`isError: true`, текстовый блок):",
        "",
        "```text",
        refused.content[0].text,
        "```",
        "",
        "## Ресурсы",
        "",
        "| URI | Имя | MIME | Описание |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| `{item.uri}` | {item.name} | `{item.mime_type}` | {item.description} |"
        for item in resources
    ]
    lines += [
        "",
        "Пример содержимого `schedule://current-week` (первый день недели и поля верхнего уровня):",
        "",
        _json(
            {**{key: week[key] for key in week if key != "days"}, "days": [week["days"][0], "…"]}
        ),
        "",
        "## Ошибки",
        "",
        "Ошибка инструмента — результат с `isError: true`, в текстовом блоке JSON",
        '`{"code": "...", "message": "..."}` (SDK может добавить перед ним префикс).',
        "Коды и их смысл — в [spec.md](spec.md), раздел 3.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    OUT.write_text(asyncio.run(render()), encoding="utf-8")
    print(f"Записан {OUT.relative_to(OUT.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
