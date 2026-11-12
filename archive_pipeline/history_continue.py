"""Continue Facebook page-history scrapes past Apify's monthly spending limit.

usage: history_continue.py        (run_all.sh starts it; safe to re-run; log: logs/fb_continue.log)

For every [[facebook.pages]] entry in the project config, the first run is raw/<dataset>.json
(run_all.sh starts it; `older_than` optionally skips recent posts collected elsewhere). The
account's monthly Apify limit can clamp that run's cap below the page's `budget_usd`. When a run
stops at its cap, this:
  1. waits for it to finish and be saved,
  2. waits for Apify's monthly usage cycle to reset if the account has no room left,
  3. starts <dataset>2, 3, ... each scraping posts older than the oldest collected so far (one day of
     overlap; posts are de-duplicated by id), until a run finishes under its cap (= reached the start
     of the page), the page's budget_usd is used up, or a run collects nothing older;
     fetch_apify.py additionally keeps total spend <= [apify] total_budget_usd,
  4. downloads the new posts' media immediately (Facebook photo links expire within days),
  5. once run_all.sh has finished, transcribes/OCRs the new media, re-indexes and re-packages.
Every step is idempotent: raw/<name>.run.json makes a re-run resume a live Apify run instead of
paying for a new one.
"""
import json, os, subprocess, sys, time
from datetime import date, datetime, timedelta, timezone
import httpx
from common import CFG, HERE, LOGS, ROOT, log

PY = str(HERE / ".venv" / "bin" / "python")
DONE = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT")
CAP_SLACK = 0.10  # a run within 10 cents of its cap stopped because of the cap
ACTOR = CFG["facebook"]["actor"]


def say(msg):
    log(f"[fb_continue] {msg}")


def token():
    from fetch_apify import token as t
    return t()


def others_running(pattern):
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split() if int(p) != os.getpid()]


def wait(cond, what, every=60, note_every=900):
    t0 = last = time.time()
    while not cond():
        if time.time() - last >= note_every:
            say(f"still waiting: {what} ({(time.time() - t0) / 3600:.1f} h so far)")
            last = time.time()
        time.sleep(every)


def run_record(name):
    f = ROOT / "raw" / f"{name}.run.json"
    return json.loads(f.read_text()) if f.exists() else None


class Page:
    def __init__(self, cfg):
        self.url, self.base, self.budget = cfg["url"], cfg["dataset"], float(cfg["budget_usd"])

    def run_names(self):
        """<dataset>, <dataset>2, <dataset>3, ... in order."""
        out, i = [self.base], 2
        while (ROOT / "raw" / f"{self.base}{i}.run.json").exists():
            out.append(f"{self.base}{i}")
            i += 1
        return out

    def next_name(self):
        return f"{self.base}{len(self.run_names()) + 1}"

    def oldest_date(self):
        ds = []
        for n in self.run_names():
            f = ROOT / "raw" / f"{n}.json"
            if f.exists():
                ds += [p["time"][:10] for p in json.loads(f.read_text()) if p.get("time")]
        return min(ds) if ds else None

    def spent(self):
        return sum((run_record(n) or {}).get("usageTotalUsd") or 0 for n in self.run_names())


def finished(name):
    r = run_record(name)
    return bool(r and r.get("status") in DONE and (ROOT / "raw" / f"{name}.json").exists())


def hit_cap(r):
    cap = (r.get("options") or {}).get("maxTotalChargeUsd") or 0
    return r["status"] != "SUCCEEDED" or (cap and (r.get("usageTotalUsd") or 0) >= cap - CAP_SLACK)


def apify_limits():
    d = httpx.get("https://api.apify.com/v2/users/me/limits", params={"token": token()}, timeout=60).json()["data"]
    end = datetime.fromisoformat(d["monthlyUsageCycle"]["endAt"].replace("Z", "+00:00"))
    room = d["limits"]["maxMonthlyUsageUsd"] - d["current"]["monthlyUsageUsd"]
    return end, room


def scrape(page: Page):
    """Returns True when this page got continuation data (now or in an earlier invocation)."""
    wait(lambda: finished(page.base), f"first run {page.base} to finish and be saved")
    names = page.run_names()
    last = run_record(names[-1])
    if not finished(names[-1]):  # a continuation was started earlier and is still live: resume it
        name = names[-1]
    elif not hit_cap(last):
        say(f"{names[-1]} finished under its cap (${last.get('usageTotalUsd', 0):.2f}): {page.url} history is complete")
        return len(names) > 1
    else:
        name = page.next_name()
    while True:
        older = None
        if not run_record(name):
            left = page.budget - page.spent()
            if left < 0.5:
                say(f"{page.base}: budget used up (${page.spent():.2f} of ${page.budget:.0f}); stopping. Oldest post: {page.oldest_date()}")
                return True
            end, room = apify_limits()
            if room < 0.5:  # monthly limit reached: wait for the cycle to reset
                resume_at = end + timedelta(minutes=5)
                say(f"Apify monthly room ${room:.2f}; waiting for the usage cycle to reset at {resume_at:%Y-%m-%d %H:%M} UTC")
                wait(lambda: datetime.now(timezone.utc) >= resume_at, f"Apify monthly reset at {resume_at:%H:%M} UTC")
                end, room = apify_limits()
            cap = round(min(left, room), 2)
            if not page.oldest_date():
                say(f"{page.base}: no posts to continue from; stopping")
                return False
            older = (date.fromisoformat(page.oldest_date()) + timedelta(days=1)).isoformat()  # one day of overlap
            inp = {"startUrls": [{"url": page.url}], "resultsLimit": 20000, "captionText": True, "onlyPostsOlderThan": older}
            say(f"starting {name}: posts older than {older}, cap ${cap:.2f} (budget left ${left:.2f}, monthly room ${room:.2f})")
        else:
            say(f"resuming {name}")
            inp, cap = {}, 0
        with (LOGS / "fb_continue.log").open("a") as lf:
            rc = subprocess.run([PY, "fetch_apify.py", name, ACTOR, str(cap or 1), json.dumps(inp)],
                                cwd=HERE, stdout=lf, stderr=subprocess.STDOUT).returncode
        r = run_record(name)
        if rc != 0 or not r:
            say(f"{name}: fetch_apify exited {rc}; stopping (re-run to resume)")
            return True
        say(f"{name}: {r['status']} ${r.get('usageTotalUsd', 0):.2f}, oldest post now {page.oldest_date()}")
        fetch_media()  # photo links expire within days: download right after every run
        if not hit_cap(r):
            say(f"reached the start of {page.url}")
            return True
        if older and page.oldest_date() >= (date.fromisoformat(older) - timedelta(days=1)).isoformat():
            say(f"{name} collected nothing older than {page.oldest_date()}; stopping instead of retrying (check the run on Apify)")
            return True
        name = page.next_name()


def fetch_media():
    wait(lambda: not others_running(r"fetch_media\.py"), "other media downloads to finish (avoid two writers)", every=30)
    say("downloading media for the new posts")
    with (LOGS / "fb_history2.log").open("a") as lf:
        subprocess.run(["nice", "-n", "10", PY, "fetch_media.py", "--platform", "fb"], cwd=HERE, stdout=lf, stderr=subprocess.STDOUT)


def reprocess():
    wait(lambda: not others_running(r"run_all\.sh"), "run_all.sh to finish before re-indexing")
    wait(lambda: not others_running(r"transcribe\.py|ocr\.py|build_index\.py|package\.py"), "other processing to finish")
    for script, lf in (("transcribe.py", "transcribe.log"), ("ocr.py", "ocr.log")):
        say(f"{script} on the new media")
        with (LOGS / lf).open("a") as f:
            subprocess.run(["nice", "-n", "10", PY, script], cwd=HERE, stdout=f, stderr=subprocess.STDOUT)
    for script, lf in (("build_index.py", "index.log"), ("package.py", "package.log")):
        say(script)
        with (LOGS / lf).open("a") as f:
            subprocess.run([PY, script], cwd=HERE, stdout=f, stderr=subprocess.STDOUT)


def main():
    if others_running(r"(fb_)?history_continue\.py"):
        print("a history continuation is already running", file=sys.stderr)
        sys.exit(0)
    pages = [Page(p) for p in CFG["facebook"]["pages"]]
    if not pages or not CFG["apify"]["enabled"]:
        return
    subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())])
    say(f"=== start (pid {os.getpid()}), {len(pages)} page(s)")
    got = False
    for page in pages:
        got = scrape(page) or got
    if got:
        reprocess()
    say("=== done")


if __name__ == "__main__":
    main()
