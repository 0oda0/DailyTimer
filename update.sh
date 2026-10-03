#!/usr/bin/env bash
# Автообновление DailyTimer: подтягивает новые коммиты, пересобирает и перезапускает.
# Запоминает реально запущенную версию (data/deployed_commit): если сборка упала,
# следующий запуск попробует снова, а не решит, что «обновлений нет».
# Запуск вручную: bash update.sh   (install.sh ставит его в cron на 04:30 каждую ночь)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
branch=$(git rev-parse --abbrev-ref HEAD)
git fetch -q origin "$branch"
remote=$(git rev-parse "origin/$branch")
deployed=$(cat data/deployed_commit 2>/dev/null || true)
if [ "$(git rev-parse HEAD)" = "$remote" ] && [ "$deployed" = "$remote" ]; then
  echo "$(date '+%F %T') обновлений нет ($(git rev-parse --short HEAD))"
  exit 0
fi
git pull -q --ff-only origin "$branch"
echo "$(date '+%F %T') собираю $(git rev-parse --short HEAD)…"
# Docker Hub иногда отвечает 429 (лимит запросов) — пробуем несколько раз с паузой.
ok=0
for attempt in 1 2 3; do
  if docker compose up -d --build --remove-orphans; then ok=1; break; fi
  echo "$(date '+%F %T') сборка не удалась (попытка $attempt), повтор через 30 с…" >&2
  sleep 30
done
if [ "$ok" != 1 ]; then
  echo "$(date '+%F %T') ОШИБКА: сборка не удалась, продолжает работать прежняя версия" >&2
  exit 1
fi
git rev-parse HEAD > data/deployed_commit
docker image prune -f >/dev/null 2>&1 || true
echo "$(date '+%F %T') обновлено до $(git rev-parse --short HEAD)"

# Новая версия агента сервера — ставим отдельной systemd-задачей: этот скрипт может работать
# внутри самого агента (кнопка «Обновить» на сайте), и прямой перезапуск оборвал бы его.
if [ -f /opt/dailytimer-agent/vps_agent.py ] && ! cmp -s agent/vps_agent.py /opt/dailytimer-agent/vps_agent.py; then
  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --quiet --no-block --collect bash "$PWD/agent/install-agent.sh"
  else
    bash agent/install-agent.sh
  fi
  echo "Агент сервера будет обновлён"
fi
