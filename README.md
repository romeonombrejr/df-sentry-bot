# DF SentryBot

Automated website defacement and SEO monitoring tool built at **Digital Feet**.  
Monitors client websites every 6 hours for signs of hacking, content tampering, or SEO poisoning, and sends alerts to Microsoft Teams.

---

## How It Works

1. **Queries Brave Search** for indexed meta title and description of each domain
2. **Visits each URL** with a headless Chromium browser (Playwright)
3. **Compares** what Brave has indexed against what is live on the page
4. **Scans page content** (headings and paragraphs) against a saved baseline to detect body changes
5. **Scans for hack keywords** — pharma spam, casino injection, defacement signatures, etc.
6. **Classifies** each page as GREEN / YELLOW / RED
7. **Alerts Microsoft Teams** when any RED or YELLOW result is found

---

## Severity Levels

| Level | Meaning |
|---|---|
| 🟢 **GREEN** | Page is up, meta matches Brave index, content unchanged |
| 🟡 **YELLOW** | Meta tags have drifted (likely an SEO update) or content changed slightly |
| 🔴 **RED** | Page is down, hack keywords detected, or both meta and content have significantly changed |

**Key rule:** if page content is intact, meta drift alone is capped at YELLOW — it almost always means a legitimate title/description update, not a defacement.

---

## Requirements

- Python 3.11+
- A [Brave Search API](https://api.search.brave.com/) key (free tier: 2,000 queries/month)
- A Microsoft Teams Incoming Webhook URL *(optional — alerts are skipped if not set)*

---

## Installation

```bash
# 1. Clone or copy this project to your server
cd /opt/df-sentrybot          # or wherever you want it

# 2. Create and activate a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate     # Linux/macOS
# .venv\Scripts\activate      # Windows

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright's Chromium browser
playwright install chromium
playwright install-deps chromium   # Linux only — installs system libraries
```

---

## Configuration

### 1. Environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```dotenv
BRAVE_API_KEY=your_brave_api_key_here
TEAMS_WEBHOOK_URL=https://your-org.webhook.office.com/...
```

> **Never commit `.env` to version control.** It is already listed in `.gitignore`.

To get a Teams webhook URL: open the target channel in Teams → **···** → **Connectors** → **Incoming Webhook** → **Configure** → copy the URL.

### 2. Domains list

Edit `domains.txt` — one domain per line, `#` for comments:

```
# Client websites
cloudway.com
digitalfeet.no
infosoft.no
infosoft.se
```

---

## Usage

```bash
# Load environment variables first (Linux)
export $(grep -v '^#' .env | xargs)

# Audit all domains in domains.txt (1 page per domain)
python sentry_bot_brave.py --domains-file domains.txt --count 1

# Audit multiple domains inline
python sentry_bot_brave.py cloudway.com digitalfeet.no --count 3

# Audit a single domain
python sentry_bot_brave.py cloudway.com

# Run headless (no browser window — required on a server)
python sentry_bot_brave.py --domains-file domains.txt --headless

# Pass API key and webhook directly (overrides .env)
python sentry_bot_brave.py cloudway.com --api-key YOUR_KEY --teams-webhook YOUR_URL
```

### Flags

| Flag | Description |
|---|---|
| `--domains-file FILE` | Path to a text file with one domain per line |
| `--count N` | Number of pages to audit per domain (default: 10) |
| `--headless` | Run browser invisibly — use this on servers |
| `--api-key KEY` | Brave API key (overrides `BRAVE_API_KEY` env var) |
| `--teams-webhook URL` | Teams webhook URL (overrides `TEAMS_WEBHOOK_URL` env var) |

---

## Script Variants

### Brave Search versions (search index comparison)

| Script | Search strategy | API queries per domain |
|---|---|---|
| `sentry_bot_brave.py` | Query 1: bare domain (homepage) · Query 2: `site:domain` (subpages) | 1 if `--count 1`, else 2 |
| `sentry_bot_brave_simple.py` | Single `site:domain` query, homepage promoted to first | Always 1 |

These compare the **live page** against what **Brave Search has indexed**. Useful for catching SEO poisoning where the search engine has been fed different content from what visitors actually see.

Use `sentry_bot_brave.py` for reliable homepage detection. Use the simple version to conserve API quota when monitoring many domains.

---

### Direct version (no search API)

**`sentry_bot_direct.py`** — no Brave API key needed.

Instead of comparing against a search index, it compares the **live page** against a **saved baseline** from the previous run. Runs hourly via cron and sends a Teams digest once every 24 hours (configurable).

```bash
# Audit all domains hourly, Teams digest every 24 hours (default)
python sentry_bot_direct.py --domains-file domains.txt --headless

# Custom intervals
python sentry_bot_direct.py --domains-file domains.txt --report-hours 12 --baseline-days 14

# Specific domains
python sentry_bot_direct.py cloudway.com digitalfeet.no --headless
```

#### Direct version flags

| Flag | Default | Description |
|---|---|---|
| `--domains-file FILE` | — | Path to domains list |
| `--report-hours N` | `24` | How many hours between Teams digest reports |
| `--baseline-days N` | `7` | How many days between baseline refreshes |
| `--teams-webhook URL` | env var | Teams Incoming Webhook URL |
| `--headless` | off | Run browser invisibly (required on servers) |

#### How the digest works

- **Every hourly run**: audits all domains, accumulates any YELLOW/RED results
- **Every 24 hours**: sends one Teams digest card summarising all accumulated alerts
- **All clear**: even if everything is GREEN, a short "all clear" digest is sent so the team knows the bot is running
- **Pending alerts** are cleared from state after each digest is sent

#### Direct version cron setup

```cron
0 * * * * cd /opt/df-sentrybot && export $(grep -v '^#' .env | xargs) && \
          .venv/bin/python sentry_bot_direct.py --domains-file domains.txt \
          --headless >> logs/sentry_direct.log 2>&1
```

#### Direct version state file

State is stored in `sentry_state_direct.json` (separate from the Brave version — both can run on the same server without interfering). It is excluded from git via `.gitignore`.

To reset the baseline for the direct version:

```bash
rm sentry_state_direct.json
```

#### Comparison: Brave vs Direct

| | Brave version | Direct version |
|---|---|---|
| API key required | Yes (Brave Search) | No |
| What is compared | Live page vs search index | Live page vs saved baseline |
| Detects SEO poisoning | Yes | No (no search index) |
| Detects defacement | Yes | Yes |
| Alert style | Immediate per-run | 24-hour digest |
| Multiple pages per domain | Yes (`--count N`) | Homepage only |

---

## Baseline & State

State is stored in `sentry_state.json` (auto-created, excluded from git).

| Behaviour | Detail |
|---|---|
| **First run** | Visits each page and saves headings/paragraph text as baseline. No comparison yet. |
| **Subsequent runs** | Compares live content against saved baseline using similarity scoring |
| **Baseline refresh** | Auto-refreshes every **7 days** — but only if the previous run had no RED alerts. If the site is compromised, the clean baseline is preserved. |

To force a baseline reset (e.g. after a legitimate site redesign), delete `sentry_state.json`:

```bash
rm sentry_state.json
```

---

## Unindexed Domains

If Brave Search has not indexed a domain, the bot falls back to auditing `https://domain/` directly. The result is shown as **YELLOW** with a note *"Not indexed by Brave Search"*. It goes **RED** only if the page is actually unreachable.

To get a domain indexed by Brave Search, visit [search.brave.com](https://search.brave.com), scroll to the footer, and use the webmaster submission tool.

---

## Deploying on Linux VPS (Ubuntu)

### Automated setup (recommended)

After cloning the repo, run the setup script once. It handles everything:

```bash
git clone <your-repo-url> /opt/df-sentrybot
cd /opt/df-sentrybot
bash setup.sh
```

The script will:
1. Install Python and system packages if missing
2. Create a `.venv` virtual environment and install all dependencies
3. Install Playwright's Chromium browser and its system libraries
4. Create `.env` from `.env.example` and prompt for your Teams webhook URL
5. Create the `logs/` directory and configure `logrotate` (30-day retention)
6. Add the hourly cron job for `sentry_bot_direct.py`
7. Offer a test run to verify everything works

### Manual setup (if you prefer)

```bash
# System packages
sudo apt update && sudo apt install -y python3 python3-pip python3-venv

# Virtual environment + dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Playwright Chromium + system libraries
.venv/bin/playwright install chromium
sudo .venv/bin/playwright install-deps chromium

# Logs directory
mkdir -p logs

# Configuration
cp .env.example .env
nano .env   # fill in TEAMS_WEBHOOK_URL
```

### Cron job (every hour, direct version)

```bash
crontab -e
```

Add:

```cron
0 * * * * cd /opt/df-sentrybot && export $(grep -v '^#' .env | xargs) && .venv/bin/python sentry_bot_direct.py --domains-file domains.txt --headless >> logs/sentry_direct.log 2>&1
```

### Useful commands on the VPS

```bash
# Check cron is registered
crontab -l

# Stream live log output
tail -f /opt/df-sentrybot/logs/sentry_direct.log

# Run manually
cd /opt/df-sentrybot && export $(grep -v '^#' .env | xargs) && \
  .venv/bin/python sentry_bot_direct.py --domains-file domains.txt --headless

# Reset baseline (e.g. after a site redesign)
rm -f /opt/df-sentrybot/sentry_state_direct.json
```

---

## Project Structure

```
df-sentrybot/
├── sentry_bot_brave.py          # Brave version — compares live page vs search index
├── sentry_bot_brave_simple.py   # Brave version — single API request variant
├── sentry_bot_direct.py         # Direct version — no search API, hourly + daily digest
├── setup.sh                     # One-time VPS setup script (run after cloning)
├── domains.txt                  # List of domains to monitor
├── requirements.txt             # Python dependencies
├── .env                         # Your API keys (never commit this)
├── .env.example                 # Template — safe to commit
├── .gitignore
├── sentry_state.json            # Brave version runtime state (auto-generated)
└── sentry_state_direct.json     # Direct version runtime state (auto-generated)
```

---

## Built With

- [Playwright](https://playwright.dev/python/) — headless browser automation
- [Brave Search API](https://api.search.brave.com/) — search index data (no CAPTCHA)
- [Microsoft Teams Incoming Webhook](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook) — alert delivery
- Python standard library — `difflib`, `json`, `asyncio`, `re`

---

*Built at [Digital Feet](https://digitalfeet.no) with the assistance of [Claude Code](https://claude.ai/code).*
