"""Project Manager AI worker daemon.

Single Claude-powered PM that:
- Watches inbox/ for tasks from the Telegram bridge or cron
- Runs `claude -p` with a persistent session_id (memory across conversations)
- Orchestrates worker agents via filesystem IPC
- Writes results to outbox/ for Telegram bridge to pick up
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

WORKER_ID = "pm"
ROOT = Path(__file__).parent
DATA_ROOT = ROOT / "data" / "pm"
DATA_WORKERS = ROOT / "data" / "workers"
INBOX           = DATA_ROOT / "inbox"
OUTBOX_TELEGRAM = DATA_ROOT / "outbox_telegram"
OUTBOX_SENT     = DATA_ROOT / "outbox_sent"
WORKSPACE       = DATA_ROOT / "workspace"
STATE_FILE      = DATA_ROOT / "state.json"
LOG_FILE        = DATA_ROOT / "log.txt"
PID_FILE        = DATA_ROOT / "worker.pid"

for d in (INBOX, OUTBOX_TELEGRAM, OUTBOX_SENT, WORKSPACE, DATA_WORKERS):
    d.mkdir(parents=True, exist_ok=True)

CLAUDE_TIMEOUT = 1200
ALLOWED_TOOLS = "WebSearch,WebFetch,Write,Read,Edit,Bash"
SESSION_MAX_BYTES = 2 * 1024 * 1024
RATE_LIMIT_NOTIFY_INTERVAL = 1800  # 30 minutes between user-facing rate-limit notifications
RATE_LIMIT_BACKOFF = 60  # sleep this long after a rate-limit before checking inbox again

_CLAUDE_SEARCH = [
    shutil.which("claude"),
    str(Path.home() / ".npm-global/bin/claude"),
    "/root/.local/bin/claude",
    "/usr/local/bin/claude",
    str(Path.home() / ".local/bin/claude"),
    str(Path.home() / ".claude/local/claude"),
]
def _path_exists(p: str) -> bool:
    try:
        return Path(p).exists()
    except PermissionError:
        return False

CLAUDE_BIN = next((p for p in _CLAUDE_SEARCH if p and _path_exists(p)), "claude")


def _write_workspace_claude_md() -> None:
    (WORKSPACE / "CLAUDE.md").write_text(f"""# Project Manager AI

You are an autonomous Project Manager AI. Your user reaches you via Telegram.
You manage a team of specialist worker agents to complete projects.

## Your Role
1. Receive goals from the user
2. Break goals into concrete subtasks
3. Create specialist workers as needed
4. Assign subtasks to workers
5. Monitor progress (hourly via cron wakeup, or on-demand when user asks)
6. Report results back to the user

## Response Pattern (MANDATORY)
Your VERY FIRST tool call on any user message must be writing an immediate acknowledgement
to the outbox so the user gets a reply instantly, before you do any other work.

Use Bash to write the ack (replace TASK_ID and REPLY_TO with values from your current task):
```bash
python3 -c "
import json, datetime
from pathlib import Path
payload = {{
  'task_id': 'TASK_ID',
  'worker_id': 'pm',
  'result': 'YOUR BRIEF REPLY HERE — plan, what you are doing next',
  'reply_to_message_id': REPLY_TO,
  'source': 'telegram',
  'completed_at': datetime.datetime.now().isoformat()
}}
Path('{OUTBOX_TELEGRAM}/TASK_ID_ack.json').write_text(json.dumps(payload))
"
```

Then proceed with the actual work (create workers, assign tasks, invoke).
Never wait for workers to finish — invoke_worker.sh is background, return immediately.

Example:
User sends task_id=abc123, reply_to=456
→ First tool call: write ack to outbox with result="Got it. Creating game_dev worker, breaking into 5 tasks. I'll update you each hour."
→ Then: create worker → assign task → invoke worker → done.

## Task Sizing Rule (MANDATORY)
Never assign a task that takes more than 15 minutes. Break big tasks into small sequential ones:

BAD:  "Build the entire gaming website with 4 games"
GOOD:
  Task 1 — "Build index.html with navigation and game grid layout"
  Task 2 — "Build Snake game in snake.html"
  Task 3 — "Build Tic-Tac-Toe in tictactoe.html"
  (assign next task only after previous one completes)

## ALWAYS DO FIRST (every task)
Read these files to restore context before doing anything:
- `{WORKSPACE}/project_status.md` — current projects and state
- `{WORKSPACE}/notes.md` — important context and decisions

## Your Files
| File | Purpose |
|------|---------|
| `{WORKSPACE}/workers.json` | Registry of all workers (name, skills, status) |
| `{WORKSPACE}/project_status.md` | Active projects, goals, assignments |
| `{WORKSPACE}/notes.md` | Important context, decisions made |
| `{WORKSPACE}/user_preferences.md` | User's style and preferences |

Update `project_status.md` after EVERY significant action (new project, task assigned, results received).

## Worker Management API

### Create a new worker
```bash
bash {ROOT}/create_worker.sh "<name>" "<one-line skills description>"
```
- Name: lowercase with underscores (`researcher`, `code_writer`, `analyst`)
- Skills: what this worker specializes in
- This creates the worker's directories, CLAUDE.md, and hourly cron job

### Assign a task to a worker
```bash
bash {ROOT}/send_task.sh "<worker_name>" "<task description>" "<optional context>"
```
If the worker doesn't exist yet, create it first with `create_worker.sh`.

### Invoke a worker immediately (don't wait for hourly cron)
```bash
bash {ROOT}/invoke_worker.sh <worker_name>
```
Starts the worker in the background. Returns immediately — worker runs async.

### Check a worker's status and results
```bash
# Status
cat {DATA_WORKERS}/<name>/state.json

# New results (unread outbox)
ls {DATA_WORKERS}/<name>/outbox/
cat {DATA_WORKERS}/<name>/outbox/<task_id>.json

# Archive after reading
mkdir -p {DATA_WORKERS}/<name>/outbox_processed
mv {DATA_WORKERS}/<name>/outbox/<task_id>.json {DATA_WORKERS}/<name>/outbox_processed/
```

### List all workers
```bash
ls {DATA_WORKERS}/
cat {WORKSPACE}/workers.json
```

### Worker's workspace (ongoing notes, status)
```bash
cat {DATA_WORKERS}/<name>/workspace/status.md
cat {DATA_WORKERS}/<name>/workspace/ongoing_work.md
```

## Hourly Check Routine

When you receive a task with `"type": "hourly_check"` (sent by cron):
1. Read `project_status.md` and `notes.md` to recall state
2. For each worker in `workers.json`:
   - Check their outbox for new completed results
   - Read, collect, and archive them
   - Note if a worker is stuck or idle too long
3. Update `project_status.md` with new information
4. Respond with a status summary (be concise — user gets this on Telegram)
   - If results worth sharing: summarize them
   - If all idle and no news: "All quiet. No new results." is fine
5. If ongoing projects need next steps: assign tasks to workers

## Telegram Communication Style
- *bold* for emphasis, `code` for names/commands
- Keep replies SHORT — user reads on mobile
- When assigning: "Sending to `worker_name` — brief description"
- When reporting results: lead with the key finding, details below
- No filler ("Certainly!", "Great question!") — be direct

## What You Are NOT
- Not doing the work yourself — you delegate to workers
- Not a general assistant — focus on project management
- Not a yes-man — push back if user's goal is vague or risky
""")


def _init_memory_files() -> None:
    defaults = {
        "project_status.md": "# Project Status\n\n(No active projects yet)\n",
        "notes.md": "# Notes\n\n(empty)\n",
        "user_preferences.md": "# User Preferences\n\n(fill as user shares info)\n",
        "workers.json": '{"workers": {}}',
    }
    for fname, content in defaults.items():
        fpath = WORKSPACE / fname
        if not fpath.exists():
            fpath.write_text(content)


def _session_file(session_id: str) -> Path:
    encoded = str(WORKSPACE).replace("/", "-").replace("_", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"


def _safe_session_id(state: dict) -> str | None:
    sid = state.get("session_id")
    if not sid:
        return None
    sf = _session_file(sid)
    if not sf.exists():
        log(f"SESSION reset: file for {sid} missing, starting fresh")
        state["session_id"] = None
        save_state(state)
        return None
    if sf.stat().st_size > SESSION_MAX_BYTES:
        log(f"SESSION rotate: file is {sf.stat().st_size // 1024}KB, starting fresh")
        try:
            sf.unlink()
        except OSError as e:
            log(f"SESSION rotate: failed to delete: {e}")
        state["session_id"] = None
        save_state(state)
        return None
    return sid


def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    print(line, end="", file=sys.stderr)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "worker_id": WORKER_ID,
        "session_id": None,
        "status": "idle",
        "current_task": None,
        "tasks_done": 0,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def run_claude(
    prompt: str, session_id: str | None, use_tools: bool = True
) -> tuple[str | None, str | None, str | None]:
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--add-dir", str(WORKSPACE),
        "--add-dir", str(DATA_WORKERS),
        "--add-dir", str(ROOT),
    ]
    if use_tools:
        cmd += ["--allowed-tools", ALLOWED_TOOLS]
    else:
        cmd += ["--tools", ""]
    if session_id:
        cmd.extend(["--resume", session_id])

    log(f"RUN claude tools={use_tools} resume={session_id or '(new)'}")
    try:
        proc = subprocess.run(
            cmd, cwd=str(WORKSPACE), capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log("TIMEOUT")
        return session_id, None, f"timeout after {CLAUDE_TIMEOUT}s"
    except FileNotFoundError:
        log(f"claude not found at {CLAUDE_BIN!r}")
        return session_id, None, f"`claude` not found at {CLAUDE_BIN}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:800]
        log(f"EXIT {proc.returncode}: {err[:200]}")
        err_lower = err.lower()
        if any(x in err_lower for x in ("429", "rate_limit", "overloaded", "too many requests", "session limit", "usage limit")):
            return session_id, None, "RATE_LIMITED"
        if "no conversation found" in err_lower:
            return session_id, None, "SESSION_LOST"
        return session_id, None, f"claude exited {proc.returncode}: {err}"

    try:
        data = json.loads(proc.stdout)
        new_sid = data.get("session_id") or session_id
        result = data.get("result", "")
        return new_sid, result, None
    except json.JSONDecodeError:
        return session_id, proc.stdout.strip(), None


def write_outbox(
    task_id: str, result: str, reply_to: int | None, error: str | None = None, source: str = "telegram"
) -> None:
    payload = {
        "task_id": task_id,
        "worker_id": WORKER_ID,
        "result": result or "",
        "error": error,
        "failed": error is not None,
        "reply_to_message_id": reply_to,
        "source": source,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    (OUTBOX_TELEGRAM / f"{task_id}.json").write_text(json.dumps(payload, indent=2))


def process_task(task_file: Path, state: dict) -> None:
    try:
        task = json.loads(task_file.read_text())
    except json.JSONDecodeError:
        log(f"bad task file {task_file.name}, discarding")
        task_file.unlink(missing_ok=True)
        return

    task_id = task.get("task_id") or str(uuid.uuid4())
    task_text = task.get("task", "").strip()
    task_type = task.get("type", "user_message")
    reply_to = task.get("reply_to_message_id")
    source = task.get("from", "telegram")

    log(f"TASK {task_id} [{task_type}]: {task_text[:80]}")
    state["status"] = "working"
    state["current_task"] = task_text[:60]
    save_state(state)

    # Prepend task metadata so Claude can write ack to outbox immediately
    # without needing to read the inbox file first
    if task_type != "hourly_check":
        prompt = (
            f"[TASK_ID={task_id} REPLY_TO={reply_to}]\n\n"
            f"Your FIRST action: write an acknowledgement to the outbox using Bash:\n"
            f"python3 -c \"\n"
            f"import json, datetime; from pathlib import Path\n"
            f"Path('{OUTBOX_TELEGRAM}/{task_id}_ack.json').write_text(json.dumps({{\n"
            f"  'task_id': '{task_id}_ack', 'worker_id': 'pm',\n"
            f"  'result': 'YOUR BRIEF REPLY HERE',\n"
            f"  'reply_to_message_id': {reply_to},\n"
            f"  'source': 'telegram',\n"
            f"  'completed_at': datetime.datetime.now().isoformat()\n"
            f"}}))\n\"\n\n"
            f"Then do the work.\n\n"
            f"User message: {task_text}"
        )
    else:
        prompt = task_text

    try:
        sid, result, error = run_claude(prompt, _safe_session_id(state), use_tools=True)
    except Exception as e:
        sid, result, error = state.get("session_id"), None, f"unexpected error: {e}"

    # Auto-recover from "No conversation found" once: clear session and retry fresh.
    if error == "SESSION_LOST":
        log(f"TASK {task_id} session lost, clearing and retrying fresh")
        state["session_id"] = None
        save_state(state)
        try:
            sid, result, error = run_claude(prompt, None, use_tools=True)
        except Exception as e:
            sid, result, error = None, None, f"unexpected error on retry: {e}"

    if error == "RATE_LIMITED":
        now = time.time()
        last_notified = state.get("last_rate_limit_notified_at", 0)
        suppress = (now - last_notified) < RATE_LIMIT_NOTIFY_INTERVAL
        log(
            f"TASK {task_id} rate limited — leaving in inbox, "
            f"{'suppressing' if suppress else 'sending'} notification"
        )
        state["status"] = "rate_limited"
        state["current_task"] = None
        if not suppress:
            state["last_rate_limit_notified_at"] = now
        save_state(state)
        if not suppress:
            write_outbox(
                task_id,
                "⚠️ Hit Claude usage limit. Your message is queued and will be processed automatically when the limit resets (usually within an hour). I'll stay quiet until then.",
                reply_to, None, source
            )
        # Back off so we don't hammer the inbox while rate-limited
        time.sleep(RATE_LIMIT_BACKOFF)
        # Leave task_file intact so it gets processed after reset
        return

    state["session_id"] = sid
    state["tasks_done"] += 1
    state["status"] = "idle"
    state["current_task"] = None
    # Successful (or hard-failed) run clears the rate-limit debounce window
    state.pop("last_rate_limit_notified_at", None)
    save_state(state)

    write_outbox(task_id, result or "", reply_to, error, source)
    task_file.unlink(missing_ok=True)
    log(f"TASK {task_id} done (error={error is not None})")


def main() -> None:
    _pid_fd = open(PID_FILE, "w")
    try:
        fcntl.flock(_pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("ERROR: another worker instance is already running. Exiting.", file=sys.stderr)
        sys.exit(1)
    _pid_fd.write(str(os.getpid()))
    _pid_fd.flush()

    _write_workspace_claude_md()
    _init_memory_files()

    log(f"{WORKER_ID} starting (workspace={WORKSPACE})")
    state = load_state()
    state["status"] = "idle"
    state["current_task"] = None
    save_state(state)

    while True:
        try:
            inbox_files = sorted(INBOX.glob("*.json"))
            if inbox_files:
                process_task(inbox_files[0], state)
                continue
            time.sleep(2)

        except KeyboardInterrupt:
            log(f"{WORKER_ID} stopping (interrupt)")
            state["status"] = "stopped"
            save_state(state)
            return
        except Exception as e:
            log(f"UNEXPECTED: {e!r}")
            time.sleep(5)


if __name__ == "__main__":
    main()
