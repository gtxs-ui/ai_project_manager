#!/bin/bash
# Create a new worker agent.
# Usage: bash create_worker.sh <worker_name> "<skills description>"
#
# - Creates directory structure under data/workers/<name>/
# - Creates SKILLS.md and initial CLAUDE.md in workspace
# - Registers hourly cron job (55 * * * * → runs at :55 each hour)
# - Updates data/pm/workspace/workers.json registry

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_NAME="$1"
SKILLS="$2"

if [ -z "$WORKER_NAME" ] || [ -z "$SKILLS" ]; then
    echo "Usage: bash create_worker.sh <worker_name> \"<skills description>\""
    exit 1
fi

# Validate name
if [[ ! "$WORKER_NAME" =~ ^[a-z][a-z0-9_]*$ ]]; then
    echo "ERROR: Worker name must be lowercase letters/numbers/underscores, starting with a letter."
    exit 1
fi

WORKER_DIR="$SCRIPT_DIR/data/workers/$WORKER_NAME"

if [ -d "$WORKER_DIR" ]; then
    echo "Worker '$WORKER_NAME' already exists at $WORKER_DIR"
    exit 1
fi

echo "Creating worker '$WORKER_NAME'..."

# Create directory structure
mkdir -p "$WORKER_DIR/inbox"
mkdir -p "$WORKER_DIR/inbox_processed"
mkdir -p "$WORKER_DIR/outbox"
mkdir -p "$WORKER_DIR/outbox_processed"
mkdir -p "$WORKER_DIR/workspace"

# Write SKILLS.md
cat > "$WORKER_DIR/workspace/SKILLS.md" << EOF
# Worker: $WORKER_NAME

## Skills & Specialization
$SKILLS

## Created
$(date -Iseconds)
EOF

# Write initial status
cat > "$WORKER_DIR/workspace/status.md" << EOF
# Worker $WORKER_NAME — Status

Last run: (never)
Current status: idle
Active tasks: none
EOF

# Write ongoing_work placeholder
cat > "$WORKER_DIR/workspace/ongoing_work.md" << EOF
# Ongoing Work

(no active projects yet)
EOF

# Write initial state.json
cat > "$WORKER_DIR/state.json" << EOF
{
  "worker_name": "$WORKER_NAME",
  "session_id": null,
  "status": "idle",
  "tasks_done": 0,
  "last_run": null,
  "skills": "$SKILLS"
}
EOF

# Register hourly cron job (runs at :55 each hour)
CRON_CMD="55 * * * * $SCRIPT_DIR/invoke_worker.sh $WORKER_NAME >> $WORKER_DIR/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "invoke_worker.sh $WORKER_NAME"; echo "$CRON_CMD" ) | crontab -
echo "Cron registered: $CRON_CMD"

# Update PM workers.json registry
WORKERS_JSON="$SCRIPT_DIR/data/pm/workspace/workers.json"
mkdir -p "$(dirname "$WORKERS_JSON")"

if [ ! -f "$WORKERS_JSON" ]; then
    echo '{"workers": {}}' > "$WORKERS_JSON"
fi

# Use Python to update the JSON cleanly
python3 - "$WORKERS_JSON" "$WORKER_NAME" "$SKILLS" << 'PYEOF'
import json, sys, datetime
path, name, skills = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.loads(open(path).read())
data["workers"][name] = {
    "name": name,
    "skills": skills,
    "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    "status": "idle",
    "tasks_done": 0,
}
open(path, "w").write(json.dumps(data, indent=2))
print(f"Registry updated: {len(data['workers'])} worker(s) total")
PYEOF

echo ""
echo "✓ Worker '$WORKER_NAME' created at $WORKER_DIR"
echo "✓ Skills: $SKILLS"
echo "✓ Hourly cron registered (runs at :55 each hour)"
echo ""
echo "To assign a task now:"
echo "  bash $SCRIPT_DIR/send_task.sh $WORKER_NAME \"task description\""
echo ""
echo "To invoke immediately:"
echo "  bash $SCRIPT_DIR/invoke_worker.sh $WORKER_NAME"
