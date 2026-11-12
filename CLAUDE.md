# CLAUDE.md

This kit collects public social-media posts through Apify. The person you are working with
may not be a programmer: do the setup and the commands for them, and explain results in
plain language.

## Rules that always apply

1. **Spending needs a yes.** Apify runs cost real money. Before any paid run, tell the user
   the target, the item limit, the estimated cost and the hard cap, then wait for their
   approval. Their approval covers that run only. `x_backfill/backfill.py --estimate` is
   also a paid run.
2. **Every paid run goes through a capped script**: `archive_pipeline/fetch_apify.py`,
   `archive_pipeline/run_all.sh` or `x_backfill/backfill.py`. Don't start actors with raw
   `curl`, your own script, or an Apify MCP server: those have no budget guard and no
   resume, so a retry pays twice.
3. **Never handle the token.** It lives in `.env`. Don't print it, echo it, put it in a
   command line or URL you write, or commit it. If the user pastes a token into the chat,
   tell them to revoke it in the Apify console and create a new one.
4. **Projects live outside this repository.** This repository is public. Project configs,
   watchlists and scraped data name research targets, so they go in a folder the user
   chooses (ask where; an external drive is typical). Never commit them, and don't write
   target names into any tracked file here.
5. **Never pay twice.** If a run is interrupted, re-run the same command: the scripts resume
   from their saved state. Don't delete `raw/*.run.json` or `state.json`, and don't rename
   a dataset to "start fresh" without the user's approval, because that starts a new paid run.
6. **Public content only.** Don't try to get past logins, private accounts or blocks.

## First session with a new user

1. Run `./setup.sh` if `archive_pipeline/.venv` is missing (`--full` on a Mac with Apple
   silicon when they want media, transcription or OCR).
2. If `.env` has no token, ask them to paste one into the file themselves (README, step 2).
3. Run `archive_pipeline/.venv/bin/python check_apify.py`. It is free and shows the plan
   and the budget left this month. Report both.
4. Ask what they want collected: platforms, accounts, how far back, and a spending limit.

## Choosing a tool

| Request | Tool |
|---|---|
| Instagram accounts or Facebook pages | `archive_pipeline` project |
| YouTube channels (free, uses yt-dlp) | `archive_pipeline` project (needs `--full`) |
| Full text history of an X account | `x_backfill/backfill.py` |
| TikTok, or any other Apify actor | `fetch_apify.py` directly, inside a project |
| Searchable review kit, transcripts, OCR | `archive_pipeline` full run (Mac, `--full`) |

## Instagram, Facebook, YouTube: an archive_pipeline project

Run these from `archive_pipeline/`. Read `archive_pipeline/README.md` for stages,
monitoring and output layout, and `project.example.toml` for every setting.

```bash
.venv/bin/python new_project.py <project folder> --name "<display name>" --slug <short_id> --archive-root <data folder>
```

Then edit `<project folder>/project.toml`:

- `[apify] env_file`: the absolute path of this kit's `.env`.
- `[instagram] accounts`, `results_limit`, `budget_usd`; one `[[facebook.pages]]` block per
  page with its `budget_usd`; `[youtube] tabs` (the template ships with example tabs: replace
  them, or set `tabs = []` when YouTube isn't wanted).
- `[apify] enabled = true` and `total_budget_usd`: **set these only after the user approves
  the spend.** The config is where the approval is recorded. Leave `enabled = false` until then.

Validate, then run:

```bash
ARCHIVE_CONFIG=<project folder>/project.toml .venv/bin/python config.py check
```

```bash
nohup ./run_all.sh --config <project folder>/project.toml >/dev/null 2>&1 &
```

`run_all.sh` needs the `--full` install (macOS on Apple silicon). It runs for hours to days
on a large archive; watch it with `status.py --watch`, don't poll in a loop.

**Metadata only, or not on a Mac:** run just the Apify stage. It needs the core install only
and saves the raw dataset to `<data folder>/raw/<dataset>.json`:

```bash
ARCHIVE_CONFIG=<project folder>/project.toml .venv/bin/python fetch_apify.py <dataset> <actor> <max_usd> '<json input>'
```

| Platform | Actor | Input |
|---|---|---|
| Instagram | `apify~instagram-post-scraper` | `{"username": ["a", "b"], "resultsLimit": 500, "dataDetailLevel": "detailedData"}` |
| Facebook | `apify~facebook-posts-scraper` | `{"startUrls": [{"url": "https://www.facebook.com/<page>/"}], "resultsLimit": 500, "captionText": true}` |

`resultsLimit` is per account. `fetch_apify.py` refuses to run while `enabled = false`, trims
the cap so the project's `total_budget_usd` can't be exceeded, and does nothing if the
dataset already exists.

## X / Twitter: x_backfill

Run from `x_backfill/`. Standard library only, so system `python3` works.

```bash
python3 backfill.py --handle <handle> --since <YYYY-MM-DD> --max-usd <cap> --out <data folder>/x/<handle>
```

Always pass `--max-usd` (the default is $20) and `--out` (a folder outside this repository).
For a large or unknown account, offer `--estimate --samples 6` first and say that it spends
a little. See `x_backfill/README.md`.

## Other platforms and actors

`fetch_apify.py` runs any actor with the same cap and resume behaviour. Before using one
that isn't listed here:

1. Read its page on apify.com for the input schema and the price per result. Actor inputs
   and prices change, so don't rely on memory.
2. Start with a small test: a low `resultsLimit` and a cap of about $1.
3. Check the saved items against `docs/apify-output-schemas.md`, which documents the X,
   Instagram, Facebook, TikTok and YouTube scrapers this kit was built against.

Actor IDs use `~` in place of `/` (`clockworks~tiktok-scraper`).

## Estimating cost

Rough prices per item: X $0.00022, Instagram $0.0023, Facebook $0.004 to $0.005. Multiply by
accounts × limit, and present it as an estimate. Compare it with the budget that
`check_apify.py` reports. A run that stops early with fewer items than expected has usually
hit its cap or the account's monthly limit: say so, don't present it as complete.

## After a run

Report what was collected: item counts per account, date range covered, spend against the
cap, where the files are, and anything that failed or was cut short. Counts come from the
saved files, not from the log's last line.

## Troubleshooting

- `APIFY_API_TOKEN not found`: `.env` is empty, or `[apify] env_file` doesn't point at it.
- `Apify is disabled for this project`: `enabled = false`. Get approval; don't just flip it.
- `refusing to start: only $X left`: the project budget is used up. Raising it is the user's call.
- HTTP 401: the token is wrong or revoked. HTTP 402 or 403 on start: the plan's monthly
  limit is reached, or the actor needs a paid plan or rental.
- `No project config`: `ARCHIVE_CONFIG` isn't set for that command.
