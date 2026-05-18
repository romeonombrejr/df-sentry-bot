#!/usr/bin/env bash
# DF SentryBot — One-time VPS setup script
# Run once after cloning: bash setup.sh
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
G='\033[0;32m'
Y='\033[1;33m'
R='\033[0;31m'
B='\033[1m'
X='\033[0m'

SEP="════════════════════════════════════════════════════════════════"

info()    { echo -e "${G}  ✔  $*${X}"; }
warn()    { echo -e "${Y}  ⚠  $*${X}"; }
error()   { echo -e "${R}  ✖  $*${X}"; exit 1; }
section() { echo -e "\n${B}${SEP}${X}\n${B}  $*${X}\n${B}${SEP}${X}"; }

# ── Resolve project directory ─────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

section "DF SentryBot — VPS Setup"
echo "  Project directory : $PROJECT_DIR"
echo "  Script            : sentry_bot_direct.py (no search API required)"

# ── 1. System packages ────────────────────────────────────────────────────────
section "Step 1/6 — System packages"

if ! command -v python3 &>/dev/null; then
    warn "python3 not found — installing via apt"
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-pip python3-venv
else
    info "Python: $(python3 --version)"
fi

# ── 2. Virtual environment ────────────────────────────────────────────────────
section "Step 2/6 — Virtual environment"

if [ -d "$PROJECT_DIR/.venv" ]; then
    info ".venv already exists — skipping creation"
else
    python3 -m venv "$PROJECT_DIR/.venv"
    info "Created .venv"
fi

PYTHON="$PROJECT_DIR/.venv/bin/python"
PIP="$PROJECT_DIR/.venv/bin/pip"

"$PIP" install --upgrade pip -q
info "pip up to date"

# ── 3. Python dependencies ────────────────────────────────────────────────────
section "Step 3/6 — Python dependencies"

"$PIP" install -r "$PROJECT_DIR/requirements.txt" -q
info "requirements.txt installed"

# ── 4. Playwright + Chromium ─────────────────────────────────────────────────
section "Step 4/6 — Playwright Chromium"

"$PROJECT_DIR/.venv/bin/playwright" install chromium
info "Chromium browser installed"

echo ""
echo "  Installing Chromium system libraries (requires sudo)…"
sudo "$PROJECT_DIR/.venv/bin/playwright" install-deps chromium
info "System libraries installed"

# ── 5. Configuration ──────────────────────────────────────────────────────────
section "Step 5/6 — Configuration"

# .env
if [ -f "$PROJECT_DIR/.env" ]; then
    info ".env already exists — skipping"
else
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    info "Created .env from .env.example"

    echo ""
    echo "  Enter your Microsoft Teams Incoming Webhook URL."
    echo "  (Leave blank to skip — you can add it to .env later)"
    echo ""
    read -rp "  TEAMS_WEBHOOK_URL: " TEAMS_URL
    if [ -n "$TEAMS_URL" ]; then
        sed -i "s|TEAMS_WEBHOOK_URL=|TEAMS_WEBHOOK_URL=$TEAMS_URL|" "$PROJECT_DIR/.env"
        info "Teams webhook saved to .env"
    else
        warn "Teams webhook skipped — alerts will not be sent until you add it to .env"
    fi
fi

# Logs directory
mkdir -p "$PROJECT_DIR/logs"
info "Logs directory: $PROJECT_DIR/logs/"

# Log rotation
LOGROTATE_CONF="/etc/logrotate.d/df-sentrybot"
if [ -f "$LOGROTATE_CONF" ]; then
    info "logrotate config already exists — skipping"
else
    echo ""
    read -rp "  Set up log rotation via logrotate? (keeps 30 days, requires sudo) [Y/n]: " ADD_LR
    if [[ ! "${ADD_LR:-Y}" =~ ^[Nn]$ ]]; then
        sudo tee "$LOGROTATE_CONF" > /dev/null << EOF
$PROJECT_DIR/logs/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    create 0644 $(whoami) $(whoami)
}
EOF
        info "logrotate configured (/etc/logrotate.d/df-sentrybot)"
    fi
fi

# domains.txt check
DOMAIN_COUNT=$(grep -cvE '^\s*#|^\s*$' "$PROJECT_DIR/domains.txt" 2>/dev/null || echo 0)
if [ "$DOMAIN_COUNT" -eq 0 ]; then
    warn "domains.txt has no active domains — edit it before running"
else
    info "domains.txt: $DOMAIN_COUNT domain(s) found"
fi

# ── 6. Cron job ───────────────────────────────────────────────────────────────
section "Step 6/6 — Cron job"

CRON_MARKER="sentry_bot_direct"
CRON_CMD="0 * * * * cd $PROJECT_DIR && export \$(grep -v '^#' .env | xargs) && .venv/bin/python sentry_bot_direct.py --domains-file domains.txt --headless >> logs/sentry_direct.log 2>&1"

if crontab -l 2>/dev/null | grep -q "$CRON_MARKER"; then
    info "Cron job already exists — skipping"
    echo ""
    echo "  Current cron entries for SentryBot:"
    crontab -l | grep "$CRON_MARKER"
else
    echo ""
    echo "  This cron job will be added:"
    echo "  $CRON_CMD"
    echo ""
    read -rp "  Add hourly cron job? [Y/n]: " ADD_CRON
    if [[ ! "${ADD_CRON:-Y}" =~ ^[Nn]$ ]]; then
        (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
        info "Cron job added — runs every hour"
    else
        warn "Cron job skipped. Add it manually with: crontab -e"
        echo ""
        echo "  Paste this line:"
        echo "  $CRON_CMD"
    fi
fi

# ── Optional test run ─────────────────────────────────────────────────────────
section "Setup complete"

echo -e "  ${G}${B}Everything is ready.${X}"
echo ""
echo "  Project   : $PROJECT_DIR"
echo "  Config    : $PROJECT_DIR/.env"
echo "  Domains   : $PROJECT_DIR/domains.txt"
echo "  Logs      : $PROJECT_DIR/logs/sentry_direct.log"
echo ""
echo "  Useful commands:"
echo "    View cron job  : crontab -l"
echo "    Stream logs    : tail -f $PROJECT_DIR/logs/sentry_direct.log"
echo "    Reset baseline : rm -f $PROJECT_DIR/sentry_state_direct.json"
echo "    Manual run     : cd $PROJECT_DIR && export \$(grep -v '^#' .env | xargs) && .venv/bin/python sentry_bot_direct.py --domains-file domains.txt --headless"
echo ""

read -rp "  Run a test audit now to verify everything works? [Y/n]: " RUN_TEST
if [[ ! "${RUN_TEST:-Y}" =~ ^[Nn]$ ]]; then
    echo ""
    info "Starting test audit…"
    echo ""
    cd "$PROJECT_DIR"
    set -a; source .env; set +a
    "$PYTHON" sentry_bot_direct.py --domains-file domains.txt --headless
fi

echo ""
echo -e "${B}${SEP}${X}"
echo ""
