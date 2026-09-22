# X to MD

Pull tweets from X accounts into markdown, then turn them into a daily brief with a local model.

## Prerequisites

- Python 3.10+
- [LM Studio](https://lmstudio.ai), for the brief: install it, download a model (`qwen/qwen3.5-9b` is what this is tested with), and do the one-time setup below. Fetching and digesting tweets needs no LLM at all.

## Install

```bash
python -m venv .venv
. .\.venv\Scripts\Activate.ps1          # Linux/macOS: . .venv/bin/activate
pip install -e .
cp sources.example.yaml sources.yaml    # settings: backend, API key
cp -r sources.example sources           # then edit sources/x.md: your account list
```

## LM Studio setup

One-time, in the LM Studio app:

1. Download the model (`qwen/qwen3.5-9b`).
2. **Switch thinking off.** Qwen 3.5 thinks by default, which spends its reply budget on reasoning instead of an answer, so replies come back empty otherwise. Untick **Thinking** in the model's settings, then reload the model.
3. Load it with a **context length of at least 8192**.
4. Check it: `python scripts\smoke_engine.py --model qwen` (synthetic posts only, about a minute).

That covers `make_brief.py` below. For the headless script, also install LM Studio's own `lms` CLI (bundled with the app — run the app once and `lms` lands on PATH) so the script can start and stop the server itself.

## Daily use

```bash
. .\.venv\Scripts\Activate.ps1
python scripts\run_headless.py --24h             # digests/<date>-<hour>-24h-brief.md, last 24 hours
python scripts\run_headless.py --since-last-run  # or: only what's new since your last digest
```

`run_headless.py` starts LM Studio's server, loads the model, fetches, digests, summarizes and assembles the brief, then stops the server again — the GUI never needs to open. `--24h` is everything published in the last 24 hours; `--since-last-run` is only what was fetched since your previous digest, so it's short when that was recent. Either one moves the since-run cursor; you choose the window every time, there's no default.

**Prefer to manage LM Studio's server yourself** (Developer tab, GUI open)? `make_brief.py` takes the same flags and writes the same output, it just expects the server already running:

```bash
python scripts\make_brief.py --24h
```

**Manual, step by step** (useful to inspect or retry one stage):
```bash
xmd fetch
xmd digest
python scripts\run_system.py --model qwen
python scripts\assemble_brief.py
```

## Commands

Run all commands from the repo root. `--config` and `--version` go before the subcommand: `xmd --config other.yaml fetch`.

| Command | Flag | Default | What it does |
| --- | --- | --- | --- |
| `xmd` | `--config PATH` | `sources.yaml` | Settings file. |
| `xmd` | `--version` | | Print the version and exit. |
| `xmd fetch` | `--loop [SECONDS]` | one pass; `900` with no value | Keep fetching, pausing `SECONDS` between passes; `Ctrl+C` stops. Fills the store only, builds no digest. |
| `xmd digest` | `--window {since-run,24h}` | `since-run` | `since-run`: everything *fetched* since the last digest (the last 24 h on a first run). `24h`: a fixed rolling 24 hours by *published* date. Any digest moves the since-run cursor. Writes nothing if the window is empty. |
| `xmd digest` | `--full` | off | Also write the full digest, `<stamp>.md`. |
| `xmd digest` | `--quick` | off | Also write the quick digest, `<stamp>-quick.md`. |
| `xmd sources` | | | List the configured handles with their group. |

`xmd fetch` keeps tweets in `xmd.db` (SQLite, deduped by tweet URL, never purged). Its per-source lookback is sized to the time since your last fetch — narrower on a quick re-run, capped at 7 days after a longer break. A source that fails to fetch does not stop the others: after each pass `xmd fetch` lists the handles that failed, with the reason, so you can spot an account that changed its @.

`xmd digest` writes to `digests/`, by default only what the brief is made from — the LLM export (`.export/<stamp>.txt` + `.map.json`, priority and image tweets left out on purpose) and the id of every item in the digest (`.export/<stamp>.items.json`). On request: `<stamp>.md` (`--full`, the full archive) and `<stamp>-quick.md` (`--quick`, one line per regular post, priority and image tweets whole).

## Config

**`sources/x.md`**: X handle per line (see `sources.example/x.md`).

- `priority`: the source's tweets sort first in their section, stay whole in the quick digest and appear in full under **★ Priority** in the brief. Also applies, unmarked, to a tweet from someone who hadn't posted in over 15 days (a source with no earlier post in your database is left alone — it can't be told from one you just added).
- `recap` (group option): a group only worth its overall picture. The quick digest collapses it; the brief keeps about 6% of its words; the full digest still lists every tweet.
- `high` or `low` (`normal` is the default): how much of the group the brief keeps, ×1.5 or ×0.5 of the usual share. A recap group ignores a level.

**`sources.yaml`**: your API keys and settings — see `sources.example.yaml`.

## The brief

**What you get**, in `digests/<date>-<hour>-<window>-brief.md`:

- A header: date, `~N min read`, a link to the full digest if `--full` wrote one.
- **★ Priority**: every priority tweet, in full.
- **Trending**: stories two or more sources posted.
- One section per group, most important first (`high`, normal, `low`, recap last). Each opens with **In short** (written by the model) and **Louder than usual** (names posted far above their normal volume over the last 14 days, counted not guessed), then the group's image tweets whole, summary bullets linking to the posts they cite (🔥 marks a repeated story), and retweet themes.

Priority and image tweets never reach a model. Retweets with your own comment count as regular posts; plain retweets become a few themes (about 4% of their words).

**Checks.** Nothing the model writes is taken on trust: a cited post number not in the chunk is removed (an item left with no real source is dropped); a number, `@handle` or `$ticker` none of the cited posts contain gets a visible ⚠; a reply that's cut off, not JSON, or far too short is retried once, then falls back to one-liners under "Not summarized".

**Files.** `digests/.runs/<label>/` is one run (`run.json`, one JSON per chunk, `sections.json`); read it with `run_stats.py`, delete old ones whenever. `digests/` is git-ignored. Only the summarizing is local, to a server on your own machine (a `--base-url` anywhere else is warned about) — `xmd fetch` sends your follow list and API key to whichever provider you chose (twitterapi.io or tweetapi.com).

## Scripts reference

Every script also takes `--help` for its full flag list. Flags several scripts share:

- `--base-url URL` (default `http://127.0.0.1:1234/v1`) / `--api-key KEY` (default `$XMD_LLM_API_KEY`): the OpenAI-compatible server. Prefer the env var over typing a key on the command line — it stays out of your shell history.
- `--digests-dir DIR` (default `digests`) / `--config PATH` (default `sources.yaml`).

### `run_headless.py` — recommended for daily use

Drives LM Studio's own `lms` CLI (server start/stop, model load/unload) around `make_brief.py`, so the GUI never opens. Only stops what it started; teardown runs even on failure or Ctrl+C.

| Flag | Default | What it does |
| --- | --- | --- |
| `--24h` / `--since-last-run` | required | Same as `make_brief.py`, below — and every other `make_brief.py` flag works here too. |
| `--smoke` | off | Run `smoke_engine.py` instead of a real brief: ~1 minute sanity check. |
| `--model NAME` | `qwen/qwen3.5-9b` | Model id for `lms load` and the pipeline. |
| `--gpu` | unset | `lms load --gpu` (`0`-`1`, `off`, `max`); left unset to keep the GPU offload already tuned in LM Studio. |
| `--port N` | `1234` | LM Studio's server port. |
| `--ttl SECONDS` | `1800` | Auto-unload if the script itself dies mid-run. |
| `--lms-path PATH` | on `PATH` | Where `lms` lives, if not on `PATH`. |

### `make_brief.py`

Fetch, digest, summarize and assemble in one go. Stops before fetching if LM Studio's server never comes up, if no model fits, or if the model doesn't answer, so the since-run window isn't used up for nothing. Exit code 1 when a chunk failed (the brief is still written, with those posts as one-liners).

| Flag | Default | What it does |
| --- | --- | --- |
| `--24h` / `--since-last-run` | required, mutually exclusive | The window the brief covers. |
| `--model NAME` | `qwen/qwen3.5-9b` | Model id, or part of one. |
| `--compression RATIO` | `0.30` | Share of the regular posts' words the brief keeps: `0.30` or `30%`; `0.10` is shorter. |
| `--no-fetch` | off | Skip the fetch: the store is fresh already. |
| `--wait SECONDS` | `900` | How long to wait for LM Studio's server to come up; `0` fails at once if it's down. |

### `run_system.py`

Chunks the newest export, sends each chunk to the model, saves the raw replies under `digests/.runs/<label>/`.

| Flag | Default | What it does |
| --- | --- | --- |
| `--model NAME` | the server's only model | Model id, or part of one. |
| `--compression RATIO` | `0.30` | As `make_brief.py`, above. |
| `--dry-run` | off | Print the chunk plan (sizes, item limits, word targets) and stop; no model call. |
| `--only ID,ID` | all chunks | Run only these chunks, e.g. `regular-01,recap-03`. |
| `--stats` | off | Print the chunk plan, per-chunk progress, token counts, chars-per-token, and the full `run_stats.py` table (always available on demand: see below). |

### `assemble_brief.py`

Checks a run's summaries and writes `digests/<date>-<hour>-<window>-brief.md`. Asks the model once per section for its **In short** line, cached in the run's folder, so re-assembling is free.

| Flag | Default | What it does |
| --- | --- | --- |
| `--run LABEL` | newest in `digests/.runs/` | Which run to assemble. |
| `--no-summaries` | off | Skip the section summaries: no model call. |
| `--refresh` | off | Ask the model again even where an answer is cached. |

### Stats and model evaluation

- **`run_stats.py`** — per-chunk counts for a run: items, cited numbers that are real, coverage, repeats. Numbers only, safe to paste. `--run LABEL`, `--show-unsupported` (also lists items with a figure no cited post contains — prints tweet text, for your eyes only).
- **`smoke_engine.py`** — checks a model with synthetic posts only: does it answer, does it honour a JSON schema, how long does one full chunk take. `--list` (print the server's models and exit), `--model NAME`. To try another model: `--list`, then `--model NAME`, then `run_system.py --model NAME --dry-run --only regular-01`.
- **`freeze_export.py`** — rebuilds a fixed past window from the database, for reproducible comparisons; not part of the daily workflow. `--from TIME --to TIME` (required, UTC), `--fetched-by TIME`.

## Troubleshooting

| You see | Cause and fix |
| --- | --- |
| `warm-up failed ... spent its tokens thinking`, or empty replies | Turn off thinking (LM Studio setup, step 2), then reload the model. |
| `WARNING ... context window is probably too small` | Raise the context length to 8192 or more in the model's load settings. |
| `cannot reach the server` | Start LM Studio's server (Developer tab), or pass `--base-url`. |
| `lms (LM Studio's CLI) is not on PATH` (`run_headless.py`) | Run the LM Studio desktop app at least once, or pass `--lms-path`. |
| `section summary ...: skipped` | The server was down at assemble time: start it and run `assemble_brief.py` again; cached answers aren't asked twice. |
| chunks FAILED "too thin" or "hit the token cap" | The model can't follow the format: try another model; look at `run_stats.py`. |
| `no export in digests/.export` | Run `xmd digest` first. |
| `unknown option 'hgih' on group ...` | A typo on a group header in `sources/x.md`: use `high`, `normal`, `low` or `recap`. |

## Layout and development

```text
xmd/        the tool. cli.py: the `xmd` command.
  core/       models, config, store, tiers: what everything else shares.
  ingest/     fetcher, filters, and adapters/: the X APIs.
  digest/     render, trending, export: the digests.
  summary/    the model side: chunk, prompt, engine, runner; verify, topics, sections, brief.
prompts/    brief.md (chunk prompt), section.md (section-summary prompt)
scripts/    the seven scripts above
docs/       digest-summary.md: the brief's changelog, design and next optimization routes
tests/      pytest, in the same folders as xmd/ (test_cli.py at the top)
```

A package may import from those above it in that list and never from below: `core` imports nothing, `ingest` and `digest` import `core`, `summary` imports `digest` and `core`, `cli.py` imports all.

Another source is an adapter module with `fetch(source, api_key, max_items, **kwargs) -> list[FeedItem]`. `xmd/ingest/fetcher.py` picks the X backend from `config.x_backend` (`x.backend` in `sources.yaml`); `Source.type` is a fixed `"twitterapi"` that nothing reads yet, so a new platform means a new branch in `fetch_all` there.

```bash
pip install -e ".[dev]"
python -m pytest
```

## License

See [LICENSE](LICENSE).
