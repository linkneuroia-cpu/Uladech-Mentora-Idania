#!/usr/bin/env bash
# Despliegue basado en Git: el servidor queda con un checkout real de un
# commit/rama/tag concreto, en vez de "lo último que alguien subió por
# tar/scp" (deploy/upload.sh + deploy/deploy.sh, que seguían el working
# directory local tal cual). Soluciona dos problemas reales encontrados:
# no había forma de saber qué versión corría en el servidor, y no había
# cómo volver atrás sin reconstruir todo a mano.
#
# Uso:
#   deploy/deploy_git.sh preprod              # despliega develop en preproducción
#   deploy/deploy_git.sh prod                 # despliega main en producción
#   deploy/deploy_git.sh prod v1.2.0          # despliega un tag puntual (rollback)
#   deploy/deploy_git.sh prod 7cca0d9         # despliega un commit puntual
#
# No toca el .env real de cada servidor (nunca se versiona, ver
# .gitignore) ni pisa archivos no versionados (temp_docs/, venv/) — usa
# `git checkout -f`, que solo actualiza archivos TRACKEADOS.
set -euo pipefail
cd "$(dirname "$0")"

ENTORNO="${1:-}"
REF="${2:-}"

case "$ENTORNO" in
  prod)
    ENV_FILE=".env.deploy"
    COMPOSE_FILE="docker-compose.yml"
    [ -z "$REF" ] && REF="main"
    ;;
  preprod)
    ENV_FILE=".env.deploy.preprod"
    COMPOSE_FILE="docker-compose.preprod.yml"
    [ -z "$REF" ] && REF="develop"
    ;;
  *)
    echo "Uso: deploy_git.sh <prod|preprod> [ref]" >&2
    exit 1
    ;;
esac

if [ ! -f "$ENV_FILE" ]; then
  echo "Falta deploy/${ENV_FILE} — copiá deploy/.env.deploy.example y completá los datos reales." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${GIT_REMOTE_URL:?Falta GIT_REMOTE_URL en deploy/${ENV_FILE} (URL del repo a desplegar).}"

REMOTE_PATH_ESCAPED="${DEPLOY_PATH// /\\ }"

# Todo el checkout + build + up corre en UNA sola sesión ssh (variables de
# shell no sobreviven entre llamadas separadas a ssh).
REMOTE_CMD=$(cat <<REMOTE
set -euo pipefail
cd "${REMOTE_PATH_ESCAPED}"
if [ ! -d .git ]; then
  echo "→ Primera vez: convirtiendo esta carpeta en un checkout de git (no toca .env ni archivos sin versionar)."
  git init -q
  git remote add origin "${GIT_REMOTE_URL}"
fi
git remote set-url origin "${GIT_REMOTE_URL}"
git fetch origin --tags -q
# "${REF}" puede ser una rama (develop/main), un tag o un commit puntual
# (rollback) — se resuelve primero como rama remota recién traída; si no
# existe como tal, se usa tal cual (tag o SHA, ya alcanzable tras el fetch).
TARGET="\$(git rev-parse --verify "origin/${REF}" 2>/dev/null || git rev-parse --verify "${REF}")"
git checkout -f "\${TARGET}"
COMMIT="\$(git rev-parse --short HEAD)"
echo "→ Checkout en commit \${COMMIT} (ref: ${REF})"
test -f .env || { echo 'Falta .env en el servidor — creá uno con los secretos reales antes de desplegar.' >&2; exit 1; }
export IMAGE_TAG="\${COMMIT}"
sudo -S docker compose -f ${COMPOSE_FILE} up -d --build
echo "\${COMMIT}" > .ultimo_deploy_exitoso
REMOTE
)

if ls "${HOME}"/.ssh/id_* >/dev/null 2>&1; then
  ssh "${DEPLOY_USER}@${DEPLOY_HOST}" "$REMOTE_CMD" <<< "${DEPLOY_PASSWORD}"
else
  command -v sshpass >/dev/null 2>&1 || { echo "Falta 'sshpass'." >&2; exit 1; }
  sshpass -p "${DEPLOY_PASSWORD}" ssh "${DEPLOY_USER}@${DEPLOY_HOST}" "$REMOTE_CMD" <<< "${DEPLOY_PASSWORD}"
fi

echo "✅ Desplegado '${REF}' en ${ENTORNO} (${DEPLOY_HOST})."
