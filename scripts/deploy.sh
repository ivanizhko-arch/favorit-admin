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
sleep 1
curl -sf http://127.0.0.1:8001/health && echo " ✓ admin service живой" || echo " ✗ admin service упал"
