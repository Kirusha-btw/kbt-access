#!/usr/bin/env bash
# KBT License - установщик для сервера. Секретов внутри нет (токен спросит при запуске).
set -e
export DEBIAN_FRONTEND=noninteractive

echo "== Установка KBT License =="
apt-get update -y
apt-get install -y python3 wget

mkdir -p /opt/kbt-license
cd /opt/kbt-license
echo "Скачиваю kbt_license.py с GitHub..."
wget -qO kbt_license.py https://raw.githubusercontent.com/Kirusha-btw/kbt-access/main/kbt_license.py

echo
echo "Введи токен бота (от BotFather) и нажми Enter:"
read -r TOKEN

cat > /etc/systemd/system/kbt-license.service <<EOF
[Unit]
After=network.target
[Service]
WorkingDirectory=/opt/kbt-license
Environment=BOT_TOKEN=$TOKEN
Environment=ADMIN_IDS=6233168517
Environment=PRICE_PER_DAY=1
Environment=BOT_USERNAME=KirushaBTWbot
ExecStart=/usr/bin/python3 /opt/kbt-license/kbt_license.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now kbt-license
sleep 2
echo
echo "== Статус =="
systemctl --no-pager status kbt-license || true
echo
echo "Готово. Проверь: http://82.146.46.244:8080/healthz и бота в Telegram."
