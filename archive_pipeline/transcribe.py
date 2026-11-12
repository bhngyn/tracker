"""Stage 5: speech -> text with mlx-whisper on the Mac GPU.

usage: transcribe.py [--source youtube,facebook,instagram] [--all-youtube]
                     [--limit N] [--model <hf repo>] [--follow] [--dry-run]
       defaults from the project config: [transcribe] model, youtube = missing | all | none

Targets: every FB/IG video, plus YouTube videos with no transcript of the actual
speech on disk (no manual subs and no original-language auto captions; see
common.youtube_caption_status). --all-youtube transcribes every YouTube video.
Language is auto-detected, so speech is transcribed in the language spoken.

Writes derived/<media path>/whisper.json (language, model, segments) + whisper.srt.
Resumable: skips media whose whisper.json exists. Audio longer than 90 min is
processed in 60-min chunks so an 8 GB Mac doesn't hold hours of PCM in memory.

Speed: the next file's audio is decoded (ffmpeg, CPU) while the GPU transcribes the
current one, and a garbled window is retried at [transcribe] temperatures (default
0.0/0.4/0.8) rather than mlx-whisper's six rungs.
"""
import argparse, subprocess, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
from common import (CFG, ROOT, derived_dir, follow, heartbeat, iter_videos, log, media_info, now, pause_state, wait_if_paused,
                    write_json, write_srt, youtube_caption_status)

CHUNK = 3600
MAX_WHOLE = 5400
PREFETCH_MAX = 1800  # decode the next file ahead only if it's short enough to hold in memory (30 min = 115 MB)
TEMPERATURES = tuple(float(t) for t in CFG["transcribe"]["temperatures"])


def targets(sources, all_youtube):
    for src, p in iter_videos(sources):
        if (derived_dir(p) / "whisper.json").exists():
            continue
        if src == "youtube" and not all_youtube and youtube_caption_status(p.parent)["original"]:
            continue
        yield src, p


def load_audio(p: Path) -> np.ndarray:
    """16 kHz mono float32, exactly as mlx_whisper.audio.load_audio, but as numpy so it can be
    decoded on a helper thread (MLX arrays are only created on the main thread). 2 ffmpeg threads,
    not all cores: this overlaps the GPU work and must stay inside the machine's core budget."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-i", str(p), "-threads", "2", "-f", "s16le", "-ac", "1",
                        "-acodec", "pcm_s16le", "-ar", "16000", "-"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"Failed to load audio: {r.stderr.decode(errors='replace')[-500:]}")
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def transcribe_file(mlx_whisper, audio, model):
    if isinstance(audio, Path):
        audio = str(audio)
    r = mlx_whisper.transcribe(audio, path_or_hf_repo=model, condition_on_previous_text=False,
                               temperature=TEMPERATURES, verbose=None)
    segs = [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
            for s in r.get("segments", []) if s.get("text", "").strip()]
    return r.get("language"), segs


def transcribe(mlx_whisper, p: Path, model: str, info: dict, audio=None) -> dict:
    """info = media_info(p); audio = the file's decoded audio if it was prefetched."""
    dur = info["duration"]
    if not info["has_audio"]:
        return {"status": "no_audio", "duration": dur, "segments": []}
    if dur <= MAX_WHOLE:
        lang, segs = transcribe_file(mlx_whisper, audio if audio is not None else p, model)
        return {"status": "ok", "duration": dur, "language": lang, "segments": segs}
    segs, langs = [], []
    with tempfile.TemporaryDirectory() as td:
        for off in range(0, int(dur) + 1, CHUNK):
            wav = Path(td) / f"c{off}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(off), "-t", str(CHUNK), "-i", str(p),
                            "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
            lang, cs = transcribe_file(mlx_whisper, wav, model)
            langs.append(lang)
            segs += [s | {"start": round(s["start"] + off, 2), "end": round(s["end"] + off, 2)} for s in cs]
            wav.unlink()
    return {"status": "ok", "duration": dur, "language": max(set(langs), key=langs.count),
            "chunk_languages": langs, "segments": segs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="youtube,facebook,instagram")
    ap.add_argument("--all-youtube", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=CFG["transcribe"]["model"])
    ap.add_argument("--follow", action="store_true", help="keep picking up new downloads until they finish")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    yt_mode = "all" if a.all_youtube else CFG["transcribe"]["youtube"]
    sources = tuple(s for s in a.source.split(",") if s != "youtube" or yt_mode != "none")

    def build_todo():
        todo = list(targets(sources, yt_mode == "all"))
        return todo[: a.limit] if a.limit else todo

    if a.dry_run:
        todo = build_todo()
        log(f"{len(todo)} videos to transcribe")
        for s, p in todo[:20]:
            print(f"  {s}: {p}")
        return

    wait_if_paused("transcribe")  # before loading anything heavy
    import mlx_whisper
    import mlx.core as mx
    from importlib.metadata import version
    # MLX keeps freed GPU buffers in a cache that otherwise grows by ~1 GB over a few
    # files on this 8 GB Mac: cap it, and empty it after every file.
    mx.set_cache_limit(256 * 1024 ** 2)
    tool = f"mlx-whisper {version('mlx-whisper')}"
    speed = {"audio": 0.0, "proc": 0.0}  # realtime factor shown by status.py
    pool = ThreadPoolExecutor(1)

    def prepare(p):
        info = media_info(p)
        audio = load_audio(p) if info["has_audio"] and 0 < info["duration"] <= PREFETCH_MAX else None
        return info, audio

    def process(todo):
        log(f"{len(todo)} videos to transcribe")
        failed = []
        ahead = {}  # index -> future of prepare(), for the next file only
        for i, (src, p) in enumerate(todo, 1):
            wait_if_paused("transcribe")
            t0 = time.time()
            try:
                info, audio = (ahead.pop(i) if i in ahead else pool.submit(prepare, p)).result()
            except Exception as e:
                log(f"[{i}/{len(todo)}] ERROR {p}: {e!r}")
                failed.append(p)
                continue
            if i < len(todo) and not pause_state("transcribe")[0]:
                ahead[i + 1] = pool.submit(prepare, todo[i][1])  # decoded while the GPU works on this one
            dur = info["duration"]
            rtf = speed["proc"] / speed["audio"] if speed["audio"] else None
            heartbeat("transcribe", item=str(p.relative_to(ROOT)), index=i, total=len(todo), item_started=t0,
                      audio_s=dur, expected_s=dur * rtf if rtf else None, detail=f"{dur / 60:.1f} min of audio")
            try:
                r = transcribe(mlx_whisper, p, a.model, info, audio)
            except Exception as e:  # keep going; the item is retried next run
                log(f"[{i}/{len(todo)}] ERROR {p}: {e!r}")
                failed.append(p)
                mx.clear_cache()
                continue
            out = derived_dir(p)
            out.mkdir(parents=True, exist_ok=True)
            write_srt(r["segments"], out / "whisper.srt")
            write_json(out / "whisper.json", r | {"source": src, "media": str(p), "model": a.model,
                                                  "temperatures": list(TEMPERATURES), "tool": tool, "created": now()})
            el = time.time() - t0
            del audio
            mx.clear_cache()
            if r["status"] == "ok" and dur:
                speed["audio"] += dur
                speed["proc"] += el
            log(f"[{i}/{len(todo)}] {r['status']} {r.get('language', '-')} {r['duration']/60:.1f} min "
                f"in {el/60:.1f} min ({len(r['segments'])} segs, gpu mem {mx.get_active_memory() / 1e9:.1f} GB "
                f"+ cache {mx.get_cache_memory() / 1e9:.1f} GB)  {p.relative_to(p.parents[2])}")
        return failed

    follow(build_todo, process, a.follow)
    heartbeat("transcribe", item=None, detail="finished")
    log("done")


if __name__ == "__main__":
    main()
