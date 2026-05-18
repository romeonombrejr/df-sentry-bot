# DF SentryBot

Automated website defacement and SEO monitoring tool built at **Digital Feet**.  
Monitors client websites every hour for signs of hacking, content tampering, or SEO poisoning, and sends alerts to Microsoft Teams.

---

## How It Works

1. **Visits each URL** with a headless Chromium browser (Playwright)
2. **Compares** live page content (headings and paragraphs) against a saved baseline to detect changes
3. **Scans page content** against hack-indicator keywords — pharma spam, casino injection, defacement signatures, etc.
4. **Classifies** each page as GREEN / YELLOW / RED
5. **Alerts Microsoft Teams** — immediately on RED, and as a full digest every 24 hours

---

## Severity Levels

| Level | Meaning |
|---|---|
| 🟢 **GREEN** | Page is up and content matches baseline |
| 🟡 **YELLOW** | Content or meta tags have drifted slightly (likely a legitimate update) |
| 🔴 **RED** | Page is down, hack keywords detected, or content has significantly changed |

**Key rule:** if page content is intact, meta drift alone is capped at YELLOW — it almost always means a legitimate title/description update, not a defacement.

---

## Requirements

- Python 3.11+
- A Microsoft Teams Workflows webhook URL *(optional — alerts are skipped if not set)*
- A [Brave Search API](https://api.search.brave.com/) key *(Brave versions only — free tier: 2,000 queries/month)*

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
TEAMS_WEBHOOK_URL=https://your-org...powerplatform.com/...
TEAMS_MENTIONS=Romeo:8c0a1a67-50ce-4114-bb6c-da9c5dbcf6ca,Jane:9d1b2b78-61df-4cd2-a519-abcdef123456
```

> **Never commit `.env` to version control.** It is already listed in `.gitignore`.

#### TEAMS_WEBHOOK_URL — how to create one

The old Microsoft 365 Connectors (Incoming Webhook) are being retired. Use the new **Workflows** method instead:

1. Open the target Teams channel
2. Click **···** → **Workflows**
3. Search for **"Post to a channel when a webhook request is received"**
4. Follow the setup steps and copy the generated URL

#### TEAMS_MENTIONS — optional @mentions in alerts

To have the bot @mention specific people in RED/YELLOW alert cards:

1. Find each person's **Azure AD Object ID** — go to [admin.teams.microsoft.com](https://admin.teams.microsoft.com) → **Users** → click the person → copy their **Object ID**
2. Add them to `.env` as `DisplayName:ObjectID`, comma-separated:

```dotenv
TEAMS_MENTIONS=Romeo:8c0a1a67-50ce-4114-bb6c-da9c5dbcf6ca
```

Mentions are sent on RED emergency alerts and on digests that contain YELLOW or RED results. All-clear digests do not mention anyone.

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

# Audit all domains in domains.txt
python sentry_bot_direct.py --domains-file domains.txt --headless

# Audit a single domain
python sentry_bot_direct.py cloudway.com --headless

# Audit multiple domains inline
python sentry_bot_direct.py cloudway.com digitalfeet.no --headless

# Brave version — compare live page against Brave Search index
python sentry_bot_brave.py --domains-file domains.txt --count 1 --headless
```

---

## Script Variants

### Direct version (recommended for continuous monitoring)

**`sentry_bot_direct.py`** — no search API key needed.

Compares the **live page** against a **saved baseline** from a previous run. Designed to run hourly via cron.

- Sends an **immediate emergency alert** to Teams when any domain is flagged RED
- Sends a **full status digest** to Teams every 24 hours covering all monitored domains — GREEN, YELLOW, and RED — so the team always sees the complete picture
- The very first run saves the baseline only (no Teams message). The second run sends the first real digest.

```bash
python sentry_bot_direct.py --domains-file domains.txt --headless
python sentry_bot_direct.py --domains-file domains.txt --report-hours 12 --baseline-days 14
```

#### Direct version flags

| Flag | Default | Description |
|---|---|---|
| `--domains-file FILE` | — | Path to domains list |
| `--report-hours N` | `24` | How many hours between Teams digest reports |
| `--baseline-days N` | `7` | How many days between baseline refreshes |
| `--teams-webhook URL` | env var | Teams Incoming Webhook URL |
| `--headless` | off | Run browser invisibly (required on servers) |

#### How Teams alerts work

- **First run** — visits all pages and saves baseline. No Teams message sent.
- **Every hourly run** — audits all domains, compares against baseline.
- **RED detected** — an emergency alert is sent to Teams immediately, without waiting for the digest. Configured mentions are included.
- **Every 24 hours** — a full digest card is posted to Teams listing every monitored domain with its status (GREEN, YELLOW, or RED). If there are issues, configured mentions are included.
- **All clear** — even when everything is GREEN, the digest is sent so the team knows the bot is running.

#### Direct version cron setup

```cron
0 * * * * cd /opt/df-sentrybot && export $(grep -v '^#' .env | xargs) && .venv/bin/python sentry_bot_direct.py --domains-file domains.txt --headless >> logs/sentry_direct.log 2>&1
```

#### Direct version state and logs

| File | Purpose |
|---|---|
| `sentry_state_direct.json` | Runtime state — baselines, pending alerts, last report time |
| `logs/sentry_direct.log` | Cron output log (stdout/stderr) |
| `logs/sentry_direct_errors.log` | Webhook errors — HTTP status, response body, payload size |

To reset the baseline (e.g. after a legitimate site redesign):

```bash
rm sentry_state_direct.json
```

---

### Brave Search versions (SEO comparison)

These compare the **live page** against what **Brave Search has indexed**. Useful for catching SEO poisoning where the search engine has been fed different content from what visitors actually see.

| Script | Search strategy | API queries per domain |
|---|---|---|
| `sentry_bot_brave.py` | Query 1: bare domain (homepage) · Query 2: `site:domain` (subpages) | 1 if `--count 1`, else 2 |
| `sentry_bot_brave_simple.py` | Single `site:domain` query, homepage promoted to first | Always 1 |

```bash
# Audit all domains — 1 page per domain (1 API query each)
python sentry_bot_brave.py --domains-file domains.txt --count 1 --headless

# Audit up to 5 pages per domain
python sentry_bot_brave.py --domains-file domains.txt --count 5 --headless

# Pass API key directly (overrides .env)
python sentry_bot_brave.py cloudway.com --api-key YOUR_KEY --headless
```

#### Brave version flags

| Flag | Description |
|---|---|
| `--domains-file FILE` | Path to a text file with one domain per line |
| `--count N` | Number of pages to audit per domain (default: 10) |
| `--headless` | Run browser invisibly — use this on servers |
| `--api-key KEY` | Brave API key (overrides `BRAVE_API_KEY` env var) |
| `--teams-webhook URL` | Teams webhook URL (overrides `TEAMS_WEBHOOK_URL` env var) |

---

## Baseline & State

| Behaviour | Detail |
|---|---|
| **First run** | Visits each page and saves headings/paragraph text as baseline. No comparison yet. |
| **Subsequent runs** | Compares live content against saved baseline using similarity scoring |
| **Baseline refresh** | Auto-refreshes every **7 days** — but only if the previous run had no RED alerts |

---

## Unindexed Domains (Brave versions only)

If Brave Search has not indexed a domain, the bot falls back to auditing `https://domain/` directly. The result is shown as **YELLOW** with a note *"Not indexed by Brave Search"*. It goes **RED** only if the page is actually unreachable.

To get a domain indexed by Brave Search, visit [search.brave.com](https://search.brave.com), scroll to the footer, and use the webmaster submission tool.

---

## Comparison: Direct vs Brave

| | Direct version | Brave version |
|---|---|---|
| API key required | No | Yes (Brave Search) |
| What is compared | Live page vs saved baseline | Live page vs search index |
| Detects SEO poisoning | No (no search index) | Yes |
| Detects defacement | Yes | Yes |
| Immediate RED alert | Yes | No |
| Teams digest | Full report — all domains | YELLOW/RED only |
| Multiple pages per domain | Homepage only | Yes (`--count N`) |

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
nano .env   # fill in TEAMS_WEBHOOK_URL (and optionally TEAMS_MENTIONS)
```

### Useful commands on the VPS

```bash
# Check cron is registered
crontab -l

# Stream live log output
tail -f /opt/df-sentrybot/logs/sentry_direct.log

# View webhook errors
cat /opt/df-sentrybot/logs/sentry_direct_errors.log

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
├── sentry_bot_direct.py         # Direct version — no search API, hourly + digest
├── sentry_bot_brave.py          # Brave version — compares live page vs search index
├── sentry_bot_brave_simple.py   # Brave version — single API request variant
├── setup.sh                     # One-time VPS setup script (run after cloning)
├── test_webhook.py              # Sends a test card to verify the Teams webhook works
├── test_server.py               # Local Flask server for testing RED flag detection
├── domains.txt                  # List of domains to monitor (excluded from git)
├── requirements.txt             # Python dependencies
├── .env                         # Your API keys and config (never commit this)
├── .env.example                 # Template — safe to commit
├── .gitignore
├── logs/
│   ├── sentry_direct.log        # Cron output log
│   └── sentry_direct_errors.log # Webhook error details
├── sentry_state.json            # Brave version runtime state (auto-generated)
└── sentry_state_direct.json     # Direct version runtime state (auto-generated)
```

---

## Built With

- [Playwright](https://playwright.dev/python/) — headless browser automation
- [Brave Search API](https://api.search.brave.com/) — search index data (Brave versions only)
- [Microsoft Teams Workflows Webhook](https://support.microsoft.com/en-us/office/create-incoming-webhooks-with-workflows-for-microsoft-teams-8ae491c7-0394-4861-ba59-055e33f75498) — alert delivery via Adaptive Cards
- Python standard library — `difflib`, `json`, `asyncio`, `re`, `logging`

---

*Built at [Digital Feet](https://digitalfeet.no) with the assistance of [Claude Code](https://claude.ai/code).*
