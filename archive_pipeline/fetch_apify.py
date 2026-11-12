"""Run an Apify actor with a hard spend cap and save the full dataset to raw/<name>.json.

usage: fetch_apify.py <name> <actor> <max_usd> '<json input>'

Safe to re-run:
- raw/<name>.run.json is written the moment the run starts. If it shows a run that is
  still READY/RUNNING, this resumes polling that run instead of starting (and paying
  for) a second one; if the run finished but the dataset wasn't saved, it just saves it.
- If raw/<name>.json already exists with a finished run, nothing is started.
- Budget guard: the cap is trimmed so that total spend across all raw/*.run.json plus
  this run can't exceed the project's [apify] total_budget_usd. Paid runs are refused
  unless [apify] enabled = true: the config is where a spend approval is recorded.
"""
import json, os, sys, time
from pathlib import Path
import httpx
from common import CFG, ROOT, resolve

API = "https://api.apify.com/v2"
TOTAL_BUDGET_USD = float(CFG["apify"]["total_budget_usd"])
PROBES_USD = float(CFG["apify"]["untracked_spend_usd"])  # spend with no saved run record (probes/tests)
DONE = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT")


def token() -> str:
    var = CFG["apify"]["token_env"]
    t = os.environ.get(var)
    if t:
        return t
    if CFG["apify"]["env_file"]:
        env = resolve(CFG, CFG["apify"]["env_file"])
        for line in env.read_text().splitlines() if env.exists() else []:
            if line.startswith(f"{var}="):
                return line.split("=", 1)[1].strip()
    sys.exit(f"{var} not found (set it in the environment or point [apify] env_file at a .env file)")


def spent_so_far(exclude: Path) -> float:
    tot = PROBES_USD
    for f in (ROOT / "raw").glob("*.run.json"):
        if f != exclude:
            tot += json.loads(f.read_text()).get("usageTotalUsd") or 0
    return tot


def main():
    name, actor, max_usd, inp = sys.argv[1], sys.argv[2], float(sys.argv[3]), json.loads(sys.argv[4])
    tok = token()
    c = httpx.Client(timeout=120)
    out = ROOT / "raw" / f"{name}.json"
    run_f = ROOT / "raw" / f"{name}.run.json"
    run = json.loads(run_f.read_text()) if run_f.exists() else None

    if run and run.get("status") in DONE and out.exists():
        print(f"[{name}] already done: run {run['id']} {run['status']} ${run.get('usageTotalUsd') or 0:.2f}", flush=True)
        return
    if run:
        print(f"[{name}] resuming run {run['id']} (last seen {run.get('status')})", flush=True)
    else:
        if not CFG["apify"]["enabled"]:
            sys.exit(f"[{name}] Apify is disabled for this project ([apify] enabled = false); not starting a paid run")
        room = TOTAL_BUDGET_USD - spent_so_far(run_f)
        if room < 0.5:
            sys.exit(f"[{name}] refusing to start: only ${room:.2f} left of the ${TOTAL_BUDGET_USD:.0f} budget")
        if max_usd > room:
            print(f"[{name}] cap trimmed from ${max_usd:.2f} to ${room:.2f} to stay within ${TOTAL_BUDGET_USD:.0f} total", flush=True)
            max_usd = round(room, 2)
        r = c.post(f"{API}/acts/{actor}/runs", params={"token": tok, "maxTotalChargeUsd": max_usd, "timeout": 36000}, json=inp)
        r.raise_for_status()
        run = r.json()["data"]
        run_f.write_text(json.dumps(run, indent=1))  # record immediately: a re-run resumes instead of paying twice
        print(f"[{name}] run {run['id']} started, cap ${max_usd:.2f}", flush=True)

    while run["status"] not in DONE:
        time.sleep(20)
        try:
            run = c.get(f"{API}/actor-runs/{run['id']}", params={"token": tok}).json()["data"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            print(f"[{name}] poll error {e!r}, retrying", flush=True)
            continue
        run_f.write_text(json.dumps(run, indent=1))
        print(f"[{name}] {run['status']} usage=${run.get('usageTotalUsd') or 0:.3f} items={run.get('stats', {}).get('outputItems', '?')}", flush=True)

    items = c.get(f"{API}/datasets/{run['defaultDatasetId']}/items", params={"token": tok, "clean": "true", "format": "json"}, timeout=600).json()
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False))
    tmp.replace(out)
    run_f.write_text(json.dumps(run, indent=1))
    print(f"[{name}] {run['status']} saved {len(items)} items -> {out}  usage=${run.get('usageTotalUsd')}", flush=True)


if __name__ == "__main__":
    main()
