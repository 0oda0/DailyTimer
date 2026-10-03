#!/usr/bin/env bash
# Автообновление DailyTimer: если в репозитории есть новые коммиты — подтягивает их и перезапускает.
# После перезапуска бот пришлёт в Telegram «DailyTimer обновлён» со списком нового.
# Запуск вручную: bash update.sh   (install.sh ставит его в cron на 04:30 каждую ночь)
set -euo pipefail
cd "$(dirname "$0")"
branch=$(git rev-parse --abbrev-ref HEAD)
git fetch -q origin "$branch"
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$branch")" ]; then
  echo "$(date '+%F %T') обновлений нет"
  exit 0
fi
git pull -q --ff-only origin "$branch"
docker compose up -d --build
docker image prune -f >/dev/null 2>&1 || true
echo "$(date '+%F %T') обновлено до $(git rev-parse --short HEAD)"
