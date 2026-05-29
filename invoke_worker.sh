#!/bin/bash
# Invoke a worker agent immediately.
# Usage: bash invoke_worker.sh <worker_name>
#
# If the worker is already running (lock held), this is a no-op.
# Safe to call from cron and from the PM (concurrent-safe via lock file).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_NAME="$1"

if [ -z "$WORKER_NAME" ]; then
    echo "Usage: bash invoke_worker.sh <worker_name>"
    exit 1
fi

WORKER_DIR="$SCRIPT_DIR/data/workers/$WORKER_NAME"

if [ ! -d "$WORKER_DIR" ]; then
    echo "ERROR: Worker '$WORKER_NAME' not found. Create it first with create_worker.sh."
    exit 1
fi

# Find Python
PYTHON=""
for p in python3 python; do
    if command -v "$p" &>/dev/null; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found"
    exit 1
fi

# Check if venv exists and prefer it
if [ -f "$SCRIPT_DIR/venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/venv/bin/python3"
fi

LOG="$WORKER_DIR/invoke.log"
# Ensure user-local CLIs (npm-global, .local) are on PATH for workers even
# when invoked from cron / non-interactive shells that skip ~/.bashrc.
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
echo "[$(date -Iseconds)] Invoking worker '$WORKER_NAME' in background..."
nohup "$PYTHON" "$SCRIPT_DIR/worker_agent.py" "$WORKER_NAME" >> "$LOG" 2>&1 &
echo "[$(date -Iseconds)] Worker '$WORKER_NAME' started (PID $!). Log: $LOG"
