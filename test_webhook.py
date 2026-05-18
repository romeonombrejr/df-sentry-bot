#!/usr/bin/env python3
"""
Quick test — sends a sample Adaptive Card to the TEAMS_WEBHOOK_URL in .env.
Run: python test_webhook.py
"""

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "")

if not webhook_url:
    print("ERROR: TEAMS_WEBHOOK_URL is not set in .env")
    raise SystemExit(1)

payload = {
    "type": "message",
    "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
            "type":    "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "body": [
                {
                    "type":   "TextBlock",
                    "text":   "DF SentryBot — Webhook Test",
                    "weight": "Bolder",
                    "size":   "Medium",
                    "color":  "Good",
                },
                {
                    "type": "TextBlock",
                    "text": "If you can read this, the webhook is working correctly.",
                    "wrap": True,
                },
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": "Status",  "value": "Connected"},
                        {"title": "Format",  "value": "Adaptive Cards 1.5"},
                        {"title": "Webhook", "value": webhook_url[:60] + "..."},
                    ],
                },
            ],
            "actions": [{
                "type":  "Action.OpenUrl",
                "title": "Digital Feet",
                "url":   "https://digitalfeet.no",
            }],
        },
    }],
}

print(f"Sending test card to:\n  {webhook_url[:80]}...\n")

try:
    resp = requests.post(webhook_url, json=payload, timeout=15)
    print(f"HTTP {resp.status_code}")
    if resp.text:
        print(f"Response: {resp.text}")
    resp.raise_for_status()
    print("\nSUCCESS — check your Teams channel for the test card.")
except requests.HTTPError as exc:
    print(f"\nFAILED — HTTP error: {exc}")
    print("Check that the Workflow is still active in Teams.")
except Exception as exc:
    print(f"\nFAILED — {exc}")
