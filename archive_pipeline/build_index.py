"""Stage 7-8: build the searchable index, place hits and exports from everything on disk.

usage: build_index.py

Reads raw/ datasets, youtube/ items, facebook/ + instagram/ media.json, and
derived/ (whisper.json, ocr.json) — whatever exists so far, so it can be re-run at
any point. Writes:
  viewer/data/items.js    window.ARCHIVE.items   one record per post/video
  viewer/data/search.js   window.ARCHIVE.segs    searchable text units [item, media, field, t, text]
  viewer/data/places.js   window.ARCHIVE.places  watchlist + hit counts; .hits
  exports/items.csv  exports/hits.csv  exports/archive.sqlite (FTS5)

Fields (where a piece of text came from): post (post text / title / description),
caption (YouTube captions), speech (Whisper), onscreen (video OCR), photo (image OCR),
alt (platform-generated image description), location (IG location tag), comment.
"""
import csv, json, re, sqlite3, unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from common import CFG, DERIVED, HERE, ROOT, derived_dir, log, now, parse_srt, resolve, youtube_caption_status
from fetch_media import load_items

OUT = ROOT / "viewer" / "data"
EXPORTS = ROOT / "exports"
FIELD_WEIGHT = {"location": 5, "post": 4, "onscreen": 3, "photo": 3, "speech": 2, "caption": 2, "alt": 2, "comment": 1}
WINDOW_S, WINDOW_CHARS = 20.0, 300  # merge caption/transcript cues into ~20 s search windows


def rel(p: Path | None):
    return str(p.relative_to(ROOT)) if p else None


def read_json(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def windows(segs):
    """Merge short timed cues into ~20 s windows so phrases spanning cues still match."""
    out, cur = [], None
    for s in segs:
        if cur and (s["start"] - cur["start"] > WINDOW_S or len(cur["text"]) > WINDOW_CHARS):
            out.append(cur)
            cur = None
        if cur is None:
            cur = {"start": s["start"], "text": s["text"]}
        else:
            cur["text"] += " " + s["text"]
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- OCR cleanup

def ocr_stoplines(ocr_docs):
    """Lines that recur across many videos (channel watermark, 'Subscribe') are noise."""
    per_video = Counter()
    for d in ocr_docs:
        per_video.update({l["text"].strip().upper() for f in d["frames"] for l in f["lines"]})
    n = max(len(ocr_docs), 1)
    return {t for t, c in per_video.items() if c >= 5 and c / n > 0.05}


def ocr_frames(doc, stop):
    """Drop stop lines and lines on >30% of this video's frames; collapse consecutive repeats."""
    frames = doc["frames"]
    local = Counter(l["text"].strip().upper() for f in frames for l in {x["text"]: x for x in f["lines"]}.values())
    thresh = max(3, 0.3 * len(frames))
    out, prev = [], None
    for f in frames:
        lines = [l["text"].strip() for l in f["lines"]
                 if l["text"].strip().upper() not in stop and local[l["text"].strip().upper()] < thresh
                 and len(l["text"].strip()) > 1]
        text = " / ".join(lines)
        if text and text != prev:
            out.append({"t": f["t"], "text": text})
        prev = text or prev
    return out


# ---------------------------------------------------------------- items

OVERRUN_SLACK = 5  # seconds of tolerance before timed text counts as "past the video's end"


def yt_is_collection(info: dict, folder: Path) -> bool:
    """True if a yt-dlp .info.json describes a playlist/channel page rather than one video.
    Primary signal is yt-dlp's own _type; fallbacks: entries/playlist_count with no duration,
    or no duration and the id equals channel_id / the folder is NA_<id>."""
    if info.get("_type") not in (None, "video", "url"):
        return True
    if info.get("duration"):
        return False
    if info.get("entries") is not None or info.get("playlist_count"):
        return True
    vid = info.get("id")
    return bool(vid) and (vid == info.get("channel_id") or folder.name == f"NA_{vid}")


class Builder:
    def __init__(self):
        self.items, self.segs = [], []
        self.video_ocr = []  # (item_idx, media_idx, doc) — cleaned once stoplines are known
        self.cap_overrun = {}  # item_idx -> {caption lang: [cues past the end, latest cue start]}

    def add_item(self, rec):
        self.items.append(rec)
        return len(self.items) - 1

    def seg(self, ii, mi, field, t, text):
        text = re.sub(r"\s+", " ", text or "").strip()
        if text:
            self.segs.append([ii, mi, field, None if t is None else round(t, 1), text])

    def derived_for(self, ii, mi, media: Path):
        dd = derived_dir(media)
        w = read_json(dd / "whisper.json")
        if w:
            for s in windows(w["segments"]):
                self.seg(ii, mi, "speech", s["start"], s["text"])
        o = read_json(dd / "ocr.json")
        if o and o.get("kind") == "video":
            self.video_ocr.append((ii, mi, o))
        elif o and o.get("kind") == "image":
            self.seg(ii, mi, "photo", None, o["text"].replace("\n", " / "))
        return {"speech": bool(w), "ocr": bool(o)}

    # ---- YouTube
    def youtube(self):
        n = skipped = 0
        for d in sorted(ROOT.glob("youtube/*/*/")):
            info = read_json(next(d.glob("*.info.json"), Path("/nonexistent")))
            if not info:
                continue
            if yt_is_collection(info, d):
                skipped += 1
                log(f"youtube: skipped playlist/channel page (not a video): {info.get('id')} "
                    f"{info.get('title')!r} [{d.relative_to(ROOT)}]")
                continue
            vid = info["id"]
            video = next((p for p in d.glob(f"{vid}.mp4")), None)
            thumb = next((p for p in d.glob(f"{vid}.jpg")), None)
            date = info.get("upload_date") or ""
            item = {"i": f"yt:{vid}", "p": "youtube", "a": info.get("channel") or info.get("channel_id"),
                    "d": f"{date[:4]}-{date[4:6]}-{date[6:]}" if date else None, "ti": info.get("title"),
                    "u": info.get("webpage_url"), "x": info.get("description") or "",
                    "tags": info.get("tags") or [], "dur": info.get("duration"),
                    "st": {"views": info.get("view_count"), "likes": info.get("like_count"),
                           "comments": info.get("comment_count")},
                    "m": [{"k": "video", "f": rel(video), "th": rel(thumb), "r": info.get("webpage_url")}]}
            ii = self.add_item(item)
            self.seg(ii, None, "post", None, info.get("title"))
            self.seg(ii, None, "post", None, info.get("description"))
            if item["tags"]:
                self.seg(ii, None, "post", None, " ".join(item["tags"]))
            cs = youtube_caption_status(d)
            manual = set(cs["manual"])
            langs = [l for l in cs["have"] if l in manual or l.endswith("-orig")]
            if "en" in cs["have"] and not any(l.startswith("en") for l in langs):
                langs.append("en")  # English auto-translation of a non-English video
            item["caps"] = langs
            for l in langs:
                for s in windows(parse_srt(d / f"{vid}.{l}.srt")):
                    self.seg(ii, 0, "caption", s["start"], s["text"])
                    if item["dur"] and s["start"] > item["dur"] + OVERRUN_SLACK:
                        o = self.cap_overrun.setdefault(ii, {}).setdefault(l, [0, 0.0])
                        o[0] += 1
                        o[1] = max(o[1], s["start"])
            if video:
                item["m"][0] |= self.derived_for(ii, 0, video)
            if thumb:
                o = read_json(derived_dir(thumb) / "ocr.json")
                if o:
                    self.seg(ii, None, "photo", None, o["text"].replace("\n", " / "))
            n += 1
        log(f"youtube: {n} items" + (f" ({skipped} playlist/channel pages skipped)" if skipped else ""))

    # ---- Facebook
    def facebook(self):
        posts = load_items("facebook*.json", "postId")
        for pid, p in sorted(posts.items(), key=lambda kv: kv[1].get("time") or ""):
            d = ROOT / "facebook" / pid
            man = read_json(d / "media.json") or {}
            item = {"i": f"fb:{pid}", "p": "facebook", "a": p.get("pageName"), "d": (p.get("time") or "")[:10] or None,
                    "ti": (p.get("text") or "").split("\n")[0][:140], "u": p.get("url"), "x": p.get("text") or "",
                    "st": {"likes": p.get("likes"), "comments": p.get("comments"), "shares": p.get("shares"),
                           "views": p.get("viewsCount")}, "m": []}
            ii = self.add_item(item)
            self.seg(ii, None, "post", None, p.get("text"))
            if p.get("link"):
                self.seg(ii, None, "post", None, " ".join(filter(None, [p.get("previewTitle"), p.get("previewDescription")])))
            by_stem = {k: v for k, v in man.items() if v.get("status") == "ok"}
            for m in p.get("media") or []:
                mid = m.get("id")
                if m.get("ocrText"):
                    self.seg(ii, None, "alt", None, m["ocrText"])
                def find(pattern):
                    return next((k for k in by_stem if mid and re.fullmatch(pattern.format(re.escape(mid)), k)), None)
                kind = {"Video": "video", "Photo": "photo"}.get(m.get("__typename"), "link")
                stem, thumb_stem = find(r"\d+_(?:photo|video)_{}"), find(r"\d+_(?:video_{}_thumb|linkpreview_{})".replace("{}", "{0}"))
                f = d / by_stem[stem]["file"] if stem else None
                th = d / by_stem[thumb_stem]["file"] if thumb_stem else (f if kind == "photo" else None)
                rec = {"k": kind, "f": rel(f), "th": rel(th), "r": m.get("url") or p.get("url")}
                item["m"].append(rec)
                mi = len(item["m"]) - 1
                if f:
                    rec |= self.derived_for(ii, mi, f)
                if th and th != f:
                    o = read_json(derived_dir(th) / "ocr.json")
                    if o:
                        self.seg(ii, mi, "photo", None, o["text"].replace("\n", " / "))
            for c in p.get("topComments") or []:
                self.seg(ii, None, "comment", None, c.get("text"))
        log(f"facebook: {len(posts)} items")

    # ---- Instagram
    def instagram(self):
        posts = load_items("instagram*.json", "shortCode")
        for sc, p in sorted(posts.items(), key=lambda kv: kv[1].get("timestamp") or ""):
            user = p.get("ownerUsername") or "_unknown"
            d = ROOT / "instagram" / user / sc
            man = {k: v for k, v in (read_json(d / "media.json") or {}).items() if v.get("status") == "ok"}
            item = {"i": f"ig:{sc}", "p": "instagram", "a": user, "d": (p.get("timestamp") or "")[:10] or None,
                    "ti": (p.get("caption") or "").split("\n")[0][:140], "u": p.get("url"), "x": p.get("caption") or "",
                    "loc": p.get("locationName"), "tags": p.get("hashtags") or [],
                    "st": {"likes": p.get("likesCount"), "comments": p.get("commentsCount"),
                           "views": p.get("videoViewCount")}, "m": [],
                    "err": p.get("error")}
            ii = self.add_item(item)
            self.seg(ii, None, "post", None, p.get("caption"))
            if p.get("locationName"):
                self.seg(ii, None, "location", None, p["locationName"])
            if p.get("alt") and not p["alt"].startswith(("Photo by", "Photo shared by")):
                self.seg(ii, None, "alt", None, p["alt"])
            stems = sorted({k.split("_")[0] for k in man})
            for s in stems:
                vid, poster = man.get(s) if man.get(s, {}).get("kind") == "video" else None, man.get(f"{s}_poster")
                photo = man.get(s) if not vid else None
                f = d / (vid or photo)["file"] if (vid or photo) else None
                th = d / poster["file"] if poster else (f if photo else None)
                rec = {"k": "video" if vid else "photo", "f": rel(f), "th": rel(th), "r": p.get("url")}
                item["m"].append(rec)
                mi = len(item["m"]) - 1
                if f:
                    rec |= self.derived_for(ii, mi, f)
                if poster:
                    o = read_json(derived_dir(th) / "ocr.json")
                    if o:
                        self.seg(ii, mi, "photo", None, o["text"].replace("\n", " / "))
            for c in p.get("latestComments") or []:
                self.seg(ii, None, "comment", None, c.get("text"))
        log(f"instagram: {len(posts)} items")

    def flag_timing_overruns(self):
        """Evidentiary flag, never a drop: set item["warn"] on YouTube items with timed text
        (caption / speech / onscreen) starting later than the video's own duration + OVERRUN_SLACK.
        The text stays searchable; the viewer should show warn like it shows err."""
        over = {}
        for ii, mi, field, t, _ in self.segs:
            it = self.items[ii]
            if t is not None and it["p"] == "youtube" and it.get("dur") and t > it["dur"] + OVERRUN_SLACK:
                o = over.setdefault(ii, {}).setdefault(field, [0, 0.0])
                o[0] += 1
                o[1] = max(o[1], t)
        for ii, fields in over.items():
            it = self.items[ii]
            parts = [f"{n} {f} entr{'y' if n == 1 else 'ies'} (latest at {int(mx // 60)}:{int(mx % 60):02d})"
                     for f, (n, mx) in sorted(fields.items())]
            langs = sorted(self.cap_overrun.get(ii, {}))
            via = f" (caption track{'s' if len(langs) > 1 else ''}: {', '.join(langs)})" if langs else ""
            dm = int(it["dur"])
            it["warn"] = (f"Timestamps run past the video's end ({dm // 60}:{dm % 60:02d}): " + "; ".join(parts) + via
                          + ". These captions likely come from a different or time-shifted version of the video, "
                          "so their seek times may not match this video. Text kept as published.")
        log(f"youtube: {len(over)} item(s) with timed text past the video's end: "
            + ", ".join(self.items[i]["i"] for i in over))

    def finish_ocr(self):
        stop = ocr_stoplines([o for *_, o in self.video_ocr])
        for ii, mi, o in self.video_ocr:
            for f in ocr_frames(o, stop):
                self.seg(ii, mi, "onscreen", f["t"], f["text"])
        log(f"video OCR: {len(self.video_ocr)} videos, {len(stop)} watermark/stop lines dropped")


# ---------------------------------------------------------------- place matching

# Watchlist entries: {id, name, category, region, note, variants: [...], ambiguous: bool,
#                     native: [...other-script forms], native_weak: [...forms that only count as weak]}
# ("he" / "he_ambiguous" are accepted as older names for native / native_weak.)
NATIVE_LETTERS = CFG["watchlist"]["native_letters"]      # regex class body, e.g. a script's letter range
NATIVE_PREFIXES = CFG["watchlist"]["native_prefixes"]    # letters that attach to the front of a name


def fold(s: str) -> str:
    """Case-preserving fold: drop accents and combining marks (incl. vowel points / cantillation)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def native(p):
    return p.get("native", p.get("he", []))


def native_weak(p):
    return p.get("native_weak", p.get("he_ambiguous", []))


def load_watchlist():
    path = CFG["watchlist"]["path"]
    if not path:
        return []
    return json.loads(resolve(CFG, path).read_text(encoding="utf-8"))


def place_matchers(gaz):
    """Case-insensitive whole-word regex for clear names; case-sensitive for ambiguous ones
    (a capitalised first name still matches, lowercase prose doesn't); native-script forms with
    optional attached prefixes; and weak native forms without prefixes (fewer accidental hits)."""
    en, amb, nat, amb_nat = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    for p in gaz:
        for v in p["variants"]:
            (amb if p["ambiguous"] else en)[fold(v).lower() if not p["ambiguous"] else fold(v)].add(p["id"])
        for h in native(p):
            (amb_nat if p["ambiguous"] or h in native_weak(p) else nat)[fold(h)].add(p["id"])

    def alt(keys):
        return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    L = NATIVE_LETTERS or r"\w"
    pre = f"(?:[{NATIVE_PREFIXES}]{{0,2}})" if NATIVE_PREFIXES else ""
    out = []
    if en:
        out.append((re.compile(rf"(?<![\w'’-])({alt(en)})(?![\w-])", re.I), lambda m: en[m.lower()], False))
    if amb:
        out.append((re.compile(rf"(?<![\w'’-])({alt(amb)})(?![\w-])"), lambda m: amb[m], True))
    if nat:
        out.append((re.compile(rf"(?<![{L}]){pre}({alt(nat)})(?![{L}])"), lambda m: nat[m], False))
    if amb_nat:
        out.append((re.compile(rf"(?<![{L}])({alt(amb_nat)})(?![{L}])"), lambda m: amb_nat[m], True))
    return out


def find_hits(segs, gaz, items):
    matchers = place_matchers(gaz)
    byid = {p["id"]: p for p in gaz}
    hits = []
    for si, (ii, mi, field, t, text) in enumerate(segs):
        ft = fold(text)
        seen = set()
        for rx, ids, weak in matchers:
            for m in rx.finditer(ft):
                for pid in ids(m.group(1)):
                    if (pid, m.start()) in seen:
                        continue
                    seen.add((pid, m.start()))
                    a, b = max(0, m.start() - 90), min(len(ft), m.end() + 90)
                    weak_hit = weak or byid[pid]["ambiguous"]
                    hits.append({"place": pid, "item": ii, "media": mi, "field": field, "t": t, "seg": si, "weak": weak_hit,
                                 "match": m.group(0), "quote": ("…" if a else "") + ft[a:b] + ("…" if b < len(ft) else ""),
                                 "score": FIELD_WEIGHT[field] * (1 if weak_hit else 3)})
    return hits


# ---------------------------------------------------------------- outputs

def write_js(p: Path, var: str, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(f"window.ARCHIVE=window.ARCHIVE||{{}};window.ARCHIVE.{var}=" +
                   json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    tmp.replace(p)


def exports(items, segs, hits, gaz):
    EXPORTS.mkdir(parents=True, exist_ok=True)
    byid = {p["id"]: p for p in gaz}
    with (EXPORTS / "items.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "platform", "account", "date", "title", "url", "location_tag", "n_media",
                    "local_files", "place_hits", "text"])
        nh = Counter(h["item"] for h in hits)
        for ii, it in enumerate(items):
            w.writerow([it["i"], it["p"], it["a"], it["d"], it["ti"], it["u"], it.get("loc") or "", len(it["m"]),
                        " | ".join(m["f"] for m in it["m"] if m.get("f")), nh[ii], it["x"]])
    with (EXPORTS / "hits.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["place_id", "place", "category", "region", "weak_match", "item_id", "platform", "date",
                    "field", "timestamp_s", "link", "matched", "quote", "local_file"])
        for h in sorted(hits, key=lambda h: (byid[h["place"]]["name"], items[h["item"]]["d"] or "")):
            it, pl = items[h["item"]], byid[h["place"]]
            m = it["m"][h["media"]] if h["media"] is not None and h["media"] < len(it["m"]) else {}
            link = it["u"]
            if it["p"] == "youtube" and h["t"] is not None:
                link = f"{it['u']}&t={int(h['t'])}s"
            w.writerow([pl["id"], pl["name"], pl["category"], pl["region"], h["weak"], it["i"], it["p"], it["d"],
                        h["field"], h["t"], link, h["match"], h["quote"], m.get("f") or ""])
    db = EXPORTS / "archive.sqlite"
    tmp = db.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    c = sqlite3.connect(tmp)
    c.executescript("""
      CREATE TABLE items(idx INTEGER PRIMARY KEY, item_id TEXT, platform TEXT, account TEXT, date TEXT,
                         title TEXT, url TEXT, location_tag TEXT, text TEXT, media_json TEXT);
      CREATE TABLE places(place_id TEXT PRIMARY KEY, name TEXT, category TEXT, region TEXT, ambiguous INT,
                          variants TEXT, native TEXT);
      CREATE TABLE hits(place_id TEXT, item_idx INT, media_idx INT, field TEXT, t REAL, matched TEXT, quote TEXT, score INT);
      CREATE VIRTUAL TABLE segments USING fts5(text, field UNINDEXED, item_idx UNINDEXED, media_idx UNINDEXED,
                                               t UNINDEXED, tokenize='unicode61 remove_diacritics 2');
    """)
    c.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)",
                  [(i, it["i"], it["p"], it["a"], it["d"], it["ti"], it["u"], it.get("loc"), it["x"],
                    json.dumps(it["m"], ensure_ascii=False)) for i, it in enumerate(items)])
    c.executemany("INSERT INTO places VALUES(?,?,?,?,?,?,?)",
                  [(p["id"], p["name"], p["category"], p["region"], int(p["ambiguous"]), " | ".join(p["variants"]),
                    " | ".join(native(p))) for p in gaz])
    c.executemany("INSERT INTO hits VALUES(?,?,?,?,?,?,?,?)",
                  [(h["place"], h["item"], h["media"], h["field"], h["t"], h["match"], h["quote"], h["score"]) for h in hits])
    c.executemany("INSERT INTO segments(text, field, item_idx, media_idx, t) VALUES(?,?,?,?,?)",
                  [(s[4], s[2], s[0], s[1], s[3]) for s in segs])
    c.commit()
    c.close()
    tmp.replace(db)


def viewer_config_js(lite: bool) -> str:
    """viewer/config.js: project wording for the generic viewer (package.py writes lite=true)."""
    v, pr = CFG["viewer"], CFG["project"]
    conf = {"lite": lite, "title": v["title"], "placeholder": v["search_placeholder"], "slug": pr["slug"],
            "watchlistLabel": CFG["watchlist"]["label"]}
    return "window.ARCHIVE=window.ARCHIVE||{};window.ARCHIVE.config=" + json.dumps(conf, ensure_ascii=False) + ";\n"


def install_viewer():
    """viewer/ sources -> <ROOT>/OPEN ME.html + <ROOT>/viewer/app.{js,css,config.js}"""
    src = HERE / "viewer"
    (ROOT / "viewer").mkdir(exist_ok=True)
    for f in ("app.js", "app.css"):
        (ROOT / "viewer" / f).write_bytes((src / f).read_bytes())
    (ROOT / "OPEN ME.html").write_bytes((src / "index.html").read_bytes())
    (ROOT / "viewer" / "config.js").write_text(viewer_config_js(lite=False), encoding="utf-8")


def main():
    gaz = load_watchlist()
    b = Builder()
    b.youtube()
    b.facebook()
    b.instagram()
    b.finish_ocr()
    b.flag_timing_overruns()
    hits = find_hits(b.segs, gaz, b.items)
    counts = Counter(h["place"] for h in hits)
    item_counts = {pid: len({h["item"] for h in hits if h["place"] == pid}) for pid in counts}
    places = [p | {"native": native(p), "n": counts.get(p["id"], 0), "ni": item_counts.get(p["id"], 0)} for p in gaz]

    write_js(OUT / "items.js", "items", b.items)
    write_js(OUT / "search.js", "segs", b.segs)
    write_js(OUT / "places.js", "places", places)
    write_js(OUT / "hits.js", "hits", [[h["place"], h["item"], h["media"], h["field"], h["t"], h["seg"], h["score"], int(h["weak"])]
                                        for h in hits])
    write_js(OUT / "meta.js", "meta", {"built": now(), "items": len(b.items), "segments": len(b.segs),
                                       "hits": len(hits), "by_platform": Counter(i["p"] for i in b.items),
                                       "by_field": Counter(s[2] for s in b.segs)})
    exports(b.items, b.segs, hits, gaz)
    install_viewer()
    top = sorted(places, key=lambda p: -p["ni"])[:12]
    log(f"{len(b.items)} items, {len(b.segs)} segments, {len(hits)} watchlist hits")
    log("top: " + ", ".join(f"{p['name']} {p['ni']}" + ("?" if p["ambiguous"] else "") for p in top))


if __name__ == "__main__":
    main()
