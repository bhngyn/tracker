#!/usr/bin/env python3
"""Check the Apify token and show the account's plan and remaining monthly budget.

Free to run: it starts no actor. Reads APIFY_API_TOKEN from the environment or ./.env.
The token is sent in a header and never printed.
"""
import json, os, sys, urllib.error, urllib.request
from pathlib import Path

API = "https://api.apify.com/v2"


def token() -> str:
    t = os.environ.get("APIFY_API_TOKEN", "")
    env = Path(__file__).resolve().parent / ".env"
    if not t and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("APIFY_API_TOKEN="):
                t = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not t:
        sys.exit("APIFY_API_TOKEN is not set. Put it in .env (see .env.example).")
    return t


def get(path: str, tok: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["data"]


def main():
    tok = token()
    try:
        me = get("/users/me", tok)
        lim = get("/users/me/limits", tok)
    except urllib.error.HTTPError as e:
        sys.exit(f"Apify rejected the token (HTTP {e.code}). Create a new one in the Apify console.")
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"Could not reach Apify: {e}")
    plan = (me.get("plan") or {}).get("id", "?")
    cap = (lim.get("limits") or {}).get("maxMonthlyUsageUsd")
    used = (lim.get("current") or {}).get("monthlyUsageUsd")
    cycle = (lim.get("monthlyUsageCycle") or {}).get("endAt", "?")
    print(f"token OK   account: {me.get('username', '?')}   plan: {plan}")
    if cap is not None and used is not None:
        print(f"this month: ${used:.2f} used of ${cap:.2f}  (${cap - used:.2f} left, resets {str(cycle)[:10]})")


if __name__ == "__main__":
    main()
