#!/bin/bash
# Run requirement -> spec -> code -> verify pipeline with OpenRouter.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Auto-detect libclang for compatibility with existing project tooling.
if [[ -z "${LIBCLANG_PATH:-}" ]]; then
  if [[ -f "/opt/clang/lib/libclang.so" ]]; then
    export LIBCLANG_PATH="/opt/clang/lib/libclang.so"
  elif [[ -f "/usr/lib/llvm-18/lib/libclang.so" ]]; then
    export LIBCLANG_PATH="/usr/lib/llvm-18/lib/libclang.so"
  fi
fi

if [[ -n "${LIBCLANG_PATH:-}" ]]; then
  libclang_dir="$(dirname "$LIBCLANG_PATH")"
  export LD_LIBRARY_PATH="$libclang_dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# -----------------------------
# User-configurable parameters
# -----------------------------
MODEL="${MODEL:-deepseek/deepseek-v3.2}"
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"

# If key is defined in shell init files, load them for non-interactive runs.
if [[ -z "$OPENROUTER_API_KEY" ]]; then
  if [[ -f "$HOME/.bashrc" ]]; then
    # shellcheck disable=SC1090
    source "$HOME/.bashrc"
  fi
  if [[ -z "${OPENROUTER_API_KEY:-}" ]] && [[ -f "$HOME/.profile" ]]; then
    # shellcheck disable=SC1090
    source "$HOME/.profile"
  fi
fi
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
export OPENROUTER_API_KEY

REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-benchmarks/frama-c-problems/requirements/requirements_51.json}"
OUTPUT_DIR="${OUTPUT_DIR:-outputs/req2code-openrouter}"
VERIFY_TIMEOUT="${VERIFY_TIMEOUT:-120}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-120}"
TEMPERATURE="${TEMPERATURE:-0.1}"
MAX_TOKENS="${MAX_TOKENS:-2048}"
SKIP_VERIFY="${SKIP_VERIFY:-false}" # true/false
TASK_ID="${TASK_ID:-}"              # optional single task id

if [[ -z "$OPENROUTER_API_KEY" ]]; then
  echo "ERROR: OPENROUTER_API_KEY is not set."
  echo "Set it before running, for example:"
  echo "  export OPENROUTER_API_KEY=sk-or-xxxx"
  exit 1
fi

CMD=(
  python3 scripts/run_requirement_pipeline.py
  --requirements-file "$REQUIREMENTS_FILE"
  --output-dir "$OUTPUT_DIR"
  --endpoint "https://openrouter.ai/api/v1/chat/completions"
  --model "$MODEL"
  --api-key "$OPENROUTER_API_KEY"
  --api-key-env "OPENROUTER_API_KEY"
  --temperature "$TEMPERATURE"
  --max-tokens "$MAX_TOKENS"
  --request-timeout "$REQUEST_TIMEOUT"
  --verify-timeout "$VERIFY_TIMEOUT"
)

if [[ "${SKIP_VERIFY,,}" == "true" ]]; then
  CMD+=(--skip-verify)
fi

if [[ -n "$TASK_ID" ]]; then
  CMD+=(--task-id "$TASK_ID")
fi

echo "[INFO] Running requirement pipeline with OpenRouter..."
echo "[INFO] REQUIREMENTS_FILE=$REQUIREMENTS_FILE"
echo "[INFO] OUTPUT_DIR=$OUTPUT_DIR"
echo "[INFO] MODEL=$MODEL"
echo "[INFO] SKIP_VERIFY=$SKIP_VERIFY"
if [[ -n "$TASK_ID" ]]; then
  echo "[INFO] TASK_ID=$TASK_ID"
fi

"${CMD[@]}"
