"""Оценка маршрутизации на базовом наборе: `python -m scripts.eval_routing --label v1`.

Каждый запрос проходит через настоящий `Agent` с настоящей моделью (настройки из .env)
и схемами, обнаруженными у настоящего MCP-сервера. Время и часовой пояс фиксированы
из `evaluation_context` набора. Инструменты не выполняются: шлюз записывает первый
проверенный вызов и останавливает цикл, а напоминание только готовится (без подтверждения).

Результат: таблица и метрики в `docs/evaluation/results/lab2-<label>.md` и сырые данные
в `.json` рядом. `--only id1,id2` повторяет выбранные запросы (вторая версия решения).
"""

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from app.agent import Agent, AgentRequest
from app.assistant import Assistant
from app.config import Settings
from app.llm import LLMClient, LLMError
from app.mcp_client import McpGateway, ToolCallError, stdio_parameters
from tests.fakes import FakeStorage

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "docs" / "evaluation" / "lab2-routing.json"
RESULTS = ROOT / "docs" / "evaluation" / "results"
TOOLS = {"get_weather", "get_schedule", "add_reminder"}


class _Stop(ToolCallError):
    """Остановка цикла после первого проверенного вызова инструмента."""


class RecordingGateway:
    """Обнаруженные схемы настоящего сервера; вызовы записываются, но не выполняются."""

    def __init__(self, real: McpGateway) -> None:
        self.tools = real.tools
        self.resources = real.resources
        self.available = True
        self.trust_secret = b""
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict, **_) -> dict:
        self.calls.append((name, arguments))
        raise _Stop("eval_stop", "оценка: вызов записан")


class RecordingLLM:
    """Обёртка над настоящим клиентом: запоминает первый ответ модели по запросу."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self.model = llm.model
        self.responses: list = []

    async def chat(self, messages, *, temperature, tools):
        response = await self._llm.chat(messages, temperature=temperature, tools=tools)
        self.responses.append(response)
        return response

    async def complete(self, messages, *, temperature):
        return await self._llm.complete(messages, temperature=temperature)


def _norm(value: object) -> str:
    text = str(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s,-]", " ", text).split())


def arguments_match(expected: dict, actual: dict | None) -> bool:
    if actual is None:
        return False
    for key, value in expected.items():
        if key not in actual:
            return False
        if key == "remind_at":
            try:
                want, got = datetime.fromisoformat(value), datetime.fromisoformat(actual[key])
            except ValueError:
                return False
            if want != got or want.utcoffset() != got.utcoffset():
                return False
        elif key == "date":
            if actual[key] != value:
                return False
        elif _norm(actual[key]) != _norm(value):
            return False
    return True


async def run_case(agent: Agent, gateway, llm, storage, case: dict, index: int) -> dict:
    gateway.calls.clear()
    llm.responses.clear()
    storage.history.clear()
    storage.actions.clear()
    request = AgentRequest(user_id=1, chat_id=1, message_id=index, text=case["query"])
    error = None
    try:
        reply = await agent.reply(request)
        answer = reply.text
    except LLMError as exc:
        answer, error = "", f"{type(exc).__name__}: {exc}"
    event = await storage.last_message_event(1)
    raw = None
    if llm.responses and llm.responses[0].tool_calls:
        first = llm.responses[0].tool_calls[0]
        raw = {"name": first.name, "arguments": first.arguments}
    if gateway.calls:
        action, arguments = gateway.calls[0]
    elif storage.actions:
        (pending,) = storage.actions.values()
        action, arguments = pending.tool, pending.arguments
    elif event and event.action == "clarify":
        action, arguments = "clarify", {}
    elif event and event.action == "rejected" and event.tools:
        action, arguments = event.tools.split(", ")[0], None
    elif event and event.action == "respond":
        action, arguments = "respond", {}
    else:
        action, arguments = (event.action if event else "error"), None
    expected = case["expected_action"]
    needs_tool = expected in TOOLS
    args_ok = arguments_match(case["expected_arguments"], arguments) if needs_tool else None
    return {
        "id": case["id"],
        "query": case["query"],
        "expected_action": expected,
        "expected_arguments": case["expected_arguments"],
        "actual_action": action,
        "actual_arguments": arguments,
        "model_tool_call": raw,
        "validation": event.validation if event else None,
        "answer": answer[:300],
        "error": error,
        "action_ok": action == expected,
        "arguments_ok": args_ok,
    }


def render(meta: dict, rows: list[dict]) -> str:
    lines = [
        f"# Маршрутизация ЛР2 — {meta['label']}",
        "",
        f"- Модель: `{meta['model']}`, temperature агента {meta['temperature']}",
        f"- Дата прогона: {meta['run_at']}",
        f"- Контекст набора: {meta['now']} ({meta['timezone']})",
        f"- Запросов: {len(rows)}",
        "",
        "| № | Запрос | Ожидаемое действие | Фактическое действие | Аргументы корректны "
        "| Результат |",
        "|---:|---|---|---|---|---|",
    ]
    for number, row in enumerate(rows, start=1):
        args = "—" if row["arguments_ok"] is None else ("да" if row["arguments_ok"] else "нет")
        ok = row["action_ok"] and row["arguments_ok"] is not False
        actual = row["actual_action"]
        if row["actual_arguments"]:
            actual += " " + json.dumps(row["actual_arguments"], ensure_ascii=False)
        lines.append(
            f"| {number} | {row['query']} | {row['expected_action']} | {actual} | {args} "
            f"| {'✅' if ok else '❌'} |"
        )
    tool_rows = [r for r in rows if r["arguments_ok"] is not None]
    routing = sum(r["action_ok"] for r in rows)
    arguments = sum(bool(r["arguments_ok"]) for r in tool_rows)
    lines += [
        "",
        f"- routing_accuracy = {routing}/{len(rows)} = {routing / len(rows):.2f}",
        f"- argument_accuracy = {arguments}/{len(tool_rows)} = "
        f"{arguments / max(len(tool_rows), 1):.2f}",
        "",
        "Ошибочные случаи (сырые вызовы модели — в JSON рядом):",
    ]
    failed = [r for r in rows if not r["action_ok"] or r["arguments_ok"] is False]
    lines += [
        f"- `{r['id']}`: ожидалось {r['expected_action']} {r['expected_arguments']}, "
        f"получено {r['actual_action']} {r['actual_arguments']}; проверка: {r['validation']}"
        for r in failed
    ] or ["- нет"]
    return "\n".join(lines) + "\n"


async def main_async(args) -> int:
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    context = dataset["evaluation_context"]
    cases = dataset["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]
    settings = Settings.load(args.env_file)
    now = datetime.fromisoformat(context["now"])
    real_gateway = McpGateway(stdio_parameters(settings, "eval"))
    await real_gateway.start(wait=20)
    if not real_gateway.available:
        print("MCP-сервер не запустился:", real_gateway.last_error)
        return 1
    try:
        async with aiohttp.ClientSession() as session:
            llm = RecordingLLM(
                LLMClient(
                    session,
                    base_url=settings.llm_api_base_url,
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                    timeout=settings.llm_timeout_seconds,
                    max_tokens=settings.llm_max_tokens,
                    project=settings.llm_api_project,
                )
            )
            storage = FakeStorage()
            storage.timezones[1] = context["timezone"]
            gateway = RecordingGateway(real_gateway)
            agent = Agent(
                Assistant(storage, llm, history_max_messages=20, history_max_chars=12000),  # type: ignore[arg-type]
                storage,  # type: ignore[arg-type]
                llm,  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                temperature=settings.agent_temperature,
                history_max_messages=20,
                history_max_chars=12000,
                clock=lambda: now.astimezone(UTC),
            )
            rows = []
            for index, case in enumerate(cases, start=1):
                row = await run_case(agent, gateway, llm, storage, case, index)
                rows.append(row)
                print(f"{row['id']}: {row['actual_action']} {row['actual_arguments']}")
    finally:
        await real_gateway.close()
    meta = {
        "label": args.label,
        "model": settings.llm_model.split("/", 3)[-1]
        if "gpt://" in settings.llm_model
        else settings.llm_model,
        "temperature": settings.agent_temperature,
        "run_at": datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "now": context["now"],
        "timezone": context["timezone"],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    report = render(meta, rows)
    (RESULTS / f"lab2-{args.label}.md").write_text(report, encoding="utf-8")
    (RESULTS / f"lab2-{args.label}.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n" + report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Оценка маршрутизации агента (ЛР2)")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--label", default="v1", help="имя версии результата")
    parser.add_argument("--only", help="id запросов через запятую")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
