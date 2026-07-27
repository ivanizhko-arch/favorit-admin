#!/usr/bin/env bash
# Деплой админ-сервиса на прод. Запускать с сервера (SSH):
#   ssh favorit@84.201.152.25 'bash ~/favorit-admin/scripts/deploy.sh'
# Иван запускает это после мёржа PR в main.

set -e

cd ~/favorit-admin

echo "▶ 1/3  git pull"
git pull --ff-only

echo "▶ 2/3  install deps (если поменялся requirements.txt)"
source .venv/bin/activate
pip install -r requirements.txt --quiet

echo "▶ 3/3  restart сервиса"
sudo systemctl restart favorit-admin
# uvicorn'у нужно ~2 сек чтобы поднять сокет — иначе curl раньше времени
# получит connection refused и мы решим что сервис упал (ложное тревога).
for i in 1 2 3 4 5; do
    sleep 1
    if curl -sf http://127.0.0.1:8001/health >/dev/null; then
        echo " ✓ admin service живой (проснулся за ${i} сек)"
        exit 0
    fi
done
echo " ✗ admin service не отвечает после 5 сек — проверь: sudo journalctl -u favorit-admin -n 50"
exit 1
