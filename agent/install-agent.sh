#!/usr/bin/env bash
# Ставит DailyTimer-агента как systemd-службу. Запускать под root на VPS:
#   bash agent/install-agent.sh            (install.sh вызывает его сам)
# Токен сохраняется в /etc/dailytimer-agent.env и в .env DailyTimer (VPS_AGENT_TOKEN).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
port="${AGENT_PORT:-9137}"

install -d -m 755 /opt/dailytimer-agent
install -m 755 "$here/vps_agent.py" /opt/dailytimer-agent/vps_agent.py

token=""
if [ -f /etc/dailytimer-agent.env ]; then
  token=$(grep -E '^AGENT_TOKEN=' /etc/dailytimer-agent.env | cut -d= -f2- || true)
fi
[ -n "$token" ] || token=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)

# Слушаем только на мосте docker0 — снаружи агент недоступен, а контейнер DailyTimer его видит.
bind=$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
bind=${bind:-127.0.0.1}

cat > /etc/dailytimer-agent.env <<EOF
AGENT_TOKEN=$token
AGENT_BIND=$bind
AGENT_PORT=$port
AGENT_ROOTS=${AGENT_ROOTS:-/root:/home:/opt:/srv:/var/www}
EOF
chmod 600 /etc/dailytimer-agent.env

cat > /etc/systemd/system/dailytimer-agent.service <<EOF
[Unit]
Description=DailyTimer VPS agent
After=network-online.target docker.service
Wants=docker.service

[Service]
EnvironmentFile=/etc/dailytimer-agent.env
ExecStart=/usr/bin/env python3 /opt/dailytimer-agent/vps_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now dailytimer-agent >/dev/null 2>&1 || true
systemctl restart dailytimer-agent

# Разрешаем контейнерам ходить к агенту, если включён ufw.
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow from 172.16.0.0/12 to any port "$port" proto tcp >/dev/null
fi

# Передаём токен DailyTimer.
if [ -f "$repo/.env" ]; then
  grep -v '^VPS_AGENT_TOKEN=' "$repo/.env" > "$repo/.env.tmp" || true
  echo "VPS_AGENT_TOKEN=$token" >> "$repo/.env.tmp"
  mv "$repo/.env.tmp" "$repo/.env" && chmod 600 "$repo/.env"
fi
echo "==> Агент сервера запущен на $bind:$port"
