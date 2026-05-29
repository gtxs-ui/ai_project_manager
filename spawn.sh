#!/bin/bash
# Start the Project Manager (bridge + worker) without systemd.
# Logs go to data/pm/bridge_stdout.log and data/pm/worker_stdout.log

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$SCRIPT_DIR/data/pm"

# Refuse to run if systemd is already managing these services — otherwise we'd
# kill systemd's process via the PID file, then systemd's Restart=on-failure
# would try to relaunch and crash-loop against our flock.
if command -v systemctl >/dev/null 2>&1; then
    for svc in project-manager-worker project-manager-bridge; do
        if systemctl is-active --quiet "$svc.service" 2>/dev/null; then
            echo "ERROR: $svc.service is active under systemd."
            echo "Manage it with: sudo systemctl {start,stop,restart} $svc.service"
            echo "Aborting spawn.sh to avoid a crash loop."
            exit 1
        fi
    done
fi

# Find Python
PYTHON=""
for p in "$SCRIPT_DIR/venv/bin/python3" python3 python; do
    if [ -f "$p" ] || command -v "$p" &>/dev/null; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found"
    exit 1
fi

# Kill any existing instances
if [ -f "$SCRIPT_DIR/data/pm/worker.pid" ]; then
    OLD_PID=$(cat "$SCRIPT_DIR/data/pm/worker.pid" 2>/dev/null)
    [ -n "$OLD_PID" ] && kill "$OLD_PID" 2>/dev/null && echo "Killed old worker (PID $OLD_PID)"
    rm -f "$SCRIPT_DIR/data/pm/worker.pid"
fi

# Install deps if venv missing
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "Creating venv..."
    python3 -m venv "$SCRIPT_DIR/venv"
    "$SCRIPT_DIR/venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"
fi

echo "Starting PM worker..."
nohup "$PYTHON" "$SCRIPT_DIR/pm_worker.py" \
    > "$SCRIPT_DIR/data/pm/worker_stdout.log" 2>&1 &
WORKER_PID=$!
echo $WORKER_PID > "$SCRIPT_DIR/data/pm/worker.pid"
echo "  Worker PID: $WORKER_PID"

sleep 1

echo "Starting PM bridge..."
nohup "$PYTHON" "$SCRIPT_DIR/pm_bridge.py" \
    > "$SCRIPT_DIR/data/pm/bridge_stdout.log" 2>&1 &
BRIDGE_PID=$!
echo $BRIDGE_PID > "$SCRIPT_DIR/data/pm/bridge.pid"
echo "  Bridge PID: $BRIDGE_PID"

echo ""
echo "Project Manager running."
echo "Logs:"
echo "  Worker: $SCRIPT_DIR/data/pm/worker_stdout.log"
echo "  Bridge: $SCRIPT_DIR/data/pm/bridge_stdout.log"
echo ""
echo "Stop with: bash $SCRIPT_DIR/stop.sh"
