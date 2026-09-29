#!/usr/bin/env bash
# Start a disposable InvenTree server in Docker for the integration tests.
#
#   tests/integration/start-inventree.sh inventree/inventree:1.5.6
#   tests/integration/start-inventree.sh --stop
#
# SQLite, no worker, no proxy: the smallest setup that serves the REST API.
# The admin account exists only inside this container. The API token is written
# to a file and never printed. The script prints the environment variables the
# tests need.
set -euo pipefail

NAME=stockthread-it
PORT=${STOCKTHREAD_IT_PORT:-8000}
TOKEN_FILE=${STOCKTHREAD_IT_TOKEN_FILE:-${TMPDIR:-/tmp}/stockthread-it-token}
ADMIN_USER=stockthread-ci
ADMIN_PASSWORD=ci-only-password

stop() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$NAME" >/dev/null 2>&1 || true
  rm -f "$TOKEN_FILE"
}

if [[ "${1:-}" == "--stop" ]]; then
  stop
  exit 0
fi
IMAGE=${1:?usage: $0 IMAGE | --stop}

stop
docker volume create "$NAME" >/dev/null
settings=(
  -e INVENTREE_DEBUG=True
  -e INVENTREE_SITE_URL="http://localhost:$PORT"
  -e INVENTREE_DB_ENGINE=sqlite3
  -e INVENTREE_DB_NAME=/home/inventree/data/database.sqlite3
  -e INVENTREE_ADMIN_USER="$ADMIN_USER"
  -e INVENTREE_ADMIN_PASSWORD="$ADMIN_PASSWORD"
  -e INVENTREE_ADMIN_EMAIL=ci@acme-fiber.example
  -e INVENTREE_PLUGINS_ENABLED=False
  -v "$NAME:/home/inventree/data"
)

echo "Migrating a fresh database ($IMAGE)" >&2
started=$SECONDS
docker run --rm "${settings[@]}" "$IMAGE" invoke migrate >/dev/null
echo "Migrated in $((SECONDS - started)) s" >&2

docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" "${settings[@]}" "$IMAGE" >/dev/null

started=$SECONDS
until curl -fsS -o /dev/null "http://localhost:$PORT/api/"; do
  if (( SECONDS - started > 240 )); then
    echo "InvenTree did not answer within 240 s" >&2
    docker logs --tail 100 "$NAME" >&2
    exit 1
  fi
  python3 -c "import time; time.sleep(2)"
done
echo "API up after $((SECONDS - started)) s" >&2

# The admin account can appear a little after the API. Retry the token request.
umask 077
for _ in $(seq 30); do
  if curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" "http://localhost:$PORT/api/user/me/token/?name=stockthread-it" \
    | python3 -c "import json, sys; print(json.load(sys.stdin)['token'])" >"$TOKEN_FILE" 2>/dev/null; then
    break
  fi
  python3 -c "import time; time.sleep(2)"
done
if [[ ! -s "$TOKEN_FILE" ]]; then
  echo "Could not get an API token" >&2
  docker logs --tail 100 "$NAME" >&2
  exit 1
fi

echo "STOCKTHREAD_IT_URL=http://localhost:$PORT"
echo "STOCKTHREAD_IT_TOKEN_FILE=$TOKEN_FILE"
