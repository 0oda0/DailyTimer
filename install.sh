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
  # Модель ИИ под объём памяти сервера (Ollama работает на CPU, видеокарта не нужна).
  mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  if   [ "$mem_gb" -ge 12 ]; then model="qwen2.5:7b"
  elif [ "$mem_gb" -ge 7 ];  then model="gemma3:4b"
  elif [ "$mem_gb" -ge 4 ];  then model="qwen2.5:3b"
  else                            model="qwen2.5:1.5b"; fi
  password=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
  cat > .env <<EOF
DAILYTIMER_USER=admin
DAILYTIMER_PASSWORD=$password
DAILYTIMER_PORT=$port
OLLAMA_MODEL=$model
DAILYTIMER_SECRET_KEY=$(docker run --rm python:3.12-slim python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
EOF
  chmod 600 .env
fi

# Агент сервера (раздел «Сервер»): порты, диск, контейнеры, обновление проектов по кнопке.
bash agent/install-agent.sh || echo "!! Агент сервера не установился — раздел «Сервер» будет недоступен"

set -a; . ./.env; set +a
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
OLLAMA_MODEL=${OLLAMA_MODEL:-qwen2.5:3b}
mkdir -p data
docker compose up -d --build --remove-orphans
git rev-parse HEAD > data/deployed_commit 2>/dev/null || true

echo "==> Скачиваю локальную модель ИИ $OLLAMA_MODEL (один раз, 1–5 ГБ)"
for _ in 1 2 3 4 5; do
  docker compose exec -T ollama ollama pull "$OLLAMA_MODEL" && break || sleep 5
done

# Ночное автообновление (выключить: AUTO_UPDATE=0 bash install.sh)
if [ "${AUTO_UPDATE:-1}" = "1" ] && command -v crontab >/dev/null 2>&1; then
  ( crontab -l 2>/dev/null | grep -v "DailyTimer-autoupdate" || true
    echo "30 4 * * * cd $(pwd) && bash update.sh >> data/update.log 2>&1 # DailyTimer-autoupdate" ) | crontab -
  echo "==> Автообновление включено: каждую ночь в 04:30"
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "$DAILYTIMER_PORT"/tcp >/dev/null
fi

ip=$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "Готово! Панель: http://$ip:$DAILYTIMER_PORT"
echo "Логин: $DAILYTIMER_USER"
echo "Пароль: $DAILYTIMER_PASSWORD   (хранится в $(pwd)/.env)"
echo "ИИ: локальная модель $OLLAMA_MODEL (RAM сервера: ${mem_gb:-?} ГБ)"
