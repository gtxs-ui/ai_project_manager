# Project Manager Agent — VPS Setup Guide

> Give this file to Claude Code on your VPS. It can deploy the entire system.
> All source files are already in this directory.

---

## What This Is

An autonomous **Project Manager AI** you talk to via Telegram. You give it goals; it breaks them down, creates specialist **worker agents**, delegates tasks, monitors progress, and reports back — all autonomously.

```
You (Telegram)
    │
    ▼
Project Manager (persistent daemon)
    │ creates / assigns tasks
    ├── Worker: researcher       (invoked hourly by cron)
    ├── Worker: code_writer      (invoked hourly by cron)
    └── Worker: [any you create]
```

**Key behaviors:**
- PM runs 24/7, responds to Telegram messages immediately
- Cron wakes PM every hour at :00 → PM collects worker results, sends Telegram summary
- Cron wakes each worker every hour at :55 → worker checks inbox, does work, exits
- PM can also invoke workers immediately (no wait for cron)
- Workers are created on-demand by PM or by you directly

---

## Architecture

```
Telegram ←→ pm_bridge.py (2 threads: poller + outbox watcher)
                │
         data/pm/inbox/  ← tasks (from Telegram or cron)
                │
         pm_worker.py    ← persistent daemon, calls `claude` subprocess
                │
         data/pm/workspace/CLAUDE.md  ← PM's identity + instructions
                │
    ┌─────────────────────────────────────────────┐
    │  worker management (via Bash tool)           │
    │                                              │
    │  data/workers/<name>/inbox/   ← tasks        │
    │  data/workers/<name>/outbox/  ← results      │
    │  data/workers/<name>/workspace/SKILLS.md     │
    └─────────────────────────────────────────────┘
                │
         worker_agent.py  ← invoked by cron (not a daemon)
                           one invocation = process inbox + exit
```

**IPC is plain JSON files on disk — no message queues, no APIs between components.**

---

## Prerequisites

On the VPS (Debian/Ubuntu):
```bash
# Python 3.10+
sudo apt install python3 python3-venv python3-pip curl

# Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt install -y nodejs

# Claude CLI
npm install -g @anthropic-ai/claude-code
```

---

## Quick Deploy

```bash
# 1. Copy files to VPS (from local machine)
rsync -avz --exclude 'data/' --exclude 'venv/' --exclude '.env' \
  ./project_manager/ root@YOUR_VPS:/opt/project_manager/

# 2. On the VPS
cd /opt/project_manager
chmod +x *.sh deploy/install.sh

# 3. Authenticate Claude (one-time — opens browser)
claude
# OR copy credentials from local machine:
# rsync -avz ~/.claude/ root@VPS:~/.claude/

# 4. Configure Telegram bot
cp .env.example .env
nano .env
# Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

# 5. Run install (installs deps + systemd + cron)
bash deploy/install.sh /opt/project_manager

# 6. Start
systemctl start project-manager-worker project-manager-bridge

# 7. Check
systemctl status project-manager-worker project-manager-bridge
journalctl -fu project-manager-worker
```

---

## Getting Telegram Credentials

### Bot token
1. Message @BotFather on Telegram
2. `/newbot` → choose a name → get the token
3. Use a **separate bot from other agents** (each agent needs its own bot)

### Chat ID (your user ID)
1. Send any message to your new bot
2. Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id": 123456789}` — that's your TELEGRAM_CHAT_ID

---

## File Structure

```
/opt/project_manager/
├── pm_bridge.py          # Telegram bridge (2 threads)
├── pm_worker.py          # PM persistent worker daemon
├── worker_agent.py       # Generic worker (invoked by cron or PM)
├── create_worker.sh      # Create a new worker
├── invoke_worker.sh      # Invoke a worker immediately
├── send_task.sh          # Drop a task in a worker's inbox
├── cron_pm_wakeup.sh     # Cron: sends hourly_check to PM inbox
├── spawn.sh              # Start without systemd (manual mode)
├── stop.sh               # Stop manual mode
├── requirements.txt      # requests
├── .env                  # Secrets (BOT_TOKEN, CHAT_ID)
├── deploy/
│   ├── install.sh
│   ├── project-manager-worker.service
│   └── project-manager-bridge.service
└── data/                 # Runtime (auto-created)
    ├── pm/
    │   ├── inbox/                    # Tasks from Telegram + cron
    │   ├── outbox_telegram/          # Results going to Telegram
    │   ├── outbox_sent/              # Archive
    │   ├── workspace/
    │   │   ├── CLAUDE.md             # PM system prompt (auto-written at startup)
    │   │   ├── workers.json          # Worker registry
    │   │   ├── project_status.md     # Active projects
    │   │   └── notes.md              # PM memory
    │   └── state.json                # PM status
    └── workers/
        └── <worker_name>/
            ├── inbox/                # Tasks for this worker
            ├── inbox_processed/      # Processed tasks archive
            ├── outbox/               # Worker results
            ├── outbox_processed/     # Collected results archive
            ├── workspace/
            │   ├── SKILLS.md         # Worker's specialization
            │   ├── status.md         # Worker self-reported status
            │   └── ongoing_work.md   # Active projects
            └── state.json            # Worker status + session_id
```

---

## Cron Schedule

After install, crontab contains:
```
# PM hourly wake-up (at :00 every hour)
0 * * * * /opt/project_manager/cron_pm_wakeup.sh >> /opt/project_manager/data/pm/cron.log 2>&1

# Worker hourly runs (at :55 every hour — runs BEFORE PM collects at :00)
55 * * * * /opt/project_manager/invoke_worker.sh researcher >> ...
55 * * * * /opt/project_manager/invoke_worker.sh code_writer >> ...
```

Workers run at :55, PM collects at the next :00 (5 min gap). Each worker is lock-protected — no concurrent invocations.

---

## Telegram Commands

| Command | Action |
|---------|--------|
| `/status` | Show PM status (working/idle, tasks done) |
| `/workers` | List all workers with their status |
| Any text | Give the PM a task or goal |

---

## Usage Examples

### Give the PM a project
```
You: I want to build a competitive analysis for launching a SaaS invoicing tool.
     I need: market research, competitor pricing, and a feature checklist.

PM: Got it. Creating 2 workers:
    - `researcher` — market research + competitor pricing
    - `analyst` — feature checklist synthesis

    Assigning tasks now. I'll report back at the next hour check.
    [1 hour later] ✓ Researcher finished competitive analysis (5 competitors, pricing table).
    Analyst has a 12-point feature checklist ready. Want me to send the full report?
```

### Ask for immediate status
```
You: What are the workers doing?
PM: researcher — idle (last task: competitor pricing, 45 min ago)
    analyst — running (processing feature synthesis)
    2 unread results in researcher outbox.
```

### Create a worker manually (bypass PM)
```bash
bash /opt/project_manager/create_worker.sh "data_analyst" \
  "Python data analysis, pandas, data visualization, statistical summaries"
```

### Send a task directly to a worker
```bash
bash /opt/project_manager/send_task.sh "researcher" \
  "Find the top 10 project management SaaS tools with their pricing"
bash /opt/project_manager/invoke_worker.sh "researcher"
```

---

## How Workers Work (detail)

When a worker is invoked (by cron or `invoke_worker.sh`):
1. Lock file created — prevents concurrent runs of the same worker
2. `worker_agent.py` reads the worker's `SKILLS.md` + `ongoing_work.md`
3. Builds a prompt with all file paths, inbox contents, and instructions
4. Calls `claude -p <prompt> --allowed-tools All --permission-mode acceptEdits`
5. Claude reads inbox tasks, does work with full tool access, writes results to outbox
6. Claude updates `workspace/status.md`
7. Lock released, worker exits

Workers have access to: Bash, WebSearch, WebFetch, Read, Write, Edit.
They can do research, write code, create files, make API calls — anything in their skills.

---

## PM Session Memory

The PM uses `claude --resume <session_id>` for persistent conversation memory across Telegram sessions. Session is rotated (fresh start) when it exceeds 2 MB. The PM's `project_status.md` and `notes.md` files provide memory across rotations — the CLAUDE.md instructs the PM to read these at the start of every task.

Workers also save `session_id` in their `state.json` and resume on the next cron invocation. This gives workers continuity across hourly runs.

---

## Managing the System

```bash
# Systemd
systemctl start/stop/restart project-manager-worker project-manager-bridge
journalctl -fu project-manager-worker
journalctl -fu project-manager-bridge

# Logs
tail -f /opt/project_manager/data/pm/log.txt
tail -f /opt/project_manager/data/pm/bridge_log.txt
tail -f /opt/project_manager/data/workers/researcher/worker.log

# Workers
ls /opt/project_manager/data/workers/          # list all workers
cat /opt/project_manager/data/workers/*/state.json  # all worker states
ls /opt/project_manager/data/workers/researcher/outbox/  # unread results

# Manual worker run
bash /opt/project_manager/invoke_worker.sh researcher

# Create new worker
bash /opt/project_manager/create_worker.sh "writer" "Technical writing, blog posts, documentation"

# Trigger PM hourly check now
bash /opt/project_manager/cron_pm_wakeup.sh
```

---

## Security Notes

- The `.env` file contains your Telegram bot token — keep it private
- Claude CLI credentials live in `~/.claude/` — also keep private
- Workers run with full tool access including Bash — they can execute shell commands
- Recommend running on a dedicated VPS you control
- The PM's `acceptEdits` permission mode allows Claude to read/write any file in the project directory

---

## Troubleshooting

**Bridge not connecting to Telegram:**
- Check `data/pm/bridge_log.txt`
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`
- Test: `curl https://api.telegram.org/bot<TOKEN>/getMe`

**Worker not processing tasks:**
- Check `data/workers/<name>/worker.log`
- Check `data/workers/<name>/lock` (delete if stale)
- Run manually: `bash invoke_worker.sh <name>`

**PM not responding:**
- Check `data/pm/log.txt`
- Check `systemctl status project-manager-worker`
- Verify `claude` CLI is accessible: `which claude` or `/root/.local/bin/claude --version`

**Claude not authenticated:**
- Run `claude` interactively to complete OAuth
- Or copy credentials: `rsync -avz ~/.claude/ root@vps:~/.claude/`

**Rate limits (429 errors):**
- Visible in logs as `claude exited 1: ...rate_limit...`
- Reduce cron frequency or spread worker invocations
- Worker crons already staggered from PM check by 5 min
