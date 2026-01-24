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

mkdir -p "$(dirname "$LOG_FILE")"

# go to the subdir that expects ./data
cd "$BASE_DIR/motion"

# header
{
  echo "------------------------------------------------------------"
  echo "[START] $(date "+%Y-%m-%dT%H:%M:%S%z")"
} >> "$LOG_FILE"

# activate venv
if [[ ! -d "$VENV_DIR" && -d "$VENV_DIR_FALLBACK" ]]; then
  VENV_DIR="$VENV_DIR_FALLBACK"
fi

if [[ -f "$VENV_DIR/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate" >> "$LOG_FILE" 2>&1
else
  {
    echo "[WARN] Virtualenv not found at $VENV_DIR"
    echo "[WARN] Proceeding with system Python"
  } >> "$LOG_FILE"
fi

# ensure data dir exists and run
mkdir -p "$DATA_DIR"
DATA_DIR="$DATA_DIR" python3 motion.py >> "$LOG_FILE" 2>&1

# footer
{
  echo "[END]   $(date "+%Y-%m-%dT%H:%M:%S%z") ✅"
  echo
} >> "$LOG_FILE"
