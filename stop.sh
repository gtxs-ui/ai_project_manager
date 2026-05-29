#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
