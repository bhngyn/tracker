# Guide: how the kit works

This explains what the kit is, how its parts fit together, and how a project runs from start
to finish. Read `docs/SETUP.md` first if nothing is installed yet.

## The idea

[Apify](https://apify.com) runs scrapers, which it calls **actors**, on its own servers. You
send an actor an input (which accounts, how many posts), it runs, and it leaves the results
in a **dataset** that you download. Apify bills for each run, mostly per item returned.

This kit is a set of scripts that sit between you and Apify. They do three things that
calling Apify directly does not:

- **Cap the spend.** Every run has a hard dollar limit, and a project has a total budget
  that no combination of runs can exceed.
- **Never pay twice.** Each run is recorded the moment it starts. Run the same command again
  after a crash and it picks up the existing run instead of starting a new one.
- **Keep the evidence.** Raw results are saved unmodified, alongside the record of the run
  that produced them.

Claude Code is the operator. `CLAUDE.md` tells it how to use the scripts and what it must
ask the user before acting.

## The parts

| Path | What it is |
|---|---|
| `CLAUDE.md` | Operating rules for Claude. Loaded automatically by Claude Code. |
| `setup.sh` | Installs the kit and creates `.env`. |
| `check_apify.py` | Free check of the token, plan and remaining monthly budget. |
| `.env` | Your Apify token. Git-ignored; created by `setup.sh`. |
| `archive_pipeline/` | Instagram, Facebook and YouTube collection, plus processing and the review kit. |
| `archive_pipeline/fetch_apify.py` | The capped runner. Works with any Apify actor. |
| `x_backfill/` | Full text history of an X account. |
| `docs/apify-output-schemas.md` | The fields each scraper returns. |

## Projects

A **project** is one piece of research: a set of accounts, a budget and a data folder. It is
described by one file, `project.toml`, kept in a folder outside this repository.

```
~/research/acme/                  project folder (you choose it)
  project.toml                    targets, budgets, languages, wording
  watchlist.json                  optional: names to flag in the results

/Volumes/Drive/acme_archive/      archive root (you choose it): all collected data
```

`archive_pipeline/new_project.py` creates the project folder from a template, and
`project.example.toml` documents every setting. Scripts find the project through the
`ARCHIVE_CONFIG` environment variable or `run_all.sh --config`.

The `[apify]` section of `project.toml` is where spending is approved:

```toml
[apify]
enabled = false            # paid runs are refused until this is true
env_file = "/path/to/tracker/.env"
total_budget_usd = 0.0     # hard ceiling across every run in this project
```

## How a paid run works

`fetch_apify.py <dataset> <actor> <max_usd> '<input json>'` is the only place the pipeline
spends money. In order, it:

1. Stops if `<archive root>/raw/<dataset>.json` already exists with a finished run.
2. Resumes the run recorded in `raw/<dataset>.run.json`, if there is one.
3. Otherwise refuses unless `enabled = true`, then adds up the spend of every earlier run in
   the project and trims `<max_usd>` so the total can't pass `total_budget_usd`. It refuses
   if less than $0.50 is left.
4. Starts the actor with that cap, which Apify enforces on its side, and writes the run
   record immediately.
5. Checks the run every 20 seconds until it finishes, then saves the dataset.

A run that reaches its cap stops with the items collected so far. That is a partial result,
not a failure, and the files still save.

## Two ways to run a project

**Collection only** (core install, any OS). Run `fetch_apify.py` once per dataset. You get
the raw post metadata as JSON: captions, timestamps, engagement counts and links to media.
Nothing is downloaded beyond that.

**The full pipeline** (full install, Apple silicon Mac). One command runs everything:

```bash
./run_all.sh --config <project folder>/project.toml
```

| Stage | What happens |
|---|---|
| 0 | Instagram and Facebook metadata through Apify, within the budgets in the config |
| 1 | YouTube videos (free, yt-dlp), plus the photos and videos behind the Instagram and Facebook posts. Transcription and OCR follow along as files arrive. |
| 1b | Facebook histories that hit Apify's monthly limit continue after it resets |
| 2–4 | Retry failed downloads, backfill captions, final transcription and OCR pass |
| 5 | Search index, watchlist hits, CSV and SQLite exports, the viewer |
| 6 | Packages: the full archive with a manifest and methodology, and a Lite zip without video |

A large archive takes hours to days. It is safe to stop and restart: every stage skips work
that is already done. Watch progress with `status.py --watch`. `archive_pipeline/README.md`
covers monitoring, pausing, the output layout and the watchlist format.

## X / Twitter

`x_backfill/backfill.py` is separate and needs no project config. It walks an account month
by month, so it reaches the full history, and writes one JSON line per post. It has its own
cap (`--max-usd`) and its own checkpoint (`state.json`), and collects text only. See
`x_backfill/README.md`.

## What you end up with

| You ran | You get |
|---|---|
| `fetch_apify.py` | `raw/<dataset>.json` (the posts) and `raw/<dataset>.run.json` (the run: time, cost, status) |
| `backfill.py` | `tweets.jsonl`, `state.json`, `run.log` |
| `run_all.sh` | The above, plus media files, transcripts, OCR text, `OPEN ME.html` (offline search and review), `exports/items.csv`, `exports/archive.sqlite`, `METHODOLOGY.md` and a checksum manifest |

`OPEN ME.html` opens in any browser from the drive, with no server and nothing to install,
so an archive can be handed to a reviewer as a folder.

## Cost

| Platform | Actor | Rough price |
|---|---|---|
| X / Twitter | `kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest` | $0.22 per 1,000 posts |
| Instagram | `apify/instagram-post-scraper` | $2.30 per 1,000 posts |
| Facebook | `apify/facebook-posts-scraper` | $4 to $5 per 1,000 posts |
| YouTube | none (yt-dlp) | Free |

Prices are set by the actors' authors and change; the actor's page on apify.com is the
authority. Three limits apply at once, and the lowest one wins: the cap on the run, the
project's `total_budget_usd`, and the monthly limit on the Apify account.

## Limits worth knowing

- Scrapers return what the platform shows publicly at the time. Deleted and private posts
  are absent, and very long histories can come back incomplete. Compare the count against
  what the profile shows before calling a collection complete.
- Engagement numbers are a snapshot from the moment of collection.
- Media links in the metadata are temporary and stop working after a while. To keep photos
  and videos, run the full pipeline soon after collecting.
- Actors change their inputs and outputs without notice. If a run fails on input, check the
  actor's page.
- Run one `run_all.sh` project at a time per machine.
