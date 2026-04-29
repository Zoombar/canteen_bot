#!/usr/bin/env bash
# Запуск из корня репозитория:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Деплой (без Docker):
#   1) fetch + checkout + pull
#   2) sudo systemctl restart canteen-bot
#
# Опции:
#   DEPLOY_BRANCH=main ./deploy.sh
#   DEPLOY_REMOTE=origin ./deploy.sh
#   DEPLOY_SYSTEMD_SERVICE=canteen-bot ./deploy.sh
#   DEPLOY_KEY_PATH=/home/deploy/.ssh/id_ed25519 ./deploy.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

REMOTE="${DEPLOY_REMOTE:-origin}"
BRANCH="${DEPLOY_BRANCH:-main}"
SYSTEMD_SERVICE_NAME="${DEPLOY_SYSTEMD_SERVICE:-canteen-bot}"

if [[ -n "${DEPLOY_KEY_PATH:-}" ]]; then
  if [[ ! -f "${DEPLOY_KEY_PATH}" ]]; then
    echo "Ошибка: DEPLOY_KEY_PATH не найден: ${DEPLOY_KEY_PATH}" >&2
    exit 1
  fi
  export GIT_SSH_COMMAND="ssh -i '${DEPLOY_KEY_PATH}' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  Deploy:  $ROOT"
echo "  Remote:  $REMOTE"
echo "  Ветка:   $BRANCH"
echo "  Сервис:  $SYSTEMD_SERVICE_NAME"
echo "  Время:   $(date -Iseconds 2>/dev/null || date)"
echo "══════════════════════════════════════════════════════════════"
echo ""

echo "[1/2] Git: fetch + checkout + pull …"
git fetch "$REMOTE"
if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git checkout "$BRANCH"
else
  git checkout -b "$BRANCH" "${REMOTE}/${BRANCH}"
fi
git pull --ff-only "$REMOTE" "$BRANCH"
echo "      ✓ Код обновлён (локальные .env не трогаются — они в .gitignore)."
echo ""

echo "[2/2] Systemd: перезапуск сервиса ${SYSTEMD_SERVICE_NAME} …"
sudo systemctl restart "${SYSTEMD_SERVICE_NAME}"
echo "      ✓ Сервис перезапущен."
echo ""

echo "Статус сервиса:"
sudo systemctl --no-pager --full status "${SYSTEMD_SERVICE_NAME}" || true
echo ""

echo "══════════════════════════════════════════════════════════════"
echo "  Готово: деплой завершён без ошибок."
echo "══════════════════════════════════════════════════════════════"

