"""Generic worker agent — invoked by cron or by the Project Manager.

Usage:
    python3 worker_agent.py <worker_name>

One invocation = one "session":
- Reads inbox tasks (if any) and processes them
- If no tasks, does proactive check on ongoing_work.md
- Writes results to outbox/
- Updates state.json
- Exits when done
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path


if len(sys.argv) < 2:
    print("Usage: python3 worker_agent.py <worker_name>", file=sys.stderr)
    sys.exit(1)

WORKER_NAME = sys.argv[1]
ROOT = Path(__file__).parent
WORKER_DIR = ROOT / "data" / "workers" / WORKER_NAME
PM_INBOX = ROOT / "data" / "pm" / "inbox"

INBOX = WORKER_DIR / "inbox"
INBOX_PROCESSED = WORKER_DIR / "inbox_processed"
OUTBOX = WORKER_DIR / "outbox"
OUTBOX_PROCESSED = WORKER_DIR / "outbox_processed"
WORKSPACE = WORKER_DIR / "workspace"
STATE_FILE = WORKER_DIR / "state.json"
LOG_FILE = WORKER_DIR / "worker.log"
LOCK_FILE = WORKER_DIR / "lock"
SKILLS_FILE = WORKSPACE / "SKILLS.md"

for d in (INBOX, INBOX_PROCESSED, OUTBOX, OUTBOX_PROCESSED, WORKSPACE):
    d.mkdir(parents=True, exist_ok=True)

CLAUDE_TIMEOUT = 1800
ALLOWED_TOOLS = "WebSearch,WebFetch,Write,Read,Edit,Bash"
SESSION_MAX_BYTES = 2 * 1024 * 1024

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


def log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}][{WORKER_NAME}] {msg}\n"
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
        "worker_name": WORKER_NAME,
        "session_id": None,
        "status": "idle",
        "tasks_done": 0,
        "last_run": None,
    }


def save_state(state: dict) -> None:
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


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
        log(f"SESSION rotate: {sf.stat().st_size // 1024}KB > 2MB, starting fresh")
        try:
            sf.unlink()
        except OSError:
            pass
        state["session_id"] = None
        save_state(state)
        return None
    return sid


def build_prompt(inbox_files: list[Path]) -> str:
    skills = SKILLS_FILE.read_text() if SKILLS_FILE.exists() else "General specialist worker"

    ongoing_file = WORKSPACE / "ongoing_work.md"
    ongoing = ongoing_file.read_text() if ongoing_file.exists() else "(none)"

    now = datetime.now().isoformat(timespec="seconds")

    if inbox_files:
        task_previews = []
        for f in inbox_files[:10]:
            try:
                data = json.loads(f.read_text())
                task_previews.append(
                    f"- task_id: {data.get('task_id', f.stem)}\n"
                    f"  task: {data.get('task', '')[:200]}"
                )
            except Exception:
                task_previews.append(f"- {f.name} (unreadable)")
        tasks_section = (
            f"You have {len(inbox_files)} task(s) in your inbox:\n" + "\n".join(task_previews)
        )
        instruction = (
            "Process ALL inbox tasks. For each task:\n"
            "1. Read the full task file\n"
            "2. Execute it using your skills\n"
            f"3. Write result JSON to {OUTBOX}/<task_id>.json:\n"
            '   {"task_id": "<id>", "worker_name": "' + WORKER_NAME + '", '
            '"result": "<your result>", "completed_at": "<ISO timestamp>"}\n'
            f"4. Archive: `mv {INBOX}/<task_id>.json {INBOX_PROCESSED}/<task_id>.json`\n"
        )
    else:
        tasks_section = "No tasks in inbox."
        instruction = (
            "No tasks — check ongoing_work.md for any active work to continue.\n"
            f"Update {WORKSPACE}/status.md to show you ran and are idle.\n"
            "If you have meaningful proactive work to do based on your skills, do it.\n"
        )

    return f"""You are Worker `{WORKER_NAME}`, a specialist agent managed by a Project Manager AI.
Your user communicates with the PM via Telegram; you work in the background.

## Your Skills
{skills}

## Your File Paths
- Inbox (tasks for you): `{INBOX}/`
- Inbox processed (archive): `{INBOX_PROCESSED}/`
- Outbox (your results): `{OUTBOX}/`
- Workspace (your files): `{WORKSPACE}/`
- PM's inbox (for urgent messages to PM): `{PM_INBOX}/`

## Current Time
{now}

## Inbox Status
{tasks_section}

## Ongoing Work
{ongoing}

## Instructions
{instruction}

## To send an urgent message to the PM
Write a JSON file to PM inbox:
```
python3 -c "
import json, uuid, datetime
from pathlib import Path
msg = {{
  'task_id': uuid.uuid4().hex[:12],
  'task': '[WORKER {WORKER_NAME}] <your message>',
  'from': 'worker_{WORKER_NAME}',
  'submitted_at': datetime.datetime.now().isoformat()
}}
Path('{PM_INBOX}/' + msg['task_id'] + '.json').write_text(json.dumps(msg, indent=2))
"
```
Only do this for blocking issues that need PM decision.

## After All Work — MANDATORY
Append a dated entry to `{WORKSPACE}/status.md` covering:
- What you did this session (commands, files, decisions, gates run)
- Anything pending / in progress / blocked
- Any issues encountered or flags for PM

CRITICAL: write these notes as if for a stranger. Your next invocation may be a *fresh session with no memory of this conversation* — the PM resets worker sessions after QA-passed work to manage rate-limit pressure. The next-you will rely entirely on:
- `status.md` (your durable journal of past sessions)
- `SKILLS.md` (your role + scope)
- `ongoing_work.md` (anything you'd want to remember mid-flow)
- `/home/agent/project_manager/data/pm/workspace/klearn_project.md` (if you're a Klearn worker)
- The codebase + git history

Do NOT rely on conversation context to carry information forward. Anything important goes in `status.md` *before* you exit.
"""


def run_claude(prompt: str, session_id: str | None) -> tuple[str | None, str | None, str | None]:
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--allowed-tools", ALLOWED_TOOLS,
        "--add-dir", str(WORKER_DIR),
        "--add-dir", str(ROOT / "data" / "pm" / "inbox"),
    ]
    if session_id:
        cmd.extend(["--resume", session_id])

    log(f"RUN claude resume={session_id or '(new)'}")
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
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        log(f"EXIT {proc.returncode}: {err[:200]}")
        err_lower = err.lower()
        if any(x in err_lower for x in ("429", "rate_limit", "overloaded", "too many requests", "session limit", "usage limit")):
            return session_id, None, "RATE_LIMITED"
        if "no conversation found" in err_lower:
            return session_id, None, "SESSION_LOST"
        return session_id, None, f"claude exited {proc.returncode}: {err}"

    try:
        data = json.loads(proc.stdout)
        return data.get("session_id") or session_id, data.get("result", ""), None
    except json.JSONDecodeError:
        return session_id, proc.stdout.strip(), None


def main() -> None:
    if not WORKER_DIR.exists():
        print(f"ERROR: worker '{WORKER_NAME}' not found at {WORKER_DIR}", file=sys.stderr)
        sys.exit(1)

    # Acquire lock — prevent concurrent invocations of the same worker
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log("Already running (lock held). Skipping this invocation.")
        sys.exit(0)

    lock_fd.write(str(os.getpid()))
    lock_fd.flush()

    log(f"Worker starting")
    state = load_state()
    state["status"] = "running"
    save_state(state)

    inbox_files = sorted(INBOX.glob("*.json"))
    log(f"Inbox: {len(inbox_files)} task(s)")

    prompt = build_prompt(inbox_files)

    try:
        sid, result, error = run_claude(prompt, _safe_session_id(state))
    except Exception as e:
        sid, result, error = state.get("session_id"), None, str(e)

    # Auto-recover from "No conversation found": clear session and retry once.
    if error == "SESSION_LOST":
        log("Session lost, clearing and retrying fresh")
        state["session_id"] = None
        save_state(state)
        try:
            sid, result, error = run_claude(prompt, None)
        except Exception as e:
            sid, result, error = None, None, str(e)

    if error == "RATE_LIMITED":
        # Rate-limited: leave inbox intact, log to PM's rate-limit ledger.
        # We do NOT write to data/pm/inbox/ because every PM-inbox task triggers
        # a user-facing Telegram reply round-trip — at 7+ workers × N cron cycles
        # that becomes spam. PM reads the ledger during hourly checks instead.
        log("Rate limited — leaving inbox intact, appending to PM rate-limit ledger")
        state["status"] = "rate_limited"
        state["rate_limited_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            ledger = ROOT / "data" / "pm" / "workspace" / "rate_limit_log.txt"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.open("a").write(
                f"{datetime.now().isoformat(timespec='seconds')} {WORKER_NAME} rate_limited\n"
            )
        except Exception as e:
            log(f"Could not append to rate-limit ledger: {e}")
        save_state(state)
    elif error:
        log(f"ERROR: {error}")
    else:
        log(f"Done. Result length: {len(result or '')} chars")

    if error != "RATE_LIMITED":
        state["session_id"] = sid
        state["status"] = "idle"
        state["tasks_done"] = state.get("tasks_done", 0) + len(inbox_files)
        # Successful run clears the rate-limit notification debounce
        state.pop("rate_limit_notified", None)
        save_state(state)

    lock_fd.close()
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    log("Worker exiting")


if __name__ == "__main__":
    main()
