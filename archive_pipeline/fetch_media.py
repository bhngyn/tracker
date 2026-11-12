"""Download Instagram / Facebook media referenced by the saved Apify datasets.

usage: fetch_media.py [--platform fb|ig|all] [--only photos|videos|all]
                      [--limit N] [--workers 4] [--dry-run]

Layout (under the project archive_root):
  facebook/<postId>/                 post.json  media.json  01_photo_<id>.jpg  02_video_<id>.mp4 ...
  instagram/<username>/<shortcode>/  post.json  media.json  01.jpg  02.mp4  02_poster.jpg ...

media.json records, per file: source URL, kind, SHA-256, bytes, fetch time (UTC) and
status. Resumable: entries already marked ok (and present on disk) are skipped.
Direct CDN links are signed and expire (`oe=` hex timestamp); expired ones are
recorded as `expired` without a request. FB videos go through yt-dlp on the
permalink, so they don't depend on signed links.
"""
import argparse, hashlib, json, os, re, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import httpx

from common import CFG, HERE, ROOT, YTDLP

RAW = ROOT / "raw"
MAX_RES = int(CFG["youtube"]["max_res"])
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
       "video/mp4": ".mp4", "audio/mp4": ".m4a", "image/gif": ".gif"}
log_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str):
    with log_lock:
        print(f"{now()} {msg}", flush=True)


def link_expiry(url: str):
    m = re.search(r"[?&]oe=([0-9A-Fa-f]{8})", url or "")
    return int(m.group(1), 16) if m else None


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- job planning
# A job = one file to fetch: {dir, stem, url, kind, method: http|ytdlp, key}

def load_items(pattern: str, key: str, exclude=("_test",)):
    """Merge every raw/<pattern> dataset by `key`; later files (mtime) override earlier."""
    files = sorted((f for f in RAW.glob(pattern)
                    if not f.name.endswith(".run.json") and not any(x in f.name for x in exclude)),
                   key=lambda f: f.stat().st_mtime)
    merged = {}
    for f in files:
        for it in json.loads(f.read_text()):
            k = it.get(key)
            if k:
                merged[k] = it | {"_source_file": f.name}
    return merged


def fb_jobs(only: str):
    posts = load_items("facebook*.json", "postId")
    out = []
    for pid, p in posts.items():
        d = ROOT / "facebook" / pid
        jobs, n = [], 0
        for m in p.get("media") or []:
            t = m.get("__typename")
            mid = m.get("id") or f"x{n}"
            if t == "Photo":
                n += 1
                url = (m.get("photo_image") or m.get("image") or {}).get("uri") or m.get("thumbnail")
                if only in ("all", "photos") and url:
                    jobs.append(dict(stem=f"{n:02d}_photo_{mid}", url=url, kind="photo", method="http"))
            elif t == "Video":
                n += 1
                if only in ("all", "videos"):
                    link = m.get("url") or m.get("permalink_url") or p.get("url")
                    jobs.append(dict(stem=f"{n:02d}_video_{mid}", url=link, kind="video", method="ytdlp"))
                thumb = m.get("thumbnail") or (m.get("thumbnailImage") or {}).get("uri")
                if only in ("all", "photos") and thumb:
                    jobs.append(dict(stem=f"{n:02d}_video_{mid}_thumb", url=thumb, kind="video_thumbnail", method="http"))
            elif t == "GenericAttachmentMedia":
                img = (m.get("large_share_image") or m.get("image") or {}).get("uri") or m.get("thumbnail")
                if only in ("all", "photos") and img:
                    n += 1
                    jobs.append(dict(stem=f"{n:02d}_linkpreview_{mid}", url=img, kind="link_preview", method="http"))
        out.append((d, p, jobs))
    return out


def ig_jobs(only: str):
    posts = load_items("instagram*.json", "shortCode")
    out = []
    for sc, p in posts.items():
        user = p.get("ownerUsername") or "_unknown"
        d = ROOT / "instagram" / user / sc
        if p.get("error"):
            out.append((d, p, []))
            continue
        nodes = p.get("childPosts") or []
        if not nodes:
            if p.get("type") == "Sidecar" and p.get("images"):
                nodes = [{"type": "Image", "displayUrl": u} for u in p["images"]]
            else:
                nodes = [p]
        jobs = []
        for i, c in enumerate(nodes, 1):
            if c.get("type") == "Video" or c.get("videoUrl"):
                if only in ("all", "videos") and c.get("videoUrl"):
                    jobs.append(dict(stem=f"{i:02d}", url=c["videoUrl"], kind="video", method="http"))
                if only in ("all", "photos") and c.get("displayUrl"):
                    jobs.append(dict(stem=f"{i:02d}_poster", url=c["displayUrl"], kind="video_poster", method="http"))
            elif only in ("all", "photos") and c.get("displayUrl"):
                jobs.append(dict(stem=f"{i:02d}", url=c["displayUrl"], kind="photo", method="http"))
        out.append((d, p, jobs))
    return out


# ---------------------------------------------------------------- fetching

class Manifest:
    """media.json for one post dir; thread-safe per dir via a lock."""
    locks: dict = {}

    def __init__(self, d: Path):
        self.path = d / "media.json"
        self.lock = Manifest.locks.setdefault(str(d), threading.Lock())

    def get(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def put(self, stem: str, rec: dict):
        with self.lock:
            m = self.get()
            m[stem] = rec
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False))
            tmp.replace(self.path)


def done(d: Path, stem: str) -> bool:
    rec = Manifest(d).get().get(stem)
    return bool(rec and rec.get("status") == "ok" and (d / rec["file"]).exists())


# ISP-embedded FB/IG caches (*.fna.fbcdn.net etc.) only resolve inside the scraper's
# ISP. The signature isn't bound to the host, so the same path works on a public edge.
PUBLIC_EDGE = {"fbcdn.net": "scontent.xx.fbcdn.net", "cdninstagram.com": "scontent.cdninstagram.com"}


def public_edge(url: str):
    host = httpx.URL(url).host
    for dom, edge in PUBLIC_EDGE.items():
        if host.endswith(dom) and host != edge:
            return str(httpx.URL(url).copy_with(host=edge))
    return None


def fetch_http(client: httpx.Client, d: Path, job: dict) -> dict:
    exp = link_expiry(job["url"])
    rec = dict(kind=job["kind"], source_url=job["url"], method="http",
               link_expires=datetime.fromtimestamp(exp, timezone.utc).isoformat() if exp else None)
    if exp and exp < time.time():
        return rec | dict(status="expired", fetched_at=now())
    url, err = job["url"], None
    for attempt in range(4):
        try:
            with client.stream("GET", url) as r:
                if r.status_code in (403, 404, 410):
                    return rec | dict(status=f"http_{r.status_code}", fetched_at=now())
                r.raise_for_status()
                ctype = r.headers.get("content-type", "").split(";")[0].strip()
                ext = EXT.get(ctype) or (".mp4" if job["kind"] == "video" else ".jpg")
                dest = d / f"{job['stem']}{ext}"
                part = dest.with_suffix(dest.suffix + ".part")
                with part.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
                part.replace(dest)
                extra = {"fetched_url": url} if url != job["url"] else {}
                return rec | extra | dict(status="ok", file=dest.name, content_type=ctype,
                                          bytes=dest.stat().st_size, sha256=sha256(dest), fetched_at=now())
        except httpx.ConnectError as e:
            err = repr(e)
            if url == job["url"] and (alt := public_edge(url)):
                url = alt  # unresolvable/unreachable cache host: retry on the public edge at once
                continue
            time.sleep(2 ** attempt * 2)
        except (httpx.HTTPError, OSError) as e:
            err = repr(e)
            time.sleep(2 ** attempt * 2)
    return rec | dict(status="error", error=err, fetched_at=now())


def fetch_ytdlp(d: Path, job: dict) -> dict:
    rec = dict(kind=job["kind"], source_url=job["url"], method="yt-dlp")
    cmd = [str(YTDLP), job["url"], "--no-progress", "--no-overwrites", "--no-playlist",
           # res:720 caps the *shorter* side, so vertical reels get 720x1280, not 360p
           "-f", "bv*+ba/b", "-S", f"res:{MAX_RES},vcodec:h264,acodec:aac",
           "--merge-output-format", "mp4", "--write-info-json",
           "--write-subs", "--sub-langs", "all", "--convert-subs", "srt",
           "--retries", "10", "--sleep-requests", "1",
           "-o", str(d / f"{job['stem']}.%(ext)s")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    mp4 = d / f"{job['stem']}.mp4"
    if r.returncode == 0 and mp4.exists():
        ver = subprocess.run([str(YTDLP), "--version"], capture_output=True, text=True).stdout.strip()
        return rec | dict(status="ok", file=mp4.name, bytes=mp4.stat().st_size, sha256=sha256(mp4),
                          tool=f"yt-dlp {ver}", fetched_at=now())
    return rec | dict(status="error", error=(r.stderr or r.stdout)[-800:], fetched_at=now())


RUN = {"done": 0, "total": 0, "stage": "media", "started": time.time()}


def run_job(client, d: Path, job: dict):
    from common import heartbeat
    if job["method"] == "ytdlp":
        heartbeat(RUN["stage"], item=str(d.relative_to(ROOT)), index=RUN["done"] + 1, total=RUN["total"],
                  item_started=time.time(), detail=f"yt-dlp {job['url']}")
    rec = fetch_ytdlp(d, job) if job["method"] == "ytdlp" else fetch_http(client, d, job)
    RUN["done"] += 1
    if job["method"] != "ytdlp":
        heartbeat(RUN["stage"], item=str(d.relative_to(ROOT)), index=RUN["done"], total=RUN["total"],
                  detail=f"last: {rec.get('file', job['stem'])} ({rec['status']})")
    Manifest(d).put(job["stem"], rec)
    log(f"{rec['status']:>9}  {d.relative_to(ROOT)}/{rec.get('file', job['stem'])}")
    return rec["status"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["fb", "ig", "all"], default="all")
    ap.add_argument("--only", choices=["photos", "videos", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0, help="max posts per platform (testing)")
    ap.add_argument("--workers", type=int, default=4, help="parallel HTTP downloads")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    plan = []
    if a.platform in ("fb", "all"):
        plan += fb_jobs(a.only)[: a.limit or None]
    if a.platform in ("ig", "all"):
        plan += ig_jobs(a.only)[: a.limit or None]

    http, ytdlp, skipped = [], [], 0
    for d, post, jobs in plan:
        if not a.dry_run:
            d.mkdir(parents=True, exist_ok=True)
            (d / "post.json").write_text(json.dumps(post, indent=1, ensure_ascii=False))
        for j in jobs:
            if done(d, j["stem"]):
                skipped += 1
            else:
                (ytdlp if j["method"] == "ytdlp" else http).append((d, j))
    expired = sum(1 for _, j in http if (e := link_expiry(j["url"])) and e < time.time())
    log(f"{len(plan)} posts | {len(http)} http files ({expired} already expired) | "
        f"{len(ytdlp)} yt-dlp videos | {skipped} already done")
    if a.dry_run:
        return

    RUN.update(total=len(http) + len(ytdlp), stage=f"media_{a.platform}_{a.only}")
    # soonest-expiring links first
    http.sort(key=lambda x: link_expiry(x[1]["url"]) or 2**40)
    stats: dict = {}
    with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": UA}) as client:
        with ThreadPoolExecutor(a.workers) as ex:
            for s in ex.map(lambda x: run_job(client, *x), http):
                stats[s] = stats.get(s, 0) + 1
        for d, j in ytdlp:  # sequential: be polite to facebook.com
            s = run_job(None, d, j)
            stats[s] = stats.get(s, 0) + 1
    log(f"finished: {stats}")


if __name__ == "__main__":
    main()
