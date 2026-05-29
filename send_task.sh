#!/bin/bash
# Send a task to a worker's inbox.
# Usage: bash send_task.sh <worker_name> "<task description>" ["<context>"]
#
# Writes a properly formatted JSON task file to data/workers/<name>/inbox/
# Does NOT invoke the worker — call invoke_worker.sh separately if you want immediate execution.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_NAME="$1"
TASK="$2"
CONTEXT="${3:-}"

if [ -z "$WORKER_NAME" ] || [ -z "$TASK" ]; then
    echo "Usage: bash send_task.sh <worker_name> \"<task>\" [\"<context>\"]"
    exit 1
fi

WORKER_DIR="$SCRIPT_DIR/data/workers/$WORKER_NAME"

if [ ! -d "$WORKER_DIR" ]; then
    echo "ERROR: Worker '$WORKER_NAME' not found. Create it first:"
    echo "  bash $SCRIPT_DIR/create_worker.sh $WORKER_NAME \"skills description\""
    exit 1
fi

python3 - "$WORKER_DIR/inbox" "$TASK" "$CONTEXT" << 'PYEOF'
import json, sys, uuid, datetime
from pathlib import Path

inbox_dir = Path(sys.argv[1])
task_text = sys.argv[2]
context = sys.argv[3] if len(sys.argv) > 3 else ""

task_id = uuid.uuid4().hex[:12]
payload = {
    "task_id": task_id,
    "task": task_text,
    "context": context,
    "priority": "normal",
    "submitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
    "from": "project_manager",
}
out = inbox_dir / f"{task_id}.json"
out.write_text(json.dumps(payload, indent=2))
print(f"Task created: {task_id}")
print(f"File: {out}")
PYEOF
