#!/usr/bin/env bash
# KBT - установка Caddy (HTTPS) перед сервером лицензий. Домен вшит.
set -e
DOMAIN="kirusha-btw.ru"
IP="82.146.46.244"

echo "== Установка Caddy для $DOMAIN =="

# проверим, куда сейчас указывает домен
RES="$(getent hosts $DOMAIN | awk '{print $1}' | head -n1)"
echo "Домен $DOMAIN сейчас указывает на: ${RES:-(не найден)}  (нужно: $IP)"
if [ "$RES" != "$IP" ]; then
  echo "ВНИМАНИЕ: домен ещё не указывает на этот сервер."
  echo "Сначала пропиши A-запись (@ и www -> $IP) и подожди, пока обновится DNS."
  echo "Можно продолжить - Caddy сам выдаст сертификат, когда DNS заработает."
fi

# 1) ставим бинарник Caddy (без apt-репозиториев)
if ! /usr/bin/caddy version >/dev/null 2>&1; then
  echo "Скачиваю Caddy..."
  wget -qO /usr/bin/caddy "https://caddyserver.com/api/download?os=linux&arch=amd64" || true
  chmod +x /usr/bin/caddy 2>/dev/null || true
fi
if ! /usr/bin/caddy version >/dev/null 2>&1; then
  echo "Пробую запасной источник (GitHub)..."
  wget -qO /tmp/caddy.tar.gz "https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_linux_amd64.tar.gz"
  tar -xzf /tmp/caddy.tar.gz -C /tmp caddy
  mv /tmp/caddy /usr/bin/caddy
  chmod +x /usr/bin/caddy
fi
echo -n "Caddy: "; /usr/bin/caddy version

# 2) конфиг: HTTPS на домен -> наш сервер лицензий на 127.0.0.1:8080
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN, www.$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
EOF

# 3) служба
cat > /etc/systemd/system/caddy.service <<EOF
[Unit]
Description=Caddy
After=network.target
[Service]
Environment=HOME=/root
ExecStart=/usr/bin/caddy run --config /etc/caddy/Caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile
Restart=always
User=root
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now caddy
sleep 3
echo
echo "== Статус Caddy =="
systemctl --no-pager status caddy || true
echo
echo "Готово. Через 10-60 сек проверь: https://$DOMAIN/healthz"
echo "Если сертификат ещё выпускается - подожди минуту и обнови страницу."
