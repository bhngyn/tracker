"""Pipeline monitor: progress, rates, ETAs, running processes, stalls, errors, disk, Apify spend.

usage: status.py                 print once
       status.py --watch [60]    refresh the terminal every N seconds
       status.py --html          also write <ROOT>/STATUS.html (auto-reloads every 30 s;
                                 open it straight from Finder, works from file://)
       status.py --watch 60 --html --quiet    background mode: keep STATUS.html fresh

Cheap to run: caption status per YouTube item is cached by info.json mtime in
logs/status_cache.json; progress snapshots go to logs/status_history.jsonl and
drive the rate / ETA figures (last ~60 min).
"""
import argparse, html, json, os, re, shutil, subprocess, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from common import CFG, DERIVED, LOGS, ROOT, derived_dir, iter_images, iter_videos, youtube_caption_status

CACHE = LOGS / "status_cache.json"
HISTORY = LOGS / "status_history.jsonl"
YT_EXPECTED_FALLBACK = int(CFG["youtube"]["expected_videos"])  # used until the yt-dlp logs report every tab's size
NAME = CFG["project"]["name"]
BUDGET = float(CFG["apify"]["total_budget_usd"])
STALL_MIN = 20
PROCS = {  # label: command-line regex
    "Pipeline (run_all)": r"run_all\.sh",
    "Packaging": r"package\.py",
    "FB history continuation": r"(fb_)?history_continue\.py",
    "YouTube download": r"fetch_youtube\.sh",
    "yt-dlp": r"\.venv/bin/yt-dlp(\s|$)",  # the toolkit's own yt-dlp, not any command line mentioning one
    "IG/FB media": r"fetch_media\.py",
    "Apify run": r"fetch_apify\.py",
    "Caption backfill": r"backfill_captions\.py",
    "Transcription": r"transcribe\.py",
    "OCR": r"ocr\.py",
    "Index build": r"build_index\.py",
    "Keep-awake": r"caffeinate -dimsu",
}
LOG_FOR = {"Pipeline (run_all)": ["run_all.log"],
           "YouTube download": ["youtube_a.log", "youtube_b.log", "youtube_retry.log"],
           "IG/FB media": ["fb_videos.log", "ig.log", "fb_history.log", "media.log"],
           "Apify run": ["fb_history.log", "ig.log"],
           "Caption backfill": ["captions.log"], "Transcription": ["transcribe.log"],
           "OCR": ["ocr.log"],  # + one ocr_<i>.log per shard, found by ocr_logs()
           "Index build": ["index.log"], "Packaging": ["package.log"],
           "FB history continuation": ["fb_continue.log", "fb_history2.log"]}
# fetch_media logs <-> the command-line args of the process writing them, and its heartbeat stage
MEDIA_LOGS = [  # (card name, log, cmd regex, heartbeat stage)
    ("Instagram media", "ig.log", r"--platform ig\b", "media_ig_all"),
    ("Facebook videos", "fb_videos.log", r"--platform fb --only videos", "media_fb_videos"),
    ("FB history media", "fb_history.log", r"--platform fb(?! --only)", "media_fb_all"),
    ("All media (run_all)", "media.log", r"--platform all", "media_all_all"),
    ("FB history media (cont.)", "fb_history2.log", r"--platform fb(?! --only)", "media_fb_all"),
    ("Facebook images", "fb_photos.log", r"--platform fb --only photos", "media_fb_photos"),
]
SIMPLE_JOBS = [("Index build", "index.log", 1800), ("Packaging", "package.log", 1800),
               ("FB history continuation", "fb_continue.log", 6 * 3600)]  # (label, log, normal silence s)


# ---------------------------------------------------------------- collectors

def processes():
    out = subprocess.run(["ps", "-axo", "pid=,etime=,pcpu=,rss=,command="], capture_output=True, text=True).stdout
    me = os.getpid()
    found = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or int(parts[0]) == me or "status.py" in parts[4]:
            continue
        for label, rx in PROCS.items():
            if re.search(rx, parts[4]) and not parts[4].startswith(("sh -c", "/bin/sh -c")):
                found.append({"label": label, "pid": int(parts[0]), "elapsed": parts[1], "cpu": float(parts[2]),
                              "mem_mb": int(parts[3]) // 1024, "cmd": parts[4][:160]})
                break
    return found


def youtube_expected():
    """Playlist sizes as yt-dlp reported them ("Downloading item 7 of 1055") per tab URL."""
    totals = {}
    for f in LOGS.glob("youtube*.log"):
        url = None
        for line in f.read_text(errors="replace").splitlines():
            if line.startswith("=== http"):
                url = line.split()[1]
            m = re.search(r"Downloading item \d+ of (\d+)", line)
            if m and url:
                totals[url] = max(totals.get(url, 0), int(m.group(1)))
    return totals


def load_cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def caption_statuses(cache):
    res = {}
    for d in ROOT.glob("youtube/*/*/"):
        info = next(d.glob("*.info.json"), None)
        if not info:
            continue
        key = str(d)
        mt = max([info.stat().st_mtime] + [p.stat().st_mtime for p in d.glob("*.srt")])
        c = cache.get(key)
        if not c or c["mt"] != mt:
            st = youtube_caption_status(d)
            c = cache[key] = {"mt": mt, "missing": st["missing"], "original": st["original"]}
        res[key] = c
    return res


def media_stats(platform):
    st = Counter()
    for mf in ROOT.glob(f"{platform}/**/media.json"):
        try:
            for rec in json.loads(mf.read_text()).values():
                kind = "video" if rec.get("kind") == "video" else "image"
                st[(kind, rec.get("status"))] += 1
                if rec.get("status") == "ok":
                    st[(kind, "bytes")] += rec.get("bytes") or 0
        except ValueError:
            pass
    return st


def planned_media():
    """How many files fetch_media.py would fetch, from the raw datasets (cached by raw mtimes)."""
    import fetch_media
    out = {}
    fb = fetch_media.fb_jobs("all")
    out["facebook"] = {"video": sum(j["kind"] == "video" for *_, js in fb for j in js),
                       "image": sum(j["kind"] != "video" for *_, js in fb for j in js)}
    ig = fetch_media.ig_jobs("all")
    out["instagram"] = {"video": sum(j["kind"] == "video" for *_, js in ig for j in js),
                        "image": sum(j["kind"] != "video" for *_, js in ig for j in js)}
    return out


def apify_spend():
    runs = []
    for f in (ROOT / "raw").glob("*.run.json"):
        r = json.loads(f.read_text())
        runs.append((f.name.replace(".run.json", ""), r.get("status"), r.get("usageTotalUsd") or 0))
    return runs


def log_tail(names, n=3):
    lines = []
    for nm in names:
        f = LOGS / nm
        if f.exists():
            tail = [l for l in f.read_text(errors="replace").splitlines() if l.strip()][-n:]
            lines += [f"{nm}: {l[:170]}" for l in tail]
    return lines


def log_age_min(names):
    ages = [(time.time() - (LOGS / n).stat().st_mtime) / 60 for n in names if (LOGS / n).exists()]
    return min(ages) if ages else None


def error_counts():
    c = {}
    for f in LOGS.glob("*.log"):
        t = f.read_text(errors="replace")
        n = len(re.findall(r"\bERROR\b|\berror\b  |Traceback", t))
        n429 = t.count("HTTP Error 429")
        if n or n429:
            c[f.name] = {"errors": n, "http_429": n429}
    return c


# ---------------------------------------------------------------- right now (cheap, every tick)

def read_tail(f: Path, nbytes=262144):
    if not f.exists():
        return []
    with f.open("rb") as fh:
        fh.seek(max(0, f.stat().st_size - nbytes))
        return fh.read().decode("utf-8", "replace").splitlines()


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def fmt_bytes(b):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or u == "TB":
            return f"{b:.0f} {u}" if u in ("B", "KB") else f"{b:.1f} {u}"
        b /= 1024


def fmt_dur(sec):
    if sec is None:
        return "—"
    sec = int(max(0, sec))
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


YT_PHASES = [  # (regex on the latest meaningful log line, phase)
    (r"^=== done", "finished"),
    (r"has already been recorded in the archive", "skipping (already downloaded)"),
    (r"^\[download\] Sleeping|^\[youtube\] Sleeping", "polite pause between requests"),
    (r"^\[Merger\]", "merging video + audio"),
    (r"^\[(SubtitlesConvertor|ThumbnailsConvertor|FixupM3u8|MoveFiles)\]|^Deleting original", "post-processing"),
    (r"subtitles|Subtitles", "fetching captions"),
    (r"^\[download\] Destination", "downloading"),
    (r"^\[download\]\s+[\d.]+%", "downloading"),
    (r"^\[(youtube|youtube:tab|info)\]", "reading video page / metadata"),
    (r"ERROR", "error on last item"),
]


def yt_expected_bytes(info):
    ids = str(info.get("format_id") or "").split("+")
    fm = {f.get("format_id"): f for f in info.get("formats") or []}
    tot = sum((fm.get(i, {}).get("filesize") or fm.get(i, {}).get("filesize_approx") or 0) for i in ids)
    return tot or info.get("filesize_approx") or 0


def youtube_workers(state):
    out = []
    for lf in sorted(LOGS.glob("youtube_*.log")):
        lines = [l for l in read_tail(lf) if l.strip()]
        if not lines:
            continue
        w = {"worker": "YouTube " + lf.stem.split("_")[-1].upper(), "log": lf.name,
             "log_age_s": time.time() - lf.stat().st_mtime}
        for l in reversed(lines):
            if l.startswith("=== http"):
                w["tab"] = l.split()[1].replace("https://www.youtube.com/", "")
                break
        w["tab"] = w.get("tab") or state.get("tab_" + lf.name)
        state["tab_" + lf.name] = w["tab"]
        for l in reversed(lines):
            m = re.search(r"Downloading item (\d+) of (\d+)", l)
            if m:
                w["pos"], w["of"] = int(m.group(1)), int(m.group(2))
                break
        vid = None
        for l in reversed(lines):
            m = re.search(r"watch\?v=([\w-]{11})|\[youtube\] ([\w-]{11}):", l)
            if m:
                vid = m.group(1) or m.group(2)
                break
        last = lines[-1]
        w["phase"] = next((ph for rx, ph in YT_PHASES if re.search(rx, last)), "working")
        w["last_line"] = last[:200]
        if vid:
            w["id"] = vid
            d = next(iter(ROOT.glob(f"youtube/*/*_{vid}")), None)
            info = read_json_safe(d / f"{vid}.info.json") if d else None
            if info:
                w["title"], w["duration_s"] = info.get("title"), info.get("duration")
                w["uploaded"] = info.get("upload_date")
                exp = yt_expected_bytes(info)
                have = sum(f.stat().st_size for f in d.glob(f"{vid}.f*") if f.is_file())
                final = d / f"{vid}.mp4"
                if final.exists() and not have:
                    have = final.stat().st_size
                    w["phase"] = w["phase"] if w["phase"] != "downloading" else "finalising"
                w["bytes"], w["expected_bytes"] = have, exp
                w["progress"] = min(1.0, have / exp) if exp else None
                prev = state.get("yt_bytes_" + vid)
                now_t = time.time()
                if prev and now_t - prev[1] > 1 and have >= prev[0]:
                    w["speed_Bps"] = (have - prev[0]) / (now_t - prev[1])
                    if w["speed_Bps"] > 50_000:  # bytes are landing, whatever the last log line says
                        w["phase"] = "downloading"
                    if w["speed_Bps"] > 0 and exp:
                        w["eta_s"] = max(0, exp - have) / w["speed_Bps"]
                state["yt_bytes_" + vid] = [have, now_t]
        out.append(w)
    return out


def read_json_safe(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def media_runs(procs):
    """fetch_media runs: progress within the current run, from its log (header + result lines).
    Each log is tied to the process whose args wrote it, so a dead run shows as stopped."""
    out = []
    cmds = [p["cmd"] for p in procs if p["label"] == "IG/FB media"]
    for name, lname, cmd_rx, stage in MEDIA_LOGS:
        lf = LOGS / lname
        lines = read_tail(lf, 1_000_000)
        hdr = max((i for i, l in enumerate(lines) if " posts | " in l), default=None)
        if hdr is None:
            continue
        m = re.search(r"\| (\d+) http files .*?\| (\d+) yt-dlp videos", lines[hdr])
        total = int(m.group(1)) + int(m.group(2)) if m else None
        res = [l for l in lines[hdr + 1:] if re.match(r"\S+Z\s+\S+\s{2}", l)]
        st = Counter(l.split()[1] for l in res)
        finished = any("finished:" in l for l in lines[hdr:])
        ts = [datetime.fromisoformat(l.split()[0].replace("Z", "+00:00")).timestamp() for l in res[-40:]]
        rate = (len(ts) - 1) / (ts[-1] - ts[0]) * 60 if len(ts) > 2 and ts[-1] > ts[0] else None
        done = sum(st.values())
        alive = any(re.search(cmd_rx, c) for c in cmds) and not finished
        hb = read_json_safe(LOGS / "now" / f"{stage}.json")
        current = hb.get("detail") if hb and alive and pid_alive(hb.get("pid")) and str(hb.get("detail", "")).startswith("yt-dlp") else None
        age = time.time() - max(lf.stat().st_mtime, (hb or {}).get("updated_ts", 0) if current else 0)
        out.append({"worker": name, "done": done, "total": total, "ok": st.get("ok", 0),
                    "failed": done - st.get("ok", 0), "per_min": rate, "finished": finished, "log_age_s": age,
                    "phase": "finished" if finished else ("downloading" if alive else "stopped"),
                    "last_line": res[-1][:200] if res else lines[-1][:200], "current": current,
                    "progress": done / total if total else None,
                    "eta_s": (total - done) / rate * 60 if rate and total and not finished else None,
                    "stall_after_s": 3600 if current else STALL_MIN * 60})  # one long FB video can be slow
    return out


def apify_live(procs):
    """Unfinished Apify runs, from raw/<name>.run.json (fetch_apify.py rewrites it every 20 s)."""
    out = []
    for f in (ROOT / "raw").glob("*.run.json"):
        r = read_json_safe(f) or {}
        if r.get("status") not in ("READY", "RUNNING"):
            continue
        name = f.name.replace(".run.json", "")
        cap = (r.get("options") or {}).get("maxTotalChargeUsd") or 0
        used = r.get("usageTotalUsd") or 0
        items = None
        for lf in LOGS.glob("*.log"):
            for l in reversed(read_tail(lf, 65536)):
                m = re.match(rf"\[{re.escape(name)}\] (?:RUNNING|READY) usage=\$[\d.]+ items=(\S+)", l)
                if m:
                    items = m.group(1)
                    break
        started = r.get("startedAt")
        el = time.time() - datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() if started else None
        poller = any(p["label"] == "Apify run" and name in p["cmd"] for p in procs)
        out.append({"worker": f"Apify: {name}", "item": f"run {r.get('id')} · {r['status'].lower()}",
                    "detail": (f"{items} items so far · " if items and items != "?" else "") +
                              ("poller running" if poller else "⚠ poller NOT running — re-run fetch_apify/run_all to resume"),
                    "elapsed_s": el, "progress": min(1.0, used / cap) if cap else None,
                    "stats": f"${used:.2f} of ${cap:.2f} cap", "phase": "scraping",
                    "log_age_s": time.time() - f.stat().st_mtime, "stall_after_s": 300})
    return out


def heartbeats():
    out = []
    for f in (LOGS / "now").glob("*.json"):
        h = read_json_safe(f)
        if not h or not pid_alive(h.get("pid")) or h.get("item") is None and h.get("detail") == "finished":
            continue
        st = h["stage"]
        if st.startswith("media_"):  # shown inside the media cards
            continue
        el = time.time() - h["item_started"] if h.get("item_started") else None
        prog = h.get("item_progress")
        if prog is None and h.get("expected_s") and el is not None:
            prog = min(0.99, el / h["expected_s"])  # estimated from the run's realtime factor
        name = {"transcribe": "Transcription", "ocr": "OCR", "captions": "Caption backfill"}.get(st) or (
            f"OCR worker {int(st[3:]) + 1}" if re.fullmatch(r"ocr\d+", st) else st)
        # how long silence is normal before calling it stalled
        since_update = time.time() - h.get("updated_ts", time.time())
        detail = str(h.get("detail") or "")
        if st == "transcribe":  # writes once per file: judge by time on this file vs expected
            allowed = max(STALL_MIN * 60, 3 * h["expected_s"] if h.get("expected_s") else 0.5 * (h.get("audio_s") or 0))
            silent = el or 0
        elif (m := re.search(r"sleeping (\d+)s", detail)):
            allowed, silent = int(m.group(1)) + 600, since_update
        elif detail.startswith("extracting"):
            allowed, silent = 3600, since_update
        else:
            allowed, silent = STALL_MIN * 60, since_update
        out.append({"worker": name,
                    "item": h.get("item"), "index": h.get("index"), "total": h.get("total"), "detail": detail,
                    "elapsed_s": el, "progress": prog, "estimated": h.get("item_progress") is None and prog is not None,
                    "eta_s": (el / prog - el) if prog and el and prog > 0.02 else None,
                    "log_age_s": silent, "stall_after_s": allowed,
                    "phase": "paused" if h.get("paused") else (h.get("kind") or "working")})
    return out


def simple_jobs(procs):
    """build_index.py / package.py: a card with the last log line while the process runs."""
    out = []
    running = {p["label"] for p in procs}
    for label, lname, quiet_ok in SIMPLE_JOBS:
        if label not in running:
            continue
        lf = LOGS / lname
        lines = [l for l in read_tail(lf, 65536) if l.strip()]
        last = next((l for l in reversed(lines) if "[fb_continue]" in l), lines[-1] if lines else "") if "continuation" in label else (lines[-1] if lines else "")
        out.append({"worker": label, "phase": "running", "detail": re.sub(r"^\S+Z\s+(\[fb_continue\]\s*)?", "", last)[:200],
                    "log_age_s": time.time() - lf.stat().st_mtime if lf.exists() else 0, "stall_after_s": quiet_ok})
    return out


def system_stats(state):
    s = {"load": [round(x, 2) for x in os.getloadavg()], "cpus": os.cpu_count()}
    try:
        mp = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=5).stdout
        s["mem_free_pct"] = int(re.search(r"(\d+)%", mp).group(1))
    except Exception:
        pass
    try:
        iface = re.search(r"interface: (\S+)", subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                                                              text=True, timeout=5).stdout).group(1)
        row = subprocess.run(["netstat", "-I", iface, "-b"], capture_output=True, text=True, timeout=5).stdout.splitlines()[1].split()
        rx, tx, t = int(row[-5]), int(row[-2]), time.time()
        prev = state.get("net")
        if prev and prev[0] == iface and t - prev[3] > 1:
            s["net_in_Bps"] = max(0, (rx - prev[1]) / (t - prev[3]))
            s["net_out_Bps"] = max(0, (tx - prev[2]) / (t - prev[3]))
        state["net"] = [iface, rx, tx, t]
        s["iface"] = iface
    except Exception:
        pass
    try:
        du = shutil.disk_usage(ROOT)
        prev = state.get("disk")
        t = time.time()
        if prev and t - prev[1] > 1:
            s["disk_write_Bps"] = max(0, (prev[0] - du.free) / (t - prev[1]))  # net growth of used space
        state["disk"] = [du.free, t]
        s["disk_free_gb"] = round(du.free / 1e9, 1)
    except OSError:
        pass
    return s


def activity_feed(state, n=25):
    """Recent completed things, newest first: YouTube downloads (first seen in archive.txt)
    plus the timestamped result lines of the media / transcription / OCR / caption logs."""
    ev = []
    arch = ROOT / "youtube" / "archive.txt"
    seen = state.setdefault("yt_seen", {})
    first_run = not seen
    if arch.exists():
        for l in arch.read_text().splitlines():
            vid = l.split()[-1] if l.strip() else None
            if vid and vid not in seen:
                seen[vid] = None if first_run else time.time()
    for vid, t in sorted(((v, t) for v, t in seen.items() if t), key=lambda x: -x[1])[:n]:
        d = next(iter(ROOT.glob(f"youtube/*/*_{vid}")), None)
        info = read_json_safe(d / f"{vid}.info.json") if d else {}
        ev.append((t, "YouTube", f"downloaded “{(info or {}).get('title', vid)}” ({fmt_dur((info or {}).get('duration'))})"))
    for label, name in (("Instagram", "ig.log"), ("Facebook", "fb_videos.log"), ("FB history", "fb_history.log"),
                        ("Media", "media.log"), ("Transcription", "transcribe.log"), ("Pipeline", "run_all.log"),
                        ("Index", "index.log"), ("Package", "package.log"), ("FB continue", "fb_continue.log"),
                        ("FB hist. media", "fb_history2.log"),
                        *ocr_logs(), ("Captions", "captions.log")):
        for l in read_tail(LOGS / name, 65536)[-n:]:
            m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)", l)
            if m:
                ev.append((datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp(), label, m.group(2).strip()[:180]))
    ev.sort(key=lambda e: -e[0])
    return [{"t": datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds"), "src": s_, "text": x} for t, s_, x in ev[:n]]


def pipeline_stage(procs):
    """Current run_all.sh stage, from its log."""
    if not any(p["label"] == "Pipeline (run_all)" for p in procs):
        return []
    lines = [l for l in read_tail(LOGS / "run_all.log", 65536) if "[run_all]" in l]
    if not lines:
        return []
    last = lines[-1].split("[run_all] ", 1)[-1]
    return [{"worker": "Pipeline (run_all)", "phase": "running", "detail": last,
             "log_age_s": 0}]  # waits are long by design, so never flagged as stalled


def now_snapshot(state):
    procs = processes()
    workers = (pipeline_stage(procs) + youtube_workers(state) + media_runs(procs) + apify_live(procs)
               + heartbeats() + simple_jobs(procs))
    running = {p["label"] for p in procs}
    for w in workers:  # mark YouTube workers idle when their shell isn't running
        if w["worker"].startswith("YouTube") and "YouTube download" not in running:
            w["phase"] = "stopped" if w["phase"] != "finished" else "finished"
        quiet = (w.get("log_age_s") or 0) > w.get("stall_after_s", STALL_MIN * 60)
        growing = (w.get("speed_Bps") or 0) > 0  # a big download writes no log lines but the file grows
        w["stalled"] = w.get("phase") not in ("finished", "stopped") and quiet and not growing
    workers.sort(key=lambda w: (not w["worker"].startswith("Pipeline"), w["worker"]))
    return {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "procs": procs, "workers": workers,
            "system": system_stats(state), "feed": activity_feed(state)}


# ---------------------------------------------------------------- snapshot

def snapshot():
    cache = load_cache()
    t0 = time.time()
    s = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # YouTube
    arch = ROOT / "youtube" / "archive.txt"
    yt_done = sum(1 for l in arch.read_text().splitlines() if l.strip()) if arch.exists() else 0
    exp = youtube_expected()
    caps = caption_statuses(cache)
    s["youtube"] = {"done": yt_done, "expected": sum(exp.values()) if len(exp) >= max(1, len(CFG["youtube"]["tabs"])) else max(YT_EXPECTED_FALLBACK, yt_done),
                    "tabs": exp, "missing_captions": sum(1 for c in caps.values() if c["missing"]),
                    "no_speech_transcript": sum(1 for c in caps.values() if not c["original"]),
                    "partial_files": len(list(ROOT.glob("youtube/*/*/*.part")))}

    # IG / FB media
    plan = cache.get("_plan")
    raw_mt = max((f.stat().st_mtime for f in (ROOT / "raw").glob("*.json") if not f.name.endswith(".run.json")), default=0)
    if not plan or plan.get("mt") != raw_mt:
        plan = cache["_plan"] = {"mt": raw_mt, **planned_media()}
    for p in ("facebook", "instagram"):
        m = media_stats(p)
        s[p] = {k: {"ok": m[(k, "ok")], "failed": sum(v for (kk, st), v in m.items() if kk == k and st not in ("ok", "bytes")),
                    "planned": plan[p][k], "bytes": m[(k, "bytes")]} for k in ("image", "video")}

    # derived
    vids = list(iter_videos())
    imgs = list(iter_images())
    whisper_done = sum(1 for _, p in vids if (derived_dir(p) / "whisper.json").exists())
    need_whisper = sum(1 for src, p in vids if src != "youtube" or not caps.get(str(p.parent), {}).get("original", False))
    s["transcription"] = {"done": whisper_done, "targets_on_disk": need_whisper}
    s["ocr"] = {"videos_done": sum(1 for _, p in vids if (derived_dir(p) / "ocr.json").exists()), "videos": len(vids),
                "images_done": sum(1 for _, p in imgs if (derived_dir(p) / "ocr.json").exists()), "images": len(imgs)}
    meta = ROOT / "viewer" / "data" / "meta.js"
    if meta.exists():
        m = re.search(r"=(\{.*\});", meta.read_text())
        s["index"] = json.loads(m.group(1)) if m else {}

    du = shutil.disk_usage(ROOT)
    s["disk"] = {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1)}
    s["apify"] = apify_spend()
    s["procs"] = processes()
    s["errors"] = error_counts()
    s["logs"] = {k: {"tail": log_tail(v), "age_min": log_age_min(v)} for k, v in LOGS_FOR().items()}
    s["took_s"] = round(time.time() - t0, 1)
    CACHE.write_text(json.dumps(cache))
    return s


def ocr_logs():
    """[(label, log name)]: the single-worker log plus one per shard, however many workers run."""
    shards = sorted(LOGS.glob("ocr_*.log"), key=lambda f: int(m.group(1)) if (m := re.fullmatch(r"ocr_(\d+)\.log", f.name)) else -1)
    return [("OCR", "ocr.log")] + [(f"OCR {int(f.stem[4:]) + 1}", f.name) for f in shards if re.fullmatch(r"ocr_\d+", f.stem)]


def LOGS_FOR():
    d = dict(LOG_FOR)
    d["OCR"] = [name for _, name in ocr_logs()]
    if (LOGS / "fb_photos.log").exists():
        d["IG/FB media"] = d["IG/FB media"] + ["fb_photos.log"]
    return d


def counters(s):
    return {"yt": s["youtube"]["done"],
            "fbv": s["facebook"]["video"]["ok"], "fbi": s["facebook"]["image"]["ok"],
            "igv": s["instagram"]["video"]["ok"], "igi": s["instagram"]["image"]["ok"],
            "wh": s["transcription"]["done"], "ocrv": s["ocr"]["videos_done"], "ocri": s["ocr"]["images_done"]}


def rates(s):
    """Items/hour for each counter over the last ~60 min of history."""
    now = time.time()
    cur = counters(s)
    hist = []
    if HISTORY.exists():
        for line in HISTORY.read_text().splitlines()[-2000:]:
            try:
                hist.append(json.loads(line))
            except ValueError:
                pass
    with HISTORY.open("a") as f:
        f.write(json.dumps({"t": now, **cur}) + "\n")
    old = [h for h in hist if now - h["t"] <= 3900]
    if not old or now - old[0]["t"] < 120:
        return {}
    h0 = old[0]
    hrs = (now - h0["t"]) / 3600
    return {k: max(0.0, (cur[k] - h0.get(k, cur[k])) / hrs) for k in cur}


# ---------------------------------------------------------------- rendering

def eta(left, rate):
    if left <= 0:
        return "done"
    if not rate:
        return "—"
    h = left / rate
    return f"{h * 60:.0f} min" if h < 1 else f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} days"


def rows(s, r):
    """(stage, done, total, rate/h, eta, note, active_label)"""
    yt, fb, ig, tr, oc = s["youtube"], s["facebook"], s["instagram"], s["transcription"], s["ocr"]
    gb = lambda b: f"{b / 1e9:.1f} GB"
    return [
        ("YouTube videos", yt["done"], yt["expected"], r.get("yt"), f"{yt['partial_files']} partial file(s) in progress", "YouTube download"),
        ("YouTube caption gaps", yt["missing_captions"], None, None, "videos missing a caption track (backfill runs after downloads)", "Caption backfill"),
        ("Facebook images", fb["image"]["ok"], fb["image"]["planned"], r.get("fbi"), f"{fb['image']['failed']} failed · {gb(fb['image']['bytes'])}", "IG/FB media"),
        ("Facebook videos", fb["video"]["ok"], fb["video"]["planned"], r.get("fbv"), f"{fb['video']['failed']} failed · {gb(fb['video']['bytes'])}", "IG/FB media"),
        ("Instagram images", ig["image"]["ok"], ig["image"]["planned"], r.get("igi"), f"{ig['image']['failed']} failed · {gb(ig['image']['bytes'])}", "IG/FB media"),
        ("Instagram videos", ig["video"]["ok"], ig["video"]["planned"], r.get("igv"), f"{ig['video']['failed']} failed · {gb(ig['video']['bytes'])}", "IG/FB media"),
        ("Transcription (Whisper)", tr["done"], tr["targets_on_disk"], r.get("wh"), "targets = FB/IG videos + YouTube without a speech transcript, among videos on disk", "Transcription"),
        ("OCR — videos", oc["videos_done"], oc["videos"], r.get("ocrv"), "of videos on disk so far", "OCR"),
        ("OCR — photos", oc["images_done"], oc["images"], r.get("ocri"), "of images on disk so far", "OCR"),
    ]


def alerts(s, nw=None):
    out = []
    running = {p["label"] for p in (nw or s)["procs"]}
    for w in (nw or {}).get("workers", []):
        if w.get("stalled"):
            out.append(f"{w['worker']}: no progress for {fmt_dur(w.get('log_age_s'))} — possibly stalled")
        if w["worker"].startswith("Apify") and "NOT running" in str(w.get("detail")):
            out.append(f"{w['worker']}: the run is live on Apify but nothing is polling it")
    if s["disk"]["free_gb"] < 40:
        out.append(f"The archive drive has only {s['disk']['free_gb']} GB free")
    work = running - {"Keep-awake"}
    if work and "Keep-awake" not in running:
        out.append("Jobs are running without caffeinate — the Mac may sleep")
    spent = sum(x[2] for x in s["apify"])
    if BUDGET and spent > 0.9 * BUDGET:
        out.append(f"Apify spend ${spent:.2f} is close to the ${BUDGET:.0f} budget")
    for f, e in s["errors"].items():
        if e["http_429"] > 50:
            out.append(f"{f}: {e['http_429']} HTTP 429 (rate-limit) responses")
    return out


def worker_line(w):
    """(title, subtitle, progress 0-1 or None, right-hand stats)"""
    if w["worker"].startswith("YouTube"):
        pos = f"item {w['pos']:,} of {w['of']:,} in {w.get('tab') or '?'}" if w.get("pos") else (w.get("tab") or "")
        title = f"“{w['title']}”" if w.get("title") else (f"{w['id']} (reading metadata…)" if w.get("id") else "—")
        sub = f"{w['phase']} · {pos}" + (f" · video length {fmt_dur(w.get('duration_s'))}" if w.get("duration_s") else "")
        stats = []
        if w.get("expected_bytes"):
            stats.append(f"{fmt_bytes(w.get('bytes', 0))} / {fmt_bytes(w['expected_bytes'])}")
        if w.get("speed_Bps"):
            stats.append(f"{fmt_bytes(w['speed_Bps'])}/s")
        if w.get("eta_s") is not None and w["phase"] == "downloading":
            stats.append(f"~{fmt_dur(w['eta_s'])} left")
        return title, sub, w.get("progress") if w["phase"] in ("downloading", "finalising") else None, " · ".join(stats)
    if "done" in w and "total" in w:  # media run
        stats = [f"{w['done']:,}/{w['total']:,}" if w.get("total") else f"{w['done']:,}"]
        if w.get("failed"):
            stats.append(f"{w['failed']} failed")
        if w.get("per_min"):
            stats.append(f"{w['per_min']:.0f}/min")
        if w.get("eta_s") is not None:
            stats.append(f"~{fmt_dur(w['eta_s'])} left")
        last = re.sub(r"^\S+Z\s+", "", w.get("last_line") or "")
        sub_ = f"now: {w['current']}" if w.get("current") else f"last: {last}"
        return w["phase"], sub_, w.get("progress"), " · ".join(stats)
    if "item" in w:  # heartbeat
        sub = (f"{w['index']:,} of {w['total']:,} · " if w.get("index") and w.get("total") else "") + (w.get("detail") or "")
        stats = [f"running {fmt_dur(w['elapsed_s'])}"] if w.get("elapsed_s") is not None else []
        if w.get("eta_s") is not None:
            stats.append(f"~{fmt_dur(w['eta_s'])} left" + (" (est.)" if w.get("estimated") else ""))
        if w.get("stats"):
            stats.insert(0, w["stats"])
        return w.get("item") or "—", sub, w.get("progress"), " · ".join(stats)
    return w.get("phase", ""), w.get("detail", ""), None, ""


def system_line(sy):
    parts = []
    if "net_in_Bps" in sy:
        parts.append(f"network ↓ {fmt_bytes(sy['net_in_Bps'])}/s ↑ {fmt_bytes(sy['net_out_Bps'])}/s")
    if "disk_write_Bps" in sy:
        parts.append(f"archive disk filling at {fmt_bytes(sy['disk_write_Bps'])}/s")
    if "mem_free_pct" in sy:
        parts.append(f"memory free {sy['mem_free_pct']}%")
    parts.append(f"load {sy['load'][0]} on {sy['cpus']} cores")
    return " · ".join(parts)


def render_now_text(nw, W):
    out = [f"RIGHT NOW  ({system_line(nw['system'])})"]
    for w in nw["workers"]:
        if w.get("phase") == "finished" and not w["worker"].startswith("YouTube"):
            continue
        title, sub, prog, stats = worker_line(w)
        flag = "  ⚠ STALLED?" if w.get("stalled") else ""
        bar = ("[" + "█" * int(prog * 20) + "░" * (20 - int(prog * 20)) + f"] {prog:4.0%} ") if prog is not None else ""
        out.append(f"▸ {w['worker']:<17} {title[: W - 22]}{flag}")
        out.append(f"  {'':<17} {bar}{stats}")
        out.append(f"  {'':<17} {sub[: W - 22]}")
    out += ["", "LATEST ACTIVITY"]
    for e_ in nw["feed"][:10]:
        out.append(f"  {e_['t'][11:19]}  {e_['src']:<13} {e_['text'][: W - 32]}")
    return out


def render_now_html(nw):
    e = html.escape
    cards = []
    for w in nw["workers"]:
        if w.get("phase") == "finished" and not w["worker"].startswith("YouTube"):
            continue
        title, sub, prog, stats = worker_line(w)
        live = w.get("phase") not in ("finished", "stopped")
        cards.append(f"""<div class="card {'stalled' if w.get('stalled') else ''}">
  <div class="cardhead"><span class="dot {'on' if live else ''}"></span><b>{e(w['worker'])}</b>
    <span class="phase">{e(w.get('phase') or '')}</span>{'<span class="warn">stalled?</span>' if w.get('stalled') else ''}</div>
  <div class="ctitle" dir="auto">{e(title)}</div>
  {f'<div class="bar big"><i style="width:{prog * 100:.1f}%"></i></div>' if prog is not None else ''}
  <div class="cstats">{f'<b>{prog:.0%}</b> · ' if prog is not None else ''}{e(stats)}</div>
  <div class="note" dir="auto">{e(sub)}</div></div>""")
    feed = "".join(f"<tr><td class='n'>{e(x['t'][11:19])}</td><td>{e(x['src'])}</td><td dir='auto'>{e(x['text'])}</td></tr>" for x in nw["feed"])
    return (f"<h2>Right now</h2><div class='sys'>{e(system_line(nw['system']))}</div>"
            f"<div class='cards'>{''.join(cards) or '<p class=note>Nothing running.</p>'}</div>"
            f"<h2>Latest activity <span class='note'>(UTC, newest first)</span></h2>"
            f"<div class='wrap'><table class='feed'><tbody>{feed or '<tr><td class=note>No activity yet.</td></tr>'}</tbody></table></div>")


def render_text(s, r, nw):
    W = shutil.get_terminal_size((110, 40)).columns
    out = [f"{NAME} pipeline — {nw['at'].replace('T', ' ')} UTC   disk free {s['disk']['free_gb']} GB   "
           f"Apify ${sum(x[2] for x in s['apify']):.2f} / ${BUDGET:.0f}", "=" * min(W, 110)]
    out += render_now_text(nw, W) + ["", "TOTALS  (totals refresh every minute)"]
    running = {p["label"] for p in nw["procs"]}
    for name, done, total, rate, note, label in rows(s, r):
        dot = "●" if label in running and (not total or done < total) else "○"
        if total:
            pct = done / total if total else 0
            bar = "█" * int(pct * 24) + "░" * (24 - int(pct * 24))
            out.append(f"{dot} {name:<26}{bar} {done:>6,}/{total:<6,} {pct:5.1%}  "
                       f"{(f'{rate:,.0f}/h' if rate else ''):>8}  ETA {eta(total - done, rate):>9}")
        else:
            out.append(f"{dot} {name:<26}{done:,}  ({note})")
    out.append("")
    out.append("Processes:" if nw["procs"] else "Processes: none running")
    for p in nw["procs"]:
        out.append(f"  {p['label']:<18} pid {p['pid']:<6} up {p['elapsed']:>11}  cpu {p['cpu']:5.1f}%  mem {p['mem_mb']:>5} MB")
    al = alerts(s, nw)
    if al:
        out += ["", "⚠ Alerts:"] + [f"  - {a}" for a in al]
    return "\n".join(out)


def render_html(s, r, nw):
    e = html.escape
    s = s | {"procs": nw["procs"]}
    running = {p["label"] for p in s["procs"]}
    trs = []
    for name, done, total, rate, note, label in rows(s, r):
        pct = done / total if total else None
        trs.append(f"""<tr><td><span class="dot {'on' if label in running and (not total or done < total) else ''}"></span>{e(name)}</td>
          <td class="n">{done:,}{f' / {total:,}' if total else ''}</td>
          <td>{f'<div class="bar"><i style="width:{pct * 100:.1f}%"></i></div><span class="pct">{pct:.1%}</span>' if pct is not None else ''}</td>
          <td class="n">{f'{rate:,.0f}/h' if rate else '—'}</td><td class="n">{eta(total - done, rate) if total else ''}</td>
          <td class="note">{e(note)}</td></tr>""")
    procs = "".join(f"<tr><td>{e(p['label'])}</td><td class='n'>{p['pid']}</td><td class='n'>{e(p['elapsed'])}</td>"
                    f"<td class='n'>{p['cpu']:.0f}%</td><td class='n'>{p['mem_mb']:,} MB</td><td class='note'><code>{e(p['cmd'])}</code></td></tr>"
                    for p in s["procs"]) or "<tr><td colspan=6 class='note'>No pipeline processes running.</td></tr>"
    al = alerts(s, nw)
    spent = sum(x[2] for x in s["apify"])
    def log_block(k, v):
        age = "" if v["age_min"] is None else f"last write {v['age_min']:.0f} min ago"
        tail = "\n".join(v["tail"])
        return f"<h3>{e(k)} <span class='note'>{age}</span></h3><pre>{e(tail) or '—'}</pre>"
    logs = "".join(log_block(k, v) for k, v in s["logs"].items())
    errs = "".join(f"<li>{e(f)}: {v['errors']} error lines, {v['http_429']} HTTP 429</li>" for f, v in s["errors"].items()) or "<li>none</li>"
    idx = s.get("index") or {}
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(NAME)} pipeline status</title>
<style>
:root {{ --bg:#f7f6f3; --panel:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e2e0da; --accent:#1f5f8b; --ok:#1e7a3c; --warn:#a56a00; --track:#ecebe6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17181a; --panel:#1f2023; --ink:#ececec; --muted:#9a9aa0; --line:#33343a; --accent:#7db4dc; --ok:#6fcf8f; --warn:#e0b050; --track:#2b2c31; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:20px 16px 60px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:26px 0 8px; }} h3 {{ font-size:13px; margin:14px 0 4px; }}
.sub, .note {{ color:var(--muted); font-size:12px; }}
.kpis {{ display:flex; flex-wrap:wrap; gap:10px; margin:14px 0; }}
.kpi {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 14px; min-width:150px; }}
.kpi b {{ display:block; font-size:20px; font-variant-numeric:tabular-nums; }}
.wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:10px; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:middle; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; }}
.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.bar {{ display:inline-block; width:180px; height:8px; background:var(--track); border-radius:4px; overflow:hidden; vertical-align:middle; }}
.bar i {{ display:block; height:100%; background:var(--accent); }}
.pct {{ margin-left:8px; font-variant-numeric:tabular-nums; font-size:12px; color:var(--muted); }}
.dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--line); margin-right:8px; }}
.dot.on {{ background:var(--ok); box-shadow:0 0 0 3px color-mix(in srgb, var(--ok) 25%, transparent); }}
.alerts {{ border-left:4px solid var(--warn); background:var(--panel); padding:8px 14px; border-radius:6px; }}
pre {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:8px 10px; font-size:12px; white-space:pre-wrap; word-break:break-all; margin:0; }}
code {{ font-size:11px; }}
.sys {{ color:var(--muted); font-size:12px; margin:-2px 0 10px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:10px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 14px; min-width:0; }}
.card.stalled {{ border-color:var(--warn); }}
.cardhead {{ display:flex; align-items:center; gap:6px; font-size:13px; }}
.phase {{ margin-left:auto; font-size:12px; color:var(--muted); background:var(--track); padding:1px 8px; border-radius:10px; }}
.warn {{ font-size:12px; color:var(--warn); font-weight:600; }}
.ctitle {{ margin:6px 0 6px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.bar.big {{ display:block; width:100%; height:10px; }}
.cstats {{ font-size:12px; margin:5px 0 2px; font-variant-numeric:tabular-nums; }}
.card .note {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
table.feed td {{ font-size:12px; padding:4px 10px; }}
</style></head><body><main>
<h1>{e(NAME)} pipeline status</h1>
<div class="sub">Live view updated {e(nw['at'].replace('T', ' '))} UTC · page refreshes every 10 s · totals recomputed every minute (last {e(s['at'][11:19])}, took {s['took_s']} s)</div>
<div class="kpis">
 <div class="kpi"><span class="note">Running jobs</span><b>{len(running - {'Keep-awake'})}</b></div>
 <div class="kpi"><span class="note">Disk free</span><b>{s['disk']['free_gb']} GB</b></div>
 <div class="kpi"><span class="note">Apify spend</span><b>${spent:.2f} / ${BUDGET:.0f}</b></div>
 <div class="kpi"><span class="note">Indexed items</span><b>{idx.get('items', 0):,}</b><span class="note">built {e(str(idx.get('built', '—'))[:16].replace('T', ' '))}</span></div>
 <div class="kpi"><span class="note">Keep-awake</span><b>{'on' if 'Keep-awake' in running else 'off'}</b></div>
</div>
{('<div class="alerts"><b>Alerts</b><ul>' + ''.join(f'<li>{e(a)}</li>' for a in al) + '</ul></div>') if al else ''}
{render_now_html(nw)}
<h2>Totals</h2>
<div class="wrap"><table><thead><tr><th>Stage</th><th class="n">Done</th><th>Progress</th><th class="n">Rate</th><th class="n">ETA</th><th>Notes</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div>
<p class="note">Rates and ETAs use the last hour of snapshots, so they appear after the monitor has run a few minutes. OCR and transcription totals grow as downloads land.</p>
<h2>Processes</h2>
<div class="wrap"><table><thead><tr><th>Job</th><th class="n">PID</th><th class="n">Up</th><th class="n">CPU</th><th class="n">Memory</th><th>Command</th></tr></thead><tbody>{procs}</tbody></table></div>
<h2>Apify runs</h2>
<div class="wrap"><table><thead><tr><th>Dataset</th><th>Status</th><th class="n">Cost</th></tr></thead><tbody>
{''.join(f"<tr><td>{e(n)}</td><td>{e(str(st))}</td><td class='n'>${u:.2f}</td></tr>" for n, st, u in sorted(s['apify']))}</tbody></table></div>
<h2>Errors in logs</h2><ul class="note">{errs}</ul>
<details><summary class="note">Raw log tails</summary>{logs}</details>
<p class="note">Logs: {e(str(LOGS))} · generated by the archive pipeline's status.py</p>
</main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", nargs="?", const=10, type=int, default=0, help="refresh every N seconds (default 10)")
    ap.add_argument("--html", action="store_true", help="write STATUS.html in the archive root")
    ap.add_argument("--quiet", action="store_true", help="no terminal output (background mode)")
    ap.add_argument("--totals-every", type=int, default=60, help="seconds between full (slower) totals scans")
    a = ap.parse_args()
    s = r = None
    last_heavy = 0.0
    while True:
        if s is None or time.time() - last_heavy >= a.totals_every:
            s, last_heavy = snapshot(), time.time()
            r = rates(s)
        cache = load_cache()
        state = cache.setdefault("_now", {})
        nw = now_snapshot(state)
        CACHE.write_text(json.dumps(cache))
        (LOGS / "status.json").write_text(json.dumps(s | {"rates": r, "now": nw}, indent=1, default=str))
        if a.html:
            out = ROOT / "STATUS.html"
            tmp = out.with_suffix(".tmp")
            tmp.write_text(render_html(s, r, nw), encoding="utf-8")
            tmp.replace(out)
        if not a.quiet:
            if a.watch:
                print("\033[2J\033[H", end="")
            print(render_text(s, r, nw), flush=True)
        if not a.watch:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
