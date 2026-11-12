# Archive pipeline

A reusable, config-driven pipeline that archives public YouTube, Instagram and Facebook
accounts to local storage. It makes everything searchable, including speech (Whisper) and
on-screen text (OCR), flags mentions of a watchlist, and packages the result as a
self-contained review kit. Reviewers open `OPEN ME.html` from a drive, with no server and
nothing to install.

It runs on macOS with Apple silicon: transcription uses `mlx-whisper` on the GPU, and OCR
uses Apple Vision (the same request `ocrmac` makes, minus its PNG re-encode; `ocrmac` is the fallback).

**Everything project-specific lives in a project folder outside this repository:**
- target channels, pages and accounts;
- budgets;
- languages;
- the watchlist;
- methodology wording.

The toolkit itself names no targets. Keep it that way: never commit a project config or
watchlist.

## Setup (once)

```bash
cd archive_pipeline
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install ffmpeg
```

## Start a project

```bash
.venv/bin/python new_project.py ~/research/acme --name "ACME Group" --slug acme \
    --archive-root /Volumes/Drive/acme_archive
$EDITOR ~/research/acme/project.toml            # targets, budgets, languages, wording
ARCHIVE_CONFIG=~/research/acme/project.toml .venv/bin/python config.py check
nohup ./run_all.sh --config ~/research/acme/project.toml >/dev/null 2>&1 &
```

`project.example.toml` documents every setting. The `[apify]` section is the spend approval.
Paid scraping only runs with `enabled = true`, and never beyond `total_budget_usd`.

## What `run_all.sh` does

| # | Stage | Script |
|---|---|---|
| 0 | Instagram and Facebook metadata via Apify (capped; each dataset runs once) | `fetch_apify.py` |
| 1 | YouTube (two workers), photos and videos from the metadata; transcription and OCR follow along | `fetch_youtube.sh`, `fetch_media.py`, `transcribe.py --follow`, `ocr.py --follow` |
| 1b | Facebook histories stopped early by Apify's monthly limit continue after the reset | `history_continue.py` |
| 2 | Retry pass for failed downloads | |
| 3 | Backfill YouTube caption tracks that hit rate limits | `backfill_captions.py` |
| 4 | Final transcription and OCR pass | |
| 5 | Search index, watchlist hits, CSV and SQLite exports, viewer | `build_index.py` |
| 6 | Full package (manifest + methodology) and Lite zip (no video) | `package.py` |
| — | One self-contained .html (viewer + all index data + overview + methodology), run by hand after 6 | `build_single_html.py` |

**Resumable and idempotent.** Re-run `run_all.sh` after a crash, reboot or drive
disconnect, and every stage skips finished work:
- yt-dlp uses its download archive;
- media downloads are recorded in `media.json`;
- derived files that exist are skipped;
- Apify runs are resumed from `raw/<name>.run.json`, never paid for twice.

Stages that are already running are adopted, not duplicated. It keeps the Mac awake with
`caffeinate`. Run one project at a time per machine: stages are recognised by script name.

## Watching it

```bash
ARCHIVE_CONFIG=... .venv/bin/python status.py --watch             # live terminal view
ARCHIVE_CONFIG=... .venv/bin/python status.py --watch 10 --html --quiet &   # keeps <archive>/STATUS.html fresh
```

**"Right now" cards** show, per worker:
- the current item and phase;
- bytes, speed and ETA for downloads;
- Apify spend against its cap;
- in-item progress for transcription and OCR, via heartbeats in `logs/now/`.

**Totals** show progress, rates and ETAs.

**Alerts** fire for stalls, low disk, missing keep-awake, budget, and a live Apify run that
nothing is polling.

## Pausing transcription and OCR

Downloads keep running; the heavy processing waits (e.g. to keep the machine free during the day):

```bash
ARCHIVE_CONFIG=... .venv/bin/python pause.py --until 00:00        # resume at the next local midnight
ARCHIVE_CONFIG=... .venv/bin/python pause.py --indefinitely
ARCHIVE_CONFIG=... .venv/bin/python pause.py --off                # resume now
ARCHIVE_CONFIG=... .venv/bin/python pause.py --stage transcribe --indefinitely   # hold one stage only (transcribe | ocr)
```

Workers check before every file, wait without loading the Whisper model, and show "paused until …" in the monitor.

## Output layout (`archive_root`)

```
youtube/<channel>/<date>_<id>/   video, .info.json, description, thumbnail, captions (.srt)
instagram/<user>/<shortcode>/    post.json, media.json, media files
facebook/<postId>/               post.json, media.json, media files
derived/<media path>/            whisper.json/.srt, ocr.json
raw/                             Apify datasets + run records, unmodified (evidence of collection)
viewer/, OPEN ME.html            the review tool (search, watchlist hits, item view, reviewer tags)
exports/                         items.csv, hits.csv, archive.sqlite (FTS5)
logs/                            per-stage logs, status.json, heartbeats
METHODOLOGY.md, MANIFEST.sha256  written by package.py
<packages_dir>/<slug>_archive_single_<date>.html   build_single_html.py: search/places/tags/methodology in one file;
                                 media load when it sits in the archive root, else items link to the original post
```

## Watchlist format

`[watchlist] path` points at a JSON list:

```json
[
  {"id": "W001", "name": "Example Place", "category": "site", "region": "North", "note": "",
   "variants": ["Example Place", "Exemple Place"], "ambiguous": false,
   "native": ["…"], "native_weak": []}
]
```

- `variants`: English and Latin-script spellings, matched whole-word and case-insensitively.
- `ambiguous: true` (names that are also common words or first names): matched case-sensitively
  and shown as weak hits.
- `native`: forms in another script. `native_prefixes` / `native_letters` in the config let
  attached prefixes match, as in scripts where prepositions join the next word.
- `native_weak`: forms that only count as weak hits.

Build the list however suits the project; a small generator script kept in the project folder
works well.

## Resource use

It's tuned for small machines (tested on an 8 GB M2): one Whisper process and `[ocr] workers`
OCR processes (default 1), all at `nice 10`. The MLX GPU cache is capped and cleared per file.
Measure whole-machine idle before adding OCR workers: kernel, memory-compression and indexing
overhead grow faster than the workers' own CPU.
