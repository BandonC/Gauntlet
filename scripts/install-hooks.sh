#!/usr/bin/env bash
# Install the pre-commit hook that runs scripts/check.sh. Safe to run again.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

hook=$(git rev-parse --git-path hooks/pre-commit)
marker="# installed by scripts/install-hooks.sh"
if [ -e "$hook" ] && ! grep -qF "$marker" "$hook"; then
  echo "install-hooks.sh: $hook already exists and isn't ours; leaving it alone" >&2
  exit 1
fi

mkdir -p "$(dirname "$hook")"
cat > "$hook" <<EOF
#!/bin/sh
$marker
exec scripts/check.sh
EOF
chmod +x "$hook"
echo "installed $hook"
