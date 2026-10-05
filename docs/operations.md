# Эксплуатация

Запуск, деплой, ключи, журналы, резервные копии и решение типовых проблем. Устройство
системы — в [architecture.md](architecture.md), переменные — в
[configuration.md](configuration.md).

## 1. Локальный запуск

Подробно — разделы 2–5 [README](../README.md). Коротко:

```bash
bash scripts/start.sh               # .venv, зависимости, PostgreSQL в Docker, запуск бота
bash scripts/start.sh --setup-only  # только окружение и БД — для запуска из IDE
python -m app.healthcheck           # проверка уже запущенного бота
```

Без Docker можно подключиться к любому PostgreSQL 16 через `POSTGRES_*` в `.env`.

**Не запускайте бота локально и на сервере с одним токеном одновременно.** Telegram
отдаёт обновления только одному polling-процессу, второй получит ошибку конфликта.

## 2. Деплой на свой сервер по SSH

Требования к серверу: Linux, Docker Engine с Compose v2, вход по SSH-ключу (алиас в
`~/.ssh/config`), доступ наружу к Telegram, провайдеру модели и Open-Meteo.

```bash
bash scripts/deploy_server.sh <ssh-хост>                          # в /opt/itmo-tg-bot
COMPOSE_EXTRA=compose.hostnet.yaml bash scripts/deploy_server.sh <ssh-хост>   # сеть хоста
bash scripts/deploy_server.sh <ssh-хост> /opt/itmo-tg-bot status   # контейнеры и ревизия
bash scripts/deploy_server.sh <ssh-хост> /opt/itmo-tg-bot logs     # 80 последних строк бота
```

Что делает скрипт:

1. Отказывается работать, если код, compose-файлы или зависимости изменены и не
   закоммичены: на сервер уходит только `HEAD`.
2. Проверяет `.env` той же функцией, что и бот (`Settings.load`).
3. Собирает `git archive HEAD` нужных путей (код, MCP-сервер, расписание, Dockerfile,
   compose-файлы, зависимости).
4. Готовит `.env.server`: `POSTGRES_HOST=db`, `POSTGRES_PORT=5432`, остальное из `.env`.
5. По SSH создаёт каталог (права 700), пишет `.env.server` через stdin с `umask 077`,
   распаковывает код, записывает `REVISION` и набор compose-файлов в `.compose-files`.
6. `docker compose up -d --build --wait`, затем `python -m app.healthcheck` в контейнере.

Данные БД хранятся в volume `itmo-tg-bot_postgres_data` и переживают деплой.

**Откат.** Переключитесь на нужный коммит (`git checkout <коммит>`) и выполните деплой.
Миграции однонаправленные: после отката код должен понимать уже применённую схему. Все
изменения схемы добавляют таблицы и колонки, ничего не удаляют.

Команды compose на сервере:

```bash
cd /opt/itmo-tg-bot
docker compose -p itmo-tg-bot --env-file .env.server --profile cloud $(cat .compose-files) ps
docker compose -p itmo-tg-bot --env-file .env.server --profile cloud $(cat .compose-files) logs -f bot
docker compose -p itmo-tg-bot --env-file .env.server --profile cloud $(cat .compose-files) restart bot
```

## 3. Деплой в Yandex Cloud (из шаблона курса)

Штатный путь шаблона — `scripts/deploy.sh`: одна ВМ, Docker, доступ по SSH только с
вашего IP. Описание — разделы 7–8 [README](../README.md). В архив для ВМ входят код
бота, SQL-миграции, MCP-сервер и расписание (`scripts/cloud.py::source_archive`).

## 4. Проверка после деплоя

1. `status` показывает `bot` и `db` в состоянии `healthy`.
2. В журнале есть строки:
   - `Telegram доступен. Бот @… запускает polling.`
   - `MCP: подключено, инструменты: get_weather, get_schedule, add_reminder; ресурсы: schedule://current-week`
   - `Start polling`
3. В Telegram, по пути пользователя:
   - `/start` — список команд;
   - `/tools` — три инструмента с пометками «чтение» и «изменение»;
   - `/timezone Europe/Moscow` — местное время;
   - «Какая погода в Казани?» — температура, ветер и источник Open-Meteo;
   - «Напомни завтра в 18:30 отправить отчёт» → «Подтвердить» → «Напоминание создано: #N…»;
   - `/why` — разбор последнего решения;
   - `/week` — расписание недели.
4. Независимая проверка MCP-сервера: `python -m scripts.mcp_check` — «все ожидания
   выполнены».

## 5. Сеть на сервере в РФ

Что выяснилось при развёртывании на сервере в российском дата-центре:

- IPv4-маршрут к `api.telegram.org` не работает (тайм-аут), работает IPv6. На хосте
  Telegram прописан в `/etc/hosts` с IPv6-адресом.
- Обычная сеть Docker — только IPv4: контейнер запрашивает A-запись, получает пустой ответ
  и падает с `ClientConnectorDNSError`.
- IPv6 в сети Docker (`enable_ipv6`) на этом хосте давал соединения по 3–5 с и обрывы.

Решение — `compose.hostnet.yaml`: бот работает в сети хоста, с его DNS, `/etc/hosts` и
маршрутами. Порты БД (`HOST_DB_PORT`, по умолчанию 55433) и `/health`
(`HOST_HEALTH_PORT`, 18081) публикуются только на `127.0.0.1`. Перед первым деплоем с этим
файлом проверьте, что порты свободны: `ss -ltn`.

Если Telegram недоступен и так, задайте `TELEGRAM_PROXY_URL` (HTTP или SOCKS5).

## 6. Ключ YandexGPT

Бот обращается к Yandex AI Studio через OpenAI-совместимый Chat Completions. Для сервера
нужен **постоянный API-ключ сервисного аккаунта**. IAM-токен живёт не больше 12 часов, и
бот с ним через полдня перестанет отвечать.

### Через консоль

1. console.yandex.cloud → каталог с подключённым биллингом → скопировать его ID
   (`b1g…`).
2. Identity and Access Management → «Сервисные аккаунты» → «Создать»: имя, например
   `itmo-tg-bot`, роль `ai.languageModels.user`.
3. Аккаунт → «Создать новый ключ» → «Создать API-ключ», область действия
   `yc.ai.languageModels.execute`. Секрет показывается один раз.
4. В `.env`: `LLM_API_KEY=<секрет>`, `LLM_MODEL=gpt://<folder_id>/yandexgpt/latest`,
   `LLM_API_PROJECT=<folder_id>`, `LLM_API_BASE_URL=https://llm.api.cloud.yandex.net/v1`.

### Через API (так ключ был создан для учебного бота)

Владелец облака получает IAM-токен (например, `yc iam create-token`), после чего:

```text
1. Найти каталог
   GET  https://resource-manager.api.cloud.yandex.net/resource-manager/v1/clouds
   GET  https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders?cloudId=<cloud>

2. Сервисный аккаунт
   POST https://iam.api.cloud.yandex.net/iam/v1/serviceAccounts
        {"folderId": "<folder>", "name": "itmo-tg-bot", "description": "…"}

3. Роль только на этот каталог
   POST https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/<folder>:updateAccessBindings
        {"accessBindingDeltas": [{"action": "ADD", "accessBinding": {
            "roleId": "ai.languageModels.user",
            "subject": {"id": "<service-account-id>", "type": "serviceAccount"}}}]}

4. API-ключ с минимальной областью
   POST https://iam.api.cloud.yandex.net/iam/v1/apiKeys
        {"serviceAccountId": "<service-account-id>", "description": "…",
         "scopes": ["yc.ai.languageModels.execute"]}
        → в ответе поле "secret" — это LLM_API_KEY
```

Все запросы идут с заголовком `Authorization: Bearer <IAM-токен>`. Операции создания
асинхронные: дождитесь `done: true` по
`GET https://operation.api.cloud.yandex.net/operations/<id>`.

Права минимальные: роль даёт только вызов моделей и только в одном каталоге, а область
ключа ограничена `yc.ai.languageModels.execute`.

Проверка без бота:

```bash
.venv/bin/python -c "
import asyncio, aiohttp
from app.config import Settings
from app.llm import LLMClient
async def main():
    s = Settings.load('.env')
    async with aiohttp.ClientSession() as session:
        llm = LLMClient(session, base_url=s.llm_api_base_url, api_key=s.llm_api_key,
                        model=s.llm_model, timeout=60, project=s.llm_api_project)
        print((await llm.complete([{'role': 'user', 'content': 'Ты работаешь?'}], temperature=0.3)).text)
asyncio.run(main())"
```

## 7. Журналы и наблюдение

- **Журнал бота** (`docker compose … logs bot`): запуск, подключение MCP, запросы к модели
  (`LLM <id>: запрос к модели …, ответ за … с, токены …`), решения агента
  (`Агент <request_id>: call_tool, инструменты: get_weather, вызовов MCP: 1, проверка:
  пройдена, выполнение: успешно, 812 мс`), предупреждения о недоступности MCP или погоды.
  Telegram ID, имён и текстов сообщений в журнале нет.
- **Журнал MCP-сервера** идёт в тот же поток (stderr дочернего процесса).
- **Аудит решений** — таблица `agent_events`. Пользователь видит последнее решение
  командой `/why`.
- **Здоровье**: `/health` (`{"status": "ok", "database": "ok", "polling": "running"}`).
  Docker проверяет его каждые 10 с.

## 8. База данных

```bash
# консоль psql
docker compose … exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

# резервная копия (формат custom)
docker compose … exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > itmo-bot-$(date +%F).dump

# восстановление в пустую базу
docker compose … exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < itmo-bot-2026-10-05.dump
```

Полезные запросы:

```sql
SELECT version, applied_at FROM schema_migrations ORDER BY version;
SELECT status, count(*) FROM pending_actions GROUP BY status;
SELECT id, remind_at, timezone, created_at FROM reminders ORDER BY id DESC LIMIT 10;
SELECT created_at, action, tools, validation, execution, duration_ms
  FROM agent_events ORDER BY id DESC LIMIT 20;
```

## 9. Смена секретов

| Секрет | Как сменить |
|---|---|
| Токен бота | @BotFather → `/revoke` → новый токен в `.env` → деплой. Старый перестаёт работать сразу |
| Ключ YandexGPT | создать новый API-ключ (раздел 6) → `.env` → деплой → удалить старый ключ в консоли |
| Пароль PostgreSQL | `ALTER ROLE … PASSWORD '…'` внутри БД, затем `.env` → деплой. Если поменять только env-файл, бот не подключится |
| Секрет MCP | не хранится: генерируется при каждом запуске бота |

Секрет, попавший в Git, чат или скриншот, считается раскрытым: его нужно перевыпустить.

## 10. Неполадки

| Симптом | Причина | Что делать |
|---|---|---|
| В журнале `TelegramUnauthorizedError` | токен отозван или с опечаткой | новый токен у @BotFather, `.env`, деплой |
| `У бота установлен webhook` при запуске | бот подключён к другому сервису через webhook | `deleteWebhook` или отдельный учебный бот |
| `ClientConnectorDNSError … api.telegram.org` в контейнере | Telegram на хосте доступен только по IPv6, а контейнер в IPv4-сети | `COMPOSE_EXTRA=compose.hostnet.yaml` (раздел 5) или `TELEGRAM_PROXY_URL` |
| `Failed to fetch updates … Request timeout` иногда | кратковременный сбой сети до Telegram | aiogram повторит сам; если часто — прокси или другой маршрут |
| Бот отвечает «Сервис модели сейчас недоступен» | неверный `LLM_API_BASE_URL`, ключ или нет сети до провайдера | проверка ключа из раздела 6; HTTP 401/403 в журнале — ключ или роль |
| «Инструменты сейчас недоступны», `/tools` — то же | MCP-сервер не запустился | `MCP: сервер недоступен (…)` в журнале — там причина; `python -m scripts.mcp_check` локально |
| «Расписание сейчас недоступно» | файл по `SCHEDULE_PATH` не найден или не прошёл проверку формата | проверьте JSON по `mcp_server/schedule.py::ScheduleFile` |
| «Погодный сервис не ответил вовремя» | Open-Meteo медленно отвечает | временно; увеличьте `WEATHER_TIMEOUT_SECONDS` (и `MCP_CALL_TIMEOUT_SECONDS`) |
| Напоминание не готовится, бот просит `/timezone` | часовой пояс не задан | `/timezone Europe/Moscow` |
| «Подтверждение просрочено» | с момента карточки прошло больше 5 минут | повторить просьбу |
| В группе бот молчит | бот обрабатывает только личные чаты | писать в личку |
| Контейнер `bot` `unhealthy` | не готов polling или БД | `logs bot`; `python -m app.healthcheck` внутри контейнера |
| `address already in use` при сети хоста | порт `/health` или БД занят на хосте | `HOST_HEALTH_PORT` / `HOST_DB_PORT` в `.env` |
| Два бота отвечают по очереди или конфликт `getUpdates` | бот запущен в двух местах с одним токеном | оставить один экземпляр |
