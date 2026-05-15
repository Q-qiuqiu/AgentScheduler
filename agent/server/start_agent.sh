#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODEL_PATH="${RKLLM_MODEL_PATH:-/root/models/Qwen3-1.7B-rk3588-w8a8.rkllm}"
TARGET_PLATFORM="${RKLLM_TARGET_PLATFORM:-rk3588}"
LLM_HOST="${LLM_HOST:-127.0.0.1}"
LLM_PORT="${LLM_PORT:-8081}"
AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
AGENT_PORT="${AGENT_PORT:-8082}"
LLM_MODEL_NAME="${LLM_MODEL_NAME:-rkllm-default}"
LLM_API_URL="${LLM_API_URL:-http://${LLM_HOST}:${LLM_PORT}/v1/chat/completions}"
LLM_STARTUP_RETRIES="${LLM_STARTUP_RETRIES:-180}"

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

wait_for_port() {
  local host="$1"
  local port="$2"
  local retries="${3:-120}"
  for _ in $(seq 1 "${retries}"); do
    if bash -lc "exec 3<>/dev/tcp/${host}/${port}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

llm_pid=""
agent_pid=""

cleanup() {
  for pid in "${agent_pid}" "${llm_pid}"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

llm_cmd=(
  python3 "${SCRIPT_DIR}/flask_openai_server.py"
  --rkllm_model_path="${MODEL_PATH}"
  --target_platform="${TARGET_PLATFORM}"
)
if [[ -n "${LORA_MODEL_PATH:-}" ]]; then
  llm_cmd+=(--lora_model_path="${LORA_MODEL_PATH}")
fi
if [[ -n "${PROMPT_CACHE_PATH:-}" ]]; then
  llm_cmd+=(--prompt_cache_path="${PROMPT_CACHE_PATH}")
fi

"${llm_cmd[@]}" &
llm_pid=$!

if ! wait_for_port "${LLM_HOST}" "${LLM_PORT}" "${LLM_STARTUP_RETRIES}"; then
  echo "LLM service did not become ready on ${LLM_HOST}:${LLM_PORT}" >&2
  exit 1
fi

python3 "${SCRIPT_DIR}/agent.py" \
  --host "${AGENT_HOST}" \
  --port "${AGENT_PORT}" \
  --llm_api_url "${LLM_API_URL}" \
  --llm_model_name "${LLM_MODEL_NAME}" &
agent_pid=$!

wait "${agent_pid}"
