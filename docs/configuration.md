# Конфигурация

Все настройки бота читаются и проверяются в одном месте — `app/config.py`
(`Settings.load`). Источники по приоритету:

1. переменные окружения процесса (в Docker их задаёт `compose.yaml`);
2. файл `.env` в корне проекта (локально).

Пример со всеми переменными и комментариями — [`.env.example`](../.env.example). Ошибка в
значении останавливает запуск сообщением с именем переменной, но без самого значения.
Тест `tests/test_docs.py` проверяет, что каждая настройка описана здесь и в `.env.example`.

## Файлы окружения

| Файл | Где | Как появляется |
|---|---|---|
| `.env` | компьютер разработчика | `scripts/start.sh` (спрашивает токен и модель) или вручную по `.env.example` |
| `.env.cloud` | ВМ Yandex Cloud | `scripts/deploy.sh` собирает из проверенных настроек и передаёт по SSH |
| `.env.server` | свой сервер (`/opt/itmo-tg-bot`) | `scripts/deploy_server.sh` копирует `.env`, заменяя `POSTGRES_HOST`/`PORT` |

Все три в `.gitignore` и в `.dockerignore`, права — 600.

## Telegram

| Переменная | По умолчанию | Проверка и назначение |
|---|---|---|
| `BOT_TOKEN` | — (обязательна) | токен от @BotFather; формат проверяет aiogram |
| `TELEGRAM_PROXY_URL` | пусто | `http://host:port` или `socks5://host:port`, при необходимости с `user:password@`; спецсимволы в логине и пароле — `%40`, `%3A`, `%2F` |

## PostgreSQL

| Переменная | По умолчанию | Проверка и назначение |
|---|---|---|
| `POSTGRES_HOST` | `127.0.0.1` | в compose — `db` (или `127.0.0.1` в сети хоста) |
| `POSTGRES_PORT` | `5432` | 1–65535 |
| `POSTGRES_DB` | `bot` | имя базы |
| `POSTGRES_USER` | `bot` | роль |
| `POSTGRES_PASSWORD` | — (обязательна) | пароль хранится внутри самой БД: после создания volume меняйте его в базе, а не только в env-файле |

## Служебное

| Переменная | По умолчанию | Проверка и назначение |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`; передаётся и MCP-серверу |
| `HEALTH_PORT` | `8080` | порт `/health` на `127.0.0.1` (1–65535) |

## Языковая модель

| Переменная | По умолчанию | Проверка и назначение |
|---|---|---|
| `LLM_API_BASE_URL` | — (обязательна) | `http(s)://…`, к адресу добавляется `/chat/completions` |
| `LLM_API_KEY` | — (обязательна) | ключ провайдера, передаётся как `Authorization: Bearer …`; для локальной модели без авторизации — любое непустое значение |
| `LLM_MODEL` | — (обязательна) | имя модели; для ЛР2 модель должна поддерживать `tools` |
| `LLM_API_PROJECT` | пусто | Yandex AI Studio: `folder_id`, передаётся заголовком `OpenAI-Project`; для других провайдеров пусто |
| `LLM_PROXY_URL` | пусто | прокси только для запросов к модели, формат как у `TELEGRAM_PROXY_URL` |
| `LLM_TIMEOUT_SECONDS` | `60` | число > 0 |
| `LLM_MAX_TOKENS` | `1024` | целое ≥ 0; `0` — параметр не передаётся |

## История диалога

| Переменная | По умолчанию | Проверка и назначение |
|---|---|---|
| `HISTORY_MAX_MESSAGES` | `20` | целое > 0: сколько последних сообщений хранится в БД и уходит модели |
| `HISTORY_MAX_CHARS` | `12000` | целое > 0: оценка объёма запроса в символах |

## Агент и MCP (ЛР № 2)

| Переменная | По умолчанию | Проверка и назначение |
|---|---|---|
| `SCHEDULE_PATH` | `data/schedule.json` | файл расписания для MCP-сервера; модель и пользователь путь не задают. Ошибка формата — ответ `schedule_unavailable`, бот при этом работает |
| `WEATHER_TIMEOUT_SECONDS` | `5` | число > 0: тайм-аут одного HTTP-запроса к Open-Meteo (плюс не больше одного повтора) |
| `MCP_CALL_TIMEOUT_SECONDS` | `30` | число > 0: тайм-аут одного MCP-вызова; должен быть больше худшего случая погоды: 2 запроса × (тайм-аут + повтор) |
| `AGENT_TEMPERATURE` | `0.2` | 0–1: `temperature` в режиме агента (команда `/settings` влияет только на режимы ЛР1) |
| `DEFAULT_TIMEZONE` | `Europe/Moscow` | IANA-имя: зона для «сегодня/завтра», пока пользователь не задал `/timezone`; для напоминаний не используется |

## Окружение MCP-сервера

Бот передаёт дочернему процессу только нужное (`app/mcp_client.py::server_environment`):

| Переменная | Откуда |
|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | из настроек бота — для таблицы `reminders` |
| `SCHEDULE_PATH`, `WEATHER_TIMEOUT_SECONDS`, `LOG_LEVEL` | из настроек бота |
| `MCP_TRUST_SECRET` | генерируется при каждом запуске бота (`secrets.token_hex(32)`), нигде не хранится |

Если запустить сервер отдельно (MCP Inspector, `scripts/mcp_check.py`), без
`MCP_TRUST_SECRET` и БД работают инструменты чтения, а `add_reminder` отвечает
`untrusted_context`.

## Деплой и тесты

| Переменная | Где используется | Назначение |
|---|---|---|
| `COMPOSE_EXTRA` | `scripts/deploy_server.sh` | дополнительный compose-файл, например `compose.hostnet.yaml` |
| `HOST_DB_PORT` | `compose.hostnet.yaml` | порт БД на `127.0.0.1` хоста (по умолчанию 55433) |
| `HOST_HEALTH_PORT` | `compose.hostnet.yaml` | порт `/health` на хосте (по умолчанию 18081) |
| `RUN_INTEGRATION` | тесты | `1` — интеграционные тесты в собственном контейнере Docker |
| `TEST_POSTGRES_DSN` | тесты | готовый PostgreSQL для интеграционных тестов ЛР2 (создаётся и удаляется временная БД) |

## Пример для Yandex AI Studio

```dotenv
LLM_API_BASE_URL=https://llm.api.cloud.yandex.net/v1
LLM_API_KEY=<API-ключ сервисного аккаунта>
LLM_MODEL=gpt://<folder_id>/yandexgpt/latest
LLM_API_PROJECT=<folder_id>
```

Как получить ключ — в [operations.md](operations.md#6-ключ-yandexgpt).
