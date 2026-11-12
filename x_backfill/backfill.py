#!/usr/bin/env python3
"""Standalone historical X/Twitter text backfill.

Stdlib only. Uses the bundled twitter_normalizer.py for field mapping, stores
slim text-only records (no media, no raw blob) and talks to Apify over plain HTTP.

Usage:
    python3 backfill.py --handle example_handle --since 2017-01-01
    python3 backfill.py --handle example_handle --estimate
    python3 backfill.py --handle example_handle --max-usd 20 --concurrency 6
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]  # kit root (holds .env)
TOOL_DIR = Path(__file__).resolve().parent

ACTOR_ID = "CJdippxWmn9uRfooo"  # kaitoeasyapi/twitter-x-data-tweet-scraper-...
API = "https://api.apify.com/v2"
PRICE_PER_ITEM_USD = 0.00022  # pay-per-event, BRONZE tier (STARTER plan)

# ---------------------------------------------------------------- normalizer


def _load_normalizer():
    """Import the bundled twitter normalizer by path."""
    path = TOOL_DIR / "twitter_normalizer.py"
    spec = importlib.util.spec_from_file_location("_sm_twitter_norm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.normalize_twitter


normalize_twitter = _load_normalizer()


class _SourceShim:
    """Minimal stand-in for the ORM Source the normalizer expects."""

    def __init__(self, username: str):
        self.username = username


# ---------------------------------------------------------------- env / http


def load_token() -> str:
    tok = os.environ.get("APIFY_API_TOKEN", "")
    if not tok:
        env = REPO_ROOT / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("APIFY_API_TOKEN="):
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not tok:
        sys.exit("APIFY_API_TOKEN not found in environment or the kit .env")
    return tok


def _http(method: str, url: str, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


def http_retry(method: str, url: str, payload=None, timeout=120, tries=5):
    last = None
    for attempt in range(tries):
        try:
            return _http(method, url, payload, timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt + random.random() * 2))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(min(60, 2 ** attempt + random.random() * 2))
    raise RuntimeError(f"HTTP failed after {tries} tries: {method} {url} :: {last}")


# ---------------------------------------------------------------- actor calls


def run_actor(token: str, run_input: dict, log) -> list[dict]:
    """Start an actor run asynchronously, poll to completion, return items."""
    run = http_retry(
        "POST", f"{API}/acts/{ACTOR_ID}/runs?token={token}&timeout=900&memory=1024",
        payload=run_input,
    )["data"]
    run_id, ds_id = run["id"], run["defaultDatasetId"]

    deadline = time.time() + 1200
    status = run["status"]
    while status in ("READY", "RUNNING") and time.time() < deadline:
        time.sleep(4)
        status = http_retry("GET", f"{API}/actor-runs/{run_id}?token={token}")["data"]["status"]

    if status != "SUCCEEDED":
        log(f"  ! run {run_id} ended {status}")
        if status in ("READY", "RUNNING"):
            try:
                http_retry("POST", f"{API}/actor-runs/{run_id}/abort?token={token}")
            except Exception:
                pass

    items = http_retry(
        "GET", f"{API}/datasets/{ds_id}/items?token={token}&clean=true&format=json",
        timeout=180,
    )
    return items if isinstance(items, list) else []


# ---------------------------------------------------------------- date utils


def month_windows(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    out, cur = [], start.replace(day=1)
    while cur < end:
        nxt = (cur.replace(day=28) + timedelta(days=8)).replace(day=1)
        out.append((cur.strftime("%Y-%m"), cur, min(nxt, end)))
        cur = nxt
    return out


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d_%H:%M:%S_UTC")


# ---------------------------------------------------------------- record slim


SNOWFLAKE_EPOCH_MS = 1288834974657  # Twitter snowflake epoch


def ts_from_id(tweet_id) -> datetime | None:
    """Derive creation time from a snowflake tweet ID (exact, offline).

    Belt-and-braces: if upstream ever hands us an unparseable date string we
    still get an authoritative timestamp, since the ID embeds one.
    """
    try:
        tid = int(tweet_id)
    except (TypeError, ValueError):
        return None
    if tid < 1:
        return None
    return datetime.fromtimestamp(((tid >> 22) + SNOWFLAKE_EPOCH_MS) / 1000, tz=timezone.utc)


KEEP_RAW = ("isPinned", "possibly_sensitive")


def is_mock(item: dict) -> bool:
    """The actor pads empty result sets with billable 'mock data' rows.

    They carry id == -1 and a vendor notice as the text. Drop them: they are
    not tweets, and they poison dedupe (every one shares the same id).
    """
    try:
        return int(item.get("id", 0)) < 1
    except (TypeError, ValueError):
        return True


def slim(item: dict, handle: str) -> dict | None:
    if is_mock(item):
        return None
    n = normalize_twitter(item, _SourceShim(handle))
    if not n:
        return None
    ts = n.get("post_timestamp") or ts_from_id(n["platform_post_id"])
    pd = n.get("platform_data", {})
    eng = n.get("engagement", {})
    author = item.get("author") or {}
    return {
        "id": n["platform_post_id"],
        "url": n["post_url"],
        "created_at": ts.astimezone(timezone.utc).isoformat() if ts else None,
        "text": n.get("text_content") or "",
        "lang": pd.get("language"),
        "username": n.get("source_username"),
        "author_name": author.get("name"),
        "likes": eng.get("likes", 0),
        "retweets": eng.get("retweets", 0),
        "replies": eng.get("replies", 0),
        "quotes": eng.get("quotes", 0),
        "bookmarks": eng.get("bookmarks", 0),
        "views": eng.get("views", 0),
        "is_reply": pd.get("is_reply"),
        "is_quote": pd.get("is_quote"),
        "is_retweet": pd.get("is_retweet"),
        "conversation_id": pd.get("conversation_id"),
        "reply_to_id": pd.get("reply_to_id"),
        "reply_to_user": pd.get("reply_to_user") or item.get("inReplyToUsername"),
        "hashtags": pd.get("hashtags") or [],
        "mentions": pd.get("mentions") or [],
        "links": [u.get("expanded_url") for u in (pd.get("urls") or []) if u.get("expanded_url")],
        "source_app": pd.get("source_app"),
        "quoted_text": ((item.get("quoted_tweet") or {}).get("text") or None),
    }


# ---------------------------------------------------------------- backfiller


class Backfill:
    def __init__(self, args, token: str):
        self.a = args
        self.token = token
        self.out_dir = Path(args.out) if args.out else TOOL_DIR / "data" / args.handle
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.out_dir / "tweets.jsonl"
        self.state_path = self.out_dir / "state.json"
        self.log_path = self.out_dir / "run.log"

        self.lock = threading.Lock()
        self.seen: set[str] = set()
        self.items_charged = 0
        self.state = {"handle": args.handle, "windows": {}, "items_charged": 0}
        self._load()

    # -- persistence
    def _load(self):
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            self.items_charged = self.state.get("items_charged", 0)
        if self.jsonl.exists():
            with self.jsonl.open() as f:
                for line in f:
                    try:
                        self.seen.add(json.loads(line)["id"])
                    except Exception:
                        continue
        if self.seen:
            self.log(f"resumed: {len(self.seen)} tweets on disk, "
                     f"{len(self.state.get('windows', {}))} windows done, "
                     f"${self.items_charged * PRICE_PER_ITEM_USD:.2f} spent")

    def _save_state(self):
        self.state["items_charged"] = self.items_charged
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.state_path.write_text(json.dumps(self.state, indent=1))

    def log(self, msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.log_path, "a") as f:
            f.write(line + "\n")

    # -- budget
    def budget_left(self) -> float:
        return self.a.max_usd - self.items_charged * PRICE_PER_ITEM_USD

    def can_afford(self, page: int) -> bool:
        return self.budget_left() >= page * PRICE_PER_ITEM_USD

    # -- one window
    def do_window(self, key: str, start: datetime, end: datetime) -> dict:
        page = self.a.page_size
        cursor = None
        wrote = raw_total = 0
        pages = 0

        while True:
            with self.lock:
                if not self.can_afford(page):
                    self.log(f"  budget cap reached, stopping before {key}")
                    return {"count": wrote, "raw": raw_total, "partial": True, "reason": "budget"}

            run_input = {
                "from": self.a.handle,
                "since": fmt(start),
                "until": fmt(end),
                "queryType": "Latest",
                "maxItems": page,
            }
            if cursor:
                run_input["max_id"] = cursor

            items = run_actor(self.token, run_input, self.log)
            pages += 1
            raw = len(items)
            raw_total += raw

            recs, ids, page_ids = [], [], set()
            mock = 0
            for it in items:
                if is_mock(it):
                    mock += 1
                    continue
                r = slim(it, self.a.handle)
                if r and r["id"] and r["id"] not in page_ids:
                    page_ids.add(r["id"])
                    recs.append(r)
                    ids.append(int(r["id"]))

            with self.lock:
                self.items_charged += raw
                fresh = [r for r in recs if r["id"] not in self.seen]
                for r in fresh:
                    self.seen.add(r["id"])
                if fresh:
                    with self.jsonl.open("a") as f:
                        for r in fresh:
                            f.write(json.dumps(r, ensure_ascii=False) + "\n")
                wrote += len(fresh)

            oldest = str(min(ids)) if ids else None
            self.log(f"  {key} p{pages}: raw={raw} new={len(fresh)} total={wrote}"
                     + (f" (dropped {mock} mock)" if mock else ""))

            # exhaustion / no-progress guards
            if raw < page:
                break
            if not fresh or oldest is None or oldest == cursor:
                break
            if pages >= self.a.max_pages:
                self.log(f"  ! {key} hit max-pages={self.a.max_pages}")
                break
            cursor = oldest

        return {"count": wrote, "raw": raw_total, "pages": pages, "partial": False}

    # -- drive
    def run(self, windows):
        todo = [w for w in windows if w[0] not in self.state["windows"]]
        self.log(f"{len(todo)} windows to fetch (of {len(windows)}); "
                 f"budget ${self.budget_left():.2f}")
        if not todo:
            self.log("nothing to do — all windows complete")
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=self.a.concurrency) as ex:
            futs = {ex.submit(self.do_window, k, s, e): k for k, s, e in todo}
            for fut in as_completed(futs):
                key = futs[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    self.log(f"  ! {key} FAILED: {type(exc).__name__}: {exc}")
                    continue
                with self.lock:
                    if not res.get("partial"):
                        self.state["windows"][key] = res
                    self._save_state()
                    spent = self.items_charged * PRICE_PER_ITEM_USD
                self.log(f"DONE {key}: +{res['count']} | archive={len(self.seen)} | ${spent:.2f}")

        self._save_state()
        self.log(f"finished: {len(self.seen)} unique tweets, "
                 f"${self.items_charged * PRICE_PER_ITEM_USD:.2f} spent -> {self.jsonl}")


# ---------------------------------------------------------------- estimate


def estimate(bf: Backfill, windows):
    picks = [w for w in windows if w[0].endswith(("-03", "-09"))]
    step = max(1, len(picks) // bf.a.samples)
    picks = picks[::step][: bf.a.samples]
    bf.log(f"sampling {len(picks)} months at maxItems={bf.a.page_size}")

    counts = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=bf.a.concurrency) as ex:
        futs = {ex.submit(bf.do_window, k, s, e): k for k, s, e in picks}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                counts[k] = fut.result()["count"]
            except Exception as exc:
                bf.log(f"  ! {k}: {exc}")
    bf._save_state()

    if not counts:
        bf.log("no samples returned"); return
    per_month = sum(counts.values()) / len(counts)
    total = per_month * len(windows)
    bf.log("\n--- SAMPLES ---")
    for k in sorted(counts):
        bf.log(f"  {k}: {counts[k]:>5}")
    bf.log("--- ESTIMATE ---")
    bf.log(f"  months in range      : {len(windows)}")
    bf.log(f"  mean tweets/month    : {per_month:.0f}")
    bf.log(f"  projected total      : ~{total:,.0f} tweets")
    bf.log(f"  projected cost       : ~${total * PRICE_PER_ITEM_USD:,.2f} "
           f"(@ ${PRICE_PER_ITEM_USD}/tweet)")
    bf.log(f"  already spent here   : ${bf.items_charged * PRICE_PER_ITEM_USD:.2f}")


# ---------------------------------------------------------------- main


def main():
    p = argparse.ArgumentParser(description="Historical X text backfill (Apify, stdlib only)")
    p.add_argument("--handle", required=True, help="X handle without @")
    p.add_argument("--since", default="2017-01-01")
    p.add_argument("--until", default=None, help="default: today")
    p.add_argument("--page-size", type=int, default=1000, help="maxItems per actor run")
    p.add_argument("--max-pages", type=int, default=20, help="max runs per month window")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-usd", type=float, default=20.0, help="hard spend cap")
    p.add_argument("--out", default=None)
    p.add_argument("--estimate", action="store_true", help="sample months, project cost, exit")
    p.add_argument("--samples", type=int, default=6)
    args = p.parse_args()

    args.handle = args.handle.lstrip("@").strip()
    start = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = (datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
           if args.until else datetime.now(timezone.utc))

    token = load_token()
    bf = Backfill(args, token)
    wins = month_windows(start, end)
    bf.log(f"@{args.handle} | {start:%Y-%m-%d} -> {end:%Y-%m-%d} | {len(wins)} month windows")

    if args.estimate:
        estimate(bf, wins)
    else:
        bf.run(wins)


if __name__ == "__main__":
    main()
