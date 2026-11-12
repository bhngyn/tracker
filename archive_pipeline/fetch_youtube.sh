#!/usr/bin/env bash
# Download every video/short/stream from the project's YouTube tabs to the archive.
# Resumable: yt-dlp's --download-archive skips anything already fetched.
# usage: ARCHIVE_CONFIG=project.toml fetch_youtube.sh [tab URL ...]   (no args = [youtube] tabs)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/.venv/bin/python"
YTDLP="$HERE/.venv/bin/yt-dlp"
cfg() { "$PY" "$HERE/config.py" get "$1"; }
ROOT="$(cfg project.archive_root)" || exit 1
OUT="$ROOT/youtube"
SUBS="$(cfg youtube.sub_langs)"
RES="$(cfg youtube.max_res)"
mkdir -p "$OUT" "$ROOT/logs"

URLS=()
if [ $# -gt 0 ]; then URLS=("$@"); else while IFS= read -r u; do [ -n "$u" ] && URLS+=("$u"); done < <(cfg youtube.tabs); fi
[ ${#URLS[@]} -gt 0 ] || { echo "no YouTube tabs configured"; exit 0; }
# YT_REVERSE=1 walks each tab bottom-up. run_all.sh sets it for the second worker so the two meet in
# the middle of a shared tab; walking the same direction they'd fetch each video in lockstep, both
# writing the same file (archive.txt only records a video once it's finished) and corrupting it.
ORDER=""; [ "${YT_REVERSE:-0}" = 1 ] && ORDER="--playlist-reverse"

for u in "${URLS[@]}"; do
  echo "=== $u  $(date -u +%FT%TZ)"
  # -S res:N caps the *shorter* side, so vertical shorts get e.g. 720x1280 rather than 360p
  "$YTDLP" "$u" $ORDER \
    --ignore-errors --no-overwrites --no-progress \
    --download-archive "$OUT/archive.txt" \
    -f "bv*+ba/b" -S "res:$RES,vcodec:h264,acodec:m4a" \
    --merge-output-format mp4 \
    --write-subs --write-auto-subs \
    --sub-langs "$SUBS" --sub-format "srt/vtt/best" --convert-subs srt \
    --write-info-json --write-description --write-thumbnail --convert-thumbnails jpg \
    --sleep-requests 0.75 --sleep-interval 1 --max-sleep-interval 4 --sleep-subtitles 2 \
    --socket-timeout 60 --retries 10 --fragment-retries 10 --concurrent-fragments 4 \
    -o "$OUT/%(channel_id)s/%(upload_date)s_%(id)s/%(id)s.%(ext)s"
done
echo "=== done $(date -u +%FT%TZ)"
