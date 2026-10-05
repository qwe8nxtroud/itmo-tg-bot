# Разработка

Как устроен репозиторий, как запускать проверки и как расширять бота. Правила проекта
(границы модулей, секреты, миграции) — в [AGENTS.md](../AGENTS.md).

## 1. Структура репозитория

```text
app/                    Telegram-приложение (MCP-хост)
  handlers/             обработчики Telegram: dialog.py (ЛР1, текст), agent.py (ЛР2, кнопки)
  migrations/           SQL-миграции NNN_*.sql
  agent.py              агентный цикл, подтверждения, аудит
  assistant.py          режимы ЛР1: сборка запроса и история
  llm.py                OpenAI-совместимый Chat Completions (+ tools)
  mcp_client.py         MCP-клиент: супервизор соединения, вызовы, ошибки
  storage.py            весь SQL и раннер миграций
  tool_args.py          проверка аргументов, резюме для аудита, скрытие «команд»
  presenters.py         тексты ответов
  prompts.py            системные инструкции режимов
  timezones.py          IANA-зоны и переходы времени
  config.py             настройки
mcp_server/             MCP-сервер (отдельный процесс, не импортирует app/)
data/schedule.json      обезличенное расписание
scripts/                запуск, деплой, независимая проверка MCP, эксперименты
tests/                  офлайн-тесты и интеграционные (-m integration)
docs/                   документация, спецификация, эксперименты, отчёты
```

## 2. Окружение и проверки

```bash
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python -r requirements-dev.txt   # или python -m pip install -r …

.venv/bin/python -m pytest -q                     # 240+ офлайн-тестов, ~4 с
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pip check                     # или uv pip check -p .venv/bin/python
```

Интеграционные тесты на настоящем PostgreSQL:

```bash
RUN_INTEGRATION=1 .venv/bin/python -m pytest -m integration -v      # свой контейнер Docker
TEST_POSTGRES_DSN=postgresql://user:pass@127.0.0.1:5432/postgres \
  .venv/bin/python -m pytest -m integration -v                       # готовый сервер
```

Во втором варианте тесты ЛР2 создают временную базу и удаляют её после себя. Тесты ЛР1
требуют Docker: они останавливают и запускают контейнер.

CI (`.github/workflows`) на каждый push запускает ruff, `pip check` и pytest на Ubuntu,
Windows и macOS. На Ubuntu дополнительно идут интеграционные тесты в Docker.

## 3. Как устроены тесты

- **Внешние границы подменяются, всё остальное — настоящее.** В `tests/fakes.py` лежат:
  - `FakeLLM` — сценарий ответов модели, включая `call("get_weather", {...})`;
  - `FakeStorage` — хранилище в памяти с той же семантикой атомарности;
  - `FakeWeather`, `MemoryReminderStore`;
  - `Clock` — подменяемое время;
  - `schedule_data` — фикстура расписания.
- **MCP-сервер в тестах настоящий.** `build_test_server()` собирает его с фейковыми
  погодой, хранилищем и часами, а `McpGateway(server)` подключается к нему в памяти. Тест
  `test_real_stdio_server_process` запускает и настоящий дочерний процесс.
- **Telegram-уровень.** `tests/bot_harness.py` — общий диспетчер с роутерами; update подаются
  через `dispatcher.feed_update`, отправленные методы собираются из подменённой сессии бота.
- **Фикстура `make_env`** в `tests/conftest.py` создаёт агента целиком: модель, хранилище,
  MCP-сервер и часы.
- **Тесты документации** (`tests/test_docs.py`) проверяют, что `docs/mcp-contracts.md`
  совпадает с работающим сервером, а каждая настройка описана в `configuration.md` и
  `.env.example`.
- Порядок Arrange — Act — Assert; для детерминированных ответов проверяется точный текст.

## 4. Как добавить MCP-инструмент

1. **Сервер.** Функция в `mcp_server/server.py::build_server`:
   - аргументы — через `Annotated[..., Field(...)]` с ограничениями и описанием для модели;
   - выход — модель Pydantic: из неё строится `outputSchema`;
   - `ToolAnnotations(readOnlyHint=True)` — для чтения;
   - ожидаемые ошибки — `ToolFailure(code, message)` → `ToolError(failure.to_json())`.
2. **Внешние вызовы** — только к заранее заданным адресам, с тайм-аутом, повтор — только для
   чтения.
3. **Бот ничего не нужно регистрировать.** Инструмент придёт через `tools/list`, появится в
   `/tools` и будет предложен модели.
4. **Значения, которые схема не выражает** (календарь, диапазоны), — правило в
   `app/tool_args.py::validate_arguments`.
5. **Человекочитаемый ответ без модели** (запасной вариант) — в
   `app/presenters.py::tool_result_text`.
6. **Если у инструмента есть побочный эффект** (`readOnlyHint` не `true`), агент не
   вызовет его из цикла, а подготовит действие для подтверждения. Нужно:
   - текст карточки в `presenters.confirmation_text`;
   - текст результата в `presenters.reminder_created_text`, сейчас он ориентирован на
     напоминания;
   - идемпотентность на сервере по `action_id` из доверенного контекста.
7. Тесты: сервер (`test_mcp_server.py`), агент (`test_agent.py`). Затем
   `python -m scripts.dump_mcp_contracts` обновит справочник контрактов.

## 5. Как добавить режим

1. `app/prompts.py`: новый `Mode` с инструкцией по R.C.T.F. и, при необходимости,
   few-shot-примерами; добавить его в `MODES`.
2. Команда режима, пункт меню Telegram и строка в `/start` появятся автоматически: они
   строятся из `MODES`.
3. Режимы, кроме `/agent`, работают через `Assistant` без инструментов.
4. Тест в `tests/test_dialog.py`: смена режима и инструкция в запросе к модели.

## 6. Как добавить команду Telegram

- Команды агента — в `app/handlers/agent.py`. Этот роутер подключается раньше общего, иначе
  обработчик неизвестных команд в `dialog.py` перехватит сообщение.
- Добавить `BotCommand` в `BOT_COMMANDS` и строку в `START_TEXT` (`app/handlers/dialog.py`).
- Логика — в сервисе (`Agent` или `Assistant`), обработчик только разбирает update и
  отправляет текст.

## 7. Как изменить схему БД

1. Новый файл `app/migrations/NNN_описание.sql` со следующим номером. Применённые миграции
   не редактируются.
2. Изменения только добавляют таблицы и колонки, данные не удаляются.
3. SQL — в `app/storage.py`, параметры только через `$1`, `$2`.
4. Та же семантика — в `tests/fakes.py::FakeStorage`.
5. Гарантии, которые держит сама БД (уникальность, атомарные переходы), проверяются в
   `tests/test_integration_lab2.py`.

## 8. Зависимости

Версии всех прямых и транзитивных зависимостей закреплены в `constraints.txt`, CI
проверяет `pip check`. Порядок добавления новой зависимости:

1. Запишите во временный файл `all.in` все прямые зависимости (`requirements.txt`,
   `requirements-dev.txt`) и новую.
2. Разрешите версии для каждой платформы, на которой идёт CI:

   ```bash
   for p in linux windows macos; do
     uv pip compile all.in --python-version 3.12 --python-platform $p -c constraints.txt -o lock-$p.txt
   done
   ```

3. Объединение всех `lock-*.txt` допишите в `constraints.txt`, прямую зависимость с
   версией — в `requirements.txt`.

## 9. Скрипты проверок и экспериментов

| Скрипт | Что делает | Нужна модель |
|---|---|---|
| `scripts/mcp_check.py` | независимый MCP-клиент: обнаружение, чтение, ожидаемые отказы | нет |
| `scripts/dump_mcp_contracts.py` | генерирует `docs/mcp-contracts.md` | нет |
| `scripts/experiment_lab1.py` | ЛР1: 12 запусков по `temperature` и сравнение инструкций | да |
| `scripts/eval_routing.py` | ЛР2: маршрутизация на наборе (`--dataset`, `--only`, `--label`) | да |
| `scripts/negative_live.py` | ЛР2: негативные сценарии на действительной модели | да |
| `scripts/deploy_server.sh` | деплой на свой сервер по SSH | — |

Результаты экспериментов коммитятся в `docs/experiments/` и `docs/evaluation/results/`
вместе с датой, моделью и сырыми ответами.

## 10. Сдача лабораторной

- Тег на сдаваемом состоянии: `git tag -a labN -m "Лабораторная работа № N"`. Уже
  опубликованный тег не переназначается: для исправлений — новый согласованный тег.
- Перед тегом: все проверки раздела 2 зелёные, `docs/plan.md` и `docs/spec.md`
  соответствуют коду, отчёт лежит в `docs/reports/`.
- Тег `lab1` стоит на ветке `lab1-final` (состояние ЛР1 + эксперимент и отчёт), тег `lab2` —
  на `main`.
