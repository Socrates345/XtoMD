# Read later

Saved X posts you won't get round to reading: put their links in a file, run one command, read one `summary.md`.

Each link is looked up on tweetapi.com with the brief's own key, recognised as one of four kinds, and summarized on its own by the hosted model the `XMD_LLM_*` settings name (Venice, as on the VPS). The brief's code is only imported here, never changed.

| Kind | What it is | What you get |
| --- | --- | --- |
| 📄 X Article | A long-form page with a title, shared by a post | The whole Article, summarized |
| 🧵 Thread | The author's chain of replies to themselves (1/, 2/, ...) | Every tweet of the chain, summarized as one piece. A link to any tweet of it works |
| 📝 Long post | One tweet over 280 characters | Summarized |
| 💬 Post | One ordinary tweet | Summarized, or shown as posted when it is short |

A summary is a headline, a TL;DR of one or two sentences, then bolded key points, more of them for a longer original. The numbers that set its length, and what counts as short enough to show as posted, are the constants at the top of `summarize.py` (`SHORT_WORDS`, `SUMMARY_SHARE` and the lines below them).

## Setup

Once, on the laptop:

1. **The X key.** Nothing to do if `sources.yaml` holds `x.tweetapi_api_key`, as it does for the brief. Otherwise set `XMD_TWEETAPI_KEY` in `.env`.
2. **The model.** Make a file named `.env` at the repo root (git ignores it) and copy into it the model lines of `deploy/env.example`: `XMD_LLM_BASE_URL`, `XMD_LLM_MODEL`, `XMD_LLM_API_KEY` and `XMD_LLM_EXTRA_BODY`. Put your Venice key after `XMD_LLM_API_KEY=`. It is the same format as the VPS's `.env`. On the laptop nothing else reads this file, so the brief keeps using LM Studio. A variable already set in the shell wins over the file.
3. **Your list.** Nothing to do: the first run makes `readlater\links.md` and asks you to paste your links in it.

## Use

From the repo root, with the virtual environment active:

```powershell
python readlater\summarize.py
```

It prints one line per link as it looks it up, one per summary as the model writes it, then the path of the file it wrote: `readlater\summaries\<date>-<time>-summary.md`, in the clock of `timezone:` in `sources.yaml`. A run never replaces an earlier summary.

- `python readlater\summarize.py other.md` reads another file.
- `python readlater\summarize.py --dry-run` looks the links up and says what each one is (Article, thread of N tweets, long post, post) and how many tweetapi.com calls that took. The model is not called and nothing is written.

Every run summarizes every link in the file, so remove the ones you have read, or start a new file.

**The links file** needs nothing but the links, pasted one per line: you never say what kind of post a link is. Each one is looked up and its type found from what X holds: a post that carries an Article is an X Article, one whose author replied to it (or that answers its own author) is a thread when the chain has two tweets or more, and a single tweet is a long post over 280 characters, a post otherwise. Links copied from the app (`?s=20` and the like), twitter.com and mobile links, and markdown links all work. Anything else in the file, a note or a heading, is ignored. An `x.com/i/article/...` link names the Article but not its post, which is what can be looked up: use the link of the post that shares the Article.

**In `summary.md`**, the entries follow the file's order. Under each title: the kind, the author (a link to the post, or to a thread's first tweet), the date, the original's length, and for a thread which of its tweets you had saved. At the end, the links that were not summarized (with the reason) and those not found (deleted, private, or not a post).

## What it costs

- **tweetapi.com:** one call per 200 links to look them up. Then, for each link that could be a thread (someone replied to it), a thread search, which takes two calls: the results, then an empty last page. An X Article costs nothing more when its text comes with the lookup, as it did when tested, and one call when it doesn't. `--dry-run` prints the total.
- **Venice:** one call per summary. A 7,000-word Article is about 10,000 tokens in. Prices are in `docs/remote.md` (Cost).

## What is sent where

- **tweetapi.com:** the post ids, and for a thread search the author's handle.
- **Venice:** the text of the saved posts (and of the posts they quote), with the author's name. On the laptop this is the one thing that uses a hosted model; the brief stays local. What Venice keeps is in `docs/remote.md` (Privacy and security).

## Limits

- Links inside a post are listed under its summary, not opened: an article on another website is not summarized.
- The model does not see pictures or videos. Their number is shown.
- A thread is the author's own chain. Other people's replies, and the author's answers to them, are left out. A post that answers someone else is summarized without the post it answers.
- Threads are found with X search (`conversation_id:`). A thread longer than `THREAD_PAGES` pages of results is cut there.
- Checked live on 2026-11-05 with a long post (Karpathy), an X Article (Ray Dalio) and an 18-tweet thread (ZachXBT), the links in `links.example.md`.

## Files

| File | What |
| --- | --- |
| `summarize.py` | The command |
| `prompt.md` | What the model is told. Edit it to change the summaries' style |
| `links.example.md` | An example list |
| `tests/test_readlater.py` | Offline tests (`python -m pytest readlater/tests`; the full suite runs them too) |
| `links.md`, `summaries/` | Your list and the summaries: git ignores both |
