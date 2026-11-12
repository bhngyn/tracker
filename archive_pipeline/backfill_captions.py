"""Stage 2: fetch YouTube captions that the main download missed (mostly HTTP 429).

usage: backfill_captions.py [--dry-run] [--max-attempts 3]

For each downloaded YouTube item, compares the caption files on disk with what
info.json says YouTube offers (see common.youtube_caption_status) and re-requests
only the missing languages, slowly. Attempts are tracked in logs/captions_backfill.json
so items YouTube never serves aren't retried forever. Run after the video download
finishes, not alongside it, or both will trip the rate limit.
"""
import argparse, json, subprocess, time
from pathlib import Path
from common import ROOT, LOGS, YTDLP, heartbeat, log, now, write_json, youtube_caption_status

STATE = LOGS / "captions_backfill.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-attempts", type=int, default=3)
    a = ap.parse_args()

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    todo = []
    for d in sorted(ROOT.glob("youtube/*/*/")):
        st = youtube_caption_status(d)
        if st["missing"] and st.get("url") and state.get(st["id"], {}).get("attempts", 0) < a.max_attempts:
            todo.append((d, st))
    log(f"{len(todo)} items missing captions")
    if a.dry_run:
        for d, st in todo[:20]:
            print(f"  {d.name}: missing {st['missing']}")
        return

    streak = 0  # consecutive 429s
    for i, (d, st) in enumerate(todo, 1):
        heartbeat("captions", item=d.name, index=i, total=len(todo), item_started=time.time(),
                  detail="requesting " + ", ".join(st["missing"]))
        cmd = [str(YTDLP), st["url"], "--skip-download", "--no-progress",
               "--write-subs", "--write-auto-subs", "--sub-langs", ",".join(st["missing"]),
               "--sub-format", "srt/vtt/best", "--convert-subs", "srt",
               "--sleep-requests", "2", "--sleep-subtitles", "6",
               "-o", str(d / f"{st['id']}.%(ext)s")]
        r = subprocess.run(cmd, capture_output=True, text=True)
        after = youtube_caption_status(d)
        got = sorted(set(st["missing"]) - set(after["missing"]))
        rate_limited = "429" in (r.stderr or "")
        rec = state.setdefault(st["id"], {"attempts": 0})
        # a 429 says nothing about availability, so it doesn't use up an attempt
        rec.update(attempts=rec["attempts"] + (0 if rate_limited else 1), last=now(), missing=after["missing"])
        write_json(STATE, state)
        log(f"{d.name}: got {got or '-'} still missing {after['missing'] or '-'}{'  [429]' if rate_limited else ''}")
        streak = streak + 1 if rate_limited and not got else 0
        if streak:
            wait = min(60 * 2 ** (streak - 1), 1800)
            log(f"rate limited x{streak}, sleeping {wait}s")
            heartbeat("captions", item=d.name, index=i, total=len(todo), detail=f"rate limited, sleeping {wait}s")
            time.sleep(wait)
    log("done")


if __name__ == "__main__":
    main()
