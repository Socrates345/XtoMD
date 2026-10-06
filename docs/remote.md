# The brief without the laptop: VPS and phone

Added 2026-10-06. This page explains what the update is and how it works, and is the reference for its settings, commands and files. To install it, follow **[remote-setup.md](remote-setup.md)** step by step.

## What it is

Until now the brief existed only when the laptop made it, with a local model in LM Studio. Nothing at home stays on, so away from home there was no brief. This update adds two ways to get one:

| | Where the pipeline runs | When | For |
| --- | --- | --- | --- |
| **VPS** | A small rented server, always on | Every day on a timer, and whenever the phone asks | The routine |
| **Phone alone** | Termux on the Android phone | When you tap a button | A fallback: no server needed |

Neither has a GPU, so both summarize with a hosted model: Venice's `qwen3-5-9b`, the same Qwen 3.5 9B the prompts were tuned on. That is the one thing that leaves your machines that did not before (see [Privacy and security](#privacy-and-security)).

The laptop is unchanged. Its commands, its LM Studio setup and its brief are what they were; the new settings only exist on a machine that has a `.env`.

## How it works

```text
VPS, always on                                      Phone (Termux)
----------------------------------------------      ---------------------------------
xmd-brief.timer    every day at 17:07
      |
      v
deploy/brief.sh  <---  deploy/vps/ssh-entry.sh  <--- SSH ---  button: brief-vps-24h
      |                        |
      v                        +--- briefs (tar) --- SSH --->  Documents/xmd-briefs
scripts/make_brief.py                                                |
   |- fetch       -->  tweetapi.com                                  v   (by hand)
   |- digest                                                   Obsidian vault
   |- summarize   -->  Venice (qwen3-5-9b)
   '- assemble    -->  digests/DATE-HOUR-24h-brief.md
```

- **The timer** starts `deploy/brief.sh 24h` once a day. The brief is written on the VPS and waits there.
- **The phone** talks to the VPS over SSH with a key that may send exactly two requests: `run` (make a brief now) and `pull` (send me the recent briefs). Home-screen buttons send them.
- **Delivery** stops at a folder on the phone, `Documents/xmd-briefs`. Moving a brief into the Obsidian vault is still done by hand.
- **The phone alone** runs the left-hand column itself, in Termux: same `brief.sh`, same `make_brief.py`, no SSH.

### One run

`deploy/brief.sh` is the single entry point on both machines. It:

1. checks its arguments (`24h` or `since`, then optionally a number of minutes);
2. reads `.env` at the repo root;
3. takes a lock, so the timer and a run asked for from the phone never overlap;
4. runs `scripts/make_brief.py`, which checks the model answers **before** fetching, then fetches, digests, summarizes and assembles, exactly as on the laptop;
5. on the phone, copies the new brief to `XMD_BRIEF_DEST`;
6. if `XMD_KEEP_DAYS` is set, deletes old briefs, exports and runs.

| Exit code | Meaning |
| --- | --- |
| 0 | A brief was written, or there was nothing new in the window |
| 1 | Either a chunk failed (the brief is still written, those posts as one-liners), or the model server could not be reached (nothing was fetched, the cursor did not move) |
| 2 | Bad arguments, or a model name the server does not have |
| 75 | Another brief was being made on this machine |

### State

Each machine has its own `xmd.db`, so its own since-run cursor and its own 14 days of history. They are not synchronized, and nothing breaks when they drift:

- `24h` covers the last 24 hours by publication date, whatever any cursor says. The timer and all the buttons use it.
- `since` means "since **this machine's** last digest".
- A new machine starts with an empty database: **Louder than usual** and the silence-based priority fill in over the first two weeks, unless you copy `xmd.db` over once.

## Files

| File | Role |
| --- | --- |
| `deploy/brief.sh` | The entry point described above (VPS and phone) |
| `deploy/common.sh` | Shared by the scripts: where the repo, its Python and its digest folder are |
| `deploy/env.example` | Template for `.env` |
| `deploy/vps/xmd-brief.service`, `xmd-brief.timer` | The daily run, as systemd units |
| `deploy/vps/ssh-entry.sh` | The only thing the phone's SSH key can run on the VPS |
| `deploy/termux/setup.sh` | One-time phone setup; creates the buttons |
| `deploy/termux/vps.sh` | Phone side of `run` and `pull` |
| `deploy/termux/phone.sh` | A brief made on the phone alone |
| `.gitattributes` | Keeps `deploy/` in LF line endings, whatever platform commits it |
| `tests/deploy/` | Tests of the scripts above (Linux only; skipped on Windows) |

`.env` itself is git-ignored and never leaves the machine it is on.

## Settings (`.env`)

`brief.sh` reads `.env` as a shell would: no spaces around `=`, and the JSON value in single quotes.

| Variable | Used on | Meaning | When unset |
| --- | --- | --- | --- |
| `XMD_LLM_BASE_URL` | VPS, phone | The model server: `https://api.venice.ai/api/v1` | LM Studio on the same machine (the laptop's default) |
| `XMD_LLM_MODEL` | VPS, phone | The model id: `qwen3-5-9b` | `qwen/qwen3.5-9b`, LM Studio's name for it |
| `XMD_LLM_API_KEY` | VPS, phone | Your Venice key | No key is sent |
| `XMD_LLM_EXTRA_BODY` | VPS, phone | A JSON object added to every request, see below | Nothing is added |
| `XMD_TWEETAPI_KEY` or `XMD_TWITTERAPI_KEY` | VPS, phone | The X backend's key, if `sources.yaml` does not hold it | The key in `sources.yaml` |
| `XMD_BRIEF_DEST` | Phone | Folder where briefs end up | `Documents/xmd-briefs` on the phone; no copy on the VPS |
| `XMD_VPS_HOST` | Phone | The VPS, as named in `~/.ssh/config` | `xmd-vps` |
| `XMD_KEEP_DAYS` | VPS, phone | After each run, delete briefs, exports and runs older than this many days | Everything is kept |

Venice needs this extra body:

```bash
XMD_LLM_EXTRA_BODY='{"venice_parameters":{"disable_thinking":true,"include_venice_system_prompt":false}}'
```

- `disable_thinking`: Qwen 3.5 thinks by default and would spend its reply budget reasoning, so replies would come back empty. This is LM Studio's "untick Thinking", for Venice.
- `include_venice_system_prompt: false`: keeps Venice's own instructions out, so the model reads only `prompts/`.

The four `XMD_LLM_*` variables are also read by `make_brief.py`, `run_system.py` and `smoke_engine.py` when you run them by hand; `--base-url`, `--model`, `--api-key` and `--extra-body` override them. `assemble_brief.py` reuses the server and extra body the run recorded. To use them outside `brief.sh`, load the file first: `set -a; . ./.env; set +a`.

## Commands

**On the VPS or the phone, from the repo:**

```bash
bash deploy/brief.sh 24h        # everything published in the last 24 hours
bash deploy/brief.sh since      # only what is new since this machine's last digest
bash deploy/brief.sh 24h 10     # fitted to a 10-minute read (any number of minutes)
```

**On the phone, to the VPS** (what the buttons run):

```bash
bash deploy/termux/vps.sh run 24h        # make a brief on the VPS now, then download it
bash deploy/termux/vps.sh run since 15   # the same arguments as brief.sh
bash deploy/termux/vps.sh pull           # download the briefs of the last day
bash deploy/termux/vps.sh pull 7         # ... of the last 7 days
```

A download never replaces a brief already in the folder.

**The buttons** (Termux:Widget):

| Button | Runs |
| --- | --- |
| `brief-vps-latest` | `vps.sh pull`: the timer's daily brief |
| `brief-vps-24h` | `vps.sh run 24h` |
| `brief-vps-10min` | `vps.sh run 24h 10` |
| `brief-phone-24h` | `phone.sh 24h`: made on the phone alone |
| `brief-phone-10min` | `phone.sh 24h 10` |

**On the VPS, as root:**

```bash
systemctl list-timers xmd-brief.timer     # when the next daily brief is due
systemctl start xmd-brief.service         # make it now, as the timer would
journalctl -u xmd-brief.service -e        # what the last run printed
journalctl -u xmd-brief.service -f        # follow a run in progress
```

## Privacy and security

**Who sees what:**

| | Sees | Keeps |
| --- | --- | --- |
| tweetapi.com / twitterapi.io | Your follow list (as today) | Their own logs |
| Venice | The export: the posts of the accounts you follow, handles included, and the machine's IP | By its own account no prompt and no reply ("private" models: zero retention, by contract); metadata such as IP, time and token counts |
| The VPS host | Whatever is on the disk: `sources.yaml` (keys), `sources/x.md` (follow list), `xmd.db`, the briefs | Until you delete it |
| The phone | The briefs; when it runs alone, also the keys and the follow list, in Termux's private storage | Until you delete it |

Venice's retention is a promise that cannot be checked from outside. Handles are not masked: a post's text identifies its author anyway. As on the laptop, image tweets and priority tweets never reach the model for a summary; with `language:` set, the ones in a language you don't read are sent to it to be translated.

**What limits the damage:**

- **The phone's key is restricted.** Its line in `authorized_keys` forces `ssh-entry.sh`, which accepts `run 24h|since [MINUTES]` and `pull [DAYS]` and refuses anything else, including any request containing a character other than lowercase letters, digits and spaces. A lost phone can start a brief and read briefs. It cannot open a shell, read `sources.yaml` or copy files. Deleting the line revokes it.
- **The app has its own user**, `xmd`, with no password and no sudo.
- **The timer's unit is sandboxed:** it can write inside the repo only (`ProtectSystem=strict`), gets a private `/tmp` and cannot gain privileges.
- **SSH is by key only** and is the only open port.
- **`.env` is readable by its owner only** (`chmod 600`) and is git-ignored.
- **`XMD_KEEP_DAYS`** keeps old briefs, exports and runs off the rented disk. The database is not pruned.

Not covered: the VPS host can read its own disk, and agents you later run on the same server can read this app's files unless they run as another user.

## What changed in the pipeline

All of it is inert on the laptop, where none of the `XMD_LLM_*` variables is set.

- **`--extra-body JSON`** (`make_brief.py`, `run_system.py`, `smoke_engine.py`): fields added to every request as they are. The run records them in `run.json`, so `assemble_brief.py` sends the same.
- **Environment defaults:** `--base-url`, `--model` and `--extra-body` default to `XMD_LLM_BASE_URL`, `XMD_LLM_MODEL` and `XMD_LLM_EXTRA_BODY`, as `--api-key` already did with `XMD_LLM_API_KEY`.
- **Stricter schemas:** every JSON schema the model is given now says `additionalProperties: false`, which a hosted server's strict mode requires. LM Studio gets the same schemas.
- **A hosted server is not waited for.** When LM Studio is down the script still says what to open and waits; a server that is not on this machine either answers or the run stops at once, before fetching.
- **`smoke_engine.py`** now sends the item bounds a real chunk sends (`minItems`, `maxItems`) and fails if the server ignores them.

## Running it day to day

- **Change the hour:** edit `OnCalendar=` in `/etc/systemd/system/xmd-brief.timer`, then `systemctl daemon-reload`.
- **Change what the timer makes:** edit `ExecStart=` in `xmd-brief.service` (for example `brief.sh 24h 15`), then `systemctl daemon-reload`.
- **Update the code:** `git pull` on each machine; `.venv/bin/pip install -e .` only when `pyproject.toml` changed; on the phone `bash deploy/termux/setup.sh` again when the buttons changed. The unit files in `/etc/systemd/system/` are copies: copy them again when they change in the repo.
- **Change the follow list:** edit `sources/x.md` on the machine that makes the briefs. The laptop's copy and the VPS's are separate files.
- **Revoke the phone:** delete its line from `/home/xmd/.ssh/authorized_keys`.
- **Move to another VPS:** set up the new one, then copy `sources.yaml`, `sources/`, `.env` and, if you want its history, `xmd.db`.
- **Cost:** at Venice's listed price on 2026-10-06 ($0.10 per million tokens in, $0.15 out), a brief costs about one cent: the summaries of 14 runs averaged 56K tokens in and 11K out (27K to 104K in), and the section summaries and translations, which are not counted, add an estimated 25K in and 6K out. Each machine that makes briefs fetches for itself, so the VPS's daily run adds its own calls to your X backend's quota.

## Limits

- **Not yet run for real.** The scripts were tested with stand-ins (`tests/deploy/`) and the pipeline against a local stand-in server. Venice itself, the systemd units on a real server and everything in Termux have not been run. Expect a small fix or two on first use.
- **The item bounds on Venice are unconfirmed.** If Venice refuses `minItems`/`maxItems`, the smoke test says so. `run_system.py --no-bound-items` runs without them, but `make_brief.py` and `brief.sh` do not pass that flag on yet.
- **A scheduled run on the phone is not dependable.** Android decides when background work runs. That is why the VPS is the routine.
- **The model on the phone itself is not set up.** When a phone can hold Qwen 3.5 9B in memory, a local server (llama.cpp's `llama-server`) can replace Venice: point `XMD_LLM_BASE_URL` at it and remove `XMD_LLM_EXTRA_BODY`. On a Pixel it runs on the CPU, so expect about an hour per brief (an estimate).
- **Delivery ends at a folder.** Nothing writes into the Obsidian vault for you.

## Troubleshooting

| You see | Cause and fix |
| --- | --- |
| `cannot reach the server, nothing was fetched` | The machine cannot reach Venice (network, or a typo in `XMD_LLM_BASE_URL`). A hosted server is not waited for: run it again. |
| `the model did not answer ... spent its tokens thinking`, or empty replies | `XMD_LLM_EXTRA_BODY` is missing or lost its single quotes. |
| `HTTP 401` | The Venice key is wrong: check `XMD_LLM_API_KEY` in `.env` (no spaces, no quotes, the whole key). |
| `HTTP 402 ... Insufficient USD or Diem balance` | The account has no credit left: add some in Venice's API settings. |
| `HTTP 402 ... API key DIEM spend limit exceeded` (or `USD spend limit`) | The key has its own daily cap, and it is 0 or used up, whatever the account's balance. In Venice's API settings, clear the key's Epoch Consumption Limits or raise them (0.25 covers many briefs), or make a new key without them and put it in `.env`. A cap that was simply used up comes back with the next 24-hour epoch. |
| `LM Studio's server is not answering at http://127.0.0.1:1234/v1` on the VPS or phone | `.env` is missing or has no `XMD_LLM_BASE_URL`, so the scripts fell back to the laptop's default. |
| Smoke test: `FAIL` on line 2 or 3 with `HTTP 400`, or `does not enforce maxItems` | Venice does not take the item bounds: see [Limits](#limits). |
| `a brief is already being made on this machine` (exit 75) | The timer's run and yours overlapped. Wait a few minutes, then `brief-vps-latest`. |
| `allowed: run 24h\|since [MINUTES] \| pull [DAYS]` | The VPS refused a request the phone's key may not make. That is the limit working. |
| `Permission denied (publickey)` from the phone | The key's line in `authorized_keys` is broken over two lines, or `~/.ssh/config` names another key. |
| The timer's run fails with `Read-only file system` | `storage:` or `digest_dir:` in `sources.yaml` points outside the repo, the only place the unit may write. |
| `no .../.venv/bin/python` | The virtual environment was not made on this machine: `python3 -m venv .venv && .venv/bin/pip install -e .` |
| A button does nothing | Settings > Apps > Termux > **Display over other apps**: allow. |
| `[Process completed (signal 9)]` in Termux | Android killed the run: battery **Unrestricted** for Termux, and **Disable child process restrictions** in Developer options. |
| `can't resolve timezone ...: no timezone database found` on the phone | `.venv/bin/pip install tzdata` (`setup.sh` does it). |
| `pip` fails building PyYAML on the phone | `pkg install -y clang libyaml`, then `bash deploy/termux/setup.sh` again. |
| **Louder than usual** is missing | The machine's database is new: it fills in over two weeks, or copy `xmd.db` once. |
