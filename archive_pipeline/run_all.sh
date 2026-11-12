#!/usr/bin/env bash
# Run a project's whole pipeline end to end, resumably. Safe to re-run at any point: every
# stage skips finished work, and a stage that is already running is waited for, not started twice.
#
#   0  metadata       Apify: Instagram accounts + Facebook page histories (only if [apify] enabled;
#                     each dataset runs once — raw/<dataset>.run.json makes re-runs resume or skip)
#   1  downloads      YouTube ×2 workers, IG/FB media      (+ transcription & OCR alongside, --follow)
#                     + history_continue.py for Facebook histories stopped by Apify's monthly limit
#   2  retry pass     one more YouTube + media pass for anything that failed
#   3  captions       backfill caption tracks YouTube rate-limited
#   4  processing     final transcription + OCR catch-up (single pass)
#   5  index          build_index.py
#   6  package        package.py (Full manifest + Lite zip)
#
# usage:  nohup ./run_all.sh --config /path/to/project.toml >/dev/null 2>&1 &
#         (or export ARCHIVE_CONFIG). Log: <archive_root>/logs/run_all.log. Keeps the Mac awake.
# env:    OCR_WORKERS (default [ocr] workers)   SKIP_PACKAGE=1   SKIP_APIFY=1   SKIP_CAPTIONS=1
# One project at a time per machine: stages are recognised by script name.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/.venv/bin/python"
if [ "${1:-}" = "--config" ] && [ -n "${2:-}" ]; then export ARCHIVE_CONFIG="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; fi
[ -n "${ARCHIVE_CONFIG:-}" ] || { echo "usage: run_all.sh --config /path/to/project.toml"; exit 2; }
cfg() { "$PY" "$HERE/config.py" get "$1"; }
ROOT="$(cfg project.archive_root)" || exit 1
LOGS="$ROOT/logs"
OCR_WORKERS="${OCR_WORKERS:-$(cfg ocr.workers)}"
cd "$HERE"

say() { echo "$(date -u +%FT%TZ) [run_all] $*"; }
if [ ! -d "$(dirname "$ROOT")" ]; then echo "archive location $(dirname "$ROOT") not found (is the drive attached?)"; exit 1; fi
mkdir -p "$LOGS"
exec >> "$LOGS/run_all.log" 2>&1
caffeinate -dimsu -w $$ &

running() { pgrep -f "$1" >/dev/null; }
wait_for() {  # wait until no process matches $1
  local n=0
  while running "$1"; do
    (( n++ % 30 == 0 )) && say "waiting for: $1"
    sleep 60
  done
}
ensure() {  # ensure <pattern> <logfile> <cmd...>: start it in the background unless already running
  local pat="$1" lf="$2"; shift 2
  if running "$pat"; then say "already running: $pat"; else say "start: $*"; nohup nice -n 10 "$@" >> "$LOGS/$lf" 2>&1 & fi
}

say "=== run_all start (pid $$, config $ARCHIVE_CONFIG)"

# ---- 0. metadata via Apify (paid; the config's [apify] section is the spend approval)
if [ "$(cfg apify.enabled)" = true ] && [ "${SKIP_APIFY:-0}" != 1 ]; then
  IG_DATASET="$(cfg instagram.dataset)"
  IG_ACCOUNTS="$("$PY" -c 'import json,config;print(json.dumps(config.load()["instagram"]["accounts"]))')"
  if [ "$IG_ACCOUNTS" != "[]" ]; then
    IG_INPUT="{\"username\":$IG_ACCOUNTS,\"resultsLimit\":$(cfg instagram.results_limit),\"dataDetailLevel\":\"detailedData\"}"
    ensure "fetch_apify\.py $IG_DATASET " ig.log sh -c \
      "'$PY' fetch_apify.py $IG_DATASET $(cfg instagram.actor) $(cfg instagram.budget_usd) '$IG_INPUT' && '$PY' fetch_media.py --platform ig"
  fi
  N_PAGES="$("$PY" -c 'import config;print(len(config.load()["facebook"]["pages"]))')"
  for ((i=0; i<N_PAGES; i++)); do
    DS="$(cfg facebook.pages.$i.dataset)"
    FB_INPUT="$("$PY" -c "
import json, config
p = config.load()['facebook']['pages'][$i]
inp = {'startUrls': [{'url': p['url']}], 'resultsLimit': 20000, 'captionText': True}
if p.get('older_than'): inp['onlyPostsOlderThan'] = p['older_than']
print(json.dumps(inp))")"
    LOGF=$([ "$i" = 0 ] && echo fb_history.log || echo "fb_history_$((i + 1)).log")
    ensure "fetch_apify\.py $DS " "$LOGF" sh -c \
      "'$PY' fetch_apify.py $DS $(cfg facebook.actor) $(cfg facebook.pages.$i.budget_usd) '$FB_INPUT' && '$PY' fetch_media.py --platform fb"
  done
  # a page history can stop at the account's monthly Apify limit: continue after the reset
  [ "$N_PAGES" -gt 0 ] && ensure '(fb_)?history_continue\.py' fb_continue.log "$PY" history_continue.py
fi

# ---- 1. downloads, with transcription + OCR following along
TABS=()
while IFS= read -r u; do [ -n "$u" ] && TABS+=("$u"); done < <(cfg youtube.tabs)
if running 'fetch_youtube\.sh'; then
  say "YouTube workers already running"
elif [ ${#TABS[@]} -gt 0 ]; then
  say "start YouTube workers"
  nohup ./fetch_youtube.sh "${TABS[@]}" >> "$LOGS/youtube_a.log" 2>&1 &
  if [ ${#TABS[@]} -gt 1 ]; then  # second worker walks the tabs, and each tab's items, in reverse
    REV=(); for ((j=${#TABS[@]}-1; j>=0; j--)); do REV+=("${TABS[j]}"); done
    sleep 30
    YT_REVERSE=1 nohup ./fetch_youtube.sh "${REV[@]}" >> "$LOGS/youtube_b.log" 2>&1 &
  fi
fi
ensure 'fetch_media\.py' media.log "$PY" fetch_media.py --platform all
ensure 'transcribe\.py' transcribe.log "$PY" transcribe.py --follow
if ! running 'ocr\.py'; then
  if [ "$OCR_WORKERS" -gt 1 ]; then
    for ((i=0; i<OCR_WORKERS; i++)); do ensure "ocr\.py.*--shard $i/" "ocr_$i.log" "$PY" ocr.py --follow --shard "$i/$OCR_WORKERS"; done
  else
    ensure 'ocr\.py' ocr.log "$PY" ocr.py --follow
  fi
fi
sleep 60
wait_for 'fetch_youtube\.sh|fetch_media\.py|fetch_apify\.py'
say "downloads finished"

# ---- 2. retry pass (quick: archives/manifests skip everything already done)
if [ ${#TABS[@]} -gt 0 ]; then
  say "retry pass: YouTube"
  ./fetch_youtube.sh >> "$LOGS/youtube_retry.log" 2>&1
fi
say "retry pass: media"
"$PY" fetch_media.py --platform all >> "$LOGS/media.log" 2>&1

# ---- 3. caption backfill
if [ "${SKIP_CAPTIONS:-0}" = 1 ]; then
  say "SKIP_CAPTIONS=1, skipping caption backfill"
else
  say "caption backfill"
  "$PY" backfill_captions.py >> "$LOGS/captions.log" 2>&1
fi

# ---- 4. final processing pass (followers exit once downloads stop and their queue is empty)
# OCR and transcription run side by side, OCR sharded like stage 1, so a paused stage
# (pause.py --stage ...) holds only itself and the other still finishes.
wait_for 'transcribe\.py|ocr\.py'
say "final transcription + OCR pass ($OCR_WORKERS OCR worker(s))"
PIDS=()
nice -n 10 "$PY" transcribe.py >> "$LOGS/transcribe.log" 2>&1 & PIDS+=($!)
if [ "$OCR_WORKERS" -gt 1 ]; then
  for ((i=0; i<OCR_WORKERS; i++)); do nice -n 10 "$PY" ocr.py --shard "$i/$OCR_WORKERS" >> "$LOGS/ocr_$i.log" 2>&1 & PIDS+=($!); done
else
  nice -n 10 "$PY" ocr.py >> "$LOGS/ocr.log" 2>&1 & PIDS+=($!)
fi
wait "${PIDS[@]}"  # not a bare wait: caffeinate is a child too, and it only exits with this script

# ---- 5. index
say "build index"
"$PY" build_index.py >> "$LOGS/index.log" 2>&1 || { say "build_index failed, see index.log"; exit 1; }

# ---- 6. package
if [ "${SKIP_PACKAGE:-0}" = 1 ]; then
  say "SKIP_PACKAGE=1, stopping after index"
else
  say "package (Full manifest + Lite zip)"
  "$PY" package.py >> "$LOGS/package.log" 2>&1 || { say "package failed, see package.log"; exit 1; }
fi
say "=== run_all done"
