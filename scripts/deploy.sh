#!/usr/bin/env bash
# 운영 VM에서 실행한다. main을 받아 다시 빌드하고, /api/health의 commit으로 반영을 확인한다.
set -euo pipefail
cd "$(dirname "$0")/.."

git fetch origin
git checkout main
git pull --ff-only origin main

export GIT_COMMIT
GIT_COMMIT="$(git rev-parse --short HEAD)"
docker compose --profile tunnel up -d --build

for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${API_PORT:-8000}/api/health" | grep -q "\"commit\":\"${GIT_COMMIT}\""; then
    echo "deployed ${GIT_COMMIT}"
    exit 0
  fi
  sleep 2
done
echo "api did not report commit ${GIT_COMMIT} within 60s; check: docker compose logs api" >&2
exit 1
