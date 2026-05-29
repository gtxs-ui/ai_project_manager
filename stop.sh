#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If systemd is managing these services, the PID files point at systemd's
# processes — killing them here would just trigger a systemd restart. Defer.
if command -v systemctl >/dev/null 2>&1; then
    for svc in project-manager-worker project-manager-bridge; do
        if systemctl is-active --quiet "$svc.service" 2>/dev/null; then
            echo "ERROR: $svc.service is active under systemd."
            echo "Stop it with: sudo systemctl stop $svc.service"
            echo "Aborting stop.sh."
            exit 1
        fi
    done
fi

for name in worker bridge; do
    PID_FILE="$SCRIPT_DIR/data/pm/${name}.pid"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill "$PID" 2>/dev/null; then
            echo "Stopped $name (PID $PID)"
        else
            echo "$name not running (PID $PID)"
        fi
        rm -f "$PID_FILE"
    else
        echo "No PID file for $name"
    fi
done
