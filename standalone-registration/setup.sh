#!/usr/bin/env bash
# Optional helper: seed .env if missing
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — please edit secrets before starting."
else
  echo ".env already exists"
fi
echo "Next: docker compose up -d --build"
