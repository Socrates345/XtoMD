# Read later

Saved X posts and web pages you won't get round to reading: put their links in a file, run one command, read one `summary.md`.

Each x.com link is looked up on tweetapi.com with the brief's own key and recognised as one of four kinds. Any other link is a web page, read with [Defuddle](https://github.com/kepano/defuddle), the extractor inside Obsidian Web Clipper: it keeps the page's main text and leaves out the menus, sidebars, ads and comments. Each one is summarized on its own by the hosted model the `XMD_LLM_*` settings name (Venice, as on the VPS). Before the model is called, the run says about what it will cost and what your key can still spend, and waits for Enter. The brief's code is only imported here, never changed.

| Kind | What it is | What you get |
| --- | --- | --- |
| 📄 X Article | A long-form page with a title, shared by a post | The whole Article, summarized |
| 🧵 Thread | The author's chain of replies to themselves (1/, 2/, ...) | Every tweet of the chain, summarized as one piece. A link to any tweet of it works |
| 📝 Long post | One tweet over 280 characters | Summarized |
| 💬 Post | One ordinary tweet | Summarized, or shown as posted when it is short |
| 🌐 Web page | Any other link: an article, an essay, a blog post | Its main text, summarized |

A summary is a headline, a TL;DR of one or two sentences, then bolded key points, more of them for a longer original. The numbers that set its length, and what counts as short enough to show as posted, are the constants at the top of `summarize.py` (`SHORT_WORDS`, `SUMMARY_SHARE` and the lines below them).

## Setup

Once, on the laptop:

1. **The X key**, for x.com links. Nothing to do if `sources.yaml` holds `x.tweetapi_api_key`, as it does for the brief. Otherwise set `XMD_TWEETAPI_KEY` in `.env`. A file with only web links needs no key.
2. **The model.** Make a file named `.env` at the repo root (git ignores it) and copy into it the model lines of `deploy/env.example`: `XMD_LLM_BASE_URL`, `XMD_LLM_MODEL`, `XMD_LLM_API_KEY` and `XMD_LLM_EXTRA_BODY`. Put your Venice key after `XMD_LLM_API_KEY=`. It is the same format as the VPS's `.env`. On the laptop nothing else reads this file, so the brief keeps using LM Studio. A variable already set in the shell wins over the file.
3. **Web pages: Node.js and Defuddle**, for links that are not x.com's. Install Node.js, its LTS from [nodejs.org](https://nodejs.org) (or `winget install OpenJS.NodeJS.LTS`), open a new terminal, then from the repo root:

   ```powershell
   cd readlater; npm install; cd ..
   ```

   This puts Defuddle, at the version `package.json` pins, in `readlater\node_modules`, which git ignores. A file with web links stops before any call when either is missing, and says which. A file with only x.com links needs neither. To move to a newer Defuddle, change its version in `package.json` and run `npm install` again.
4. **Your list.** Nothing to do: the first run makes `readlater\links.md` and asks you to paste your links in it.

## Use

From the repo root, with the virtual environment active:

```powershell
python readlater\summarize.py
```

It prints one line per step: the x.com lookup, the web pages read, then the run's estimate and your balance, and asks:

```
about 63,431 tokens in, 3,500 out ≈ 1.30¢ · balance $6.03
Enter to summarize, s to stop:
```

Enter goes on. `s`, or any other answer, stops: nothing is sent to the model and nothing is written. Then it prints one line per summary as the model writes it, what Venice actually counted (`used 61,044 tokens in, 2,552 out ≈ 1.17¢`), and the path of the file it wrote: `readlater\summaries\<date>-<time>-summary.md`, in the clock of `timezone:` in `sources.yaml`. A run never replaces an earlier summary.

- `python readlater\summarize.py other.md` reads another file.
- `python readlater\summarize.py --dry-run` looks the links up, reads the web pages and says what each one is (Article, thread of N tweets, long post, post, web page) and how long, how many tweetapi.com calls that took, and (with the model settings in `.env`) the estimate and balance line. It asks nothing, the model is not called and nothing is written.

Every run summarizes every link in the file, so remove the ones you have read, or start a new file.

**The links file** needs nothing but the links, pasted one per line: you never say what kind of link it is.
- An x.com link is looked up and its type found from what X holds. A post that carries an Article is an X Article. One whose author replied to it (or that answers its own author) is a thread when the chain has two tweets or more. A single tweet is a long post over 280 characters, a post otherwise. Links copied from the app (`?s=20` and the like), twitter.com and mobile links, and markdown links all work. An `x.com/i/article/...` link names the Article but not its post, which is what can be looked up: use the link of the post that shares the Article.
- Any other link that starts with `https://` (or `http://`) is a web page: bare, `<link>` or `[words](link)`. Its `#part` is ignored. Links to x.com that are not posts (a profile) and `t.co` short links are left out: paste the link a `t.co` one leads to.

Anything else in the file, a note or a heading, is ignored.

**In `summary.md`**, the entries follow the file's order, x.com posts and web pages mixed as you pasted them. Under each title:
- a post: the kind, the author (a link to the post, or to a thread's first tweet), the date, the original's length, and for a thread which of its tweets you had saved
- a web page: its site (a link to the page), its author and date when the page gives them, its length and its number of pictures

At the end come the links that were not summarized (with the reason) and those not found (deleted, private, or not a post). A run with only x.com links is titled "Tweet summaries", as before; one with web pages, "Read later".

## What it costs

- **tweetapi.com**, for x.com links: one call per 200 links to look them up. Then, for each link that could be a thread (someone replied to it), a thread search, which takes two calls: the results, then an empty last page. An X Article costs nothing more when its text comes with the lookup, as it did when tested, and one call when it doesn't. `--dry-run` prints the total.
- **Web pages:** reading them is free. Defuddle fetches each page from the laptop.
- **Venice:** one call per summary. The model's prices and window are read from Venice's `/models` and your balance from `/api_keys/rate_limits` at each run. Both are free, and an inference-only key may read them (`/billing/balance` would want an admin key). If either can't be read, the line says "unknown" and the run still asks.

The estimate counts 1.4 tokens a word (`TOKENS_PER_WORD`) and 700 tokens a reply (`REPLY_TOKENS`). These were measured with Qwen 3.5's tokenizer on the test pages: an essay is 1.27 to 1.37 tokens a word, a GitHub README 1.6, a Wikipedia article 2.0. For the five test pages (42,911 words) the run said 1.30¢ and Venice counted 61,044 tokens in and 2,552 out: 1.17¢. At Venice's prices on 2026-11-10 ($0.15 per million tokens in, $1.00 out), one page costs:

| Page | Tokens in | Cost |
| --- | --- | --- |
| 10,000 words | about 14,500 | 0.28¢ |
| 50,000 words | about 70,500 | 1.12¢ |
| 100,000 words | about 140,500 | 2.17¢ |
| 126,000 words, the most read of one page | about 177,000 | 2.7¢ (3.9¢ for a page as dense as Wikipedia) |

**A page too long for the model's window** is cut to its first words, and `summary.md` says "the first N summarized". The window is 256,000 tokens for Qwen 3.5 on Venice, read at each run. The cut is reckoned at 2 tokens a word (`WINDOW_TOKENS_PER_WORD`, the densest page measured) so Venice never turns a page down for its length: about 126,000 words. Nothing else is ever cut: the summary keeps to its length (at most 400 words in 10 points) whatever the original's.

## What is sent where

- **tweetapi.com:** the post ids, and for a thread search the author's handle.
- **Each web page's site:** a request from the laptop, with Defuddle's user agent (`Mozilla/5.0 (compatible; Defuddle/1.0; +https://defuddle.md)`).
- **Venice:** the text of the saved posts (and of the posts they quote) with the author's name, and the main text of the saved pages with their title, site and author. Also, at each run, the two free requests for the model's prices and the key's balance. On the laptop this is the one thing that uses a hosted model; the brief stays local. What Venice keeps is in `docs/remote.md` (Privacy and security).

## Limits

- Links inside a post are listed under its summary, not opened: paste the link itself to have the page summarized.
- The model does not see pictures or videos. Their number is shown.
- A thread is the author's own chain. Other people's replies, and the author's answers to them, are left out. A post that answers someone else is summarized without the post it answers.
- Threads are found with X search (`conversation_id:`). A thread longer than `THREAD_PAGES` pages of results is cut there.
- A web page that shows its text only after a click gives only what is there as it loads. On ccunpacked.dev, its eleven steps and its lists of tools are built by JavaScript and shown one click at a time, so about 200 words of headings are summarized. Obsidian Web Clipper sees no more.
- Behind a paywall or a login, only the free part is read. A page with less than 100 words of text (`MIN_PAGE_WORDS` in `web.py`) is not summarized: it is listed with the reason.
- Some sites turn scripts away (Medium, behind Cloudflare: `403 Forbidden`). PDFs are not read. Both are listed under "Not summarized" with the reason.
- Checked live on 2026-11-05 with a long post (Karpathy), an X Article (Ray Dalio) and an 18-tweet thread (ZachXBT), and on 2026-11-10 with five web pages (Paul Graham, David Perell, two Substacks, ccunpacked.dev): the links in `links.example.md`.

## Files

| File | What |
| --- | --- |
| `summarize.py` | The command |
| `web.py` | The web pages: Defuddle run with node, and the page's text cleaned for the model |
| `prompt.md` | What the model is told for an X post. Edit it to change the summaries' style |
| `prompt_web.md` | What the model is told for a web page |
| `package.json`, `package-lock.json` | Defuddle's pinned version, for `npm install` |
| `links.example.md` | An example list |
| `tests/test_readlater.py` | Offline tests (`python -m pytest readlater/tests`; the full suite runs them too) |
| `links.md`, `summaries/`, `node_modules/` | Your list, the summaries and Defuddle: git ignores them |
