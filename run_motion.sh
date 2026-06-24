#!/bin/bash
set -Eeuo pipefail

# Nightly motion digest launcher (Apple Silicon).
#
# Activates the repo virtualenv, runs the motion digest (which downloads
# overnight clips, compresses them with VideoToolbox, and posts them to the
# "cctv" Discord channel via gRPC), and appends a timestamped log.
#
# Override paths with environment variables if needed:
#   VENV_DIR     Repo virtualenv (default: <repo>/.venv)
#   LOG_FILE     Log path        (default: <repo>/motion/motion.log)
#   DATA_DIR     Work dir for clips (default: /Volumes/drive/CCTV/motion/data)
#   MOTION_DB_DIR Source-of-truth motion DB dir (default: /Volumes/drive/CCTV/recordings/esp_cam1)
#   CCTV_RECORDINGS_DIR Recording/DB dir (default: /Volumes/drive/CCTV/recordings/esp_cam1)
#   FFMPEG_DIR   Extra PATH entry for ffmpeg/ffprobe (default: /opt/homebrew/bin)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$SCRIPT_DIR"

VENV_DIR="${VENV_DIR:-$BASE_DIR/.venv}"
LOG_FILE="${LOG_FILE:-$BASE_DIR/motion/motion.log}"
DATA_DIR="${DATA_DIR:-/Volumes/drive/CCTV/motion/data}"
MOTION_DB_DIR="${MOTION_DB_DIR:-/Volumes/drive/CCTV/recordings/esp_cam1}"
CCTV_RECORDINGS_DIR="${CCTV_RECORDINGS_DIR:-/Volumes/drive/CCTV/recordings/esp_cam1}"
FFMPEG_DIR="${FFMPEG_DIR:-/opt/homebrew/bin}"

export PATH="$FFMPEG_DIR:/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH="$BASE_DIR${PYTHONPATH:+:$PYTHONPATH}"
export DATA_DIR
export MOTION_DATA_DIR="${MOTION_DATA_DIR:-$DATA_DIR}"
export MOTION_DB_DIR
export CCTV_RECORDINGS_DIR

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$DATA_DIR"

cd "$BASE_DIR"

{
  echo "------------------------------------------------------------"
  echo "[START] $(date "+%Y-%m-%dT%H:%M:%S%z")"
} >> "$LOG_FILE"

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

# Sanity check: VideoToolbox encoder must be available (Apple Silicon).
if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_videotoolbox; then
  echo "[ERROR] h264_videotoolbox encoder unavailable; install ffmpeg (brew install ffmpeg)" >> "$LOG_FILE"
  exit 1
fi

if "$PYTHON_BIN" -m motion.motion >> "$LOG_FILE" 2>&1; then
  {
    echo "[END]   $(date "+%Y-%m-%dT%H:%M:%S%z") OK"
    echo
  } >> "$LOG_FILE"
else
  {
    echo "[ERROR] $(date "+%Y-%m-%dT%H:%M:%S%z") motion job failed"
    echo
  } >> "$LOG_FILE"
  exit 1
fi
