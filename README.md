# Project Manager Agent

An autonomous Project Manager AI that runs as a persistent daemon on a VPS, takes goals from you via Telegram, breaks them into sub-tasks, creates specialist worker agents on demand, delegates, monitors, and reports back — all autonomously.

```
You (Telegram)
    │
    ▼
Project Manager (persistent, 24/7)
    │
    ├── Worker: researcher       (hourly cron + on-demand invoke)
    ├── Worker: code_writer      (hourly cron + on-demand invoke)
    └── Worker: <anything PM creates>
```

**Workers are forked Claude Code sessions** with their own workspace, inbox, outbox, and durable session memory. The PM creates new ones whenever a goal needs a fresh specialty.

For the full architecture + every flag + every troubleshooting tip, see **[SETUP.md](SETUP.md)**. This README is the front-door: requirements, optional pre-install hardening, install, and first run.

---

## Requirements

**VPS / host:**
- Debian or Ubuntu (the install script uses `apt`)
- Public IPv4 (for Telegram polling — no inbound ports needed)
- ≥ 2 GB RAM, ≥ 10 GB disk (Claude CLI + venv + data growth)
- Root or `sudo` access

**Software (install.sh installs all of these):**
- Python 3.10+ with `venv`
- Node.js 20 (for Claude CLI)
- `@anthropic-ai/claude-code` (the Claude CLI)
- `requests` (the only Python dep)

**Accounts you need before install:**
1. **Telegram bot** — create via [@BotFather](https://t.me/BotFather), get the token
2. **Telegram chat ID** — message your bot ONCE first, then *immediately* visit `https://api.telegram.org/bot<TOKEN>/getUpdates` (the URL only returns recent messages — if you wait too long it'll return an empty list). Look for `"chat":{"id": 123456789}` in the JSON.
3. **Claude auth — pick ONE:**
   - **Option A (OAuth / subscription):** A working Anthropic / Claude account — you'll run `claude` once interactively to sign in
   - **Option B (API key):** A key from [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) — pay per token, no browser dance

---

## Security hardening (optional but recommended, BEFORE install)

The agent runs Claude Code with `--allowed-tools WebSearch,WebFetch,Write,Read,Edit,Bash` and `--permission-mode acceptEdits`. In addition, the Claude Code harness layer provides built-in capabilities — sub-agent spawning (`Agent`), scheduled wake-ups, tool discovery, and skill invocation — that aren't governed by `--allowed-tools` and can't be disabled at the operator level. Net effect: **treat the daemon as a CI runner with shell + filesystem + outbound-web access that can also fork additional Claude sessions.** The defaults below are the bare-minimum hardening for a VPS that will be left running unattended.

Skip this section if you're testing on a throwaway VPS. Don't skip it for anything you care about.

### 1. Run as a non-root user

```bash
# Create a regular user (not --system — you'll need an interactive shell for
# Option A's `claude` OAuth dance, which --system disables by default)
sudo adduser --disabled-password --gecos "" --shell /bin/bash pmagent
sudo usermod -aG sudo pmagent          # optional — only if PM needs to install packages
```

Install the agent under that user's home and pass it to install.sh:

```bash
sudo bash deploy/install.sh /home/pmagent/project_manager pmagent
```

The systemd units, the cron entries, and the Claude credentials in `~/.claude/` all bind to that user — root never executes the agent code at runtime.

### 2. Limit `sudo` to the specific commands the agent needs

If you gave `pmagent` sudo above and want to narrow it down, drop a sudoers file:

```bash
sudo visudo -f /etc/sudoers.d/pmagent
```

Paste (adjust to your needs):

```
# Allow only the package operations the agent legitimately needs.
# Sudoers denies by default — listing ONLY the commands below is the
# security boundary. Do NOT add an `ALL` rule above this.
pmagent ALL=(root) NOPASSWD: /usr/bin/apt-get update
pmagent ALL=(root) NOPASSWD: /usr/bin/apt-get install -y *
pmagent ALL=(root) NOPASSWD: /bin/systemctl restart project-manager-worker
pmagent ALL=(root) NOPASSWD: /bin/systemctl restart project-manager-bridge
```

`visudo` will reject the file if the syntax is wrong, which prevents you locking yourself out.

### 3. SSH hardening

```bash
sudo nano /etc/ssh/sshd_config
```

Set / confirm:

```
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AllowUsers pmagent your_admin_user
```

Then `sudo systemctl restart ssh`. Always test in a second terminal before closing the first.

### 4. Firewall

```bash
sudo apt install ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
```

The agent only needs **outbound** HTTPS to `api.telegram.org`, `api.anthropic.com`, `console.anthropic.com` (for Option A's OAuth login), and whatever endpoints your workers reach. Don't open inbound ports unless a specific worker exposes a service.

### 5. fail2ban (optional)

```bash
sudo apt install fail2ban
sudo systemctl enable --now fail2ban
```

Defaults are fine — picks up failed `sshd` auth attempts and bans for 10 min.

### 6. Lock down `.env`

```bash
# Use the absolute path you passed to install.sh (e.g. /opt/project_manager)
sudo chmod 600 /opt/project_manager/.env
sudo chown pmagent:pmagent /opt/project_manager/.env
```

### 7. (Optional) Restrict the worker workspace

By default workers can read/write anywhere their tools allow. If you want a hard boundary, run the worker user inside a `systemd-nspawn` container or set `ProtectHome=true` + an explicit `ReadWritePaths=` allowlist in the worker service unit.

---

## Install

```bash
# 1. Get the code onto your VPS — pick whichever fits how you got it:
#    From a private GitHub repo you own:
#      git clone git@github.com:youruser/project_manager.git /tmp/project_manager
#    From a local checkout pushed up to the VPS:
#      rsync -avz --exclude data/ --exclude venv/ --exclude .env \
#        ./project_manager/ pmagent@vps:/tmp/project_manager/

cd /tmp/project_manager

# 2. Inspect what install.sh will do (always — it runs as root)
less deploy/install.sh

# 3. Run as the user you want the agent to run as (or root)
sudo bash deploy/install.sh /opt/project_manager pmagent
#                            ^ install dir          ^ run-as user
```

`install.sh` does:
1. Installs Python, Node.js, Claude CLI
2. `rsync`-copies the source into the install dir (excludes `data/`, `venv/`, `.env`)
3. Creates a Python venv and installs `requirements.txt`
4. Generates `.env` from `.env.example` if it doesn't exist
5. Writes both systemd units to `/etc/systemd/system/`
6. Registers an hourly cron at `0 * * * *`
7. Prints a 4-step "what to do next" with the Option A vs Option B Claude-auth paths

---

## First run (post-install)

Follow the 4 steps printed by `install.sh`:

1. **Fill in `.env`** — TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and (if Option B) ANTHROPIC_API_KEY
2. **Authenticate Claude** — Option A: `claude` interactively. Option B: nothing more (systemd loads `ANTHROPIC_API_KEY` from `.env` automatically)
3. **Start services** — `systemctl start project-manager-worker project-manager-bridge`
4. **Verify** — `systemctl status …`, `journalctl -fu project-manager-worker`, smoke-test Telegram with `curl https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe`, then send your bot a real message

**Logs** live at:
- `journalctl -fu project-manager-worker` / `…-bridge` (systemd)
- `data/pm/log.txt`, `data/pm/bridge_log.txt` (PM)
- `data/workers/researcher/worker.log` (per-worker — one path per worker name)

**Uninstall:**
```bash
sudo systemctl stop project-manager-worker project-manager-bridge
sudo systemctl disable project-manager-worker project-manager-bridge
sudo rm /etc/systemd/system/project-manager-{worker,bridge}.service
sudo systemctl daemon-reload
crontab -l | grep -v cron_pm_wakeup.sh | crontab -    # drop the hourly entry
sudo rm -rf /opt/project_manager                       # adjust to your install dir
```

> **You do NOT run `spawn.sh` after `install.sh`.**  
> `spawn.sh` is the non-systemd shortcut for dev / laptop runs. Once `install.sh` has set up systemd, `systemctl` is the right control surface.

---

## Daily usage

Send the bot any text — it'll be treated as a goal. The PM acknowledges immediately, then either dispatches workers or asks clarifying questions.

```
You: Build a 5-page brochure site for my dad's dental clinic with a contact form.
PM:  Got it. Creating `web_dev` worker. Drafting wireframes + content in parallel.
     I'll update you each hour. Site goes to /var/www when ready.
```

Hourly summary at the top of each hour. PM and worker logs live under `data/pm/` and `data/workers/` (one subdirectory per worker — e.g. `data/workers/researcher/`). See [SETUP.md](SETUP.md) for the full Telegram command list and runtime ops.

---

## What's in the repo

| File / dir | Purpose |
|---|---|
| `pm_bridge.py` | Telegram poller + outbox dispatcher (2 threads) |
| `pm_worker.py` | PM persistent daemon — calls `claude` per task |
| `worker_agent.py` | Generic worker — invoked by cron or PM, processes inbox, exits |
| `create_worker.sh` | Scaffolds a new worker (name + skills → workspace + cron entry) |
| `send_task.sh` | Drops a JSON task into a worker's inbox |
| `invoke_worker.sh` | Runs a worker immediately (background) |
| `cron_pm_wakeup.sh` | Sends `hourly_check` to PM's inbox |
| `spawn.sh` / `stop.sh` | Non-systemd lifecycle (dev only) |
| `deploy/install.sh` | Full VPS install |
| `deploy/*.service` | Systemd unit files |
| `SETUP.md` | Full deployment + architecture guide |
| `.env.example` | Template — TELEGRAM + optional ANTHROPIC_API_KEY |

Not in the repo (gitignored): `.env` (secrets), `venv/`, `data/` (runtime), `*.log`, `*.pid`.

---

## License

Apache-2.0 — see [LICENSE](LICENSE).
