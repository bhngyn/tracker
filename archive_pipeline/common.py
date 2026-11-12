"""Shared paths and helpers for the archive pipeline scripts. Everything project-specific
comes from the project config (config.py, ARCHIVE_CONFIG)."""
import json, os, re, subprocess, threading, time
from datetime import datetime, timezone
from pathlib import Path
from config import get, load, resolve

CFG = load()
HERE = Path(__file__).resolve().parent
ROOT = Path(CFG["project"]["archive_root"])
DERIVED = ROOT / "derived"
LOGS = ROOT / "logs"
PACKAGES = Path(CFG["project"]["packages_dir"])
YTDLP = HERE / ".venv" / "bin" / "yt-dlp"
CAPTION_LANGS = tuple(CFG["youtube"]["caption_langs"])
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str):
    print(f"{now()} {msg}", flush=True)


def derived_dir(media: Path) -> Path:
    """derived/<path of the media file relative to ROOT, extension included>/
    (keeping the extension stops <id>.mp4 and its thumbnail <id>.jpg colliding)"""
    return DERIVED / media.relative_to(ROOT)


def write_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    # unique temp name: several threads/processes may write the same file (e.g. heartbeats)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(p)


def _finished(p: Path) -> bool:
    n = p.name
    # .temp.<ext> = yt-dlp mid-merge; .f<id>.<ext> = one stream before merging
    return not (n.endswith((".part", ".tmp", ".ytdl")) or ".temp." in n or re.search(r"\.f\d+\.\w+$", n))


def iter_videos(sources=("youtube", "facebook", "instagram")):
    """Yield (source, path) for every finished local video, in a stable order."""
    globs = {"youtube": "youtube/*/*/*", "facebook": "facebook/*/*", "instagram": "instagram/*/*/*"}
    for s in sources:
        for p in sorted(ROOT.glob(globs[s])):
            if p.suffix.lower() in VIDEO_EXT and _finished(p):
                yield s, p


def iter_images(sources=("youtube", "facebook", "instagram")):
    globs = {"youtube": "youtube/*/*/*", "facebook": "facebook/*/*", "instagram": "instagram/*/*/*"}
    for s in sources:
        for p in sorted(ROOT.glob(globs[s])):
            if p.suffix.lower() in IMAGE_EXT and _finished(p):
                yield s, p


def ffprobe(p: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", str(p)], capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def duration(p: Path) -> float:
    try:
        return float(ffprobe(p)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def has_audio(p: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in ffprobe(p).get("streams", []))


def media_info(p: Path) -> dict:
    """duration, has_audio and video codec from a single ffprobe call."""
    info = ffprobe(p)
    streams = info.get("streams", [])
    try:
        dur = float(info["format"]["duration"])
    except (KeyError, ValueError):
        dur = 0.0
    return {"duration": dur, "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "vcodec": next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None)}


# ---------------------------------------------------------------- captions / SRT

def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments, p: Path):
    out = []
    for i, s in enumerate(segments, 1):
        out.append(f"{i}\n{srt_time(s['start'])} --> {srt_time(s['end'])}\n{s['text'].strip()}\n")
    p.write_text("\n".join(out), encoding="utf-8")


_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(p: Path):
    """[{start, end, text}] — tolerant of YouTube's rolling auto-caption duplicates."""
    segs, cur = [], None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _TS.search(line)
        if m:
            g = [int(x) for x in m.groups()]
            cur = {"start": g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                   "end": g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000, "text": ""}
            segs.append(cur)
        elif line.strip() and cur is not None and not line.strip().isdigit():
            cur["text"] = (cur["text"] + " " + re.sub(r"<[^>]+>", "", line.strip())).strip()
    # drop exact repeats of the previous cue's text (auto-caption roll-up)
    out = []
    for s in segs:
        if s["text"] and (not out or s["text"] != out[-1]["text"]):
            out.append(s)
    return out


def youtube_caption_status(item_dir: Path) -> dict:
    """What captions a YouTube item has on disk vs what YouTube offers.

    wanted   = manual subs in en/he/iw + original-language auto captions (<lang>-orig)
               + English auto captions (a translation when the video isn't English).
    original = True if a manual sub or a *-orig auto caption is on disk, i.e. there is a
               transcript of the actual speech and Whisper isn't required.
    """
    info_f = next(item_dir.glob("*.info.json"), None)
    have = {p.name.split(".")[-2] for p in item_dir.glob("*.srt")}
    if not info_f:
        return {"have": sorted(have), "wanted": [], "missing": [], "original": False, "manual": []}
    info = json.loads(info_f.read_text())
    manual = [k for k in (info.get("subtitles") or {}) if k.split("-")[0] in CAPTION_LANGS]
    auto = info.get("automatic_captions") or {}
    wanted = set(manual) | {k for k in auto if k.endswith("-orig") and k.split("-")[0] in CAPTION_LANGS}
    if "en" in auto:
        wanted.add("en")
    lang = (info.get("language") or "").split("-")[0]
    original = (any(l in have for l in manual) or any(l.endswith("-orig") for l in have)
                or (lang in CAPTION_LANGS and lang in have))
    return {"have": sorted(have), "wanted": sorted(wanted), "missing": sorted(wanted - have),
            "original": original, "manual": manual, "id": info.get("id"),
            "url": info.get("webpage_url"), "language": info.get("language")}


# ---------------------------------------------------------------- live status for status.py

NOW_DIR = LOGS / "now"


def heartbeat(stage: str, **fields):
    """Record what this process is doing right now (logs/now/<stage>.json).
    status.py shows it while the pid is alive. Typical fields: item, index, total,
    item_started (epoch s), item_progress (0-1), detail, expected_s."""
    try:  # status reporting must never take a job down
        write_json(NOW_DIR / f"{stage}.json", {"stage": stage, "pid": os.getpid(), "updated": now(),
                                               "updated_ts": time.time(), **fields})
    except OSError:
        pass


def in_shard(p: Path, shard: str | None) -> bool:
    """--shard i/n: stable split of files across n parallel workers (by path hash)."""
    if not shard:
        return True
    i, n = (int(x) for x in shard.split("/"))
    import zlib
    return zlib.crc32(str(p).encode()) % n == i


DOWNLOADERS = r"fetch_youtube\.sh|fetch_media\.py|fetch_apify\.py"


def downloads_active() -> bool:
    return subprocess.run(["pgrep", "-f", DOWNLOADERS], capture_output=True).returncode == 0


def follow(build_todo, process, enabled: bool, idle_s: int = 300):
    """Run process(todo) on build_todo(), and with enabled=True keep rescanning while
    downloads are still landing new files. Items that failed once are not retried in
    this run (they will be on the next run)."""
    failed: set = set()
    while True:
        todo = [t for t in build_todo() if str(t[-1]) not in failed]
        if todo:
            failed |= {str(x) for x in process(todo)}
            if enabled:
                continue
        if not enabled or not downloads_active():
            return
        log(f"waiting for new downloads (rescan in {idle_s}s)")
        time.sleep(idle_s)


# ---------------------------------------------------------------- processing pause (pause.py)

PAUSE_FILE = LOGS / "processing.pause"  # pauses transcription and OCR together


def stage_pause_file(stage: str):
    """Pause for one stage only ("transcribe" or "ocr", shards included): logs/pause.<stage>.
    Kept apart from PAUSE_FILE so workers that only know the shared pause aren't held by it."""
    return LOGS / f"pause.{'ocr' if stage.startswith('ocr') else stage}"


def _read_pause(path):
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return False, None, ""
    until = d.get("until_ts")
    if until is not None and time.time() >= until:
        return False, until, d.get("note", "")
    return True, until, d.get("note", "")


def pause_state(stage: str | None = None):
    """(paused, until_epoch or None, note) from logs/processing.pause, or from the stage's own
    pause file when a stage is given, if present and not expired."""
    state = _read_pause(PAUSE_FILE)
    if not state[0] and stage:
        state = _read_pause(stage_pause_file(stage))
    return state


def _rss_gb(pid: int | None = None) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid or os.getpid())], capture_output=True, text=True).stdout
    return int(out.strip() or 0) / 2**20


def _mem_free_pct() -> int | None:
    """macOS's own 'memory free' percentage (what memory_pressure reports), or None if unavailable."""
    try:
        return int(subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True).stdout)
    except ValueError:
        return None


# hold all workers when macOS reports less than this % memory free, resume at mem_resume_pct
MEM_HOLD_PCT, MEM_RESUME_PCT = int(CFG["processing"]["mem_hold_pct"]), int(CFG["processing"]["mem_resume_pct"])


def memory_guard(stage: str):
    """Keep a runaway worker from taking the whole Mac down (it did: 2026-09-26 kernel panic, 3 leaking
    OCR workers at ~7.5 GB each on an 8 GB machine). Checked before each file:
      1. this worker above its RSS cap ([ocr]/[transcribe] max_rss_gb) -> re-exec itself: same args,
         same log, fresh memory; finished files are skipped on the way back in.
      2. the Mac as a whole nearly out of memory ([processing] mem_hold_pct) -> hold until it recovers.
         Set it below the healthy full-load level, or held workers (still holding their memory) never resume."""
    cap = float(CFG["ocr" if stage.startswith("ocr") else stage]["max_rss_gb"])
    rss = _rss_gb()
    if rss > cap:
        log(f"memory guard: RSS {rss:.1f} GB > {cap} GB cap, restarting this worker")
        heartbeat(stage, item=None, detail=f"restarting (RSS {rss:.1f} GB > {cap} GB)")
        import sys
        sys.stdout.flush(); sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, *sys.argv])
    free = _mem_free_pct()
    if free is None or free >= MEM_HOLD_PCT:
        return
    log(f"memory guard: Mac has {free}% memory free, holding until {MEM_RESUME_PCT}%")
    while (free := _mem_free_pct()) is not None and free < MEM_RESUME_PCT:
        heartbeat(stage, item=None, detail=f"holding: {free}% memory free", paused=True)
        time.sleep(30)
    log(f"memory guard: {free if free is not None else '?'}% free, resuming")


def wait_if_paused(stage: str):
    """Block while transcription/OCR are paused (checked before each file). Removing the pause
    file or reaching its resume time lets work continue within a minute. Also runs memory_guard."""
    memory_guard(stage)
    announced = False
    while True:
        paused, until, note = pause_state(stage)
        if not paused:
            if announced:
                log("pause over, resuming")
            return
        when = datetime.fromtimestamp(until).astimezone().strftime("%Y-%m-%d %H:%M %Z") if until else "manual resume"
        if not announced:
            log(f"paused until {when}" + (f" ({note})" if note else ""))
            announced = True
        heartbeat(stage, item=None, detail=f"paused until {when}", paused=True)
        time.sleep(60)
