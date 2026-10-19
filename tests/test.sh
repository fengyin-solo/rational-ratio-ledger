#!/bin/bash
set -euo pipefail

LOG_DIR=/logs/verifier
REWARD_FILE="$LOG_DIR/reward.txt"
TEST_LOG=/tmp/ratio-verifier-output.log
completed=0
mkdir -p "$LOG_DIR"

write_reward() {
    chmod 0644 "$REWARD_FILE" 2>/dev/null || true
    printf '%s\n' "$1" > "$LOG_DIR/reward.txt"
    chmod 0444 "$REWARD_FILE"
}

on_exit() {
    if [ "$completed" -ne 1 ]; then
        write_reward 0.000
    fi
}

on_signal() {
    write_reward 0.000
    completed=1
    exit 0
}

write_reward 0.000
trap on_exit EXIT
trap on_signal INT TERM HUP

set +e
timeout --signal=TERM --kill-after=10s 300s python3 -u -m pytest -v -p no:cacheprovider /tests/test_outputs.py > "$TEST_LOG" 2>&1
rc=$?
set -e
cat "$TEST_LOG" 2>/dev/null || true

if [ "$rc" -eq 0 ]; then
    write_reward 1.000
else
    write_reward 0.000
fi
completed=1
exit 0
