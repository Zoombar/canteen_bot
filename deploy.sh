#!/usr/bin/env bash
# Запуск из корня репозитория:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Деплой:
#   1) fetch + checkout + pull
#   2) ручной рестарт сервиса отдельной командой
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
echo "  Время:   $(date -Iseconds 2>/dev/null || date)"
echo "══════════════════════════════════════════════════════════════"
echo ""

echo "[1/1] Git: fetch + checkout + pull …"
git fetch "$REMOTE"
if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git checkout "$BRANCH"
else
  git checkout -b "$BRANCH" "${REMOTE}/${BRANCH}"
fi
git pull --ff-only "$REMOTE" "$BRANCH"
echo "      ✓ Код обновлён (локальные .env не трогаются — они в .gitignore)."
echo ""

echo "══════════════════════════════════════════════════════════════"
echo "  Готово: код обновлён."
echo "  Перезапустите бота вручную:"
echo "    sudo systemctl restart canteen-bot"
echo "══════════════════════════════════════════════════════════════"

