"""Stage 9: package the archive for the team.

usage: package.py [--full] [--lite] [--verify] [--out <dir>]   (default out: [project] packages_dir)
       (no flags = --full --lite)

--full    The archive root itself is the Full package (open "OPEN ME.html"; copy the whole
          folder to share). Writes METHODOLOGY.md and MANIFEST.sha256 into it.
--lite    Everything except video files, in <out>/<slug>_archive_lite_<date>/ plus a .zip.
          The viewer runs in Lite mode: videos link to the original post (YouTube at the
          matched timestamp). Has its own METHODOLOGY.md + MANIFEST.sha256.
--verify  Re-hash every file listed in the archive's MANIFEST.sha256 and report mismatches.

Hashes are cached in logs/hash_cache.json by (size, mtime), so re-runs only hash new or
changed files. Run build_index.py first so the viewer data and exports are current.
"""
import argparse, hashlib, json, platform, re, shutil, subprocess, sys, zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from common import CFG, HERE, LOGS, PACKAGES, ROOT, YTDLP, VIDEO_EXT, downloads_active, log, now, resolve

HASH_CACHE = LOGS / "hash_cache.json"
INCLUDE_DIRS = ("youtube", "facebook", "instagram", "derived", "raw", "viewer", "exports")
INCLUDE_FILES = ("OPEN ME.html", "METHODOLOGY.md")
SKIP = re.compile(r"(\.part$|\.part-Frag|\.tmp$|\.ytdl$|\.temp\.|/\.DS_Store$|/\._)")
# Post counts the Instagram profiles reported (optional, from the project config) to state coverage gaps
IG_PROFILE_POSTS = dict(CFG["instagram"]["profile_posts"])
IG_PROFILE_DATE = CFG["instagram"]["profile_posts_date"]
ACTORS = {"shu8hvrXbJbY3Eb9W": "apify/instagram-scraper", "nH2AHrwxeTRJoN5hX": "apify/instagram-post-scraper",
          "KoJrdxJCTtpon81KY": "apify/facebook-posts-scraper"}


# ---------------------------------------------------------------- files + hashes

def package_files(root: Path, lite=False):
    files = [root / f for f in INCLUDE_FILES if (root / f).exists()]
    for d in INCLUDE_DIRS:
        for p in sorted((root / d).rglob("*")):
            if not p.is_file() or SKIP.search(str(p)):
                continue
            if lite and p.suffix.lower() in VIDEO_EXT | {".m4a", ".webm"}:
                continue
            files.append(p)
    return files


def hash_files(files, root: Path, cache_path: Path | None):
    cache = json.loads(cache_path.read_text()) if cache_path and cache_path.exists() else {}
    out, fresh, t_bytes = {}, 0, 0
    for i, p in enumerate(files, 1):
        st = p.stat()
        key = str(p)
        c = cache.get(key)
        if c and c[0] == st.st_size and c[1] == st.st_mtime:
            out[p] = c[2]
            continue
        h = hashlib.sha256()
        with p.open("rb") as f:
            for b in iter(lambda: f.read(1 << 22), b""):
                h.update(b)
        out[p] = h.hexdigest()
        cache[key] = [st.st_size, st.st_mtime, out[p]]
        fresh += 1
        t_bytes += st.st_size
        if fresh % 500 == 0 and cache_path:
            cache_path.write_text(json.dumps(cache))
            log(f"hashed {fresh:,} new files ({t_bytes / 1e9:.1f} GB), {i:,}/{len(files):,}")
    if cache_path:
        cache_path.write_text(json.dumps(cache))
    log(f"hashes: {len(files):,} files, {fresh:,} newly hashed ({t_bytes / 1e9:.1f} GB)")
    return out


def write_manifest(root: Path, hashes: dict):
    lines = [f"{h}  {p.relative_to(root)}" for p, h in sorted(hashes.items(), key=lambda kv: str(kv[0]))]
    (root / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    total = sum(p.stat().st_size for p in hashes)
    log(f"MANIFEST.sha256: {len(lines):,} files, {total / 1e9:.1f} GB")
    return len(lines), total


def verify(root: Path):
    man = root / "MANIFEST.sha256"
    bad = missing = ok = 0
    for line in man.read_text(encoding="utf-8").splitlines():
        h, rel = line.split("  ", 1)
        p = root / rel
        if not p.exists():
            missing += 1
            log(f"MISSING {rel}")
            continue
        d = hashlib.sha256()
        with p.open("rb") as f:
            for b in iter(lambda: f.read(1 << 22), b""):
                d.update(b)
        if d.hexdigest() != h:
            bad += 1
            log(f"CHANGED {rel}")
        else:
            ok += 1
    log(f"verify: {ok:,} ok, {bad} changed, {missing} missing")
    return bad == missing == 0


# ---------------------------------------------------------------- methodology

def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return "?"


def pkg_version(name):
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version(name)
    except PackageNotFoundError:
        return "?"


def load_js(name):
    p = ROOT / "viewer" / "data" / name
    if not p.exists():
        return None
    m = re.search(r"window\.ARCHIVE\.\w+=(.*);\s*$", p.read_text(encoding="utf-8"), re.S)
    return json.loads(m.group(1)) if m else None


def gather():
    """Facts for METHODOLOGY.md, read from disk (never from memory)."""
    g = {"generated": now()}
    items = load_js("items.js") or []
    meta = load_js("meta.js") or {}
    g["meta"] = meta
    by = defaultdict(list)
    for it in items:
        by[(it["p"], it["a"])].append(it.get("d") or "")
    g["accounts"] = [{"platform": p, "account": a, "items": len(ds), "first": min(d for d in ds if d) if any(ds) else "",
                      "last": max(ds) if any(ds) else ""} for (p, a), ds in sorted(by.items())]
    g["ig_errors"] = sum(1 for it in items if it["p"] == "instagram" and it.get("err"))

    runs = []
    for f in sorted((ROOT / "raw").glob("*.run.json")):
        r = json.loads(f.read_text())
        runs.append({"dataset": f.name.replace(".run.json", ".json"), "run": r.get("id"),
                     "actor": ACTORS.get(r.get("actId"), r.get("actId")), "build": r.get("buildNumber"),
                     "status": r.get("status"), "started": r.get("startedAt"), "finished": r.get("finishedAt"),
                     "usd": r.get("usageTotalUsd") or 0})
    g["runs"] = runs

    # media downloads
    media = Counter()
    fetched = []
    for mf in list(ROOT.glob("facebook/*/media.json")) + list(ROOT.glob("instagram/*/*/media.json")):
        plat = mf.relative_to(ROOT).parts[0]
        for rec in json.loads(mf.read_text()).values():
            media[(plat, rec.get("kind"), rec.get("status"))] += 1
            if rec.get("fetched_at") and rec.get("status") == "ok":
                fetched.append(rec["fetched_at"])
            if rec.get("fetched_url"):
                media[(plat, "via_public_edge", "ok")] += 1
    g["media"] = {"|".join(k): v for k, v in sorted(media.items())}
    g["media_window"] = (min(fetched), max(fetched)) if fetched else None

    # youtube
    arch = ROOT / "youtube" / "archive.txt"
    # archive.txt is yt-dlp's download archive: one line per download event, with duplicate lines, so count unique ids
    ids = {l.split()[-1] for l in arch.read_text().splitlines() if l.strip()} if arch.exists() else set()
    g["yt_downloaded"] = len(ids)
    st = json.loads((LOGS / "status.json").read_text()) if (LOGS / "status.json").exists() else {}
    # status.json's "expected" is the SUM of the per-tab sizes, so a video listed on several tabs is counted once per tab
    g["yt_tab_listings"] = st.get("youtube", {}).get("expected")
    g["yt_tabs"] = st.get("youtube", {}).get("tabs", {})
    if g["yt_tabs"]:
        g["yt_tab_listings"] = sum(g["yt_tabs"].values())
    g["yt_caption_gaps"] = st.get("youtube", {}).get("missing_captions")
    mt = [p.stat().st_mtime for p in ROOT.glob("youtube/*/*/*.info.json")]
    g["yt_window"] = (datetime.fromtimestamp(min(mt), timezone.utc).isoformat(timespec="seconds"),
                      datetime.fromtimestamp(max(mt), timezone.utc).isoformat(timespec="seconds")) if mt else None
    g["whisper"] = len(list(ROOT.glob("derived/**/whisper.json")))
    g["ocr"] = len(list(ROOT.glob("derived/**/ocr.json")))
    g["ongoing"] = downloads_active() or bool(subprocess.run(["pgrep", "-f", "transcribe.py|ocr.py"],
                                                             capture_output=True).returncode == 0)
    wl = CFG["watchlist"]["path"]
    gaz = json.loads(resolve(CFG, wl).read_text(encoding="utf-8")) if wl else []
    g["watchlist"] = {"n": len(gaz), "cats": Counter(p["category"] for p in gaz), "ambiguous": sum(p["ambiguous"] for p in gaz)}
    g["tools"] = {
        "yt-dlp": sh([str(YTDLP), "--version"]), "ffmpeg": sh(["ffmpeg", "-version"]).split("\n")[0].replace("ffmpeg version ", "").split(" ")[0],
        "Python": platform.python_version(), "mlx-whisper": pkg_version("mlx-whisper"), "mlx": pkg_version("mlx"),
        "ocrmac": pkg_version("ocrmac"), "httpx": pkg_version("httpx"),
        "macOS": sh(["sw_vers", "-productVersion"]), "machine": sh(["sysctl", "-n", "machdep.cpu.brand_string"]),
    }
    return g


def sources_md(g=None):
    """Section 1 source list, generated from the project config (and, for Facebook, the date range found in the index)."""
    fb_acc = [a for a in (g or {}).get("accounts", []) if a["platform"] == "facebook"]
    fb_first = min((a["first"] for a in fb_acc if a["first"]), default="")
    fb_last = max((a["last"] for a in fb_acc if a["last"]), default="")
    yt, fb, ig = CFG["youtube"]["tabs"], CFG["facebook"]["pages"], CFG["instagram"]["accounts"]
    out = []
    if yt:
        out.append("- YouTube: " + ", ".join(f"`{u}`" for u in yt) + ".")
    for p in fb:
        extra = f" ({fb_first} to {fb_last}, as returned by the scraper; see the datasets in §3)" if fb_first else ""
        out.append(f"- Facebook `{p['url']}`: page posts{extra}.")
    if ig:
        out.append("- Instagram " + ", ".join(f"`@{a}`" for a in ig) + ": all posts.")
    return "\n".join(out) or "- (no sources configured)"


def methodology(g, lite=False):
    m = g["meta"]
    runs = "\n".join(f"| `{r['dataset']}` | {r['actor']} (build {r['build']}) | `{r['run']}` | {r['status']} | "
                     f"{(r['started'] or '')[:16].replace('T', ' ')} → {(r['finished'] or '')[:16].replace('T', ' ')} | ${r['usd']:.2f} |"
                     for r in g["runs"]) or "| — | — | — | — | — | — |"
    accts = "\n".join(f"| {a['platform']} | {a['account']} | {a['items']:,} | {a['first']} | {a['last']} |" for a in g["accounts"])
    med = g["media"]
    def mc(plat, kind, status="ok"):
        return med.get(f"{plat}|{kind}|{status}", 0)
    failed = {k: v for k, v in med.items() if not k.endswith("|ok")}
    n_vid, n_list = g["yt_downloaded"], g["yt_tab_listings"]
    yt_count = (f"{n_vid:,} unique videos in the download archive"
                + (f" (the channel tabs list {n_list:,} entries in total; a video can appear on more than one tab, "
                   f"so {n_list - n_vid:,} entries are repeats)" if n_list and n_list > n_vid else ""))
    tabs = ", ".join(f"{k.split('youtube.com/')[-1]}: {v:,}" for k, v in g["yt_tabs"].items()) or "n/a"
    t = g["tools"]
    gz = g["watchlist"]
    M = CFG["methodology"]
    capt = ", ".join(CFG["youtube"]["caption_langs"])
    ocr_langs = " + ".join(CFG["ocr"]["languages"])
    res = CFG["youtube"]["max_res"]
    targets = set(CFG["instagram"]["accounts"])
    ig_cov = "; ".join(
        f"@{a['account']}: {a['items']:,} of {IG_PROFILE_POSTS[a['account']]:,} posts the profile reported"
        + (f" on {IG_PROFILE_DATE}" if IG_PROFILE_DATE else "")
        + (f" (collection reaches back only to {a['first']})" if a['items'] < IG_PROFILE_POSTS[a['account']] - 5 else "")
        for a in g['accounts'] if a['platform'] == 'instagram' and a['account'] in IG_PROFILE_POSTS)
    collab = [a['account'] for a in g['accounts'] if a['platform'] == 'instagram' and targets and a['account'] not in targets]
    extra = "\n".join(f"- {x}" for x in M["extra_limitations"])
    return f"""# Methodology — {CFG['project']['name']} archive

Generated {g['generated']} by the archive pipeline's `package.py`. {'This is the **Lite** package: it contains everything except video files. Videos open on the original platform instead.' if lite else 'This is the **Full** package.'}
{'> **Snapshot note:** collection or processing was still running when this was generated. Later packages supersede it.' if g['ongoing'] else ''}

## 1. Purpose and scope

{' '.join(M['purpose'].split()) or 'This archive preserves public social-media output for review. A match in the index is a pointer for a human reviewer to check against the original media, **not** a finding.'}

Sources:
{sources_md(g)}

## 2. What is in the package

| Platform | Account | Items | Earliest | Latest |
|---|---|---|---|---|
{accts}
{chr(10) + 'Instagram accounts other than the configured targets (' + ', '.join(collab) + ') are collaboration posts that appear on the target profiles (Instagram lists them under the co-author\'s name).' + chr(10) if collab else ''}
Index: {m.get('items', 0):,} items, {m.get('segments', 0):,} searchable text passages, {m.get('hits', 0):,} watchlist matches
(built {m.get('built', '?')}).

## 3. Collection

**YouTube:** yt-dlp {t['yt-dlp']} downloaded each video with its metadata (`.info.json`), description, thumbnail
and captions (uploaded and YouTube automatic; languages {capt}; converted to SRT). Video is capped at {res}p
(shorter side), preferring H.264 + AAC. Downloaded {yt_count}. Entries per tab: {tabs}. Collection window (metadata file times, UTC):
{g['yt_window'][0] + ' → ' + g['yt_window'][1] if g['yt_window'] else 'n/a'}.

**Instagram and Facebook metadata:** collected through the Apify platform. Each run's full output is kept unmodified in
`raw/` together with its run record (`*.run.json`) as evidence of collection:

| Dataset | Actor | Run id | Status | Started → finished (UTC) | Cost |
|---|---|---|---|---|---|
{runs}

Runs marked ABORTED were stopped deliberately (budget/pause decisions). Their output up to that point is complete and kept.
Where the same post appears in several datasets, the most recent capture is used for the index. All captures stay in `raw/`.

**Instagram and Facebook media:** `fetch_media.py` downloaded the photos and videos referenced in those datasets.
For every file, `media.json` in the post folder records the source URL, the time it was fetched (UTC), its size and
its SHA-256. Facebook videos were fetched with yt-dlp from the post permalink. Everything else came from the platform CDN.
Downloaded: Facebook {mc('facebook', 'photo')} photos, {mc('facebook', 'video')} videos, {mc('facebook', 'video_thumbnail')} video
thumbnails, {mc('facebook', 'link_preview')} link previews; Instagram {mc('instagram', 'photo')} photos, {mc('instagram', 'video')} videos,
{mc('instagram', 'video_poster')} video cover images. Fetch window: {' → '.join(g['media_window']) if g['media_window'] else 'n/a'}.
{mc('facebook', 'via_public_edge')} Facebook files were fetched from Facebook's public CDN host rather than the regional cache host named
in the dataset (same signed path; both URLs are recorded).

## 4. Processing

- **Speech → text:** mlx-whisper {t['mlx-whisper']} with `{CFG['transcribe']['model'].split('/')[-1]}`, language auto-detected, on every
  Facebook and Instagram video and on {'every YouTube video' if CFG['transcribe']['youtube'] == 'all' else 'no YouTube video' if CFG['transcribe']['youtube'] == 'none' else 'YouTube videos with no transcript of the speech (no uploaded captions and no original-language automatic captions)'}.
  Output: `derived/<file>/whisper.json` + `whisper.srt`. {g['whisper']:,} files transcribed.
- **On-screen text (OCR):** Apple Vision (via ocrmac {t['ocrmac']}, "accurate" mode, {ocr_langs}). For video, ffmpeg
  ({t['ffmpeg']}) decodes keyframes and keeps at most one every ~{CFG['ocr']['interval_s']:g} s. Near-identical consecutive frames and blank frames are
  skipped, and timestamps are the frame's real position in the video. Photos, thumbnails and cover images are OCR'd too.
  Output: `derived/<file>/ocr.json`. {g['ocr']:,} files processed. Lines that recur across many videos (channel watermark,
  "Subscribe") and lines on most frames of one video are dropped from the index, but kept in `ocr.json`.
- **Watchlist matching ({CFG['watchlist']['label']}):** {f"{gz['n']} entries ({', '.join(f'{v} {k}' for k, v in sorted(gz['cats'].items()))}): {' '.join(M['watchlist_description'].split()) or 'see the project watchlist file.'}" if gz['n'] else 'no watchlist configured.'}
  Matching is whole-word and ignores case, accents and vowel marks{'; native-script names may carry attached prefixes' if CFG['watchlist']['native_prefixes'] else ''}.
  **Weak matches** ({gz['ambiguous']} names that are also common words or first names, plus weak native-script forms) are ranked
  lower and hidden by default in the viewer.
- **Index and viewer:** `build_index.py` merges post text, titles, descriptions, captions, transcripts, OCR, platform image
  descriptions, Instagram location tags and comments into `viewer/data/` (for `OPEN ME.html`) and `exports/`
  (`items.csv`, `hits.csv`, `archive.sqlite` with an FTS5 full-text table).

## 5. Known gaps and limitations

- **Facebook coverage** is the union of the `raw/facebook*.json` datasets (see §3), de-duplicated by post. It runs from the earliest post the scraper returned to the collection date (first/last dates in the table in §2). Whether the scraper reached the page's very first post cannot be checked from here.
- **Instagram:** {g['ig_errors']} posts came back from the platform with an error (e.g. restricted), so only partial metadata and no media.{(chr(10) + '  ' + ig_cov + '.') if ig_cov else ''}
- **Media downloads that failed** (by platform|kind|status): {', '.join(f'{k} {v}' for k, v in failed.items()) or 'none'}.
- **YouTube captions:** {g['yt_caption_gaps'] if g['yt_caption_gaps'] is not None else '?'} videos were missing at least one caption track at packaging time
  (YouTube rate limits). Where the speech had no transcript, Whisper filled the gap.
- **Photo resolution:** Facebook supplies photos pre-resized (about 590–960 px on the long side); originals are not available.
- **Video quality:** capped at {res}p (shorter side) to keep the archive portable. Facebook serves some videos only as VP9.
- **Machine transcripts, automatic captions and OCR contain errors**, especially names, non-English speech and noisy audio.
  Always confirm a match against the media before relying on it.
- Engagement counts (likes, views) are as captured at collection time and will have changed since.
{extra}

## 6. Integrity

`MANIFEST.sha256` lists a SHA-256 for every file in this package (except itself). To verify on macOS or Linux, run from the
package folder:

    shasum -a 256 -c MANIFEST.sha256

Per-file fetch records (`media.json`, `*.info.json`) and the raw datasets preserve provenance for each item.

## 7. Tools and environment

| Tool | Version |
|---|---|
{chr(10).join(f'| {k} | {v} |' for k, v in t.items())}

Pipeline code: the archive pipeline toolkit (`fetch_youtube.sh`, `fetch_apify.py`, `fetch_media.py`, `history_continue.py`,
`backfill_captions.py`, `transcribe.py`, `ocr.py`, `build_index.py`, `package.py`), run with this project's configuration.
"""


# ---------------------------------------------------------------- builds

def build_full(g):
    (ROOT / "METHODOLOGY.md").write_text(methodology(g), encoding="utf-8")
    files = [p for p in package_files(ROOT) if p.name != "MANIFEST.sha256"]
    n, total = write_manifest(ROOT, hash_files(files, ROOT, HASH_CACHE))
    log(f"Full package = {ROOT}  ({n:,} files, {total / 1e9:.1f} GB). Share by copying the whole folder.")


def build_lite(g, out: Path):
    stamp = datetime.now().strftime("%Y%m%d")
    dest = out / f"{CFG['project']['slug']}_archive_lite_{stamp}"
    files = package_files(ROOT, lite=True)
    dest.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for p in files:
        rel = p.relative_to(ROOT)
        wanted.add(rel)
        q = dest / rel
        st = p.stat()
        if not q.exists() or q.stat().st_size != st.st_size or q.stat().st_mtime < st.st_mtime:
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, q)
    for q in [q for q in dest.rglob("*") if q.is_file()]:  # drop files no longer in the archive
        rel = q.relative_to(dest)
        if rel not in wanted and str(rel) not in ("MANIFEST.sha256", "METHODOLOGY.md", "viewer/config.js"):
            q.unlink()
    from build_index import viewer_config_js
    (dest / "viewer" / "config.js").write_text(viewer_config_js(lite=True), encoding="utf-8")
    (dest / "METHODOLOGY.md").write_text(methodology(g, lite=True), encoding="utf-8")
    lite_files = [p for p in package_files(dest, lite=True) if p.name != "MANIFEST.sha256"]
    write_manifest(dest, hash_files(lite_files, dest, None))
    zpath = out / f"{dest.name}.zip"
    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as z:
        for p in sorted(q for q in dest.rglob("*") if q.is_file()):
            comp = zipfile.ZIP_STORED if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"} else zipfile.ZIP_DEFLATED
            z.write(p, Path(dest.name) / p.relative_to(dest), compress_type=comp)
    tmp.replace(zpath)
    log(f"Lite package = {dest}  zip {zpath} ({zpath.stat().st_size / 1e9:.2f} GB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--lite", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--out", default=str(PACKAGES))
    a = ap.parse_args()
    if a.verify:
        sys.exit(0 if verify(ROOT) else 1)
    if not (a.full or a.lite):
        a.full = a.lite = True
    g = gather()
    if g["ongoing"]:
        log("note: collection/processing still running; this package is a snapshot")
    if a.full:
        build_full(g)
    if a.lite:
        build_lite(g, Path(a.out))


if __name__ == "__main__":
    main()
