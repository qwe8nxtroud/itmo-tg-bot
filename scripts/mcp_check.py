"""Независимая проверка MCP-сервера: `python -m scripts.mcp_check [--weather Казань]`.

Скрипт — посторонний MCP-клиент: он запускает `python -m mcp_server` по stdio без секрета
доверенного контекста и без доступа к БД бота, проходит обнаружение возможностей и
вызывает инструменты. Ожидаемые результаты: три инструмента со схемами, один ресурс,
расписание и неделя читаются, а прямой вызов add_reminder и лишний аргумент owner_id
отклоняются. Код возврата 0 — все ожидания выполнены.
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent


def show(title: str, value: object) -> None:
    print(f"\n== {title}")
    print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))


def error_code(result) -> str:
    text = " ".join(getattr(block, "text", "") for block in result.content)
    try:
        return json.loads(text[text.index("{") :])["code"]
    except (ValueError, KeyError):
        return text[:120]


async def check(weather_city: str | None) -> list[str]:
    failures: list[str] = []
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        env={"SCHEDULE_PATH": str(ROOT / "data" / "schedule.json"), "LOG_LEVEL": "WARNING"},
        cwd=str(ROOT),
    )
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        show(
            "tools/list",
            [
                {
                    "name": tool.name,
                    "readOnlyHint": tool.annotations.read_only_hint if tool.annotations else None,
                    "input": sorted(tool.input_schema.get("properties", {})),
                    "additionalProperties": tool.input_schema.get("additionalProperties"),
                    "output": sorted((tool.output_schema or {}).get("properties", {})),
                }
                for tool in tools
            ],
        )
        if {tool.name for tool in tools} != {"get_weather", "get_schedule", "add_reminder"}:
            failures.append("ожидались три инструмента")
        if not all(tool.output_schema for tool in tools):
            failures.append("у каждого инструмента должна быть outputSchema")

        resources = (await client.list_resources()).resources
        show("resources/list", [{"uri": str(r.uri), "mimeType": r.mime_type} for r in resources])
        if [str(r.uri) for r in resources] != ["schedule://current-week"]:
            failures.append("ожидался ресурс schedule://current-week")

        today = date.today().isoformat()
        result = await client.call_tool("get_schedule", {"date": today})
        show(f"tools/call get_schedule {today}", result.structured_content)
        if result.is_error:
            failures.append("get_schedule вернул ошибку")

        week = await client.read_resource("schedule://current-week")
        data = json.loads(week.contents[0].text)
        show("resources/read schedule://current-week", f"{data['week_start']} — {data['week_end']}")

        result = await client.call_tool(
            "add_reminder", {"text": "проверка", "remind_at": "2030-01-01T10:00:00+03:00"}
        )
        code = error_code(result) if result.is_error else "создано!"
        show("tools/call add_reminder без доверенного контекста", code)
        if code != "untrusted_context":
            failures.append("add_reminder должен отклоняться без доверенного контекста")

        result = await client.call_tool("get_weather", {"city": "Казань", "owner_id": 1})
        code = error_code(result) if result.is_error else "выполнено!"
        show("tools/call get_weather с лишним owner_id", code)
        if code != "invalid_arguments":
            failures.append("лишние аргументы должны отклоняться")

        if weather_city:
            result = await client.call_tool("get_weather", {"city": weather_city})
            show(
                f"tools/call get_weather {weather_city}",
                result.structured_content if not result.is_error else error_code(result),
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--weather", help="дополнительно запросить погоду (нужен интернет)")
    failures = asyncio.run(check(parser.parse_args().weather))
    print("\nИтог:", "все ожидания выполнены" if not failures else "; ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
