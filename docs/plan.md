# План реализации ЛР № 2

Связывает требования методички с этапами и тестами. `[x]` — сделано, `[ ]` — в работе.
Отклонения от первоначального решения записаны в конце.

| Этап | Требование | Реализация | Тесты |
|---|---|---|---|
| [ ] 1 | Спецификация, правила проекта | `docs/spec.md`, `docs/plan.md`, `AGENTS.md` | — |
| [ ] 2 | Схема БД: часовой пояс, действия, напоминания, аудит | `app/migrations/002_lab2.sql`, раннер миграций в `app/storage.py` | `tests/test_integration.py` |
| [ ] 3 | MCP-сервер: `get_weather` | `mcp_server/weather.py` (Open-Meteo, тайм-аут, 1 повтор, неоднозначность) | `tests/test_mcp_weather.py` |
| [ ] 4 | MCP-сервер: `get_schedule` и ресурс недели | `mcp_server/schedule.py`, `data/schedule.json` | `tests/test_mcp_schedule.py` |
| [ ] 5 | MCP-сервер: `add_reminder`, доверенный владелец, идемпотентность | `mcp_server/reminders.py`, `mcp_server/trust.py` | `tests/test_mcp_server.py`, `tests/test_integration.py` |
| [ ] 6 | Схемы, лишние поля, единый формат ошибок | `mcp_server/server.py` | `tests/test_mcp_server.py` |
| [ ] 7 | MCP-клиент: обнаружение, переподключение, проверка результата | `app/mcp_client.py` | `tests/test_mcp_client.py` |
| [ ] 8 | Часовые пояса IANA, переходы на летнее время | `app/timezones.py` | `tests/test_timezones.py` |
| [ ] 9 | Агентный цикл: нативные `tools`, лимит 2 вызова, проверка аргументов | `app/agent.py`, `app/tool_args.py`, `app/llm.py` | `tests/test_agent.py` |
| [ ] 10 | Подтверждение, отмена, просрочка, изоляция пользователей | `app/agent.py`, `app/storage.py` | `tests/test_agent.py`, `tests/test_integration.py` |
| [ ] 11 | Команды `/tools`, `/timezone`, `/why`, `/week`, кнопки | `app/handlers/agent.py` | `tests/test_agent_handlers.py` |
| [ ] 12 | Независимая проверка сервера | `scripts/mcp_check.py` | ручной запуск, вывод в отчёте |
| [ ] 13 | Эксперимент по маршрутизации, две версии | `scripts/eval_routing.py`, `docs/evaluation/results/` | — |
| [ ] 14 | Развёртывание на сервер | `scripts/deploy_server.sh`, `Dockerfile`, `compose.yaml` | healthcheck после деплоя |
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

Заполняется по ходу работы.
