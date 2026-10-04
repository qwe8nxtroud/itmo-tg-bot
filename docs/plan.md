# План реализации ЛР № 2

Связывает требования методички с этапами и тестами. `[x]` — сделано, `[ ]` — в работе.
Отклонения от первоначального решения записаны в конце.

| Этап | Требование | Реализация | Тесты |
|---|---|---|---|
| [x] 1 | Спецификация, правила проекта | `docs/spec.md`, `docs/plan.md`, `AGENTS.md` | — |
| [x] 2 | Схема БД: часовой пояс, действия, напоминания, аудит | `app/migrations/002_lab2.sql`, раннер миграций в `app/storage.py` | `tests/test_integration.py` |
| [x] 3 | MCP-сервер: `get_weather` | `mcp_server/weather.py` (Open-Meteo, тайм-аут, 1 повтор, неоднозначность) | `tests/test_mcp_weather.py` |
| [x] 4 | MCP-сервер: `get_schedule` и ресурс недели | `mcp_server/schedule.py`, `data/schedule.json` | `tests/test_mcp_schedule.py` |
| [x] 5 | MCP-сервер: `add_reminder`, доверенный владелец, идемпотентность | `mcp_server/reminders.py`, `mcp_server/trust.py` | `tests/test_mcp_server.py`, `tests/test_integration.py` |
| [x] 6 | Схемы, лишние поля, единый формат ошибок | `mcp_server/server.py` | `tests/test_mcp_server.py` |
| [x] 7 | MCP-клиент: обнаружение, переподключение, проверка результата | `app/mcp_client.py` | `tests/test_mcp_client.py` |
| [x] 8 | Часовые пояса IANA, переходы на летнее время | `app/timezones.py` | `tests/test_timezones.py` |
| [x] 9 | Агентный цикл: нативные `tools`, лимит 2 вызова, проверка аргументов | `app/agent.py`, `app/tool_args.py`, `app/llm.py` | `tests/test_agent.py` |
| [x] 10 | Подтверждение, отмена, просрочка, изоляция пользователей | `app/agent.py`, `app/storage.py` | `tests/test_agent.py`, `tests/test_integration.py` |
| [x] 11 | Команды `/tools`, `/timezone`, `/why`, `/week`, кнопки | `app/handlers/agent.py` | `tests/test_agent_handlers.py` |
| [x] 12 | Независимая проверка сервера | `scripts/mcp_check.py` | ручной запуск, вывод в отчёте |
| [ ] 13 | Эксперимент по маршрутизации, две версии | `scripts/eval_routing.py`, `docs/evaluation/results/` | — |
| [x] 14 | Развёртывание на сервер | `scripts/deploy_server.sh`, `Dockerfile`, `compose.yaml` | healthcheck после деплоя |
| [ ] 15 | Документация и отчёт | `README.md`, `docs/reports/lab2-report.md` | — |

## Соответствие обязательным тестам методички

| № | Сценарий | Тест |
|---:|---|---|
| 1 | Погода для существующего города | `test_mcp_weather.py::test_weather_for_existing_city` |
| 2 | Город не найден | `test_mcp_weather.py::test_city_not_found` |
| 3 | Тайм-аут или некорректный ответ погодного API | `test_mcp_weather.py::test_timeout_*`, `::test_bad_response_*` |
| 4 | Занятия на дату найдены | `test_mcp_schedule.py::test_lessons_on_date` |
| 5 | Занятий нет | `test_mcp_schedule.py::test_no_lessons_on_sunday` |
| 6 | Неверный формат даты | `test_mcp_server.py::test_schedule_rejects_bad_date` |
| 7 | Напоминание в будущем после подтверждения | `test_agent.py::test_reminder_created_after_confirmation` |
| 8 | Прошедшее время, пустой текст | `test_agent.py::test_reminder_in_past_rejected`, `test_mcp_server.py::test_add_reminder_rejects_empty_text` |
| 9 | Повтор с тем же ключом | `test_mcp_server.py::test_add_reminder_is_idempotent`, `test_integration.py::test_reminder_idempotency_pg` |
| 10 | Повтор update и двойное подтверждение | `test_agent.py::test_same_update_twice_*`, `test_integration.py::test_concurrent_*_pg` |
| 11 | Чужое действие | `test_agent.py::test_other_user_cannot_*` |
| 12 | Неделя на границе месяца и года | `test_mcp_schedule.py::test_week_crosses_*` |
| 13 | Неизвестная зона, время без зоны | `test_timezones.py`, `test_agent.py::test_reminder_requires_timezone` |
| 14 | Неизвестный инструмент, лишние аргументы | `test_agent.py::test_unknown_tool_rejected`, `::test_extra_arguments_rejected` |

## Отклонения от первоначального решения

1. **Лишние аргументы.** Предполагалось, что MCP SDK отклоняет поля вне схемы. Проверка
   показала, что `MCPServer` 2.3 их молча отбрасывает. Добавлен `StrictMCPServer`, а в
   схемы — `additionalProperties: false`.
2. **Доверенный контекст.** В документации SDK заголовки названы вводом клиента, который
   не может служить идентичностью; атрибута `ctx.meta` в 2.3 нет. Владелец передаётся
   подписанным HMAC токеном в `_meta` запроса, сервер читает его из
   `ctx.request_context.meta`.
3. **Режим по умолчанию.** Вместо `/study` теперь `/agent`. Тесты ЛР № 1 явно работают в
   режиме `/study`.
4. **Схема БД.** `CREATE TABLE IF NOT EXISTS` из кода заменён нумерованными миграциями
   (`app/migrations`) с учётом версий.
5. **Ожидание старта MCP.** `start()` ждал успешного подключения до тайм-аута (10 с) даже
   при упавшем сервере. Теперь он ждёт первой попытки, а причина пишется в журнал.
6. **По итогам ревью:**
   - смерть процесса сервера (`MCPError CONNECTION_CLOSED`) теперь ведёт к
     переподключению, а отказ SDK в `structuredContent` — к `bad_result` без него;
   - модель знает только текущее смещение, поэтому допустимо и смещение на дату
     напоминания;
   - подтверждённое действие можно повторить после 5 минут, зависшее `executing`
     захватывается снова, неожиданная ошибка возвращает действие в `pending`;
   - подготовка действия сериализуется блокировкой по пользователю;
   - без `/timezone` даты считаются по `DEFAULT_TIMEZONE`, а не по UTC;
   - тайм-аут MCP поднят до 30 с (больше худшего случая погоды);
   - имя неизвестного инструмента не попадает в журнал.
7. **Деплой.** Сервер находится в РФ: IPv4 к `api.telegram.org` там не работает, а IPv6-NAT
   Docker нестабилен (тайм-ауты соединения). Бот развёрнут в сети хоста
   (`compose.hostnet.yaml`), база доступна только на `127.0.0.1`. Архив штатного деплоя в
   Yandex Cloud не включал миграции, MCP-сервер и расписание — исправлено.
