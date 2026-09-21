# XtoMD — Phase 2 (the LLM brief): changelog and next steps

sources/x.md ──► API ──► FeedItem ──► SQLite (dedupe) ──► XMD digest ──► LLM/LMstudio/CLaude with skills ──► XMD Brief

## digest-summary Branch State

- Workflow: `xmd fetch` → `xmd digest` → `python scripts\run_system.py --model qwen` → `python scripts\assemble_brief.py` → `digests/<date>-brief.md` (header: date, `~N min read`, link to the full digest; model, compression and prompt hash in a trailing HTML comment).
- Engine: Qwen 3.5 9B in LM Studio (127.0.0.1:1234), thinking off, context ≥ 8192. About 3–4.5 min per 24h window (10–11 calls) plus a few seconds per section summary.
I like the brief that way and it reads in under 30 min at their pace.
- Qwen model really stands out for my laptop performances. Bonsai and Dolphin models were unsufficient. note: The engine layer stays generic (any OpenAI-compatible server).
- Tests: `python -m pytest` (301 pass on 2026-09-20).

## Mandatory Rules

- **Goal:** gather the news fast. A full digest is ~3 h of reading a day for ~250 sources. **Target: under 30 min a day** of brief (diagonal reading is fine), **never above 1 h**. One brief per since-run window, never one per day; the target scales with the days a window covers.
- **The LLM never sees images**, their URLs or the text of image tweets: they pass through verbatim and the export already excludes them. No captioning or vision experiments...
- **Privacy first, local GPU over paid tokens.** No cloud model by default. A Claude run would send the export, follow list included, to tier companies.
- **Tiers** (first match wins): priority → recap → media → plain retweet → regular. Priority: all in full. Image tweets: verbatim. Recap group and plain retweets: very short themes. The rest: summarized. Priority is also any non-plain-retweet post from a poster whose previous post (any kind, from the database) was more than 15 days earlier (`SILENCE_DAYS`, `Store.flag_after_silence`); a poster with no earlier post is never judged. A priority source's plain retweets stay in the Priority band; a recap group's image tweet collapses with the recap. Retweets with the user's own comment are quote-tweets, i.e. regular posts.
- **Importance ladder:** priority > the followed handles' own posts > plain retweets. Groups in `sources/x.md` carry a level (`| high` ×1.5, normal ×1, `| low` ×0.5 of the tier's share; a recap group is always the lowest and ignores a level).
- **Repeated stories = trending:** highlighted, never merged away. No engagement data, no ranking by likes, no hiding tweets: all tweets are equal.
- **Obsidian:** digests avoid `<details>`/folds (Live Preview cannot render them); an `&` in a section label breaks its contents link (written "and").
- **Handling:** `sources.yaml` holds API keys: never open it. `sources/x.md` is the user's follow list: don't print it. `digests/.runs/` holds tweet-derived text: count it with `scripts/run_stats.py`, don't read it (`--show-unsupported` is for the user's eyes only). `scripts/results_on_current_gpu.md` is the user's own notes: never edit it.

### Size and reading time

- Reference window: 682 items, 38,956 words, 141 images in 120 image tweets. Words by tier: priority 823, image tweets 5,233, regular 8,430, plain retweets 9,896, recap 14,574.
- The verbatim tiers (priority + image tweets) are 6,056 words, a **16% floor**, so "reduce to 10%" cannot mean 10% overall. Plan **A** = regular tier at 30% (≈26% of all words), **B** = regular at 10% (≈21%), **C** = B plus image-tweet text clipped at 60 words (never built: that text is already capped at 500 characters, so it would save ~1 min diagonal). The flag is `--compression`: A is `0.30`, the default; B is `0.10`.
- Share of a tier's full-text words the brief keeps (`xmd/chunk.py`): regular = `--compression`, plain retweets 4%, recap 6% (was 3% → 2% → 6%: "way too short"), times the level factor.
- Reading time, in the user's unit: **40 lines a minute + 4 s per image** (measured: 200 lines in 5 min, links clicked, so skimming; the first guess of 230/400 words a minute was ~2× too pessimistic). Constants in `xmd/brief.py`. Images dominate: the first brief's 150 images cost ~10 min, and the verbatim tiers were ~65% of its words.

## Design

`xmd digest` → **export** (`.export/<stamp>.txt` + `.map.json`: numbered lines `[n] @source RT@x|QT@x text`, priority and media excluded; the map gives each n its URL, tier, group and full word count) → **chunk** (`xmd/chunk.py`: one tier and level per chunk, never mixed; ≤3,750 estimated tokens of posts at 3.6 chars/token; ~10 chunks per 24h, ~27 per 3 days; header `Band:`, `Importance:`, `Length: at most N items and about W words`) → **engine** per chunk (`xmd/engine.py`; JSON reply `{items:[{headline, detail, ids:[n]}]}`, temperature 0.2, `prompts/brief.md` v5, min/max item counts also in the JSON schema, which LM Studio enforces) → `run_system.py` saves a **run folder** → **assemble** (`xmd/brief.py`, `scripts/assemble_brief.py`).

- **Verify** (`xmd/verify.py`): cited ids must be in the chunk (an item with none real is dropped); a number, @handle or $ticker in none of its cited posts gets ⚠; a failed or missing chunk falls back to quick-digest one-liners ("Not summarized"). `run_stats.py` also counts acronyms and capitalized names.
- **Sections:** `xmd/sections.py` + `prompts/section.md` make one model call per section (**In short**; its figures are checked against its input), cached in `<run>/sections.json` under `input_hash(system, message)`. **The model is not in the key**: use `--refresh` after switching models. `xmd/topics.py` counts **Louder than usual**: a name in ≥6 posts from ≥3 accounts with posts / (1 + usual daily posts) ≥ 2.5 (14 days of history).
- **Brief layout:** header, ★ Priority (in full), Trending, then one section per group (high, normal, low, recap last): images whole with the caption below, summary bullets linking the posts they cite, 🔥 on a repeated story, retweet themes.
- **Seams for another engine.** (1) `runner.Model.complete_json(system, user, schema, max_tokens, temperature) -> (dict, Completion)`, with `Completion(text, finish, prompt_tokens, completion_tokens, reasoning_chars, seconds)`. (2) The **run folder** `digests/.runs/<label>/`: `run.json` (`label`, `engine{model, base_url}`, `export{file, sha256}`, `prompt{sha256}`, `levels`, `compression_ratio`, `chunks[{chunk}]`) plus one `<chunk-id>.json` per chunk (`chunk`, `tier`, `level`, `ids`, `ok`, `reply{items}`). Whatever writes that folder gets verification and assembly for free. Assemble also needs the export named in `export.file` (a hash mismatch only warns), its full digest `<stem>.md` (missing: exit 1), and `xmd.db` and `sources.yaml` via `--config`.

## Changelog

**Phase 1 (by 2026-09-19, deterministic):** tiers (`xmd/tiers.py`), Trending (`trending.py`), quick digest (`render.build_quick`), the LLM export (`export.py`), recap groups and `digest:` knobs; `&` written "and" in section labels. Keyword-burst detection was tested on the recap group and does not work (diffuse opinion), so its picture must come from a model.

**Phase 2 (2026-09-20), in order:**

1. **Engines** (`scripts/smoke_engine.py`, LM Studio, 8 GB VRAM laptop GPU, 16 GB RAM): Qwen 3.5 9B passes (35 tok/s, ~6.5 GB VRAM; 8K context is the ceiling). Thinking is **on** by default in LM Studio (untick). Bonsai 27B loads in LM Studio (33 tok/s) but was dropped; Dolphin-Mistral-24B needs CPU offload (first 16-token reply took 99 s): too slow.
2. **Freeze:** `scripts/freeze_export.py` rebuilds a fixed window deterministically. `--fetched-by` matters: a later fetch back-filled 109 posts into the window. The hash is taken with LF line endings (Windows writes CRLF).
3. **First real run** (prompt v1): 6/10 chunks ok, replies cut at `max_tokens`: a reply costs ~2.5 tokens a word (~4 for headline-only items), the tokenizer gives 3.6 chars a token, and the retweet rule was ambiguous. Fixed with a reply budget counted in items (≤3,200 tokens) and prompt v2 (one-line JSON, `Length:` header, details ≤25 words).
4. **Second and third rounds:** Qwen 10/10 in ~3.4 min. Bonsai answered one item per chunk and cited ids not in its chunk (up to 31% of figures unsupported): dropped. Fixes: a reply under `min_items` (a quarter of the limit) is retried; prompt v3 shows a three-item example (weak models copy the count they see); item bounds in the schema stop collapse and overshoot on regular and recap chunks but **truncate retweet chunks by post order**, which step 5 replaced with themes. Bounds stay on (`--no-bound-items`).
5. **Importance ladder** (prompt v5): levels read from `sources/x.md`; plain retweets become themes (4%, ≤8 items a chunk) and repeated-story retweets are promoted to regular posts; chunk ids carry the level (`regular-high-01`). Qwen: 10/10 in 4.4 min, retweets 99% cited, 2.9% of figures unsupported.
6. **Assemble** (`brief.py`): first brief 11/11 chunks in 2.9 min, 99 summary items, 3 marked ⚠ (of 121 figures), 8 trending; 8,998 words + 150 images ≈ 49 min at 230 words a minute, ≈ 32 diagonal, under 30 at the user's pace. Review: "really good".
7. **Review changes:** per-section **In short** plus counted **Louder than usual** (a volume burst is countable; a quiet but big name only the summary can carry); captions below pictures with a blank line after each; recap 2% → 6% and up to 14 themes; reading time in the user's unit.
8. **Adopted and cleaned:** frozen files, eight Qwen runs and long-named briefs deleted; kept the user's digests, the reference brief (`2026-09-19-brief.md`), three Bonsai runs (evidence for other LLMs; the code stays generic) and `freeze_export.py`.
9. **Later the same day:** `--scenario A|B` became `--compression RATIO` (`0.30` or `30%`; a bare `30` is refused; old run manifests still read); README rewritten with every flag; tests and docs anonymized (placeholders ACME, Globex, alice, dave; groups business, news, chatter); this file compressed.

## Weak spots and open items

- Large regular chunks sometimes collapse to the schema minimum (`regular-03`: 8 items, 305 words against 886 wanted); ~45% of regular posts are cited, recap 21–36%. Item overshoot is not rejected. Tiny remainder chunks cost a whole call each (merging them saves ~1 min).
- Faithfulness: ~2–3% of Qwen's figures are unsupported (mostly small whole numbers, probably word counts); nothing catches a wrong emphasis.
- Not measured: RAM per engine, the quantization and context actually loaded, a 3-day window (~27 calls, est. 13–17 min on the 9B). Other special characters in group names are untested in Obsidian.

- Alternatives:
    - pseudonymizing handles before any Claude run. (scenario C (moot))
    - anonymous AI with Venice.ai using an API (may be needed in the future to run the digest at any time, any place)

## Next: optimization routes

Baseline to compare against: ~4 min a day, free, private, with the weak spots above.

1. **Claude Code as the engine** instead of the local model. Write an engine with `complete_json` that shells out to `claude -p` (installed here: 2.1.278). Its `--help` lists `--output-format json`, `--json-schema`, `--system-prompt`, `--model`, `--max-budget-usd`, `--no-session-persistence`, `--allowedTools`; `--bare` authenticates only by `ANTHROPIC_API_KEY` or `apiKeyHelper`. Pipe `Chunk.render()` in, pass `BRIEF_SCHEMA` or `bounded_schema(min, max)` as the schema, map the result to `Completion`; `run_chunks`, verify and assemble stay unchanged, and section summaries use the same seam. Gains: no 8K ceiling (raise `--max-input-tokens` for far fewer calls), fewer collapsed or truncated chunks. Costs: roughly 16K words in and 3K out per 24h window, and the export, follow list included, leaves the machine (see Rules): needs the user's OK; pseudonymize handles reversibly through `.map.json`.
2. **Skill route.** `prompts/brief.md` and `prompts/section.md` already read as skill instructions. A skill (or one subagent per chunk, in parallel) would run `xmd fetch` and `xmd digest`, get the chunk plan from `run_system.py --dry-run`, summarize each chunk itself, write the run folder (contract above), then run `assemble_brief.py --run <label> --no-summaries`. Fetch, chunk, verify and assemble stay the existing scripts; the skill replaces only the engine. Same privacy cost as route 1, but it runs inside a session on the user's plan, not unattended.
3. **Scheduled daily digest.** Today's chain is cron-safe: `xmd fetch` (or `--loop`), `xmd digest`, `run_system.py`, `assemble_brief.py`. Exit codes: `run_system` 0 ok, 1 a chunk failed or the server is down, 2 a bad `--model` or `--only`; `assemble_brief` 1 on a missing run or full digest; section summaries are skipped when the server is down (re-run assemble later). Schedule **one `xmd digest` per brief**: every digest, `--window 24h` included, moves the since-run cursor. Local variant: Windows Task Scheduler chaining the four commands, with LM Studio's server up (Just-In-Time loading on, thinking-off template, laptop on AC). Claude variant: the same chain with route 1's engine, headless and capped by `--max-budget-usd`. Claude Code cloud routines (`/schedule`) run off the machine and cannot reach `xmd.db`, `sources/`, the keys or LM Studio; they would need the export shipped out, which breaks the privacy rule.
4. **Cheaper wins in place:** merge remainder chunks; reject item overshoot; split large regular chunks, or cluster same-story posts before briefing; keep the quick digest as the deterministic fallback.

## Working notes

- Tests: the repo's `.venv` is a Windows venv; build a throwaway one (`python3 -m venv <dir> && <dir>/bin/pip install httpx PyYAML pytest`) and run `python -m pytest` from the repo root. The sandbox has no GPU: local-model measurements run on the user's Windows host.
- Real-data checks: run `xmd digest` on a **copy** of `xmd.db` with a dummy-key config (a digest advances `last_digest_at`), then delete the copies.
- Trying another local model: `scripts/smoke_engine.py --list`, then `--model NAME` (an empty reply plus `reasoning_content` means it is thinking), then `run_system.py --model NAME --dry-run`.
- Links: Claude Code headless https://code.claude.com/docs/en/headless · data usage https://code.claude.com/docs/en/data-usage · prior art: Meridian github.com/iliane5/meridian (cluster, then brief), sift github.com/shivaswaroop40/sift (triage first; small models struggle), AINews news.smol.ai (topic-grouped, cited bullets), daily_research_digest github.com/srijit316/daily_research_digest (deterministic fallback when the LLM step fails).
