#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Start a disposable InvenTree server in Docker for the integration tests.
#
#   tests/integration/start-inventree.sh inventree/inventree:1.6.3
#   STOCKTHREAD_IT_DB=postgres tests/integration/start-inventree.sh inventree/inventree:1.6.3
#   tests/integration/start-inventree.sh --stop
#   STOCKTHREAD_IT_NAME=stockthread-soak STOCKTHREAD_IT_PORT=8010 tests/integration/start-inventree.sh ...
#
# STOCKTHREAD_IT_NAME names the containers, volumes, network and token file, so a second server (a long soak
# run) can live next to the one the tests use. Pass the same name with --stop.
# SQLite by default, or PostgreSQL in a second container. No worker, no proxy: the smallest setup that
# serves the REST API. Only named volumes and a private network: no host folder is mounted.
# The admin account exists only inside this container. The API token is written
# to a file and never printed. The script prints the environment variables the
# tests need.
set -euo pipefail

NAME=${STOCKTHREAD_IT_NAME:-stockthread-it}
if [[ ! "$NAME" =~ ^[a-z0-9][a-z0-9_.-]*$ ]]; then
  echo "STOCKTHREAD_IT_NAME must be lower case letters, digits, '.', '_' or '-'" >&2
  exit 2
fi
PORT=${STOCKTHREAD_IT_PORT:-8000}
TOKEN_FILE=${STOCKTHREAD_IT_TOKEN_FILE:-${TMPDIR:-/tmp}/$NAME-token}
ADMIN_USER=stockthread-ci
ADMIN_PASSWORD=ci-only-password
DB=${STOCKTHREAD_IT_DB:-sqlite}
DB_NAME="$NAME-db"
DB_IMAGE=postgres:16-alpine
DB_PASSWORD=ci-only-db-password

stop() {
  docker rm -f "$NAME" "$DB_NAME" >/dev/null 2>&1 || true
  docker volume rm "$NAME" "$DB_NAME" >/dev/null 2>&1 || true
  docker network rm "$NAME" >/dev/null 2>&1 || true
  rm -f "$TOKEN_FILE"
}

if [[ "${1:-}" == "--stop" ]]; then
  stop
  exit 0
fi
IMAGE=${1:?usage: $0 IMAGE | --stop}

stop
docker volume create "$NAME" >/dev/null
docker network create "$NAME" >/dev/null
case "$DB" in
  sqlite)
    database=(-e INVENTREE_DB_ENGINE=sqlite3 -e INVENTREE_DB_NAME=/home/inventree/data/database.sqlite3)
    ;;
  postgres)
    docker volume create "$DB_NAME" >/dev/null
    docker run -d --name "$DB_NAME" --network "$NAME" -v "$DB_NAME:/var/lib/postgresql/data" \
      -e POSTGRES_USER=inventree -e POSTGRES_PASSWORD="$DB_PASSWORD" -e POSTGRES_DB=inventree \
      "$DB_IMAGE" >/dev/null
    started=$SECONDS
    # pg_isready over TCP: the image answers on the socket during its init phase, before the final start.
    until docker exec "$DB_NAME" pg_isready -q -h 127.0.0.1 -U inventree -d inventree; do
      if (( SECONDS - started > 60 )); then
        echo "PostgreSQL did not start within 60 s" >&2
        docker logs --tail 50 "$DB_NAME" >&2
        exit 1
      fi
      python3 -c "import time; time.sleep(1)"
    done
    echo "PostgreSQL up after $((SECONDS - started)) s" >&2
    database=(
      -e INVENTREE_DB_ENGINE=postgresql -e INVENTREE_DB_NAME=inventree -e INVENTREE_DB_HOST="$DB_NAME"
      -e INVENTREE_DB_PORT=5432 -e INVENTREE_DB_USER=inventree -e INVENTREE_DB_PASSWORD="$DB_PASSWORD"
    )
    ;;
  *)
    echo "STOCKTHREAD_IT_DB must be sqlite or postgres" >&2
    exit 2
    ;;
esac
settings=(
  --network "$NAME"
  -e INVENTREE_DEBUG=True
  -e INVENTREE_SITE_URL="http://localhost:$PORT"
  "${database[@]}"
  -e INVENTREE_ADMIN_USER="$ADMIN_USER"
  -e INVENTREE_ADMIN_PASSWORD="$ADMIN_PASSWORD"
  -e INVENTREE_ADMIN_EMAIL=ci@acme-fiber.example
  -e INVENTREE_PLUGINS_ENABLED=False
  -v "$NAME:/home/inventree/data"
)

echo "Migrating a fresh $DB database ($IMAGE)" >&2
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
  if curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" "http://localhost:$PORT/api/user/me/token/?name=$NAME" \
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
echo "STOCKTHREAD_IT_DB=$DB"
echo "STOCKTHREAD_IT_TOKEN_FILE=$TOKEN_FILE"
