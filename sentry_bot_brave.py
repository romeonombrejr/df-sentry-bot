#!/usr/bin/env python3
"""
SEO / Defacement Monitor — DF SentryBot (Brave Search API edition)
Two-request version. Homepage is fetched first via a direct URL query,
then remaining pages via site:<domain>. Audits up to --count pages.

Severity tiers
  GREEN  — Brave meta matches + page alive + content unchanged
  YELLOW — slight Brave mismatch OR content changed since baseline
  RED    — major Brave mismatch, page down, hack keywords found,
           or both meta + content changed

Baseline behaviour
  First run    : visits pages, saves content hashes as baseline, no comparison
  Every 6 hrs  : compares current content against saved baseline
  Every 7 days : refreshes baseline IF no RED alerts in the previous run
                 (skips refresh if RED detected — preserves clean reference)

State is saved to sentry_state.json in the same directory as this script.
Alerts are posted to a Microsoft Teams channel via an Incoming Webhook.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ONE-TIME SETUP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Brave API  — https://api.search.brave.com/
   Sign up, choose the free "Data for Good" plan (2,000 queries/month)

2. Teams webhook — in Teams: channel > ··· > Connectors > Incoming Webhook
   Copy the generated URL.

3. Environment variables:
   PowerShell : $env:BRAVE_API_KEY="your_key_here"
                $env:TEAMS_WEBHOOK_URL="https://your-org.webhook.office.com/..."
   CMD        : set BRAVE_API_KEY=your_key_here
                set TEAMS_WEBHOOK_URL=https://your-org.webhook.office.com/...
   Linux/cron : export BRAVE_API_KEY=your_key_here
                export TEAMS_WEBHOOK_URL=https://...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python sentry_bot_brave.py cloudway.com
  python sentry_bot_brave.py cloudway.com site2.com --count 5
  python sentry_bot_brave.py --domains-file clients.txt --count 5
  python sentry_bot_brave.py cloudway.com --teams-webhook WEBHOOK_URL
  python sentry_bot_brave.py cloudway.com --api-key YOUR_KEY --headless

  clients.txt format (one domain per line, # for comments):
    cloudway.com
    anothersite.com
    # this line is ignored

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
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

load_dotenv(Path(__file__).parent / ".env")

# ── Tunable constants ─────────────────────────────────────────────────────────
THRESHOLD_GREEN       = 0.95
THRESHOLD_YELLOW      = 0.50
BASELINE_REFRESH_DAYS = 7

# ── Hack-indicator keywords (body text scan) ──────────────────────────────────
# Common patterns injected by pharma hacks, SEO spam, and defacement scripts.
HACK_KEYWORDS = [
    # Defacement signatures
    "hacked by", "defaced by", "owned by", "this site has been hacked",
    # Pharma spam (most common WordPress hack)
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

# ── State file (lives next to this script) ────────────────────────────────────
STATE_FILE = Path(__file__).parent / "sentry_state.json"

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

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# TLD → (country code, search language) for Brave API localisation.
# Generic TLDs (.com, .org, etc.) are intentionally absent — no country
# filter gives broader results for international domains.
_TLD_LOCALE: dict[str, tuple[str, str]] = {
    "no": ("NO", "nb"),  # Norway / Norwegian Bokmål
    "as": ("NO", "nb"),  # Norwegian company suffix (AS = Aksjeselskap)
    "se": ("SE", "sv"),  # Sweden / Swedish
    "dk": ("DK", "da"),  # Denmark / Danish
    "fi": ("FI", "fi"),  # Finland / Finnish
    "de": ("DE", "de"),  # Germany / German
    "fr": ("FR", "fr"),  # France / French
    "nl": ("NL", "nl"),  # Netherlands / Dutch
    "uk": ("GB", "en"),  # United Kingdom / English
    "us": ("US", "en"),  # United States / English
    "au": ("AU", "en"),  # Australia / English
    "ca": ("CA", "en"),  # Canada / English
}


def _locale_params(domain: str) -> dict:
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld in _TLD_LOCALE:
        country, lang = _TLD_LOCALE[tld]
        return {"country": country, "search_lang": lang}
    return {}


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


# ── Teams alert ───────────────────────────────────────────────────────────────

def send_teams_alert(webhook_url: str, domain: str,
                     results: list[dict], now_iso: str):
    if not webhook_url:
        return
    alerts = [r for r in results if r["severity"] in ("RED", "YELLOW")]
    if not alerts:
        return

    has_red = any(r["severity"] == "RED" for r in alerts)
    color   = "FF0000" if has_red else "FFA500"

    sections = []
    for r in alerts:
        facts = [
            {"name": "URL",         "value": r["url"]},
            {"name": "HTTP status", "value": str(r["status"] or "N/A")},
            {"name": "Title match", "value": f"{r['title_sim'] * 100:.1f}%"},
            {"name": "Desc match",  "value": f"{r['desc_sim']  * 100:.1f}%"},
        ]
        cs = r.get("content_sim")
        if cs is not None and cs < THRESHOLD_GREEN:
            facts.append({"name": "Content match",
                          "value": f"{cs * 100:.1f}%  (baseline: {r.get('baseline_date', '')})"  })
        if r.get("keywords_found"):
            facts.append({"name": "Hack keywords",
                          "value": ", ".join(r["keywords_found"])})
        if r.get("note"):
            facts.append({"name": "Error", "value": r["note"]})

        sections.append({
            "activityTitle": f"[{r['severity']}]  {r['url']}",
            "facts":         facts,
        })

    payload = {
        "@type":      "MessageCard",
        "@context":   "https://schema.org/extensions",
        "summary":    f"DF SentryBot — {len(alerts)} alert(s) on {domain}",
        "themeColor": color,
        "title":      f"DF SentryBot — {len(alerts)} alert(s) on {domain}",
        "text":       f"Audit run: {now_iso}",
        "sections":   sections,
        "potentialAction": [{
            "@type":   "OpenUri",
            "name":    "Visit site",
            "targets": [{"os": "default", "uri": f"https://{domain}"}],
        }],
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"\n  {_G}Teams alert sent ({len(alerts)} item(s)).{_X}")
    except Exception as exc:
        print(f"\n  {_R}Teams webhook failed: {exc}{_X}")


# ── Domain helpers ────────────────────────────────────────────────────────────

def belongs_to(url: str, domain: str) -> bool:
    try:
        host   = urlparse(url).netloc.lower().removeprefix("www.")
        target = domain.lower().removeprefix("www.")
        return host == target
    except Exception:
        return False


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


def classify(title_sim: float, desc_sim: float, status: int,
             content_sim: float | None = None,
             keywords_found: list | None = None) -> str:
    if status == 0 or status >= 400:
        return "RED"
    if keywords_found:
        return "RED"

    avg        = (title_sim + desc_sim) / 2
    # content_sim is None on first run — treat as "intact" until we have a baseline
    content_ok = content_sim is None or content_sim >= THRESHOLD_GREEN

    if content_ok:
        # Content is intact — meta drift alone is at most YELLOW (likely an SEO update)
        return "GREEN" if avg >= THRESHOLD_GREEN else "YELLOW"

    # Content has changed — now weight meta similarity too
    if content_sim < THRESHOLD_YELLOW or avg < THRESHOLD_YELLOW:
        return "RED"
    return "YELLOW"


def severity_colour(level: str) -> str:
    return {"GREEN": _G, "YELLOW": _Y, "RED": _R}.get(level, _X)


def meter(ratio: float, width: int = 28) -> str:
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


# ── Brave Search (two-request — homepage guaranteed first) ────────────────────

def brave_search(domain: str, api_key: str, count: int = 1) -> list[dict]:
    """
    Two-step search to guarantee homepage is always first.
      Request 1 — '{domain}'          (count=1)    : homepage result
      Request 2 — 'site:{domain}'     (count=count) : subpages (skipped if count=1)
    Uses 1 API query when count=1, 2 queries otherwise.
    """
    headers = {
        "Accept":               "application/json",
        "Accept-Encoding":      "gzip",
        "X-Subscription-Token": api_key,
    }
    loc = _locale_params(domain)

    def fetch(q: str, n: int) -> list:
        resp = requests.get(
            BRAVE_SEARCH_URL,
            headers=headers,
            params={"q": q, "count": n, **loc},
            timeout=15,
        )
        if resp.status_code == 401:
            raise RuntimeError("Invalid API key — check your BRAVE_API_KEY.")
        if resp.status_code == 429:
            raise RuntimeError(
                "Rate limit hit — monthly free quota (2,000 queries) may be exhausted.")
        resp.raise_for_status()
        return resp.json().get("web", {}).get("results", [])

    seen: set[str] = set()
    results: list[dict] = []

    def add(url: str, title: str, description: str):
        if not url.startswith("http") or not belongs_to(url, domain) or url in seen:
            return
        seen.add(url)
        results.append({"title": title, "url": url, "description": description})

    # Request 1: bare domain query — returns homepage as top result
    for item in fetch(domain, 1):
        add(item.get("url", "").strip(),
            item.get("title", "").strip(),
            item.get("description", "").strip())

    # Request 2: site: query for remaining pages
    if count > 1:
        for item in fetch(f"site:{domain}", count):
            add(item.get("url", "").strip(),
                item.get("title", "").strip(),
                item.get("description", "").strip())
            for btn in item.get("deep_results", {}).get("buttons", []):
                add(btn.get("url", "").strip(),
                    btn.get("title", "").strip(),
                    "")

    return results[:count]


# ── Per-URL meta + content fetch ──────────────────────────────────────────────

async def fetch_meta(page, url: str) -> dict:
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

        # Scan for hack-indicator keywords in full body text
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


# ── Output ────────────────────────────────────────────────────────────────────

SEP  = "═" * 64
DASH = "─" * 64


def print_result(r: dict, idx: int, total: int):
    sev = r["severity"]
    col = severity_colour(sev)

    print(f"\n{col}{_B}[ {sev} ]{_X}  {idx}/{total}  {r['url']}")
    print(f"  HTTP status : {r['status'] or 'N/A'}")

    ts = r["title_sim"]
    ds = r["desc_sim"]

    b_title = r["brave_title"] or f"{_D}(none){_X}"
    a_title = r["actual_title"] or f"{_D}(none){_X}"
    b_desc  = r["brave_desc"]  or f"{_D}(none){_X}"
    a_desc  = r["actual_desc"] or f"{_D}(none){_X}"

    if r.get("unindexed"):
        print(f"\n  {_Y}⚠  Not indexed by Brave Search — index comparison skipped{_X}")
        print(f"\n  {_B}Title (live){_X}")
        print(f"    Actual  : {a_title}")
        print(f"\n  {_B}Description (live){_X}")
        print(f"    Actual  : {a_desc}")
    else:
        print(f"\n  {_B}Title{_X}")
        print(f"    Brave   : {b_title}")
        print(f"    Actual  : {a_title}")
        print(f"    Match   : {meter(ts)}  {ts * 100:5.1f} %")

        print(f"\n  {_B}Description{_X}")
        print(f"    Brave   : {b_desc}")
        print(f"    Actual  : {a_desc}")
        print(f"    Match   : {meter(ds)}  {ds * 100:5.1f} %")

    print(f"\n  {_B}Content (headings & paragraphs){_X}")
    cs = r.get("content_sim")
    bd = r.get("baseline_date", "")
    if cs is None:
        print(f"    Status  : {_D}First run — baseline saved{_X}")
    else:
        col   = _G if cs >= THRESHOLD_GREEN else (_Y if cs >= THRESHOLD_YELLOW else _R)
        label = "Unchanged" if cs >= THRESHOLD_GREEN else "Changed"
        print(f"    Match   : {meter(cs)}  {cs * 100:5.1f} %")
        print(f"    Status  : {col}{label} since baseline ({bd}){_X}")

    kw = r.get("keywords_found")
    if kw:
        print(f"\n  {_R}{_B}Hack keywords detected:{_X}")
        for word in kw:
            print(f"    {_R}• {word}{_X}")

    if r["note"]:
        print(f"\n  {_R}! {r['note']}{_X}")


def print_summary(results: list[dict], is_first_run: bool,
                  refreshed: bool, baseline_date: str):
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    for r in results:
        counts[r["severity"]] += 1
    total = len(results)

    print(f"\n{SEP}")
    print(f"{_B}SUMMARY  —  {total} page(s) audited{_X}")
    print(DASH)
    print(f"  {_G}{_B}●  Green   exact / near-exact match  : {counts['GREEN']:>3}{_X}")
    print(f"  {_Y}{_B}●  Yellow  partial change             : {counts['YELLOW']:>3}{_X}")
    print(f"  {_R}{_B}●  Red     major change / broken      : {counts['RED']:>3}{_X}")
    print(DASH)

    if is_first_run:
        print(f"  Baseline : {_D}First run — baseline saved today{_X}")
    elif refreshed:
        print(f"  Baseline : Refreshed today (previous was {baseline_date})")
    else:
        print(f"  Baseline : Set {baseline_date}  "
              f"(refreshes every {BASELINE_REFRESH_DAYS} days if no RED)")

    print(SEP)


# ── Orchestration ─────────────────────────────────────────────────────────────

async def run(domain: str, api_key: str, count: int, headless: bool,
              teams_webhook: str = ""):
    now     = datetime.now()
    now_iso = now.isoformat(timespec="seconds")

    print(f"\n{SEP}")
    print(f"{_B}  DF SentryBot (Brave)  —  {domain}  [{count} page(s)]{_X}")
    print(f"  Run time : {now_iso}")
    print(SEP)

    # ── Load state ────────────────────────────────────────────────────────
    state        = load_state()
    domain_state = state.setdefault("domains", {}).setdefault(domain, {})
    pages_state  = domain_state.setdefault("pages", {})

    is_first_run = "baseline_set" not in domain_state

    refreshed     = False
    baseline_date = domain_state.get("baseline_set", "")[:10]
    if not is_first_run:
        age_days     = (now - datetime.fromisoformat(domain_state["baseline_set"])).days
        last_had_red = domain_state.get("last_run_had_red", False)
        if age_days >= BASELINE_REFRESH_DAYS and not last_had_red:
            refreshed = True
            print(f"  {_Y}Baseline is {age_days} days old and last run was clean "
                  f"— will refresh after this audit.{_X}\n")

    # ── 1. Brave search ───────────────────────────────────────────────────
    print(f"\n  [1/2]  Fetching Brave index data for {domain} …")
    try:
        hits = brave_search(domain, api_key, count)
    except RuntimeError as exc:
        print(f"\n{_R}  {exc}{_X}\n")
        return
    except Exception as exc:
        print(f"\n{_R}  Request failed: {exc}{_X}\n")
        return

    if not hits:
        print(
            f"         Not indexed by Brave — falling back to direct homepage audit.\n"
        )
        hits = [{
            "url":         f"https://{domain}/",
            "title":       "",
            "description": "",
            "unindexed":   True,
        }]

    print(f"         Found {len(hits)} result(s).\n")

    # ── 2. Audit each URL ─────────────────────────────────────────────────
    print(f"  [2/2]  Auditing each URL …\n")
    results = []

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

        for i, hit in enumerate(hits, 1):
            url = hit["url"]
            print(f"         [{i:>2}/{len(hits)}]  {url}")
            meta = await fetch_meta(page, url)
            await page.wait_for_timeout(700)

            ts  = similarity(hit["title"],       meta["title"])
            ds  = similarity(hit["description"], meta["description"])

            saved              = pages_state.get(url, {})
            baseline_content   = saved.get("baseline_content") if not is_first_run else None
            page_baseline_date = saved.get("baseline_set", "")[:10]

            if baseline_content is not None and not is_first_run:
                content_sim = similarity(baseline_content, meta["content_text"])
            else:
                content_sim = None

            kw_found     = meta.get("keywords_found", [])
            is_unindexed = hit.get("unindexed", False)

            if is_unindexed:
                sev = "RED" if (meta["status"] == 0 or meta["status"] >= 400) else "YELLOW"
            else:
                sev = classify(ts, ds, meta["status"], content_sim, kw_found)

            note = meta["note"]
            if is_unindexed:
                note = "Not indexed by Brave Search" + (f" — {note}" if note else "")

            results.append({
                "url":             url,
                "brave_title":     hit["title"],
                "brave_desc":      hit["description"],
                "actual_title":    meta["title"],
                "actual_desc":     meta["description"],
                "status":          meta["status"],
                "content_text":    meta["content_text"],
                "content_sim":     content_sim,
                "baseline_date":   page_baseline_date,
                "title_sim":       ts,
                "desc_sim":        ds,
                "severity":        sev,
                "keywords_found":  kw_found,
                "unindexed":       is_unindexed,
                "note":            note,
            })

        await browser.close()

    # ── Update state ──────────────────────────────────────────────────────
    had_red = any(r["severity"] == "RED" for r in results)

    for r in results:
        url        = r["url"]
        page_entry = pages_state.setdefault(url, {})

        page_entry["last_content"] = r["content_text"]
        page_entry["last_seen"]    = now_iso

        if is_first_run or refreshed or "baseline_content" not in page_entry:
            page_entry["baseline_content"] = r["content_text"]
            page_entry["baseline_set"]     = now_iso
            if is_first_run or "baseline_content" not in page_entry:
                r["baseline_date"] = now_iso[:10]

    if is_first_run or refreshed:
        domain_state["baseline_set"] = now_iso

    domain_state["first_run"]        = domain_state.get("first_run", now_iso)
    domain_state["last_run"]         = now_iso
    domain_state["last_run_had_red"] = had_red
    state["last_run"]                = now_iso

    save_state(state)

    # ── Print results ─────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print(f"{_B}  DETAILED RESULTS{_X}")
    print(SEP)

    for i, r in enumerate(results, 1):
        print_result(r, i, len(results))

    print_summary(results, is_first_run, refreshed, baseline_date)

    # ── Teams alert ───────────────────────────────────────────────────────
    send_teams_alert(teams_webhook, domain, results, now_iso)


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args(args: list[str]) -> dict:
    api_key       = os.environ.get("BRAVE_API_KEY", "")
    teams_webhook = os.environ.get("TEAMS_WEBHOOK_URL", "")
    headless      = False
    count         = 10
    domains: list[str] = []
    domains_file  = None

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--headless":
            headless = True
        elif a == "--api-key" and i + 1 < len(args):
            api_key = args[i + 1]; i += 1
        elif a == "--count" and i + 1 < len(args):
            count = int(args[i + 1]); i += 1
        elif a == "--teams-webhook" and i + 1 < len(args):
            teams_webhook = args[i + 1]; i += 1
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
        "api_key":       api_key,
        "count":         count,
        "headless":      headless,
        "teams_webhook": teams_webhook,
    }


def main():
    parsed = parse_args(sys.argv[1:])

    if not parsed["domains"]:
        print(__doc__)
        sys.exit(1)

    if not parsed["api_key"]:
        print(
            f"\n{_R}  Missing API key.{_X}\n"
            "  Set it as an environment variable:\n"
            "    PowerShell : $env:BRAVE_API_KEY='your_key_here'\n"
            "    CMD        : set BRAVE_API_KEY=your_key_here\n\n"
            "  Or pass it directly:\n"
            "    python sentry_bot_brave.py cloudway.com --api-key YOUR_KEY\n"
        )
        sys.exit(1)

    for domain in parsed["domains"]:
        asyncio.run(run(
            domain,
            parsed["api_key"],
            parsed["count"],
            parsed["headless"],
            parsed["teams_webhook"],
        ))


if __name__ == "__main__":
    main()
