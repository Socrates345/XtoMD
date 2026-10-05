# X to MD

Pull tweets from X accounts into markdown, then turn them into a daily brief with a local model.

## Prerequisites

- Python 3.10+
- [LM Studio](https://lmstudio.ai), for the brief: install it, download a model (`qwen/qwen3.5-9b` is what this is tested with), and do the one-time setup below. Fetching and digesting tweets needs no LLM at all.
- Tweetapi.com subscription (or twitterapi.io)

## Install

```bash
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -e .
cp sources.example.yaml sources.yaml    # settings: backend, API key
cp -r sources.example sources           # then edit sources/x.md: provide all the X accounts you'd like to retrieve tweets from
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
python scripts\run_headless.py --24h --time 10   # short on time: a brief you can read in ~10 minutes
```

`run_headless.py` starts LM Studio's server, loads the model, fetches, digests, summarizes and assembles the brief, then stops the server again — the GUI never needs to open. `--24h` is everything published in the last 24 hours; `--since-last-run` is only what was fetched since your previous digest, so it's short when that was recent. Either one moves the since-run cursor; you choose the window every time, there's no default. `--time MIN` fits the brief to a reading time (see [Reading time](#reading-time)); without it the brief is as long as the day was.

**Prefer to manage LM Studio's server yourself** (Developer tab, GUI open)? `make_brief.py` takes the same flags and writes the same output, it just expects the server already running:

```bash
python scripts\make_brief.py --24h
```

### Manual, step by step

Useful to inspect or retry one stage. **These scripts never start LM Studio**: the server must be running, with the model loaded, before `run_system.py`, and still running for `assemble_brief.py` (it asks the model for the section summaries and translations). `run_headless.py` stops the server when it finishes, so after a headless run it is down. Start it in the app (Developer tab, then load the model), or from a terminal with `lms`, as below:

```bash
lms server start --port 1234 --bind 127.0.0.1               # or: LM Studio app, Developer tab
lms load qwen/qwen3.5-9b --context-length 8192 --ttl 1800   # --ttl: unloads itself after 30 idle minutes
xmd fetch
xmd digest --window 24h                                     # or --window since-run (the default)
python scripts\run_system.py --model qwen --time 10         # summarizes the newest export (or --compression 0.3)
python scripts\assemble_brief.py                            # writes digests\<date>-<hour>-<window>-10min-brief.md
lms unload qwen/qwen3.5-9b
lms server stop
```

`run_system.py` takes no window flag: `--24h` and `--since-last-run` belong to `make_brief.py` and `run_headless.py`. It summarizes the newest export that `xmd digest` wrote, whatever window that digest covered.

### Redo a brief with another reading time

Same tweets, shorter or longer brief. Skip `xmd fetch` and `xmd digest`: a new digest covers a different window and moves the since-run cursor. A new brief never replaces an earlier one: its name carries its reading time (`-10min`), and a name already taken gets a number (`...-brief-2.md`).

**Quick:** re-cut the run you already have. Only the image and priority tweets change, the summaries stay as long as they were. It needs the model only to translate tweets it newly shows, when `language:` is set; with the server down they stay as posted:

```bash
python scripts\assemble_brief.py --time 10
```

**Thorough:** summarize again, so the summaries are sized for the time too. It only differs from the quick way when the new time changes the compression (a short time on a long day): `python scripts\run_system.py --time 10 --dry-run` prints the compression it would use, without calling the model.

```bash
lms server start --port 1234 --bind 127.0.0.1
lms load qwen/qwen3.5-9b --context-length 8192 --ttl 1800
python scripts\run_system.py --model qwen --time 10
python scripts\assemble_brief.py
lms unload qwen/qwen3.5-9b
lms server stop
```

Every run stays in `digests\.runs\`; `assemble_brief.py --run LABEL` assembles any of them again.

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
- `recap` (group option): a group only worth its overall picture. The quick digest collapses it; the brief sorts it into themes (about 12% of its words, roughly one theme per 5 posts), each linking every post it groups; the full digest still lists every tweet.
- `high` or `low` (`normal` is the default): how much of the group the brief keeps, ×1.5 or ×0.5 of the usual share. A recap group ignores a level.

**`sources.yaml`**: your API keys and settings — see `sources.example.yaml`.

## The brief

**What you get**, in `digests/<date>-<hour>-<window>-brief.md` (the date and hour the brief was written, on the `timezone:` clock in `sources.yaml`, Europe/Paris by default; `-10min` before `-brief` when fitted to a reading time; `-2`, `-3`... after it rather than replace an earlier brief of the same name):

- A header: date, `~N min read` (and, fitted to a reading time, what was cut), a link to the full digest if `--full` wrote one.
- **★ Priority**: every priority tweet, in full (one line each when a short `--time` needs it).
- **Trending**: stories two or more sources posted.
- One section per group, most important first (`high`, normal, `low`, recap last). Each opens with **In short** (written by the model) and **Louder than usual** (names posted far above their normal volume over the last 14 days, counted not guessed), then the group's image tweets whole, summary bullets linking to the posts they cite (🔥 marks a repeated story), and retweet themes.

**Languages.** Set `language:` in `sources.yaml` to the languages you read (`accepted`) and the one of them the rest is turned into (`translate_to`). A summary of posts in an accepted language stays in that language; posts in any other language, or a story told in several, are summarized in `translate_to`. The tweets the brief shows as posted (priority, images, trending, "Not summarized") are tagged by the model and, when not in an accepted language, translated with the original quoted under them (`🌐`); a number or handle the original lacks gets a ⚠. This happens only while the brief is written: the store and the digests keep every post as it was posted. `assemble_brief.py --no-translate` skips it for one run.

Priority and image tweets never reach a model for a summary; with `language:` set, the ones in a language you don't read are sent to it to be translated. Retweets with your own comment count as regular posts; plain retweets become a few themes (about 4% of their words).

**Checks.** Nothing the model writes is taken on trust: a cited post number not in the chunk is removed (an item left with no real source is dropped); a number, `@handle` or `$ticker` none of the cited posts contain gets a visible ⚠; a reply that's cut off, not JSON, or far too short is retried once, then falls back to one-liners under "Not summarized".

**Files.** `digests/.runs/<label>/` is one run (`run.json`, one JSON per chunk, `sections.json`); read it with `run_stats.py`, delete old ones whenever. `digests/` is git-ignored. Only the summarizing is local, to a server on your own machine (a `--base-url` anywhere else is warned about) — `xmd fetch` sends your follow list and API key to whichever provider you chose (twitterapi.io or tweetapi.com).

### Reading time

**How it is counted.** The `~N min read` in the header is your pace, not a word count: **40 lines a minute, plus 4 seconds a picture**. Blank lines are free.

| In the brief | Costs |
| --- | --- |
| A summary bullet, a one-line tweet, a heading | 1 line: 1.5 s |
| An image tweet shown whole, one picture | 2 lines (picture, caption) + 4 s: 7 s |
| The same tweet cut to a line (🖼) | 1.5 s |
| 10 minutes | 400 lines, or about 85 one-picture image tweets |

The pace is two constants in `xmd/summary/brief.py`, `LINES_PER_MINUTE` and `SECONDS_PER_IMAGE` (measured once: 200 lines in 5 minutes, links clicked). Change them if you read faster or slower: the header, `--time` and everything the scripts print follow.

**Without `--time`** the brief is as long as the day was: the summaries keep 30% of the regular posts' words (`--compression`), everything else is shown whole. Most of the time is not the summaries: on 2026-09-29, image tweets were two thirds of a 30-minute brief and priority tweets a fifth, so lowering the compression from 0.30 to 0.10 saved about a minute. The compression is the weak lever, the pictures are the strong one.

**With `--time MIN`** the brief is fitted to that many minutes at that pace. The time is spent in this order, each part taking from what the ones above leave:

| | Part | Rule | Decided |
| --- | --- | --- | --- |
| 1 | Header, trending, section headings, **In short** | Always shown. | |
| 2 | Priority tweets | In full, unless that takes more than half the time, or the brief can't fit otherwise: then one line each, the full tweet behind its link. | when assembled |
| 3 | Summaries | Compression 0.30, unless they would take more than half the time, or more than 1 and 2 leave: then the highest compression at which they don't, never under 0.10. | before the model runs |
| 4 | Image tweets | What is left. Half of it shows the first ones whole with their pictures, the rest gives the next ones a line (🖼), and the others are listed as links. They take turns across groups (a `high` group gets three times the share of a `low` one of the same size) and across sources, so one prolific account can't take them all. | when assembled |

So a time a little short of the day costs you pictures only; the summaries get thinner only when they alone would crowd the brief.

**Example.** One real digest (2026-10-01, 909 tweets): uncut it is ~27 minutes, of which image tweets ~17, summaries ~6 at most, priority tweets ~2.

| You ask for | Compression | Summaries | Priority tweets | Image tweets |
| --- | --- | --- | --- | --- |
| no `--time`, or `--time 30` | 0.30 | ~6 min | in full | all whole, ~17 min |
| `--time 20` | 0.30 | ~6 min | in full | ~11 min of the 17 |
| `--time 10` | 0.21 | ~5 min: half the time | in full | ~1 min, plus what the summaries don't use |
| `--time 5` | 0.10 | ~3 min: the floor | one line each | links only; the brief may come out a little longer than asked |

`run_system.py` prints this split before it calls the model:

```text
time 10 min (40 lines a minute, 4 s a picture): uncut, this brief would take ~27 min. So:
  summaries        ~5.0 min at most, compression 0.21 (lowered from 0.30 to fit half the time)
  priority tweets  ~2.2 min, in full
  image tweets     ~1.3 min of the ~17.4 they take whole, plus what the summaries leave unused: pictures first, then a line each, then links
  the summaries are sized now; the priority and image tweets are cut when the brief is assembled
```

"At most" because the summaries don't exist yet: they are counted as the longest the model may write, and it usually writes about a fifth less. `assemble_brief.py` then measures the brief as written, gives what is left to the image tweets, and prints what it did (`time 10 min: N of M image tweets keep their pictures, K get a line, the rest a link`). The header of the brief says the same: `~10 min read · fitted to 10 min: N of M image tweets with pictures`.

**Which command does what:**

| You type | Summaries | Priority and image tweets |
| --- | --- | --- |
| `run_headless.py --24h` or `make_brief.py --24h` | compression 0.30 | whole: the brief is as long as the day |
| the same with `--time 10` | sized for 10 minutes (rule 3) | cut to 10 minutes (rules 2 and 4) |
| `run_system.py --time 10`, then `assemble_brief.py` | the same, by hand | the same: the run remembers its time |
| `assemble_brief.py --time 10` on a run you already have | unchanged, no model call | re-cut to 10 minutes |
| `run_system.py --compression 0.15`, then `assemble_brief.py --time 10` | the compression you chose | cut to 10 minutes |
| `run_system.py --time 10 --dry-run` | prints the split and the compression, calls no model | |

A time shorter than the barest brief (summaries at 0.10, trending, a line per priority tweet, links for the images) gives that brief, and `assemble_brief.py` says how long it is.

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
| `--time MIN` | unset: the whole day | Fit the brief to a reading time in minutes, e.g. `10` or `30` (see [Reading time](#reading-time)). |
| `--no-fetch` | off | Skip the fetch: the store is fresh already. |
| `--wait SECONDS` | `900` | How long to wait for LM Studio's server to come up; `0` fails at once if it's down. |

### `run_system.py`

Chunks the newest export, sends each chunk to the model, saves the raw replies under `digests/.runs/<label>/`. Needs the server up and the model loaded (see [Manual, step by step](#manual-step-by-step)). No window flag: the window is whatever the newest `xmd digest` covered, or pass `--export`.

| Flag | Default | What it does |
| --- | --- | --- |
| `--model NAME` | the server's only model | Model id, or part of one. |
| `--time MIN` | unset | Size the summaries for a reading time: compression 0.30 unless they would take more than half of it (see [Reading time](#reading-time)). `assemble_brief.py` then cuts priority and image tweets to it. |
| `--compression RATIO` | `0.30` | Or set it yourself: share of the regular posts' words the summaries keep, `0.30` or `30%`. Not with `--time`. |
| `--dry-run` | off | Print the chunk plan (sizes, item limits, word targets) and stop; no model call. |
| `--only ID,ID` | all chunks | Run only these chunks, e.g. `regular-01,recap-03`. |
| `--stats` | off | Print the chunk plan, per-chunk progress, token counts, chars-per-token, and the full `run_stats.py` table (always available on demand: see below). |
| `--export PATH` | newest in `digests/.export/` | The export `.txt` to summarize. |

### `assemble_brief.py`

Checks a run's summaries and writes `digests/<date>-<hour>-<window>-brief.md`. Asks the model once per section for its **In short** line (and, with `language:` set, translates), cached in the run's folder, so re-assembling is free. The server must still be up for those calls: with it down, the brief is written without them. It never replaces an earlier brief: a name already taken gets a number. A run made with `--time` is fitted to it (see [Reading time](#reading-time)).

| Flag | Default | What it does |
| --- | --- | --- |
| `--run LABEL` | newest in `digests/.runs/` | Which run to assemble. |
| `--time MIN` | the run's own `--time` | Fit the brief to this reading time; any run can be re-cut this way. |
| `--out PATH` | `digests/<date>-<hour>-<window>[-<N>min]-brief.md` | Where to write the brief (a path given here is used as is). |
| `--no-summaries` | off | Skip the section summaries: no model call. |
| `--no-translate` | off | Show every tweet as posted, whatever `language:` says. |
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
| `cannot reach the server` | The server isn't running: `run_headless.py` stops it when it's done, and the other scripts never start it. Start it in LM Studio (Developer tab) or with `lms server start --port 1234 --bind 127.0.0.1` then `lms load qwen/qwen3.5-9b --context-length 8192` (see [Manual, step by step](#manual-step-by-step)), or pass `--base-url` if it runs elsewhere. |
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
