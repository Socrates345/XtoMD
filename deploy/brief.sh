#!/usr/bin/env bash
# One brief, start to finish, on a machine that is not the laptop: the VPS (its timer, or the phone over SSH) or
# the phone itself (Termux). Run it as `bash deploy/brief.sh ...`: neither the executable bit nor the line above
# is relied on, a Windows checkout keeps neither.
#
#   bash deploy/brief.sh 24h        everything published in the last 24 hours
#   bash deploy/brief.sh since      only what is new since this machine's last digest
#   bash deploy/brief.sh 24h 10     the same, fitted to a 10-minute read
#
# The settings (which model server, the keys, where to copy the brief) come from .env at the repo root, never
# from the command line: deploy/env.example lists them, docs/remote.md explains them.
set -euo pipefail

usage() {
    echo "usage: brief.sh 24h|since [MINUTES]" >&2
    exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
case "$1" in
    24h) window=--24h ;;
    since) window=--since-last-run ;;
    *) usage ;;
esac
minutes="${2:-}"
[[ -z "$minutes" || "$minutes" =~ ^[0-9]{1,3}$ ]] || usage

. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$repo"
if [[ ! -x "$python" ]]; then
    echo "no $python: make the virtual environment first (docs/remote-setup.md)" >&2
    exit 1
fi
if [[ -f .env ]]; then
    set -a
    . ./.env
    set +a
fi
export PYTHONUNBUFFERED=1  # progress line by line over SSH and in the journal, not in one block at the end

digests="$(digests_dir)"
mkdir -p "$digests"

# one brief at a time: the timer and a run asked for from the phone must not share a fetch, a digest and its cursor
exec 9> "$digests/.brief.lock"
if ! flock -n 9; then
    echo "a brief is already being made on this machine: try again in a few minutes" >&2
    exit 75
fi

shopt -s nullglob
declare -A before=()  # the briefs already there: a brief never replaces another, so what is new is this run's
for brief in "$digests"/*-brief*.md; do
    before["$brief"]=1
done

args=("$window" --wait 0)  # nobody is there to start a server that is down: fail now, not in 15 minutes
if [[ -n "$minutes" ]]; then
    args+=(--time "$minutes")
fi
code=0
"$python" scripts/make_brief.py "${args[@]}" || code=$?

# the phone on its own: put the brief where the Files app, or the vault, can see it
if [[ -n "${XMD_BRIEF_DEST:-}" ]]; then
    mkdir -p "$XMD_BRIEF_DEST"
    for brief in "$digests"/*-brief*.md; do
        if [[ -z "${before[$brief]:-}" ]]; then
            cp "$brief" "$XMD_BRIEF_DEST/"
            echo "copied $(basename "$brief") to $XMD_BRIEF_DEST"
        fi
    done
fi

# a rented disk need not keep what has been read: the briefs and digests, the exports and the runs they came from
# (the database stays: "louder than usual" is measured against its last 14 days)
if [[ "${XMD_KEEP_DAYS:-}" =~ ^[0-9]+$ ]]; then
    find "$digests" -maxdepth 1 -type f -name '*.md' -mtime +"$XMD_KEEP_DAYS" -delete
    if [[ -d "$digests/.export" ]]; then
        find "$digests/.export" -maxdepth 1 -type f -mtime +"$XMD_KEEP_DAYS" -delete
    fi
    if [[ -d "$digests/.runs" ]]; then
        find "$digests/.runs" -mindepth 1 -maxdepth 1 -type d -mtime +"$XMD_KEEP_DAYS" -exec rm -rf {} +
    fi
fi

exit "$code"
