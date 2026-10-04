"""Эксперимент ЛР1: `python -m scripts.experiment_lab1`.

1. Влияние temperature: режим /study, один запрос, по 3 независимых запуска для 0.0, 0.3,
   0.7 и 1.0 (12 ответов). Каждый запуск — с пустой историей, как после /reset;
   модель, промпт и остальные параметры одинаковы.
2. Неполная инструкция против улучшенной (R.C.T.F.): тот же запрос, 3 запуска каждой
   при temperature 0.3.

Формат ответа проверяется по признакам, названным до эксперимента (см. FORMAT_RULES).
Результат: `docs/experiments/lab1-temperature.json` (все ответы) и `.md` (таблицы).
"""

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from app.assistant import TEMPERATURES, build_messages
from app.config import Settings
from app.llm import LLMClient, LLMError
from app.prompts import STUDY, Mode

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "experiments"
QUERY = "Что такое рекурсия и когда её стоит использовать?"
RUNS = 3
# Неполная инструкция: только роль, без контекста, ограничений и формата.
WEAK = Mode(
    command="weak",
    title="Неполная инструкция",
    summary="",
    hint="",
    system_prompt="Ты помощник по программированию.",
)
FORMAT_RULES = "есть блок кода ```, не длиннее 200 слов, названа типичная ошибка, ответ на русском"


def check_format(text: str) -> dict:
    words = len(re.findall(r"\w+", text))
    checks = {
        "code_block": "```" in text,
        "max_200_words": words <= 200,
        "typical_mistake": bool(re.search(r"ошибк", text, re.IGNORECASE)),
        "russian": len(re.findall(r"[а-яё]", text, re.IGNORECASE)) > len(text) * 0.3,
    }
    return {"words": words, "checks": checks, "ok": all(checks.values())}


async def ask(llm: LLMClient, mode: Mode, temperature: float) -> dict:
    messages = build_messages(mode, [], QUERY, max_messages=20, max_chars=12000)
    try:
        response = await llm.complete(messages, temperature=temperature)
    except LLMError as exc:
        return {"temperature": temperature, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "temperature": temperature,
        "chars": len(response.text),
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        **check_format(response.text),
        "text": response.text,
    }


def table(rows: list[dict], start: int = 1) -> list[str]:
    lines = [
        "| № | temperature | Символов | Слов | Формат | Код | ≤200 слов | Ошибка новичка "
        "| Токены (запрос/ответ) |",
        "|---:|---:|---:|---:|---|---|---|---|---|",
    ]
    for number, row in enumerate(rows, start=start):
        if "error" in row:
            lines.append(
                f"| {number} | {row['temperature']:.1f} | — | — | ошибка: {row['error']} |"
            )
            continue
        c = row["checks"]
        mark = {True: "да", False: "нет"}
        lines.append(
            f"| {number} | {row['temperature']:.1f} | {row['chars']} | {row['words']} "
            f"| {mark[row['ok']]} | {mark[c['code_block']]} | {mark[c['max_200_words']]} "
            f"| {mark[c['typical_mistake']]} | {row['prompt_tokens']}/{row['completion_tokens']} |"
        )
    return lines


async def main_async(env_file: str) -> int:
    settings = Settings.load(env_file)
    async with aiohttp.ClientSession() as session:
        llm = LLMClient(
            session,
            base_url=settings.llm_api_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            project=settings.llm_api_project,
        )
        temperature_runs = [
            await ask(llm, STUDY, temperature) for temperature in TEMPERATURES for _ in range(RUNS)
        ]
        weak_runs = [await ask(llm, WEAK, 0.3) for _ in range(RUNS)]
        strong_runs = [await ask(llm, STUDY, 0.3) for _ in range(RUNS)]
    model = settings.llm_model
    model = model.split("/", 3)[-1] if model.startswith("gpt://") else model
    meta = {
        "model": model,
        "query": QUERY,
        "mode": STUDY.command,
        "system_prompt": STUDY.system_prompt,
        "weak_prompt": WEAK.system_prompt,
        "max_tokens": settings.llm_max_tokens,
        "format_rules": FORMAT_RULES,
        "run_at": datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M %Z"),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "lab1-temperature.json").write_text(
        json.dumps(
            {"meta": meta, "temperature": temperature_runs, "weak": weak_runs, "rcft": strong_runs},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report = [
        "# ЛР1: эксперимент с temperature",
        "",
        f"- Модель: `{meta['model']}`, max_tokens {meta['max_tokens']}",
        f"- Дата: {meta['run_at']}",
        f"- Режим: /study, запрос: «{QUERY}»",
        f"- Признаки формата (заданы до запуска): {FORMAT_RULES}",
        "- Перед каждым запуском история пустая (эквивалент /reset). Полные ответы — в JSON.",
        "",
        "## 12 запусков",
        "",
        *table(temperature_runs),
        "",
        "## Неполная инструкция и R.C.T.F. (temperature 0.3)",
        "",
        f"Неполная: «{WEAK.system_prompt}»",
        "",
        *table(weak_runs),
        "",
        "Улучшенная (системная инструкция режима /study):",
        "",
        *table(strong_runs, start=RUNS + 1),
        "",
    ]
    (OUT / "lab1-temperature.md").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Эксперимент ЛР1 с temperature")
    parser.add_argument("--env-file", default=".env")
    return asyncio.run(main_async(parser.parse_args().env_file))


if __name__ == "__main__":
    raise SystemExit(main())
