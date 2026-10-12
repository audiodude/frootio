#!/usr/bin/env bash
# Launch frootio: load API keys, rebuild the frontend if stale, run the server.
set -eo pipefail
cd "$(dirname "$0")"

if [ -f ./frootio.env ]; then
  # shellcheck disable=SC1091
  set -a; . ./frootio.env; set +a
fi

index=frontend/dist/index.html
if [ ! -f "$index" ] || [ -n "$(find frontend/src frontend/index.html frontend/package.json \
    frontend/package-lock.json frontend/vite.config.ts frontend/tsconfig.json \
    -newer "$index" -print -quit)" ]; then
  echo "run.sh: building frontend" >&2
  (cd frontend && { [ -d node_modules ] || npm ci; } && npm run build)
fi

cd backend
exec uv run frootio
