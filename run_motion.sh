#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$SCRIPT_DIR"
VENV_DIR_DEFAULT="$BASE_DIR/.venv"
VENV_DIR_FALLBACK="$BASE_DIR/venv"
LOG_FILE_DEFAULT="$BASE_DIR/motion/motion.log"
DATA_DIR_DEFAULT="$BASE_DIR/motion/data"

VENV_DIR="${VENV_DIR:-$VENV_DIR_DEFAULT}"
LOG_FILE="${LOG_FILE:-$LOG_FILE_DEFAULT}"
DATA_DIR="${DATA_DIR:-$DATA_DIR_DEFAULT}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH="$BASE_DIR${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$DATA_DIR"

# Run from repo root so package imports (e.g. cctv_telegram) resolve correctly.
cd "$BASE_DIR"

{
  echo "------------------------------------------------------------"
  echo "[START] $(date "+%Y-%m-%dT%H:%M:%S%z")"
} >> "$LOG_FILE"

if [[ ! -d "$VENV_DIR" && -d "$VENV_DIR_FALLBACK" ]]; then
  VENV_DIR="$VENV_DIR_FALLBACK"
fi

PYTHON_BIN="python3"
if [[ -f "$VENV_DIR/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  PYTHON_BIN="$VENV_DIR/bin/python"
  echo "[INFO] Using virtualenv: $VENV_DIR" >> "$LOG_FILE"
else
  {
    echo "[WARN] Virtualenv not found at $VENV_DIR"
    echo "[WARN] Proceeding with system Python"
  } >> "$LOG_FILE"
fi

if DATA_DIR="$DATA_DIR" MOTION_DATA_DIR="$DATA_DIR" "$PYTHON_BIN" -m motion.motion >> "$LOG_FILE" 2>&1; then
  {
    echo "[END]   $(date "+%Y-%m-%dT%H:%M:%S%z") ✅"
    echo
  } >> "$LOG_FILE"
else
  {
    echo "[ERROR] $(date "+%Y-%m-%dT%H:%M:%S%z") motion job failed"
    echo
  } >> "$LOG_FILE"
  exit 1
fi
