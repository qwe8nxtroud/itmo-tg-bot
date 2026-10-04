#!/usr/bin/env bash
# Деплой на собственный сервер с Docker по SSH (альтернатива Yandex Cloud из шаблона).
#
#   bash scripts/deploy_server.sh <ssh-хост> [каталог]          # сборка и запуск
#   bash scripts/deploy_server.sh <ssh-хост> [каталог] status   # состояние контейнеров
#   bash scripts/deploy_server.sh <ssh-хост> [каталог] logs     # последние строки журнала бота
#
# COMPOSE_EXTRA=compose.ipv6.yaml — дополнительный файл compose (например, если Telegram на
# сервере доступен только по IPv6). Выбор запоминается на сервере для status и logs.
#
# Отправляется зафиксированное состояние (git archive HEAD): без .env, .venv и локальных
# правок. Конфигурация берётся из локального .env и передаётся через stdin SSH сразу с
# правами 600; БД — сервис db внутри compose, наружу порты не публикуются.
set -euo pipefail

host="${1:?Укажите SSH-хост: bash scripts/deploy_server.sh vh-tw}"
remote="${2:-/opt/itmo-tg-bot}"
action="${3:-deploy}"
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
files="-f compose.yaml${COMPOSE_EXTRA:+ -f $COMPOSE_EXTRA}"
# На сервере набор файлов берётся из .compose-files, записанного при деплое.
compose="cd '$remote' && docker compose -p itmo-tg-bot --env-file .env.server --profile cloud \$(cat .compose-files 2>/dev/null || echo -f compose.yaml)"

case "$action" in
  status) exec ssh "$host" "$compose ps && cat '$remote/REVISION'" ;;
  logs) exec ssh "$host" "$compose logs --tail 80 bot" ;;
  deploy) ;;
  *) echo "Неизвестное действие: $action (deploy, status, logs)" >&2; exit 2 ;;
esac

cd "$root"
[ -f .env ] || { echo "Нет файла .env: заполните его по .env.example" >&2; exit 1; }
if [ -n "$(git status --porcelain -- app mcp_server data Dockerfile .dockerignore compose.yaml compose.ipv6.yaml requirements.txt constraints.txt)" ]; then
  echo "Есть незакоммиченные изменения в коде: на сервер уходит только HEAD. Сделайте коммит." >&2
  exit 1
fi
# Проверка конфигурации до отправки: текст ошибки не содержит секретов.
python="$root/.venv/bin/python"; [ -x "$python" ] || python="python3"
"$python" -c "from app.config import Settings; Settings.load('.env')"

revision="$(git rev-parse --short HEAD)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git archive --format=tar.gz -o "$tmp/source.tar.gz" HEAD \
  Dockerfile .dockerignore requirements.txt constraints.txt compose.yaml compose.ipv6.yaml \
  app mcp_server data
# Внутри compose бот ходит в БД по имени сервиса; остальные значения — из .env.
grep -v -E '^(POSTGRES_HOST|POSTGRES_PORT)=' .env > "$tmp/env.server"
printf 'POSTGRES_HOST=db\nPOSTGRES_PORT=5432\n' >> "$tmp/env.server"

echo "Отправка $revision на $host:$remote…"
ssh "$host" "mkdir -p '$remote' && chmod 700 '$remote'"
ssh "$host" "umask 077 && cat > '$remote/.env.server'" < "$tmp/env.server"
ssh "$host" "cat > '$remote/source.tar.gz'" < "$tmp/source.tar.gz"
ssh "$host" "set -eu; cd '$remote'; rm -rf app mcp_server data; tar -xzf source.tar.gz; \
  rm source.tar.gz; echo '$revision' > REVISION; echo '$files' > .compose-files; \
  $compose up -d --build --wait --wait-timeout 180 && $compose exec -T bot python -m app.healthcheck"
echo "Готово: $revision работает на $host. Журнал: bash scripts/deploy_server.sh $host $remote logs"
