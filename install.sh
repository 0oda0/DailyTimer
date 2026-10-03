#!/usr/bin/env bash
# Установка DailyTimer на VPS одной командой (запускать из папки репозитория под root):
#   bash install.sh
# Скрипт сам ставит Docker, находит свободный порт, генерирует пароль панели и запускает сервис.
set -euo pipefail
cd "$(dirname "$0")"

port_busy() { ss -Htln "sport = :$1" 2>/dev/null | grep -q .; }

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Ставлю Docker"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { echo "Нужен плагин docker compose"; exit 1; }

if [ ! -f .env ]; then
  port=""
  for candidate in 8080 8088 8090 8181 8888 9090 18080 28080; do
    if ! port_busy "$candidate"; then port=$candidate; break; fi
  done
  [ -n "$port" ] || { echo "Не нашёл свободный порт"; exit 1; }
  password=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)
  cat > .env <<EOF
DAILYTIMER_USER=admin
DAILYTIMER_PASSWORD=$password
DAILYTIMER_PORT=$port
DAILYTIMER_SECRET_KEY=$(docker run --rm python:3.12-slim python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
EOF
  chmod 600 .env
fi

set -a; . ./.env; set +a
mkdir -p data
docker compose up -d --build

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "$DAILYTIMER_PORT"/tcp >/dev/null
fi

ip=$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "Готово! Панель: http://$ip:$DAILYTIMER_PORT"
echo "Логин: $DAILYTIMER_USER"
echo "Пароль: $DAILYTIMER_PASSWORD   (хранится в $(pwd)/.env)"
