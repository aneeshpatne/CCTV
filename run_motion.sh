#!/bin/bash
set -Eeuo pipefail

BASE_DIR="/home/aneesh/Desktop/Code/CCTV"
VENV_DIR="$BASE_DIR/venv"
LOG_FILE="$BASE_DIR/motion/motion.log"

export PATH=/usr/local/bin:/usr/bin:/bin

# go to the subdir that expects ./data
cd "$BASE_DIR/motion"

# header
{
  echo "------------------------------------------------------------"
  echo "[START] $(date -Is)"
} >> "$LOG_FILE"

# activate venv
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate" >> "$LOG_FILE" 2>&1

# ensure data dir exists and run
mkdir -p "$BASE_DIR/motion/data"
python motion.py >> "$LOG_FILE" 2>&1

# footer
{
  echo "[END]   $(date -Is) ✅"
  echo
} >> "$LOG_FILE"
