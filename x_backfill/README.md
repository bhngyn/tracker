# x_backfill: historical X/Twitter text backfill

Collects the full post history of one X account as text, through Apify. It reaches further
back than a "latest posts" scrape can, because it walks the account month by month.

**Standard library only.** No `pip install`, no virtualenv. It talks to Apify over plain
HTTP and writes JSONL.

## Quick start

```bash
python3 backfill.py --handle example_handle --since 2017-01-01 --max-usd 5
```

It reads `APIFY_API_TOKEN` from the environment, falling back to the kit's `.env`.

Size the job before committing to it:

```bash
python3 backfill.py --handle example_handle --estimate --samples 6
```

`--estimate` samples months across the range and projects total volume and cost. **It is a
paid run**, though a small one: sampled posts are written to the real archive and their
windows are marked complete, so the spend counts towards the backfill.

## How it works

1. It slices the range into monthly windows with the actor's `since`/`until` search operators.
2. It runs one actor job per window at `--page-size` (default 1000).
3. If a window returns a full page, it walks backwards through the month with a `max_id`
   cursor until the month is exhausted.
4. It dedupes by post ID and appends to `tweets.jsonl`.
5. It checkpoints each finished window to `state.json`. A re-run skips completed windows,
   so the job can be interrupted and resumed.

## Cost control

The actor bills per item returned (about $0.00022 per post on the Starter plan; check the
actor's page for the current price). `--max-usd` is a hard cap, checked before every call:
the run stops cleanly instead of overshooting. The default cap is $20, so pass your own.

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--handle` | required | X handle, without `@` |
| `--since` / `--until` | `2017-01-01` / today | Date range |
| `--page-size` | 1000 | `maxItems` per actor run |
| `--max-pages` | 20 | Cursor pages per month (runaway guard) |
| `--concurrency` | 4 | Windows fetched in parallel |
| `--max-usd` | 20.0 | Hard spend cap |
| `--estimate` | off | Sample, project the cost, then exit |
| `--out` | `data/<handle>/` | Output directory |

## Output

`data/<handle>/tweets.jsonl` holds one post per line, sorted by ID (chronological):

```json
{"id":"847644787817590785","url":"https://x.com/...","created_at":"2017-03-31T03:00:40+00:00",
 "text":"...","lang":"es","username":"example_handle","likes":747,"retweets":263,"replies":105,
 "quotes":47,"views":0,"is_reply":false,"is_quote":false,"is_retweet":false,
 "conversation_id":"...","reply_to_id":null,"hashtags":[],"mentions":[],"links":[],
 "quoted_text":null}
```

Alongside it are `state.json` (checkpoint and spend) and `run.log`.

It collects text only: no media, and the raw Apify item is dropped after mapping
(`twitter_normalizer.py`). `data/` is git-ignored. For anything sensitive, pass `--out` a
folder outside this repository.
