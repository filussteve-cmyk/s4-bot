#!/usr/bin/env bash
# S4 bot paper-mode launcher (Monday-ready).
#
# Maps the Rust bot's .env credentials (rotated Oct 14) to the S4 bot's
# env-var names at runtime. Values never touch git — config.yaml keeps its
# placeholders, and this script only reads the .env file into memory.
#
# Usage: ./run_paper.sh        # starts bot.py detached in paper mode
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="/home/hatch/workspace/trading-bot/.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE" >&2; exit 1; }

TOPSTEPX_USERNAME=""
TOPSTEPX_KEY=""
TOPSTEPX_ACCOUNT_ID="${TOPSTEPX_ACCOUNT_ID:-0}"
while IFS='=' read -r key val; do
  case "$key" in
    PROJECT_X_USERNAME) TOPSTEPX_USERNAME="$val" ;;
    PROJECT_X_API_KEY)  TOPSTEPX_KEY="$val" ;;
  esac
done < "$ENV_FILE"
export TOPSTEPX_USERNAME
export TOPSTEPX_API_KEY="$TOPSTEPX_KEY"
export TOPSTEPX_ACCOUNT_ID

[ -n "$TOPSTEPX_USERNAME" ] && [ -n "$TOPSTEPX_KEY" ] \
  || { echo "TopstepX credentials not found in $ENV_FILE" >&2; exit 1; }

pip install -q -r requirements.txt

LOG="paper-$(date +%F).log"
nohup python3 bot.py > "$LOG" 2>&1 &
echo "S4 paper bot started, PID $!. Log: $LOG"
echo "Paper mode: connects, reads live 5-min bars, logs signals, places ZERO orders."
