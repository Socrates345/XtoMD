# X to MD

**Pull tweets from X accounts into markdown.**

X to MD is a small tool that fetches X posts given a plain list of X handles.
Tweets and retweets, full text, images as remote links, every 24h or last handle.



## How it works

```text
sources/x.md ──► API ──► FeedItem ──► SQLite (dedupe)
                                          │
                                     xmd digest
                                          │
            ┌─────────────────────────────┼─────────────────────────────┐
            ▼                             ▼                             ▼
   digests/<stamp>.md         digests/<stamp>-quick.md      digests/.export/<stamp>.txt
   full archive               scan layer                    numbered text for an LLM
```

- **`xmd fetch`** — pulls new tweets for every handle in `sources/x.md` into
  a local SQLite store (`xmd.db`), deduped by tweet URL. Nothing is ever
  purged — it's a personal archive, not a delivery queue.
- **`xmd digest`** — renders everything in a time window to three files in
  `digests/`:
  - `<stamp>.md`, the **full archive**: full tweet text, images embedded as
    `![](remote-url)`. A stats line and, once there's more than one section,
    a linked table of contents sit at the top so a long file can be jumped
    into instead of scrolled through. Retweets sort after normal tweets
    within their section and are collapsed into a single "🔁 Retweets — N
    items" toggle per section — one click reveals all of them, while normal
    tweets are always fully visible. A retweet's text is printed once (X
    hands back the original's text as the retweet's own).
  - `<stamp>-quick.md`, the **scan layer**: every tweet, but at a glance —
    one line per regular post, plain retweets shorter still, while priority
    sources' tweets and every tweet with images are kept whole. Plain
    headings and lists only, no toggles.
  - `.export/<stamp>.txt` + `.map.json`: the posts that would need
    summarizing, as compact numbered lines, plus the number → URL map. It
    calls no model; it is the ready-made input for one (see below).

  Both digests open with a **Trending** block: stories that two or more of
  your sources posted or amplified. Repetition is a signal, so those are
  highlighted rather than merged away. `--window since-run`
  (default) covers everything stored since the last `xmd digest` call —
  filtered by when it was *fetched*, not when it was posted, so a digest
  run right after a fetch never comes up empty just because the tweets
  themselves are older. `--window 24h` is a fixed rolling window instead.

X is fetched via one of two paid, hosted APIs — no X account, no login, no
self-hosting: [twitterapi.io](https://twitterapi.io) (pay-per-tweet) or
[tweetapi.com](https://tweetapi.com) (flat monthly quota), picked with
`x.backend` in `sources.yaml`.

## Commands

```bash
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
cp sources.example.yaml sources.yaml   # settings: backend, API key
cp -r sources.example sources          # who you follow — edit sources/x.md
xmd fetch                              # pull new tweets into the local store
xmd digest --window 24h                # write digests/YYYY-MM-DD-HHMM.md
```

### Reference

Every command takes `--config PATH` (default `sources.yaml`), placed before
the subcommand: `xmd --config other.yaml fetch`. `xmd --version` prints the
version and exits.

| Command | Flags | What it does |
| --- | --- | --- |
| `xmd fetch` | `--loop [SECONDS]` | Fetch every handle in `sources/x.md` into `xmd.db`, deduped by tweet URL. Without `--loop`, runs once. With `--loop`, repeats forever, pausing `SECONDS` between passes (default `900` = 15 min) — no digest is built, this only fills the store; `Ctrl+C` stops it. |
| `xmd digest` | `--window since-run\|24h` | Render stored items to `digests/YYYY-MM-DD-HHMM.md` (+ `-quick.md` and `.export/`), or print "nothing new" and write nothing if the window is empty. `since-run` (default) — everything *fetched* since the last `xmd digest` call. `24h` — a fixed rolling 24-hour window by *published* date, ignoring the since-run cursor. |
| `xmd sources` | — | List configured handles, one per line, with their group if any. Takes no other flags. |

```bash
xmd fetch --loop            # keep fetching every 15 min, forever
xmd fetch --loop 1800       # same, every 30 min
xmd digest --window 24h     # everything posted in the last 24h, regardless of fetch history
xmd digest --window since-run
xmd --config work.yaml fetch --loop 300   # a second, differently-configured instance
```

## Config

- `sources/x.md`
one handle per line, optional `| Display Name`, `#` comments allowed.
`## group` headers split handles into sections in the output file.
A third field, `| priority`, sorts that source's tweets first within its
section, ahead of non-priority sources (e.g. `karpathy | | priority`) and
keeps them whole in the quick digest.
A group header can carry `| recap` (e.g. `## spammer but interesting | recap`)
for a group that is only worth its global picture: the quick digest collapses
it to a count plus the stories repeated inside it, and the full digest still
has every tweet.
- `sources.yaml`
a `digest:` block tunes the quick digest and the Trending block —
`snippet_chars`, `retweet_chars`, `media_chars` (one-liner lengths) and
`trending_similarity` (0–1, how alike two posts must be to count as one
story; lower finds looser repeats). See `sources.example.yaml`.

### The LLM export

`xmd digest` writes `digests/.export/<stamp>.txt` for a model to summarize:
numbered lines (`[n] @source text`, URLs stripped, retweet echoes removed)
grouped by section, with `repeated: [3] [17]` hints. Priority and image tweets
are left out on purpose — they stay verbatim in the digest, so a model never
sees, or garbles, them. A summary can cite `[n]`, and `<stamp>.map.json` turns
each number back into its tweet URL, so links come from data, not from a
model's memory. Nothing in this repo calls a model.


### Since-last-run gap handling

If you skip a fetch for a while, `xmd fetch` detects the gap since its last
successful run and temporarily widens that fetch's lookback to cover it
(capped at 7 days, so a long absence can't blow up API cost) — so the tweets
from your time away actually land in the store, and `xmd digest --window
since-run` (which tracks its own cursor, separate from fetch's) picks them
up on the next run.

## Other platforms — not built, but the ground is prepared

X is the only adapter today, but the shape is generic: an adapter is a
module with `fetch(source, max_items, **kwargs) -> list[FeedItem]`
(`xmd/adapters/twitterapi.py`), dispatched in `xmd/fetcher.py` on
`source.type`. Adding RSS/YouTube/Substack later needs no restructuring — a
generic feedparser-based RSS adapter already existed in this project's
history (`main` branch, `rss40/adapters/rss.py`) and can be resurrected when
that's actually wanted.


## Inspiration

This is a lightweight rewrite of [RSS4.0](https://github.com/Socrates404/RSS4.0)
(see `main`), which grew a local-LLM recap feature and a Telegram bot on top
of the same tweet-fetching core. The LLM part was unreliable and dragged in
~150MB of dependencies plus a 2GB model file; the bot added another hard
dependency and a long-running process. The part that actually worked well —
pulling tweets via a paid, hosted API and turning them into markdown — is
what's left here, with nothing else attached.

## License

See [LICENSE](LICENSE).
