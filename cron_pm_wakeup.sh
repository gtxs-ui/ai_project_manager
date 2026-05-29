#!/bin/bash
# Hourly PM wake-up — drops a "hourly_check" task into the PM's inbox.
# Registered in cron by deploy/install.sh: 0 * * * * /path/cron_pm_wakeup.sh
#
# The PM daemon picks it up and does its hourly routine:
# - Collect worker results
# - Update project status
# - Send summary to Telegram

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PM_INBOX="$SCRIPT_DIR/data/pm/inbox"

mkdir -p "$PM_INBOX"

python3 - "$PM_INBOX" << 'PYEOF'
import json, sys, uuid, datetime
from pathlib import Path

inbox = Path(sys.argv[1])
task_id = uuid.uuid4().hex[:12]
payload = {
    "task_id": task_id,
    "task": "Perform your hourly check: collect worker results, update project status, and send a brief Telegram summary.",
    "type": "hourly_check",
    "from": "cron",
    "submitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
}
(inbox / f"{task_id}.json").write_text(json.dumps(payload, indent=2))
print(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] Hourly check task queued: {task_id}")
PYEOF
