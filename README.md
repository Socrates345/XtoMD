# X to MD

Pull tweets from X accounts into markdown. Then summarize tweets using a local model. 

- **Digest** (`xmd fetch`, `xmd digest`): Retrieve all tweets from a list of account using an API.
- **Brief**: Summarize the digest into today's Brief.


## Install

run commands from the repo root. Replace the model with the most appropriate for your setup.
```bash
python -m venv .venv
. .\.venv\Scripts\Activate.ps1          # Linux/macOS: . .venv/bin/activate
pip install -e .
cp sources.example.yaml sources.yaml    # settings: backend, API key
cp -r sources.example sources           # Fill the account list: edit sources/x.md
xmd fetch                               # pull new tweets into the local store
xmd digest                              # write digests/YYYY-MM-DD-HHMM.md
```

## Daily use, with the brief

```bash
. .\.venv\Scripts\Activate.ps1
xmd fetch                                    # pull new tweets
xmd digest                                   # digests + export, since the last digest
# start LM Studio's local (see setup)
python scripts\run_system.py --model "qwen/qwen3.5-9b"    # summary in minutes.
python scripts\assemble_brief.py             # get: digests/<date>-brief.md
```


## Commands

Run all commands from the repo root.

`--config` and `--version` go before the subcommand: `xmd --config other.yaml fetch`.

| Command | Flag | Default | What it does |
| --- | --- | --- | --- |
| `xmd` | `--config PATH` | `sources.yaml` | Settings file. |
| `xmd` | `--version` | | Print the version and exit. |
| `xmd fetch` | `--loop [SECONDS]` | one pass; `900` with no value | Keep fetching, pausing `SECONDS` between passes; `Ctrl+C` stops. Fills the store only, builds no digest. |
| `xmd digest` | `--window {since-run,24h}` | `since-run` | `since-run`: everything *fetched* since the last digest (the last 24 h on a first run). `24h`: a fixed rolling 24 hours by *published* date. Any digest moves the since-run cursor. Writes nothing if the window is empty. |
| `xmd sources` | | | List the configured handles with their group. |

`xmd fetch` keeps tweets in `xmd.db` (SQLite, deduped by tweet URL, never purged). After a break it widens its lookback to cover the gap, capped at 7 days, so tweets from your time away still land.

`xmd digest` writes to `digests/`:

- `<stamp>.md`, the full archive: every tweet in full, images as remote links, a **Trending** block (stories two or more sources posted), retweets collapsed per section.
- `<stamp>-quick.md`, the scan layer: one line per regular post, plain retweets shorter; priority and image tweets whole.
- `.export/<stamp>.txt` + `.map.json`, the input for a model: numbered lines `[n] @source text` (URLs stripped, retweet echoes removed) grouped by section, and a number → tweet URL map. Priority and image tweets are left out on purpose. It calls no model.


## Config

**`sources/x.md`**: X handle per line. (see `sources.example/x.md`)

- `priority`: the source's tweets sort first in their section, stay whole in the quick digest and appear in full under **★ Priority** in the brief.
- Priority also applies, without any marking, to a tweet from someone who had not posted for more than 15 days: the first post after such a silence is treated like a priority tweet, even when it is a retweet with the poster's own comment. A plain retweet doesn't count, and any earlier post (a plain retweet included) ends the silence. A source with no earlier post in your database is left alone, since it can't be told from one you added yesterday, and the database has to reach back far enough: right after a long gap in fetching, some active posters can look silent.
- `recap` (group option): a group only worth its overall picture. The quick digest collapses it and the brief keeps about 6% of its words; the full digest still lists every tweet.
- `high` or `low` (`normal`, `medium` and `regular` are the default): how much of the group the brief keeps, ×1.5 or ×0.5 of the usual share. A recap group ignores a level; an unknown option word is warned about.

**`sources.yaml`**: Make sure to set your api keys and tweak settings.



### `run_system.py`

Chunk the newest export, send each chunk to the model, save the raw replies under `digests/.runs/<label>/`. Prints counts and timings, no tweet text.

| Flag | Default | What it does |
| --- | --- | --- |
| `--compression RATIO` | `0.30` | Share of the regular posts' words the brief keeps (summary words over source words): `0.30` or `30%`; `0.10` is shorter. Retweets (4%) and the recap group (6%) keep fixed shares. Outside 0 to 1 is refused. |
| `--model NAME` | the server's only model | Model id, or part of one (case-insensitive). |
| `--base-url URL` | `http://127.0.0.1:1234/v1` | OpenAI-compatible server (LM Studio). |
| `--api-key KEY` | none | Only for a server that wants one. |
| `--export FILE` | newest in `digests/.export/` | Export `.txt` to summarize. |
| `--digests-dir DIR` | `digests` | Where `.export/` and `.runs/` live. |
| `--sources-dir DIR` | `sources` | Where `x.md` is: its group levels set how much each group keeps. |
| `--out-dir DIR` | `<digests-dir>/.runs` | Where runs are saved. |
| `--label NAME` | `<model>__c0.30__<time>` | Name of the run's folder. |
| `--only ID,ID` | all chunks | Run only these chunks, e.g. `regular-01,recap-03`. |
| `--dry-run` | off | Print the chunk plan (sizes, item limits, word targets) and stop; no model is called. |
| `--no-think` | off | Send `enable_thinking=false`, for servers that honour it (LM Studio does not: see Setup). |
| `--bound-items` / `--no-bound-items` | bound | Put each chunk's min and max item counts in the JSON schema (LM Studio enforces them), or send the plain schema. |
| `--max-input-tokens N` | `3750` | Estimated tokens of posts per chunk. |
| `--temperature T` | `0.2` | Sampling temperature. |
| `--retries N` | `1` | Extra attempts for a chunk that fails. |
| `--timeout S` | `600` | Seconds per call. |

### `assemble_brief.py`

Check a run's summaries and write `digests/<date>-brief.md` (one brief per date: the newest overwrites). It asks the model once per section for its **In short** line, cached in the run's folder, so re-assembling is free.

| Flag | Default | What it does |
| --- | --- | --- |
| `--run LABEL` | newest in `digests/.runs/` | Which run to assemble. |
| `--config PATH` | `sources.yaml` | Settings file (priority sources, recap groups, digest lengths). |
| `--digests-dir DIR` | `digests` | Where the digests and runs are. |
| `--out FILE` | `<digests-dir>/<date>-brief.md` | Where to write the brief. |
| `--no-summaries` | off | Skip the section summaries: no model call. |
| `--refresh` | off | Ask the model again even where an answer is cached. |
| `--model NAME` | the run's model | Model for the section summaries. |
| `--base-url URL` | the run's server | Server for the section summaries. |
| `--api-key KEY` | none | Only for a server that wants one. |

---

## Stats and model evaluation

### `run_stats.py`

Per-chunk counts for a run: items, cited numbers that are real, coverage, repeats, figures not found in the cited posts. Numbers only, safe to paste.

| Flag | Default | What it does |
| --- | --- | --- |
| `--run LABEL` | newest in `digests/.runs/` | Which run. |
| `--digests-dir DIR` | `digests` | Where the runs are. |
| `--show-unsupported` | off | Also list the items with a figure no cited post contains. Prints tweet text: for your eyes only. |

### `smoke_engine.py`

Check a model with synthetic posts only: does it answer, does it honour a JSON schema, how long does one full chunk take.

| Flag | Default | What it does |
| --- | --- | --- |
| `--list` | off | Print the models the server offers and exit. |
| `--model NAME` | the server's only model | Model id, or part of one (case-insensitive). |
| `--base-url URL` | `http://127.0.0.1:1234/v1` | Server to test. |
| `--api-key KEY` | none | Only for a server that wants one. |
| `--chunk-posts N` | `150` | Posts in the full-chunk test (about 5K tokens). |
| `--timeout S` | `600` | Seconds per call. |
| `--no-think` | off | Send `enable_thinking=false` (Qwen's switch; not every server honours it). |

To try another model: `smoke_engine.py --list`, then `--model NAME`, then `run_system.py --model NAME --dry-run` and `--only regular-01`.

### `freeze_export.py`

Rebuild a fixed past window from the database as a digest plus export, for reproducible comparisons. Not part of the daily workflow.

| Flag | Default | What it does |
| --- | --- | --- |
| `--from TIME` | required | Window start, UTC, inclusive (`2026-09-18T16:00`). |
| `--to TIME` | required | Window end, UTC, exclusive. |
| `--fetched-by TIME` | none | Only posts stored by then (UTC), so a later fetch cannot change the window. |
| `--config PATH` | `sources.yaml` | Settings file. |

---

## The brief

**What you get**, in `digests/<date>-brief.md`: a header (date, `~N min read`, link to the full digest); **★ Priority** (every priority tweet, in full); **Trending**; then one section per group, most important first (`high`, normal, `low`, recap last). Each section opens with **In short** (written by the model) and **Louder than usual** (names posted far above their normal volume over the last 14 days: counted, not guessed), then the group's image tweets whole, summary bullets that link to the posts they cite (🔥 marks a repeated story) and retweet themes. Priority and image tweets never reach a model. Retweets with your own comment count as regular posts; plain retweets become a few themes (about 4% of their words).

**Setup**, tested with [LM Studio](https://lmstudio.ai) and `qwen/qwen3.5-9b`:

1. Download the model in LM Studio.
2. **Switch thinking off.** Qwen 3.5 thinks by default and LM Studio ignores the request to stop, so replies come back empty.
3. Load it with a **context length of at least 8192**.
4. Developer tab: start the server (port 1234)
5. Check it: `python scripts\smoke_engine.py --model qwen` (synthetic posts only, about a minute).

**Checks.** Nothing the model writes is taken on trust:

- Cited post numbers that are not in the chunk are removed, and an item left with no real source is dropped.
- A number, `@handle` or `$ticker` that none of the cited posts contain gets a visible ⚠. A section summary with a figure its input never had is not shown.
- A reply that is cut off, not JSON or far too short is retried once, then its posts fall back to one-liners under "Not summarized".

**Files.** `digests/<date>-brief.md` is the brief. `digests/.runs/<label>/` is one run: `run.json`, one JSON per chunk with the raw reply, and `sections.json` (the section-summary cache). Runs hold text derived from your tweets: read them with `run_stats.py`, delete old ones whenever. `digests/` is git-ignored, everything talks to a server on your own machine, and the scripts print counts and timings, not tweet text.

**Troubleshooting**

| You see | Cause and fix |
| --- | --- |
| `warm-up failed ... spent its tokens thinking`, or empty replies | The model is thinking: set the Jinja first line (Setup, step 2), then reload. |
| `WARNING ... context window is probably too small` | Raise the context length to 8192 or more in the model's load settings. |
| `cannot reach the server` | Start LM Studio's server (Developer tab), or pass `--base-url`. |
| `section summary ...: skipped` | The server was down at assemble time: start it and run `assemble_brief.py` again; cached answers are not asked twice. |
| chunks FAILED "too thin" or "hit the token cap" | The model cannot follow the format: try another model; look at `run_stats.py`. |
| `no export in digests/.export` | Run `xmd digest` first. |
| `unknown option 'hgih' on group ...` | A typo on a group header in `sources/x.md`: use `high`, `normal`, `low` or `recap`. |


## Layout and development

```text
xmd/        the tool. adapters/: the X APIs. cli, fetcher, store, config, filters, models: fetching.
            render, trending, tiers, export: the digests. chunk, prompt, engine, runner: the model side.
            verify, topics, sections, brief: checks, louder names, section summaries, the brief.
prompts/    brief.md (chunk prompt), section.md (section-summary prompt)
scripts/    the five scripts above
docs/       digest-summary.md: the brief's changelog, design and next optimization routes
tests/      pytest
```

Another source is an adapter module with `fetch(source, max_items, **kwargs) -> list[FeedItem]`, dispatched in `xmd/fetcher.py` on `source.type`.

```bash
pip install -e ".[dev]"
python -m pytest
```

## License

See [LICENSE](LICENSE).
