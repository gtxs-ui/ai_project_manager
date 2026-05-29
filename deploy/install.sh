#!/bin/bash
# Full VPS setup for the Project Manager agent.
#
# Usage (run as root or with sudo):
#   bash deploy/install.sh [/opt/project_manager]
#
# What it does:
#   1. Installs system deps (Python, Node.js, Claude CLI)
#   2. Creates venv and installs Python deps
#   3. Installs systemd services for worker + bridge
#   4. Registers hourly PM cron wake-up (0 * * * *)
#   5. Verifies .env is configured

set -e

INSTALL_DIR="${1:-/opt/project_manager}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${2:-$(whoami)}"

echo "=== Project Manager Install ==="
echo "Source: $SCRIPT_DIR"
echo "Install dir: $INSTALL_DIR"
echo "Service user: $SERVICE_USER"
echo ""

# ── 1. System dependencies ─────────────────────────────────────────────────────

if command -v apt-get &>/dev/null; then
    echo "[1/6] Installing system deps..."
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip curl

    if ! command -v node &>/dev/null; then
        echo "Installing Node.js..."
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
        apt-get install -y -qq nodejs
    fi
else
    echo "[1/6] Skipping apt-get (not Debian/Ubuntu). Ensure Python3 and Node.js are installed."
fi

# ── 2. Claude CLI ─────────────────────────────────────────────────────────────

if ! command -v claude &>/dev/null && [ ! -f "/root/.local/bin/claude" ] && [ ! -f "$HOME/.npm-global/bin/claude" ]; then
    echo "[2/6] Installing Claude CLI..."
    npm install -g @anthropic-ai/claude-code
else
    echo "[2/6] Claude CLI already installed."
fi

# ── 3. Copy files ─────────────────────────────────────────────────────────────

echo "[3/6] Copying files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
rsync -a --exclude 'data/' --exclude 'venv/' --exclude '__pycache__/' --exclude '.env' \
    "$SCRIPT_DIR/" "$INSTALL_DIR/"

chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true

# ── 4. Python venv ────────────────────────────────────────────────────────────

echo "[4/6] Setting up Python venv..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# ── 5. Configure .env ─────────────────────────────────────────────────────────

if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "⚠️  Configure your .env file before starting:"
    echo "  nano $INSTALL_DIR/.env"
    echo ""
fi

# ── 6. Systemd services ───────────────────────────────────────────────────────

echo "[5/6] Installing systemd services..."

for svc in worker bridge; do
    SVC_FILE="/etc/systemd/system/project-manager-${svc}.service"
    sed \
        -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
        "$INSTALL_DIR/deploy/project-manager-${svc}.service.template" \
        > "$SVC_FILE"
    echo "  Installed: $SVC_FILE"
done

systemctl daemon-reload
systemctl enable project-manager-worker project-manager-bridge

# ── 7. Hourly cron wake-up ────────────────────────────────────────────────────

echo "[6/6] Registering hourly PM cron wake-up..."
CRON_CMD="0 * * * * $INSTALL_DIR/cron_pm_wakeup.sh >> $INSTALL_DIR/data/pm/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "cron_pm_wakeup.sh"; echo "$CRON_CMD" ) | crontab -
echo "  Cron: $CRON_CMD"

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "=== Install complete ==="
echo ""
echo "STEP 1 — Fill in .env"
echo "  nano $INSTALL_DIR/.env"
echo "  Required: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID"
echo "  Optional: ANTHROPIC_API_KEY (only for Option B below)"
echo ""
echo "STEP 2 — Authenticate Claude. Pick ONE option:"
echo ""
echo "  Option A — OAuth / Claude subscription (interactive, free if subbed):"
echo "    1. Leave ANTHROPIC_API_KEY blank in .env"
echo "    2. Run as the SAME user the systemd unit runs as:"
echo "         claude"
echo "    3. Complete browser OAuth — credentials are saved to ~/.claude/"
echo "    4. Done. Skip Option B."
echo ""
echo "    (Alternative to step 2-3: rsync existing creds from another machine:"
echo "       rsync -avz ~/.claude/ root@vps:~/.claude/)"
echo ""
echo "  Option B — API key (headless / unattended, billed per token):"
echo "    1. Get a key at https://console.anthropic.com/settings/keys"
echo "    2. Edit .env and set:  ANTHROPIC_API_KEY=sk-ant-..."
echo "    3. systemd already loads .env via EnvironmentFile, so the key is in"
echo "       scope automatically. No 'claude' OAuth dance required."
echo ""
echo "STEP 3 — Start services (NOT spawn.sh — that's only for non-systemd dev):"
echo "  systemctl start project-manager-worker project-manager-bridge"
echo ""
echo "STEP 4 — Verify:"
echo "  systemctl status project-manager-worker project-manager-bridge"
echo "  journalctl -fu project-manager-worker"
echo "  Send a test message to your Telegram bot — PM should reply."
echo ""
echo "──────────────────────────────────────────────────────────────"
echo "Useful runtime commands:"
echo "  Create a worker:  bash $INSTALL_DIR/create_worker.sh researcher 'Web research'"
echo "  Send a task:      bash $INSTALL_DIR/send_task.sh researcher 'Research X'"
echo "  Invoke now:       bash $INSTALL_DIR/invoke_worker.sh researcher"
echo "  List workers:     ls $INSTALL_DIR/data/workers/"
echo ""
echo "Note: spawn.sh is for dev / non-systemd start (no root, laptop, etc.)"
echo "After install.sh on a VPS, you use systemctl — not spawn.sh."
echo ""
