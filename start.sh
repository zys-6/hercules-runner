#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
if [[ -z "${HERCULES_RUNNER_TOKEN:-}" ]]; then
  python3 init_token.py
  export HERCULES_RUNNER_TOKEN="$(cat data/service.token)"
fi
exec "${RUNNER_PYTHON:-python3}" -m uvicorn runner:app --host 127.0.0.1 --port "${HERCULES_RUNNER_PORT:-8096}" --workers 1 --no-access-log
