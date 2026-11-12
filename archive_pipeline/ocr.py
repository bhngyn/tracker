"""Stage 6: on-screen text (OCR) for videos and photos with Apple Vision.

usage: ocr.py [--videos] [--images] [--source youtube,facebook,instagram]
              [--interval 5] [--limit N] [--dry-run]
       (neither --videos nor --images = both)

Videos: ffmpeg decodes keyframes only (-skip_frame nokey, far cheaper than a full
decode) and keeps one at least every --interval seconds; near-duplicate frames
(32x18 grayscale mean diff < 4) and blank frames are skipped before OCR.
Timestamps are the keyframe's real pts, so seek-to-time in the viewer is exact.
VP9 ignores -skip_frame nokey (every frame gets decoded), so VP9 is decoded on the
Apple media engine (VideoToolbox) instead of the CPU. The next video's frames are
extracted while the current one is OCR'd.

Text is read by Apple Vision (VNRecognizeTextRequest, accurate); bbox = Vision's normalised
[x, y, w, h], origin bottom-left. Same request and pixels as ocrmac (output is identical),
minus its PNG re-encode; ocrmac itself remains the fallback.

Writes derived/<media path>/ocr.json. Resumable: skips media whose ocr.json exists.
"""
import argparse, re, subprocess, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
from PIL import Image
from common import (CFG, ROOT, derived_dir, follow, heartbeat, in_shard, iter_images, iter_videos, log, media_info, now,
                    pause_state, wait_if_paused, write_json)

LANGS = list(CFG["ocr"]["languages"])  # Apple Vision language codes, from the project config
MIN_CONF = 0.3
DUP_DIFF = 4.0
FF_THREADS = 1  # per worker; parallelism comes from --shard workers
HW_DECODE = {"vp9"}  # codecs where keyframe-only decode doesn't work, so decode on the media engine


_fallback_logged = []


def _lines(res):
    return [{"text": t.strip(), "conf": round(float(c), 3), "bbox": [round(v, 4) for v in b]}
            for t, c, b in res if t.strip() and c >= MIN_CONF]


def _ocrmac(img: Image.Image):
    from ocrmac import ocrmac
    return _lines(ocrmac.OCR(img, recognition_level="accurate", language_preference=LANGS, unit="line").recognize())


def ocr_image(img: Image.Image):
    """The same Vision request ocrmac makes, on the same (PIL-decoded) pixels, handed over as a raw
    bitmap instead of re-encoded as PNG: identical output, about half the time."""
    import objc, Quartz, Vision
    from Foundation import NSData
    try:
        with objc.autorelease_pool():
            im = img if img.mode == "RGBA" else img.convert("RGBA")
            w, h = im.size
            data = im.tobytes()
            # CFData-backed provider, NOT CGDataProviderCreateWithData(None, data, ...): with no release
            # callback PyObjC pins the bytes forever, leaking one full RGBA frame (~3.7 MB at 1280x720) per
            # call. That grew 3 workers to ~7.5 GB each and kernel-panicked the 8 GB Mac on 2026-09-26.
            provider = Quartz.CGDataProviderCreateWithCFData(NSData.dataWithBytes_length_(data, len(data)))
            cg = Quartz.CGImageCreate(w, h, 8, 32, 4 * w, Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB),
                                      Quartz.kCGImageAlphaLast, provider, None, False, Quartz.kCGRenderingIntentDefault)
            req = Vision.VNRecognizeTextRequest.alloc().init()
            req.setRecognitionLevel_(0)  # accurate
            req.setRecognitionLanguages_(LANGS)
            ret = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None).performRequests_error_([req], None)
            ok, err = ret if isinstance(ret, tuple) else (bool(ret), None)
            if not ok or err is not None:
                raise RuntimeError(err)
            res = []
            for o in req.results() or []:
                b = o.boundingBox()
                res.append((o.topCandidates_(1)[0].string(), o.confidence(),
                            [b.origin.x, b.origin.y, b.size.width, b.size.height]))
            return _lines(res)
    except Exception as e:
        if not _fallback_logged:
            _fallback_logged.append(e)
            log(f"direct Vision call failed ({e!r}); using ocrmac for this and any further failures")
        return _ocrmac(img)


def thumb(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L").resize((32, 18), Image.BILINEAR), dtype=np.float32)


def extract_frames(p: Path, interval: float):
    """-> (TemporaryDirectory, [(jpg, pts)], decoder). The caller cleans up the directory."""
    vf = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval - 0.5})',"
          "scale='min(1280\\,iw)':-2,showinfo")
    hw = media_info(p)["vcodec"] in HW_DECODE
    td = tempfile.TemporaryDirectory()
    for accel in ((["-hwaccel", "videotoolbox"], []) if hw else ([],)):
        for f in Path(td.name).glob("f_*.jpg"):
            f.unlink()
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-threads", str(FF_THREADS), *accel, "-skip_frame", "nokey",
                            "-i", str(p), "-filter_threads", "1", "-vf", vf, "-fps_mode", "vfr", "-q:v", "3",
                            f"{td.name}/f_%06d.jpg"],
                           capture_output=True, text=True)
        files = sorted(Path(td.name).glob("f_*.jpg"))
        if r.returncode == 0 or files:
            break
    else:
        td.cleanup()
        raise RuntimeError(r.stderr[-500:])
    pts = [float(x) for x in re.findall(r"\bn:\s*\d+.*?pts_time:([\d.]+)", r.stderr)]
    return td, list(zip(files, pts)), "videotoolbox" if accel else "cpu"


def ocr_video(p: Path, interval: float, progress=None, extracted=None) -> dict:
    """extracted = extract_frames(p, interval) if it was already run ahead of time."""
    td, pairs, decoder = extracted or extract_frames(p, interval)
    frames, sampled = [], 0
    try:
        last = None
        if progress:
            progress(0, len(pairs), "frames extracted")
        for f, t in pairs:
            sampled += 1
            if progress and sampled % 10 == 0:
                progress(sampled, len(pairs), f"frame {sampled}/{len(pairs)} at {t:.0f}s")
            with Image.open(f) as img:
                small = thumb(img)
                if small.std() < 3:  # blank / black frame
                    continue
                if last is not None and np.abs(small - last).mean() < DUP_DIFF:
                    continue
                last = small
                lines = ocr_image(img)
            if lines:
                frames.append({"t": round(t, 2), "text": "\n".join(l["text"] for l in lines), "lines": lines})
    finally:
        td.cleanup()
    return {"kind": "video", "interval": interval, "decoder": decoder, "frames_sampled": sampled, "frames": frames}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", action="store_true")
    ap.add_argument("--images", action="store_true")
    ap.add_argument("--source", default="youtube,facebook,instagram")
    ap.add_argument("--interval", type=float, default=float(CFG["ocr"]["interval_s"]))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shard", help="i/n: this worker's share of the files, e.g. 0/3")
    ap.add_argument("--follow", action="store_true", help="keep picking up new downloads until they finish")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    both = not (a.videos or a.images)
    sources = tuple(a.source.split(","))
    stage = "ocr" + (a.shard.split("/")[0] if a.shard else "")

    def build_todo():
        todo = []
        if a.images or both:  # images first: fast, and thumbnails give early coverage
            todo += [("image", s, p) for s, p in iter_images(sources)
                     if in_shard(p, a.shard) and not (derived_dir(p) / "ocr.json").exists()]
        if a.videos or both:
            todo += [("video", s, p) for s, p in iter_videos(sources)
                     if in_shard(p, a.shard) and not (derived_dir(p) / "ocr.json").exists()]
        return todo[: a.limit] if a.limit else todo

    import platform
    tool = f"Apple Vision VNRecognizeTextRequest (accurate, {'+'.join(LANGS)}; ocrmac-equivalent), macOS {platform.mac_ver()[0]}"
    pool = ThreadPoolExecutor(1)

    def process(todo):
        log(f"{sum(k == 'image' for k, *_ in todo)} images, {sum(k == 'video' for k, *_ in todo)} videos to OCR")
        failed = []
        ahead = {}  # index -> future of extract_frames(), for the next video only

        def prefetch(j):
            if j <= len(todo) and todo[j - 1][0] == "video" and j not in ahead and not pause_state(stage)[0]:
                ahead[j] = pool.submit(extract_frames, todo[j - 1][2], a.interval)

        for i, (kind, src, p) in enumerate(todo, 1):
            wait_if_paused(stage)
            t0 = time.time()
            item = str(p.relative_to(ROOT))
            beat = lambda done, tot, detail: heartbeat(stage, item=item, kind=kind, index=i, total=len(todo), item_started=t0,
                                                       item_progress=done / tot if tot else None, detail=detail)
            if kind == "video":
                beat(0, 0, "extracting keyframes")
            elif i % 25 == 1:
                beat(0, 0, "photos")
            try:
                if kind == "image":
                    with Image.open(p) as img:
                        lines = ocr_image(img)
                    r = {"kind": "image", "text": "\n".join(l["text"] for l in lines), "lines": lines}
                else:
                    extracted = ahead.pop(i).result() if i in ahead else extract_frames(p, a.interval)
                    prefetch(i + 1)  # next video's frames decode while this one is read
                    r = ocr_video(p, a.interval, progress=beat, extracted=extracted)
            except Exception as e:
                log(f"[{i}/{len(todo)}] ERROR {p}: {e!r}")
                failed.append(p)
                continue
            write_json(derived_dir(p) / "ocr.json", r | {"source": src, "media": str(p), "tool": tool, "created": now()})
            if kind == "video" or i % 200 == 0:
                n = len(r["frames"]) if kind == "video" else len(r["lines"])
                log(f"[{i}/{len(todo)}] {kind} {n} {'frames w/ text' if kind == 'video' else 'lines'} "
                    f"in {time.time() - t0:.0f}s  {p.name}")
        return failed

    if a.dry_run:
        todo = build_todo()
        log(f"{sum(k == 'image' for k, *_ in todo)} images, {sum(k == 'video' for k, *_ in todo)} videos to OCR")
        return
    wait_if_paused(stage)
    follow(build_todo, process, a.follow)
    heartbeat(stage, item=None, detail="finished")
    log("done")


if __name__ == "__main__":
    main()
