#!/usr/bin/env bash
# Start a throwaway Postgres for `pytest -m db` when Docker isn't available
# (e.g. in a cloud scheduled-task run). Prints the DATABASE_URL to use.
#
#   export DATABASE_URL=$(scripts/test-db.sh)
#
# Locally, prefer `docker compose up -d testdb` (port 5433) instead.
set -euo pipefail

PORT="${TEST_DB_PORT:-54329}"
DATA="${TEST_DB_DIR:-/tmp/arena-test-pg}"
BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)"
[ -z "$BIN" ] && BIN="$(dirname "$(command -v initdb 2>/dev/null || echo /nonexistent/x)")"
if [ ! -x "$BIN/initdb" ]; then
  echo "postgres binaries not found; install with: apt-get install -y postgresql" >&2
  exit 1
fi

# Postgres refuses to run as root, so run it as the postgres user when we are root.
run() { if [ "$(id -u)" = 0 ]; then su postgres -s /bin/bash -c "$*"; else bash -c "$*"; fi; }

if ! run "$BIN/pg_isready -h /tmp -p $PORT" >/dev/null 2>&1; then
  if [ ! -f "$DATA/PG_VERSION" ]; then
    mkdir -p "$DATA"
    [ "$(id -u)" = 0 ] && chown postgres:postgres "$DATA"
    run "$BIN/initdb -D $DATA -U arena --auth=trust" >/dev/null
  fi
  run "$BIN/pg_ctl -D $DATA -o '-p $PORT -k /tmp' -l $DATA/log.txt -w start" >/dev/null
  run "$BIN/createdb -h /tmp -p $PORT -U arena arena_test" 2>/dev/null || true
fi

echo "postgresql://arena@/arena_test?host=/tmp&port=$PORT"
