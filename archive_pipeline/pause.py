"""Pause or resume transcription and OCR (downloads keep running).

usage: ARCHIVE_CONFIG=project.toml python pause.py --until 00:00        # next local midnight
       ARCHIVE_CONFIG=project.toml python pause.py --until "2026-09-26 06:30"
       ARCHIVE_CONFIG=project.toml python pause.py --indefinitely
       ARCHIVE_CONFIG=project.toml python pause.py --off                # resume now
       ARCHIVE_CONFIG=project.toml python pause.py                      # show state
       add --stage transcribe (or --stage ocr) to any of these to pause/resume just that stage


transcribe.py and ocr.py check the pause before starting and before every file, so a
pause set mid-run takes effect after the current file. Paused workers wait without loading
the Whisper model; run_all.sh's later passes honour the pause too.
"""
import argparse, json, time
from datetime import datetime, timedelta
from common import PAUSE_FILE, pause_state, stage_pause_file, write_json


def parse_when(s: str) -> datetime:
    now = datetime.now().astimezone()
    for fmt in ("%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            t = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":  # next occurrence of that local time
            t = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            return t if t > now else t + timedelta(days=1)
        return t.astimezone()
    raise SystemExit(f"can't read time {s!r}: use HH:MM or 'YYYY-MM-DD HH:MM' (local time)")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--until", help="local time to resume: HH:MM (next occurrence) or 'YYYY-MM-DD HH:MM'")
    g.add_argument("--indefinitely", action="store_true")
    g.add_argument("--off", action="store_true", help="resume now")
    ap.add_argument("--note", default="")
    ap.add_argument("--stage", choices=["transcribe", "ocr"], help="pause/resume only this stage")
    a = ap.parse_args()
    path = stage_pause_file(a.stage) if a.stage else PAUSE_FILE
    what = {"transcribe": "transcription", "ocr": "OCR", None: "transcription + OCR"}[a.stage]
    if a.off:
        path.unlink(missing_ok=True)
        print(f"{what} resumed (workers pick it up within a minute)")
        return
    if a.until or a.indefinitely:
        t = parse_when(a.until) if a.until else None
        write_json(path, {"until_ts": t.timestamp() if t else None, "until": t.isoformat() if t else None,
                                "note": a.note, "set_at": datetime.now().astimezone().isoformat()})
        print(f"{what} paused until {t.strftime('%Y-%m-%d %H:%M %Z') if t else 'you run pause.py ' + (f'--stage {a.stage} ' if a.stage else '') + '--off'}")
        return
    for stage in ("transcribe", "ocr"):
        paused, until, note = pause_state(stage)
        print(f"{stage}: " + ("paused until " + (datetime.fromtimestamp(until).astimezone().strftime("%Y-%m-%d %H:%M %Z") if until else "manual resume")
              + (f" ({note})" if note else "") if paused else "not paused"))


if __name__ == "__main__":
    main()
