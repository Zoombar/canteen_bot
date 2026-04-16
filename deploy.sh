#!/usr/bin/env bash
# Запуск из корня репозитория (рядом с проектом):
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Деплой:
#   1) fetch + checkout + pull
#   2) docker compose down
#   3) docker compose up -d --build
#
# Ветка задаётся переменной окружения:
#   DEPLOY_BRANCH=main ./deploy.sh
#
# Compose-конфиг:
#   Если не найден docker-compose.yml/compose.yml — docker-часть будет пропущена.
#   При необходимости:
#     DEPLOY_COMPOSE_DIR=/path/to/dir ./deploy.sh
#     DEPLOY_COMPOSE_FILE_PATH=/path/to/docker-compose.yml ./deploy.sh
#
# SSH deploy key (опционально):
#   Скопируй приватный ключ на сервер и укажи путь:
#     DEPLOY_KEY_PATH=/home/deploy/.ssh/id_ed25519 ./deploy.sh
#   Скрипт выставит GIT_SSH_COMMAND так, чтобы git использовал этот ключ.
#
# Примечания:
#   - .env обычно остаётся локальным (должен быть в .gitignore).
#   - Если при сборке видите 429 Too Many Requests из Docker Hub:
#     проверь, что образы в Dockerfile/compose идут через зеркало/registry.
#
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

  # Включаем строгое использование указанного приватного ключа для git по SSH.
  export GIT_SSH_COMMAND="ssh -i '${DEPLOY_KEY_PATH}' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi

COMPOSE_FILE_PATH="${DEPLOY_COMPOSE_FILE_PATH:-}"
if [[ -z "${COMPOSE_FILE_PATH}" ]]; then
  COMPOSE_SEARCH_DIR="${DEPLOY_COMPOSE_DIR:-$ROOT}"
  for candidate in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
    if [[ -f "${COMPOSE_SEARCH_DIR}/${candidate}" ]]; then
      COMPOSE_FILE_PATH="${COMPOSE_SEARCH_DIR}/${candidate}"
      break
    fi
  done
fi

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  Deploy: $ROOT"
echo "  Remote: $REMOTE"
echo "  Ветка:  $BRANCH"
echo "  Время:  $(date -Iseconds 2>/dev/null || date)"
if [[ -n "${COMPOSE_FILE_PATH}" ]]; then
  echo "  Compose: ${COMPOSE_FILE_PATH}"
else
  echo "  Compose: (не найден — docker-часть будет пропущена)"
fi
echo "══════════════════════════════════════════════════════════════"
echo ""

echo "[1/4] Git: fetch + checkout + pull …"
git fetch "$REMOTE"

if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git checkout "$BRANCH"
else
  # Если локальной ветки ещё нет — создаём из удалённой.
  git checkout -b "$BRANCH" "${REMOTE}/${BRANCH}"
fi

# --ff-only чтобы не городить merge на сервере.
git pull --ff-only "$REMOTE" "$BRANCH"
echo "      ✓ Код обновлён (локальные .env не трогаются — они в .gitignore)."
echo ""

if [[ -z "${COMPOSE_FILE_PATH}" ]]; then
  echo "Похоже, docker compose конфиг не лежит в репозитории."
  echo "Пропускаю docker-часть. Проверь файлы: docker-compose*.yml/compose*.yml"
  echo ""
  exit 0
fi

echo "[2/4] Docker: останавливаем контейнеры этого проекта …"
docker compose -f "$COMPOSE_FILE_PATH" down --remove-orphans
echo "      ✓ Остановлено (тома БД и данные volumes не удаляются)."
echo ""

if [[ "${DEPLOY_DOCKER_PRUNE:-}" == "1" ]]; then
  echo "[2b] Docker: промывка кэша BuildKit (DEPLOY_DOCKER_PRUNE=1) …"
  if docker buildx prune -af 2>/dev/null; then
    echo "      ✓ buildx prune выполнен."
  elif docker builder prune -af 2>/dev/null; then
    echo "      ✓ builder prune выполнен."
  else
    echo "      ! prune недоступен — выполните вручную: docker buildx prune -af"
  fi
  echo ""
fi

echo "[3/4] Docker: compose up -d --build …"
docker compose -f "$COMPOSE_FILE_PATH" up -d --build
echo "      ✓ Контейнеры пересобраны и запущены."
echo ""

echo "[4/4] Состояние сервисов:"
docker compose -f "$COMPOSE_FILE_PATH" ps
echo ""

echo "══════════════════════════════════════════════════════════════"
echo "  Готово: деплой завершён без ошибок."
echo "  Проверь логи: docker compose -f \"$COMPOSE_FILE_PATH\" logs -f --tail=100"
echo "══════════════════════════════════════════════════════════════"

