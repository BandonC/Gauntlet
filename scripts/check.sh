#!/usr/bin/env bash
# Every check a commit must pass. The pre-commit hook runs this; so should you.
#
#   scripts/check.sh
#   DATABASE_URL=postgresql://arena:arena@localhost:5433/arena_test scripts/check.sh
#
# Don't pipe this or pytest through tail or head without pipefail: the pipe's exit code
# hides the failure, and whatever is chained after it runs anyway.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${PYTHON:-}" ]; then
  PYTHON=python
  for candidate in .venv/Scripts/python.exe .venv/bin/python; do
    if [ -x "$candidate" ]; then
      PYTHON=$candidate
      break
    fi
  done
fi

# tests/test_store_db.py DROPS the public schema, so never run it on anything else. Both
# the testdb container and scripts/test-db.sh name their database arena_test.
if [ -n "${DATABASE_URL:-}" ]; then
  database=${DATABASE_URL%%\?*}
  database=${database##*/}
  if [ "$database" != arena_test ]; then
    echo "check.sh: refusing to run db tests: DATABASE_URL must name arena_test" >&2
    exit 1
  fi
fi

echo "==> ruff check"
"$PYTHON" -m ruff check arena tests
echo "==> ruff format --check"
"$PYTHON" -m ruff format --check arena tests
echo "==> mypy"
"$PYTHON" -m mypy arena
# Plain pytest would run the db tests too when DATABASE_URL is set, and then again below.
echo "==> pytest"
"$PYTHON" -m pytest -q -m "not slow and not db"
if [ -n "${DATABASE_URL:-}" ]; then
  echo "==> pytest -m db"
  "$PYTHON" -m pytest -m db -q
fi
