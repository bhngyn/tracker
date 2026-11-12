# tracker

A kit for collecting public social-media posts through [Apify](https://apify.com), built to
be driven by [Claude Code](https://claude.com/claude-code). You say what you want collected;
Claude sets up the project, shows you the cost, and runs the scrape once you approve it.

| Platform | What you get | Tool |
|---|---|---|
| Instagram | Post metadata, then photos and videos | `archive_pipeline/` |
| Facebook pages | Post metadata, then photos and videos | `archive_pipeline/` |
| YouTube | Videos, captions, descriptions (free: yt-dlp, no Apify) | `archive_pipeline/` |
| X / Twitter | Full text history of an account | `x_backfill/` |
| TikTok and others | Raw dataset from any Apify actor, with a spend cap | `archive_pipeline/fetch_apify.py` |

On a Mac with Apple silicon, `archive_pipeline/` can also transcribe speech, read on-screen
text, flag a watchlist of names, and package everything as an offline review kit that opens
in a browser.

## Quick start with Claude

Open Claude Code in the folder where you want the kit, and paste this:

> Clone https://github.com/bhngyn/tracker, read its CLAUDE.md and docs/SETUP.md, and walk
> me through setup one step at a time.

Claude installs the kit, tells you where to get an Apify token and where to paste it, checks
the connection, and runs a small first scrape with you. If you already have the folder, open
Claude Code inside it and say "walk me through setup".

## Setup by hand

You need Python 3.11 or newer, Claude Code, and your own Apify account. The steps below are
the short version; `docs/SETUP.md` has the full walkthrough with expected output and fixes.

**1. Get the kit and install it.**

```bash
git clone https://github.com/bhngyn/tracker.git
```

```bash
cd tracker && ./setup.sh
```

On a Mac with Apple silicon, use `./setup.sh --full` to add media download, transcription
and OCR (and run `brew install ffmpeg`).

**2. Add your Apify token.** Sign up at [apify.com](https://apify.com), then copy a token
from **Settings → API & Integrations** in the Apify console. Paste it into `.env`, after
`APIFY_API_TOKEN=`. Paste it into the file yourself: don't paste it into a chat.

The free plan includes a small monthly credit, enough to try things. Larger jobs need a paid
plan. In the Apify console you can also set a monthly spending limit, which is a good backstop.

**3. Check it works.** This is free and starts no scraper:

```bash
archive_pipeline/.venv/bin/python check_apify.py
```

**4. Open the folder in Claude Code and ask.** For example:

- "Check my Apify setup and tell me how much budget I have left."
- "Collect the last 500 Instagram posts from these three accounts. Keep it under $5."
- "Get the full X history of @example_handle since 2020. Estimate the cost first."
- "Archive this Facebook page to my external drive and build the review kit."

Claude reads `CLAUDE.md`, which tells it how the tools work and what it must ask you first.

## What it costs

Apify charges per item returned. These are rough figures; the actor's page on Apify has the
current price.

| Platform | Approximate cost |
|---|---|
| X / Twitter | $0.22 per 1,000 posts |
| Instagram | $2.30 per 1,000 posts |
| Facebook | $4 to $5 per 1,000 posts |
| YouTube | Free |

Every paid run has a hard cap. A scrape won't start until a budget is written into the
project's config, and it stops when it reaches that budget.

## Where your data goes

Scraped data and project configs live in a folder you choose, **outside this repository**,
such as an external drive. Nothing you collect is uploaded anywhere. Keep it that way: this
repository is public, so never commit a project config, a watchlist, scraped data or your
`.env`.

## Using it responsibly

Collect public content only, and only what your work needs. You are responsible for
following the platforms' terms, Apify's terms, and the privacy and data-protection law that
applies to you. Posts contain personal data: store them securely and share them only with
people who need them.

## More detail

- `docs/SETUP.md`: step-by-step setup, for a person or an agent
- `docs/GUIDE.md`: how the kit works, what a project is, what you end up with
- `CLAUDE.md`: the rules Claude follows when operating the kit
- `archive_pipeline/README.md`: the pipeline's stages, monitoring, output layout, watchlist format
- `x_backfill/README.md`: how the X backfill works and its options
- `docs/apify-output-schemas.md`: the fields each scraper returns
