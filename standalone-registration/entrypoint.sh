#!/usr/bin/env bash
# Standalone registration entrypoint:
# 1) optional inline Turnstile Solver (local captcha)
# 2) FastAPI Web UI + registration API
set -euo pipefail
cd /app

export PYTHONPATH="${PYTHONPATH:-/app:/app/grok-build-auth}"
case ":${PYTHONPATH}:" in
  *":/app:"*) ;;
  *) export PYTHONPATH="/app:${PYTHONPATH}" ;;
esac
case ":${PYTHONPATH}:" in
  *":/app/grok-build-auth:"*) ;;
  *) export PYTHONPATH="${PYTHONPATH}:/app/grok-build-auth" ;;
esac

export GROK2API_DATA_DIR="${GROK2API_DATA_DIR:-/app/data}"
export GROK2API_STORE_BACKEND=file
export GROK2API_REG_SKIP_PROBE="${GROK2API_REG_SKIP_PROBE:-1}"
export GROK2API_REQUIRE_SHARED_STORES=0
export GROK2API_DATABASE_URL="${GROK2API_DATABASE_URL:-}"
export DATABASE_URL="${DATABASE_URL:-}"
export STANDALONE_VENDOR_ROOT="${STANDALONE_VENDOR_ROOT:-/app}"
mkdir -p "${GROK2API_DATA_DIR}" /app/turnstile-solver/logs /app/turnstile-solver/keys

provider="$(echo "${GROK2API_CAPTCHA_PROVIDER:-${CAPTCHA_PROVIDER:-local}}" | tr '[:upper:]' '[:lower:]')"
enable_solver="${GROK2API_INLINE_SOLVER:-1}"
solver_port="${TURNSTILE_PORT:-5072}"
reg_concurrency="${GROK2API_REG_CONCURRENCY:-3}"
solver_thread="${TURNSTILE_THREAD:-${reg_concurrency}}"
solver_browser="${TURNSTILE_BROWSER_TYPE:-camoufox}"
solver_host="${TURNSTILE_HOST:-127.0.0.1}"
solver_pid=""

cleanup() {
  if [[ -n "${solver_pid}" ]] && kill -0 "${solver_pid}" 2>/dev/null; then
    echo "[entrypoint] stopping turnstile-solver pid=${solver_pid}"
    kill "${solver_pid}" 2>/dev/null || true
    wait "${solver_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

start_inline_solver() {
  if [[ ! -f /app/turnstile-solver/api_solver.py ]]; then
    echo "[entrypoint] turnstile-solver missing; skip inline solver"
    return 0
  fi
  export TURNSTILE_LAZY="${TURNSTILE_LAZY:-1}"
  export TURNSTILE_IDLE_SEC="${TURNSTILE_IDLE_SEC:-180}"
  echo "[entrypoint] starting turnstile-solver on ${solver_host}:${solver_port} (thread=${solver_thread}, browser=${solver_browser})"
  (
    cd /app/turnstile-solver
    exec python api_solver.py \
      --browser_type "${solver_browser}" \
      --thread "${solver_thread}" \
      --host "${solver_host}" \
      --port "${solver_port}" \
      --debug
  ) > /app/turnstile-solver/logs/turnstile_solver.log 2>&1 &
  solver_pid=$!
  echo "${solver_pid}" > /app/turnstile-solver/logs/turnstile_solver.pid
  for i in $(seq 1 90); do
    if curl -fsS -m 1 "http://127.0.0.1:${solver_port}/health" >/dev/null 2>&1 \
      || curl -fsS -m 1 "http://127.0.0.1:${solver_port}/" >/dev/null 2>&1; then
      echo "[entrypoint] turnstile-solver ready"
      return 0
    fi
    if ! kill -0 "${solver_pid}" 2>/dev/null; then
      echo "[entrypoint] WARN: turnstile-solver exited early; see turnstile-solver/logs/turnstile_solver.log" >&2
      return 0
    fi
    sleep 1
  done
  echo "[entrypoint] WARN: turnstile-solver not ready after 90s; continuing" >&2
}

if [[ "${provider}" == "local" && "${enable_solver}" != "0" ]]; then
  start_inline_solver || true
  export GROK2API_LOCAL_SOLVER_URL="${GROK2API_LOCAL_SOLVER_URL:-http://127.0.0.1:${solver_port}}"
  export LOCAL_SOLVER_URL="${LOCAL_SOLVER_URL:-http://127.0.0.1:${solver_port}}"
fi

host="${REG_UI_HOST:-0.0.0.0}"
port="${REG_UI_PORT:-8080}"
echo "[entrypoint] starting standalone registration UI on ${host}:${port}"
exec python -m app.main
