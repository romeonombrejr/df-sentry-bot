#!/usr/bin/env python3
"""
DF SentryBot — Direct edition (no search API required)
Visits each domain's homepage directly with a headless browser,
compares live content against a saved baseline, and scans for hack keywords.

Designed to run hourly via cron. Teams alerts are batched into a digest
sent once every 24 hours (configurable). An "all clear" digest is sent too,
so the team always knows the bot is running.

Severity tiers
  GREEN  — page alive, title/description/content unchanged vs baseline
  YELLOW — minor drift in title, description, or content
  RED    — page down, hack keywords found, or major content change

Baseline behaviour
  First run    : visits each page, saves title/description/content as baseline
  Every 7 days : refreshes baseline IF no RED in the previous run

State is saved to sentry_state_direct.json (separate from the Brave version).
Pending alerts accumulate between digest sends and are cleared after each report.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Set environment variables (or pass as CLI flags):
  PowerShell : $env:TEAMS_WEBHOOK_URL="https://your-org.webhook.office.com/..."
  Linux/cron : export TEAMS_WEBHOOK_URL=https://...

No Brave API key is needed for this script.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python sentry_bot_direct.py --domains-file domains.txt --headless
  python sentry_bot_direct.py cloudway.com digitalfeet.no --headless
  python sentry_bot_direct.py --domains-file domains.txt --report-hours 12
  python sentry_bot_direct.py --domains-file domains.txt --baseline-days 14
  python sentry_bot_direct.py --domains-file domains.txt --teams-webhook URL

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRON EXAMPLE (run every hour, report every 24 hours)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  0 * * * * cd /opt/df-sentrybot && export $(grep -v '^#' .env | xargs) && \
            .venv/bin/python sentry_bot_direct.py --domains-file domains.txt \
            --headless >> logs/sentry_direct.log 2>&1

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEPENDENCIES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  pip install -r requirements.txt
  playwright install chromium
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import traceback
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv(Path(__file__).parent / ".env")

# ── Error log (written alongside this script) ─────────────────────────────────
_LOG_DIR  = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "sentry_direct_errors.log"

logging.basicConfig(
    filename=str(_LOG_FILE),
    level=logging.ERROR,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def _log_error(label: str, exc: Exception | None = None,
               response_status: int | None = None,
               response_body: str | None = None,
               payload_bytes: int | None = None):
    parts = [label]
    if payload_bytes is not None:
        parts.append(f"payload={payload_bytes}B")
    if response_status is not None:
        parts.append(f"http={response_status}")
    if response_body:
        parts.append(f"body={response_body[:500]}")
    if exc:
        parts.append(traceback.format_exc().strip())
    msg = " | ".join(parts)
    logging.error(msg)
    print(f"  [ERROR logged → {_LOG_FILE}]  {label}"
          + (f"  HTTP {response_status}" if response_status else "")
          + (f"  {response_body[:120]}" if response_body else ""))

# ── Teams mention helpers ─────────────────────────────────────────────────────

def _parse_mentions(env_str: str) -> list[dict]:
    """Parse 'Name:ObjectID,Name2:ObjectID2' from TEAMS_MENTIONS env var."""
    result = []
    for part in env_str.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        name, obj_id = part.split(":", 1)
        name, obj_id = name.strip(), obj_id.strip()
        if name and obj_id:
            result.append({"name": name, "id": obj_id})
    return result


def _mention_text(mentions: list[dict]) -> str:
    return "  ".join(f"<at>{m['name']}</at>" for m in mentions)


def _mention_entities(mentions: list[dict]) -> list[dict]:
    return [
        {"type": "mention",
         "text": f"<at>{m['name']}</at>",
         "mentioned": {"id": m["id"], "name": m["name"]}}
        for m in mentions
    ]

# ── Tunable defaults (all overridable via CLI flags) ──────────────────────────
THRESHOLD_GREEN       = 0.95
THRESHOLD_YELLOW      = 0.50
BASELINE_REFRESH_DAYS = 7
REPORT_INTERVAL_HOURS = 24
FETCH_TIMEOUT_SEC     = 40   # hard cap per URL across all browser operations

# ── Hack-indicator keywords (body text scan) ──────────────────────────────────
HACK_KEYWORDS = [
    # Defacement signatures
    "hacked by", "defaced by", "owned by", "this site has been hacked",
    # Pharma spam
    "buy viagra", "cheap viagra", "generic viagra", "order viagra",
    "buy cialis", "cheap cialis", "online pharmacy", "buy pills online",
    # Loan spam
    "payday loans", "cash advance loans", "quick loans online",
    # Gambling spam
    "online casino", "casino games", "slots online", "sports betting",
    # Crypto spam
    "buy bitcoin now", "bitcoin investment", "crypto trading signals",
    # Generic earn-money spam
    "make money online fast", "earn money online",
]

# ── State file (separate from the Brave version) ──────────────────────────────
STATE_FILE = Path(__file__).parent / "sentry_state_direct.json"

# ── ANSI colours ──────────────────────────────────────────────────────────────
_G = "\033[92m"
_Y = "\033[93m"
_R = "\033[91m"
_B = "\033[1m"
_D = "\033[2m"
_X = "\033[0m"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SEP  = "═" * 64
DASH = "─" * 64


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── String helpers ────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    s = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s*[—–-]\s*", "", (s or "").strip())
    return " ".join(s.lower().split())


def _url_for(domain: str) -> str:
    """Use http:// for localhost/127.x, https:// for everything else."""
    if domain.startswith("localhost") or re.match(r"^127\.", domain):
        return f"http://{domain}/"
    return f"https://{domain}/"


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def meter(ratio: float, width: int = 28) -> str:
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def severity_colour(level: str) -> str:
    return {"GREEN": _G, "YELLOW": _Y, "RED": _R}.get(level, _X)


# ── Classification ────────────────────────────────────────────────────────────

def classify(status: int, title_sim: float | None, desc_sim: float | None,
             content_sim: float | None, keywords_found: list) -> str:
    if status == 0 or status >= 400:
        return "RED"
    if keywords_found:
        return "RED"

    content_ok = content_sim is None or content_sim >= THRESHOLD_GREEN

    if content_ok:
        # Content intact — title/description drift alone is at most YELLOW
        if title_sim is None and desc_sim is None:
            return "GREEN"  # first run
        avg = (title_sim + desc_sim) / 2
        return "GREEN" if avg >= THRESHOLD_GREEN else "YELLOW"

    # Content has changed — escalate based on severity
    if content_sim < THRESHOLD_YELLOW:
        return "RED"
    return "YELLOW"


# ── Per-URL fetch ─────────────────────────────────────────────────────────────

async def fetch_page(page, url: str) -> dict:
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        status = resp.status if resp else 0

        # Let JS redirects settle without waiting for all heavy resources
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

        title = (await page.title() or "").strip()

        desc_el = await page.query_selector('meta[name="description" i]')
        desc = ""
        if desc_el:
            desc = (await desc_el.get_attribute("content") or "").strip()

        # Extract headings and paragraphs only — ignores dynamic/ad content
        content_text = await page.evaluate("""
            () => {
                const els = document.querySelectorAll('h1, h2, h3, h4, h5, h6, p');
                return Array.from(els)
                    .map(el => (el.innerText || '').trim())
                    .filter(t => t.length > 0)
                    .join(' ');
            }
        """)

        # Scan for hack-indicator keywords in full body
        body_lower     = (await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )).lower()
        keywords_found = [kw for kw in HACK_KEYWORDS if kw in body_lower]

        return {
            "status":         status,
            "title":          title,
            "description":    desc,
            "content_text":   content_text,
            "keywords_found": keywords_found,
            "note":           "",
        }

    except PlaywrightTimeout:
        return {"status": 0, "title": "", "description": "",
                "content_text": "", "keywords_found": [], "note": "Request timed out"}
    except Exception as exc:
        return {"status": 0, "title": "", "description": "",
                "content_text": "", "keywords_found": [], "note": str(exc)}


# ── Terminal output ───────────────────────────────────────────────────────────

def print_result(domain: str, url: str, meta: dict, saved: dict,
                 title_sim: float | None, desc_sim: float | None,
                 content_sim: float | None, sev: str, is_first_run: bool):
    col = severity_colour(sev)
    baseline_date = saved.get("baseline_set", "")[:10]

    print(f"\n{col}{_B}[ {sev} ]{_X}  {domain}")
    print(f"  URL         : {url}")
    print(f"  HTTP status : {meta['status'] or 'N/A'}")

    print(f"\n  {_B}Title{_X}")
    print(f"    Live      : {meta['title'] or f'{_D}(none){_X}'}")
    if title_sim is not None:
        tc = _G if title_sim >= THRESHOLD_GREEN else (_Y if title_sim >= THRESHOLD_YELLOW else _R)
        print(f"    vs base   : {meter(title_sim)}  {title_sim * 100:5.1f} %  {tc}{'unchanged' if title_sim >= THRESHOLD_GREEN else 'changed'}{_X}")
    else:
        print(f"    vs base   : {_D}First run — baseline saved{_X}")

    print(f"\n  {_B}Description{_X}")
    print(f"    Live      : {meta['description'] or f'{_D}(none){_X}'}")
    if desc_sim is not None:
        dc = _G if desc_sim >= THRESHOLD_GREEN else (_Y if desc_sim >= THRESHOLD_YELLOW else _R)
        print(f"    vs base   : {meter(desc_sim)}  {desc_sim * 100:5.1f} %  {dc}{'unchanged' if desc_sim >= THRESHOLD_GREEN else 'changed'}{_X}")
    else:
        print(f"    vs base   : {_D}First run — baseline saved{_X}")

    print(f"\n  {_B}Content (headings & paragraphs){_X}")
    if content_sim is not None:
        col2  = _G if content_sim >= THRESHOLD_GREEN else (_Y if content_sim >= THRESHOLD_YELLOW else _R)
        label = "Unchanged" if content_sim >= THRESHOLD_GREEN else "Changed"
        print(f"    Match     : {meter(content_sim)}  {content_sim * 100:5.1f} %")
        print(f"    Status    : {col2}{label} since baseline ({baseline_date}){_X}")
    else:
        print(f"    Status    : {_D}First run — baseline saved{_X}")

    kw = meta.get("keywords_found")
    if kw:
        print(f"\n  {_R}{_B}Hack keywords detected:{_X}")
        for word in kw:
            print(f"    {_R}• {word}{_X}")

    if meta["note"]:
        print(f"\n  {_R}! {meta['note']}{_X}")


# ── Teams alerts ─────────────────────────────────────────────────────────────

def send_emergency_alert(webhook_url: str, domain: str, url: str,
                         meta: dict, content_sim: float | None,
                         now_iso: str):
    """Fires immediately when a domain is flagged RED — does not wait for digest."""
    if not webhook_url:
        return

    mentions = _parse_mentions(os.environ.get("TEAMS_MENTIONS", ""))

    facts = [
        {"title": "URL",         "value": url},
        {"title": "HTTP status", "value": str(meta["status"] or "N/A")},
    ]
    if content_sim is not None:
        facts.append({"title": "Content match",
                      "value": f"{content_sim * 100:.1f}% vs baseline"})
    if meta.get("keywords_found"):
        facts.append({"title": "Hack keywords",
                      "value": ", ".join(meta["keywords_found"])})
    if meta.get("note"):
        facts.append({"title": "Error", "value": meta["note"]})

    body = [
        {
            "type":   "TextBlock",
            "text":   f"EMERGENCY ALERT — {domain}",
            "weight": "Bolder",
            "size":   "Large",
            "color":  "Attention",
        },
        {
            "type": "TextBlock",
            "text": (
                f"A RED flag was detected during the hourly audit "
                f"at {now_iso}. Immediate review recommended."
                + (f"  {_mention_text(mentions)}" if mentions else "")
            ),
            "wrap": True,
        },
        {"type": "FactSet", "facts": facts},
    ]

    content = {
        "type":    "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body":    body,
        "actions": [{
            "type":  "Action.OpenUrl",
            "title": "Visit site now",
            "url":   url,
        }],
    }
    if mentions:
        content["msteams"] = {"entities": _mention_entities(mentions)}

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content":     content,
        }],
    }

    raw      = json.dumps(payload).encode()
    try:
        resp = requests.post(webhook_url, data=raw,
                             headers={"Content-Type": "application/json"}, timeout=10)
        if not resp.ok:
            _log_error(f"Emergency alert failed for {domain}",
                       response_status=resp.status_code, response_body=resp.text,
                       payload_bytes=len(raw))
        else:
            print(f"  {_R}{_B}Emergency Teams alert sent for {domain}.{_X}")
    except Exception as exc:
        _log_error(f"Emergency alert exception for {domain}", exc=exc,
                   payload_bytes=len(raw))


# ── Teams digest ──────────────────────────────────────────────────────────────

def send_teams_digest(webhook_url: str, pending: list[dict],
                      now_iso: str, report_hours: int):
    if not webhook_url:
        return

    # Deduplicate: worst severity per domain in this period
    worst: dict[str, dict] = {}
    for alert in pending:
        d = alert["domain"]
        if d not in worst or (
            alert["severity"] == "RED" or
            (alert["severity"] == "YELLOW" and worst[d]["severity"] == "GREEN")
        ):
            worst[d] = alert

    flagged = [a for a in worst.values() if a["severity"] in ("RED", "YELLOW")]
    green   = [a for a in worst.values() if a["severity"] == "GREEN"]
    all_clear = len(flagged) == 0

    mentions = _parse_mentions(os.environ.get("TEAMS_MENTIONS", ""))

    has_red      = any(a["severity"] == "RED" for a in flagged)
    header_color = "Good" if all_clear else ("Attention" if has_red else "Warning")
    title_str = (
        f"DF SentryBot — All clear  ({len(worst)} domain(s) checked)"
        if all_clear else
        f"DF SentryBot — {len(flagged)} issue(s) across {len(worst)} domain(s)"
    )

    subtitle = f"Report period: last {report_hours} hour(s) — generated {now_iso}"
    if mentions and not all_clear:
        subtitle += f"  {_mention_text(mentions)}"

    body: list = [
        {
            "type":   "TextBlock",
            "text":   title_str,
            "weight": "Bolder",
            "size":   "Medium",
            "color":  header_color,
        },
        {
            "type":     "TextBlock",
            "text":     subtitle,
            "isSubtle": True,
            "spacing":  "None",
            "wrap":     True,
        },
    ]

    # RED and YELLOW domains — highlighted containers with full details
    for a in sorted(flagged, key=lambda x: 0 if x["severity"] == "RED" else 1):
        facts = [
            {"title": "URL",         "value": a["url"]},
            {"title": "HTTP status", "value": str(a.get("status") or "N/A")},
        ]
        cs = a.get("content_sim")
        if cs is not None:
            facts.append({"title": "Content match", "value": f"{cs * 100:.1f}%"})
        if a.get("keywords_found"):
            facts.append({"title": "Hack keywords",
                          "value": ", ".join(a["keywords_found"])})
        if a.get("note"):
            facts.append({"title": "Error", "value": a["note"]})
        body.append({
            "type":    "Container",
            "style":   "attention" if a["severity"] == "RED" else "warning",
            "spacing": "Medium",
            "items": [
                {"type": "TextBlock",
                 "text": f"[{a['severity']}]  {a['domain']}",
                 "weight": "Bolder"},
                {"type": "FactSet", "facts": facts},
            ],
        })

    # GREEN domains — compact list at the bottom
    if green:
        green_list = "  ·  ".join(
            sorted(a["domain"] for a in green)
        )
        body.append({
            "type":    "Container",
            "style":   "good",
            "spacing": "Medium",
            "items": [
                {"type": "TextBlock",
                 "text": f"No issues ({len(green)} domain(s))",
                 "weight": "Bolder",
                 "color":  "Good"},
                {"type": "TextBlock",
                 "text": green_list,
                 "wrap": True,
                 "isSubtle": True},
            ],
        })

    content = {
        "type":    "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body":    body,
    }
    if mentions and not all_clear:
        content["msteams"] = {"entities": _mention_entities(mentions)}

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content":     content,
        }],
    }

    raw = json.dumps(payload).encode()
    print(f"  Sending Teams digest  ({len(raw)} bytes) …")
    try:
        resp = requests.post(webhook_url, data=raw,
                             headers={"Content-Type": "application/json"}, timeout=15)
        if not resp.ok:
            _log_error("Digest webhook rejected",
                       response_status=resp.status_code, response_body=resp.text,
                       payload_bytes=len(raw))
        else:
            status_label = "All-clear digest" if all_clear else f"Digest ({len(flagged)} alert(s))"
            print(f"\n  {_G}Teams {status_label} sent.{_X}")
    except Exception as exc:
        _log_error("Digest webhook exception", exc=exc, payload_bytes=len(raw))


# ── Orchestration ─────────────────────────────────────────────────────────────

async def run(domains: list[str], headless: bool, teams_webhook: str,
              report_hours: int, baseline_days: int):
    now     = datetime.now()
    now_iso = now.isoformat(timespec="seconds")

    print(f"\n{SEP}")
    print(f"{_B}  DF SentryBot (Direct)  —  {len(domains)} domain(s){_X}")
    print(f"  Run time     : {now_iso}")
    print(f"  Baseline     : every {baseline_days} day(s)")
    print(f"  Report cadence: every {report_hours} hour(s)")
    print(SEP)

    state           = load_state()
    pending_alerts  = state.setdefault("pending_alerts", [])
    domains_state   = state.setdefault("domains", {})

    # Decide if it's time to send the Teams digest
    last_report    = state.get("last_report")
    should_report  = (
        last_report is None or
        (now - datetime.fromisoformat(last_report)).total_seconds() >= report_hours * 3600
    )

    all_results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            user_agent=BROWSER_UA,
            viewport={
                "width":  random.choice([1280, 1366, 1440, 1920]),
                "height": random.choice([768, 800, 900, 1080]),
            },
            locale="en-US",
        )
        page = await ctx.new_page()

        for domain in domains:
            url          = _url_for(domain)
            domain_state = domains_state.setdefault(domain, {})
            pages_state  = domain_state.setdefault("pages", {})
            is_first_run = "baseline_set" not in domain_state

            # Check if baseline needs refreshing
            refreshed = False
            if not is_first_run:
                age_days = (now - datetime.fromisoformat(domain_state["baseline_set"])).days
                if age_days >= baseline_days and not domain_state.get("last_run_had_red", False):
                    refreshed = True
                    print(f"\n  {_Y}[{domain}] Baseline is {age_days} days old and last run was "
                          f"clean — refreshing after this audit.{_X}")

            print(f"\n  Auditing {url} …")
            try:
                meta = await asyncio.wait_for(fetch_page(page, url), timeout=FETCH_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                print(f"  Timed out after {FETCH_TIMEOUT_SEC}s — resetting browser page")
                meta = {"status": 0, "title": "", "description": "",
                        "content_text": "", "keywords_found": [],
                        "note": f"Audit timed out after {FETCH_TIMEOUT_SEC}s"}
                try:
                    await page.goto("about:blank", wait_until="domcontentloaded", timeout=5_000)
                except Exception:
                    pass
            await page.wait_for_timeout(700)

            saved = pages_state.get(url, {})

            # Compute similarities vs saved baseline
            if not is_first_run and "baseline_content" in saved:
                title_sim   = similarity(saved.get("baseline_title",   ""), meta["title"])
                desc_sim    = similarity(saved.get("baseline_desc",    ""), meta["description"])
                content_sim = similarity(saved.get("baseline_content", ""), meta["content_text"])
            else:
                title_sim = desc_sim = content_sim = None

            kw_found = meta.get("keywords_found", [])
            sev      = classify(meta["status"], title_sim, desc_sim, content_sim, kw_found)

            print_result(domain, url, meta, saved,
                         title_sim, desc_sim, content_sim, sev, is_first_run)

            # Immediate emergency alert for RED — does not wait for digest
            if sev == "RED":
                send_emergency_alert(teams_webhook, domain, url,
                                     meta, content_sim, now_iso)

            # Accumulate all results for the full status digest
            pending_alerts.append({
                "timestamp":      now_iso,
                "domain":         domain,
                "url":            url,
                "severity":       sev,
                "status":         meta["status"],
                "content_sim":    content_sim,
                "keywords_found": kw_found,
                "note":           meta["note"],
            })

            # Update state
            page_entry = pages_state.setdefault(url, {})
            page_entry["last_title"]   = meta["title"]
            page_entry["last_desc"]    = meta["description"]
            page_entry["last_content"] = meta["content_text"]
            page_entry["last_seen"]    = now_iso

            if is_first_run or refreshed or "baseline_content" not in page_entry:
                page_entry["baseline_title"]   = meta["title"]
                page_entry["baseline_desc"]    = meta["description"]
                page_entry["baseline_content"] = meta["content_text"]
                page_entry["baseline_set"]     = now_iso

            if is_first_run or refreshed:
                domain_state["baseline_set"] = now_iso

            domain_state["first_run"]        = domain_state.get("first_run", now_iso)
            domain_state["last_run"]         = now_iso
            domain_state["last_run_had_red"] = (sev == "RED")

            all_results.append({"domain": domain, "severity": sev,
                                "is_first_run": is_first_run})

        await browser.close()

    # ── Summary ───────────────────────────────────────────────────────────
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    for r in all_results:
        counts[r["severity"]] += 1

    print(f"\n{SEP}")
    print(f"{_B}SUMMARY  —  {len(all_results)} domain(s) audited{_X}")
    print(DASH)
    print(f"  {_G}{_B}●  Green   page intact              : {counts['GREEN']:>3}{_X}")
    print(f"  {_Y}{_B}●  Yellow  minor drift               : {counts['YELLOW']:>3}{_X}")
    print(f"  {_R}{_B}●  Red     major change / broken     : {counts['RED']:>3}{_X}")
    print(DASH)

    # ── Teams digest ──────────────────────────────────────────────────────
    any_comparison = any(not r.get("is_first_run") for r in all_results)

    if not any_comparison:
        # All domains were on their first run — baseline saved, nothing compared yet.
        # Leave last_report unset so the very next run sends the first real digest.
        print(f"  First run complete — baseline saved for {len(all_results)} domain(s).")
        print(f"  Teams digest will be sent on the next run.")
    elif should_report:
        send_teams_digest(teams_webhook, pending_alerts, now_iso, report_hours)
        state["pending_alerts"] = []
        state["last_report"]    = now_iso
    else:
        next_report = (
            datetime.fromisoformat(last_report) + timedelta(hours=report_hours)
        )
        hrs_left = max(0, (next_report - now).total_seconds() / 3600)
        pending_count = len(pending_alerts)
        print(f"  Next Teams digest in ~{hrs_left:.1f} hour(s)  "
              f"({pending_count} pending alert(s))")

    print(SEP)

    state["last_run"] = now_iso
    save_state(state)


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args(args: list[str]) -> dict:
    teams_webhook = os.environ.get("TEAMS_WEBHOOK_URL", "")
    headless      = False
    report_hours  = REPORT_INTERVAL_HOURS
    baseline_days = BASELINE_REFRESH_DAYS
    domains: list[str] = []
    domains_file  = None

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--headless":
            headless = True
        elif a == "--teams-webhook" and i + 1 < len(args):
            teams_webhook = args[i + 1]; i += 1
        elif a == "--report-hours" and i + 1 < len(args):
            report_hours = int(args[i + 1]); i += 1
        elif a == "--baseline-days" and i + 1 < len(args):
            baseline_days = int(args[i + 1]); i += 1
        elif a == "--domains-file" and i + 1 < len(args):
            domains_file = args[i + 1]; i += 1
        elif not a.startswith("--"):
            domains.append(a)
        i += 1

    if domains_file:
        try:
            lines = Path(domains_file).read_text(encoding="utf-8").splitlines()
            file_domains = [ln.strip() for ln in lines
                            if ln.strip() and not ln.strip().startswith("#")]
            domains = file_domains + domains
        except Exception as exc:
            print(f"\n{_R}  Could not read domains file: {exc}{_X}\n")
            sys.exit(1)

    return {
        "domains":       domains,
        "headless":      headless,
        "teams_webhook": teams_webhook,
        "report_hours":  report_hours,
        "baseline_days": baseline_days,
    }


def main():
    parsed = parse_args(sys.argv[1:])

    if not parsed["domains"]:
        print(__doc__)
        sys.exit(1)

    asyncio.run(run(
        parsed["domains"],
        parsed["headless"],
        parsed["teams_webhook"],
        parsed["report_hours"],
        parsed["baseline_days"],
    ))


if __name__ == "__main__":
    main()
