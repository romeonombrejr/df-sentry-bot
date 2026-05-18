#!/usr/bin/env python3
"""
DF SentryBot — local test server
Simulates a normal business site that can be "hacked" on demand.

Usage:
  1. pip install flask   (if not already installed)
  2. python test_server.py
  3. Add  localhost:5000  to domains.txt
  4. Run the bot (first run — saves clean baseline)
  5. Open http://localhost:5000/toggle in your browser to inject hack content
  6. Run the bot again — should flag RED (hack keywords + content change)
  7. Open http://localhost:5000/toggle again to restore the clean page
"""

from flask import Flask, redirect, url_for

app = Flask(__name__)

_hacked = False

CLEAN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Cloudway Digital — Cloud Solutions</title>
  <meta name="description" content="Cloudway Digital helps businesses move to the cloud with expert guidance and support.">
</head>
<body>
  <h1>Welcome to Cloudway Digital</h1>
  <h2>Your trusted cloud partner</h2>
  <p>We help businesses of all sizes migrate to the cloud securely and efficiently.</p>
  <p>Our team of certified engineers has over 10 years of experience in cloud infrastructure.</p>
  <h2>Our Services</h2>
  <p>Cloud migration, DevOps consulting, managed infrastructure, and 24/7 support.</p>
  <h2>Contact Us</h2>
  <p>Get in touch with our team today to start your cloud journey.</p>
</body>
</html>"""

HACKED_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Buy Cheap Viagra Online — Best Prices</title>
  <meta name="description" content="Buy cheap viagra online. Best online pharmacy prices guaranteed.">
</head>
<body>
  <h1>Hacked by PharmaCrew</h1>
  <h2>Buy Viagra Online — Lowest Prices</h2>
  <p>Buy cheap viagra and cialis from our online pharmacy. No prescription needed.</p>
  <p>This site has been hacked. Buy pills online at discount prices.</p>
  <h2>Online Casino Games</h2>
  <p>Play casino games and sports betting — win big today!</p>
</body>
</html>"""


@app.route("/")
def index():
    return HACKED_PAGE if _hacked else CLEAN_PAGE


@app.route("/toggle")
def toggle():
    global _hacked
    _hacked = not _hacked
    status = "HACKED" if _hacked else "CLEAN"
    color  = "red" if _hacked else "green"
    return f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;padding:2rem;">
  <h2 style="color:{color}">Page is now: {status}</h2>
  <p><a href="/">View homepage</a></p>
  <p><a href="/toggle">Toggle again</a></p>
  <p>Now run the bot to see the result.</p>
</body></html>"""


if __name__ == "__main__":
    print("\n  DF SentryBot — Test Server")
    print("  ──────────────────────────────────────────")
    print("  Homepage : http://localhost:5000/")
    print("  Toggle   : http://localhost:5000/toggle")
    print()
    print("  Steps:")
    print("  1. Make sure  localhost:5000  is in domains.txt")
    print("  2. Run the bot (first run — saves clean baseline)")
    print("  3. Open http://localhost:5000/toggle to inject hack content")
    print("  4. Run the bot again — should flag RED")
    print("  ──────────────────────────────────────────\n")
    app.run(port=5000, debug=False)
