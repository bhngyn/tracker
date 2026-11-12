#!/bin/bash
# One-time setup for the scraping kit.
#   ./setup.sh          core: Apify scraping only (any OS, Python 3.11+)
#   ./setup.sh --full   + media download, transcription, OCR, review kit (macOS on Apple silicon)
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11 or newer is required (found: $("$PY" --version 2>&1))"; exit 1; }

[ -d archive_pipeline/.venv ] || "$PY" -m venv archive_pipeline/.venv
VPY=archive_pipeline/.venv/bin/python
"$VPY" -m pip install --quiet --upgrade pip

if [ "${1:-}" = "--full" ]; then
  if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
    echo "--full needs macOS on Apple silicon (mlx-whisper and Apple Vision OCR)."; exit 1
  fi
  "$VPY" -m pip install --quiet -r archive_pipeline/requirements.txt
  command -v ffmpeg >/dev/null || echo "note: ffmpeg is missing. Install it with: brew install ffmpeg"
  echo "installed: full pipeline"
else
  "$VPY" -m pip install --quiet httpx==0.28.1
  echo "installed: core (Apify scraping). Re-run with --full for media, transcription and OCR."
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "created .env. Paste your Apify token into it (see README.md, step 2)."
fi

if grep -q '^APIFY_API_TOKEN=.\+' .env; then
  set -a; . ./.env; set +a
  "$VPY" check_apify.py || true
else
  echo "next: add your token to .env, then run:  archive_pipeline/.venv/bin/python check_apify.py"
fi
