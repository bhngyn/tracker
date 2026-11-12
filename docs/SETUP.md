# Setup walkthrough

This walkthrough takes a new user from nothing to a first working scrape. It is written so
that an AI agent (Claude Code) can run it with the user, and so that a person can follow it
alone. It takes about ten minutes.

**If you are an agent:** do one step at a time. Run the commands yourself, check the result
shown under "Expect", and tell the user in a sentence what happened. Stop wherever a step
says **User does this** and wait for them. Read `CLAUDE.md` first: its rules apply throughout.

## What the user needs

- A computer with Python 3.11 or newer and `git`.
- An Apify account. The free plan is enough for setup and a small test.
- Optional: a Mac with Apple silicon, for media download, transcription and OCR.

## Step 1: Get the kit

Skip this if the folder already exists.

```bash
git clone https://github.com/bhngyn/tracker.git
```

All later commands run from inside the `tracker` folder.

## Step 2: Check Python

```bash
python3 --version
```

**Expect:** `Python 3.11` or higher. If it is older or missing, install a current Python
(on a Mac: `brew install python`; otherwise python.org) and repeat.

## Step 3: Install

Pick one. Ask the user which they want if it isn't clear: the core install is enough to
collect posts; the full install adds everything that happens after collection.

```bash
./setup.sh
```

```bash
./setup.sh --full
```

| | Core | Full |
|---|---|---|
| Works on | macOS, Linux, Windows (WSL) | macOS on Apple silicon only |
| Collect Instagram, Facebook, X, other actors | Yes | Yes |
| Download photos and videos, YouTube | No | Yes |
| Transcripts, OCR, searchable review kit | No | Yes |
| Install time | Under a minute | Several minutes |

The full install also needs ffmpeg: `brew install ffmpeg`.

**Expect:** a line starting `installed:`, and `created .env`. Switching from core to full
later is safe: run `./setup.sh --full` again.

## Step 4: Create an Apify token. **User does this.**

1. Sign in at <https://console.apify.com> (create a free account if needed).
2. Open **Settings → API & Integrations**.
3. Under **Personal API tokens**, copy the default token, or create a new one named `tracker`.

While there, the user can set a monthly spending limit in the console's billing settings. It
is a second line of defence behind the kit's own caps.

## Step 5: Put the token in `.env`. **User does this.**

The token goes into the file named `.env` in the `tracker` folder, on the line
`APIFY_API_TOKEN=`, with nothing after it but the token. For example:

```
APIFY_API_TOKEN=apify_api_xxxxxxxxxxxxxxxxxxxx
```

The file is hidden in most file browsers because its name starts with a dot. To open it in a
text editor on a Mac:

```bash
open -e .env
```

**Agents:** the user pastes the token into the file, not into the chat. Don't ask for it,
don't read `.env` back, and don't print its contents. If a token does appear in the chat,
tell the user to delete it in the Apify console and create a new one. `.env` is git-ignored
and is never committed.

## Step 6: Check the connection

This is free. It starts no scraper.

```bash
archive_pipeline/.venv/bin/python check_apify.py
```

**Expect:**

```
token OK   account: <name>   plan: FREE
this month: $0.00 used of $5.00  ($5.00 left, resets 2026-01-31)
```

| Message | Meaning | Fix |
|---|---|---|
| `APIFY_API_TOKEN is not set` | `.env` is empty or the line is malformed | Repeat step 5 |
| `Apify rejected the token (HTTP 401)` | Wrong or revoked token | Copy it again; check for spaces or quotes |
| `Could not reach Apify` | Network problem | Check the connection or proxy |

Tell the user their plan and how much budget is left this month. That number limits what
they can collect until the reset date.

## Step 7: Choose where data goes. **User decides.**

Projects and collected data live outside the `tracker` folder, because this repository is
public and projects name the accounts being researched. Ask the user for a location, for
example `~/research/<project>` for the config and an external drive for the data. A large
archive with video needs tens to hundreds of gigabytes; metadata alone needs very little.

## Step 8: A first, small scrape

A small test proves the whole chain and costs a few cents. Ask the user for one public
account and get their approval for the spend, as `CLAUDE.md` requires.

Create a project (replace the three placeholders):

```bash
archive_pipeline/.venv/bin/python archive_pipeline/new_project.py <project folder> --name "First test" --slug first_test --archive-root <data folder>
```

Edit `<project folder>/project.toml`:

```toml
[apify]
enabled = true                      # the user approved this spend
env_file = "/absolute/path/to/tracker/.env"
total_budget_usd = 1.0

[youtube]
tabs = []
```

Then collect 20 Instagram posts from one account, capped at $0.50. Run this from
`archive_pipeline/`:

```bash
ARCHIVE_CONFIG=<project folder>/project.toml .venv/bin/python fetch_apify.py first_test apify~instagram-post-scraper 0.5 '{"username": ["<account>"], "resultsLimit": 20, "dataDetailLevel": "detailedData"}'
```

**Expect:** a `run ... started` line, a status line every 20 seconds, then
`SUCCEEDED saved 20 items -> <data folder>/raw/first_test.json`, usually within two minutes.

Show the user what came back: the number of posts, the date range, and one example caption.
Setup is complete.

## What next

- `docs/GUIDE.md` explains what each tool does and how a real project runs.
- `README.md` has example requests the user can make.
- To collect more from the same account, use a new dataset name or a real project config:
  re-running the same command does nothing, because that dataset is already saved.
