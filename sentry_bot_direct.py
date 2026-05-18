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
import os
import random
import re
import sys
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Tunable defaults (all overridable via CLI flags) ──────────────────────────
THRESHOLD_GREEN       = 0.95
THRESHOLD_YELLOW      = 0.50
BASELINE_REFRESH_DAYS = 7
REPORT_INTERVAL_HOURS = 24

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
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        status = resp.status if resp else 0

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

    facts = [
        {"name": "URL",         "value": url},
        {"name": "HTTP status", "value": str(meta["status"] or "N/A")},
    ]
    if content_sim is not None:
        facts.append({"name": "Content match",
                      "value": f"{content_sim * 100:.1f}% vs baseline"})
    if meta.get("keywords_found"):
        facts.append({"name": "Hack keywords",
                      "value": ", ".join(meta["keywords_found"])})
    if meta.get("note"):
        facts.append({"name": "Error", "value": meta["note"]})

    payload = {
        "@type":      "MessageCard",
        "@context":   "https://schema.org/extensions",
        "summary":    f"🚨 EMERGENCY — {domain} flagged RED",
        "themeColor": "FF0000",
        "title":      f"🚨 EMERGENCY ALERT — {domain}",
        "text":       (
            f"A RED flag was detected during the hourly audit at **{now_iso}**. "
            f"Immediate review recommended."
        ),
        "sections": [{"facts": facts}],
        "potentialAction": [{
            "@type":   "OpenUri",
            "name":    "Visit site now",
            "targets": [{"os": "default", "uri": url}],
        }],
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"  {_R}{_B}Emergency Teams alert sent for {domain}.{_X}")
    except Exception as exc:
        print(f"  {_R}Emergency Teams alert failed: {exc}{_X}")


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

    alerts = [a for a in worst.values() if a["severity"] in ("RED", "YELLOW")]
    all_clear = len(alerts) == 0

    has_red   = any(a["severity"] == "RED" for a in alerts)
    color     = "00AA00" if all_clear else ("FF0000" if has_red else "FFA500")
    title_str = (
        f"DF SentryBot — All clear ({len(worst)} domain(s) checked)"
        if all_clear else
        f"DF SentryBot — {len(alerts)} domain(s) need attention"
    )

    sections = []
    if all_clear:
        sections.append({"text": f"No issues detected in the past {report_hours} hour(s). "
                                 f"All {len(worst)} monitored domain(s) look healthy."})
    else:
        for a in sorted(alerts, key=lambda x: 0 if x["severity"] == "RED" else 1):
            facts = [
                {"name": "Domain",      "value": a["domain"]},
                {"name": "URL",         "value": a["url"]},
                {"name": "HTTP status", "value": str(a.get("status") or "N/A")},
            ]
            cs = a.get("content_sim")
            if cs is not None:
                facts.append({"name": "Content match",
                              "value": f"{cs * 100:.1f}%"})
            if a.get("keywords_found"):
                facts.append({"name": "Hack keywords",
                              "value": ", ".join(a["keywords_found"])})
            if a.get("note"):
                facts.append({"name": "Error", "value": a["note"]})
            sections.append({
                "activityTitle": f"[{a['severity']}]  {a['domain']}",
                "facts":         facts,
            })

    payload = {
        "@type":      "MessageCard",
        "@context":   "https://schema.org/extensions",
        "summary":    title_str,
        "themeColor": color,
        "title":      title_str,
        "text":       f"Report period: last {report_hours} hour(s) — generated {now_iso}",
        "sections":   sections,
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        status_label = "All-clear digest" if all_clear else f"Digest ({len(alerts)} alert(s))"
        print(f"\n  {_G}Teams {status_label} sent.{_X}")
    except Exception as exc:
        print(f"\n  {_R}Teams webhook failed: {exc}{_X}")


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
            url          = f"https://{domain}/"
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
            meta = await fetch_page(page, url)
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

            # Accumulate non-GREEN results for the digest
            if sev in ("RED", "YELLOW"):
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

            all_results.append({"domain": domain, "severity": sev})

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
    if should_report:
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
