#!/data/data/com.termux/files/usr/bin/bash
# On the phone (Termux): ask the VPS for a brief, and bring the briefs it has made back to the phone.
# The home-screen shortcuts setup.sh installs call this; so can you:
#
#   bash deploy/termux/vps.sh run 24h       make one now, then download it
#   bash deploy/termux/vps.sh run 24h 30    ... cut to 30% of the whole day
#   bash deploy/termux/vps.sh run since     only what is new since the last brief the VPS made
#   bash deploy/termux/vps.sh pull          download the briefs of the last day (the timer's daily one)
#   bash deploy/termux/vps.sh pull 7        ... of the last 7 days
#
# The VPS answers these requests and no other (deploy/vps/ssh-entry.sh). .env at the repo root says which host
# (XMD_VPS_HOST, a Host of ~/.ssh/config) and where the briefs go (XMD_BRIEF_DEST). docs/remote.md.
#
# A brief is downloaded once. The names of those already downloaded are kept in ~/.xmd-pulled, outside the folder
# they go to: a brief moved on from there (verify.sh moves it to 2_verified) must not come back with the next pull.
# To download one again, delete its line from ~/.xmd-pulled.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -f "$repo/.env" ]]; then
    set -a
    . "$repo/.env"
    set +a
fi
host="${XMD_VPS_HOST:-xmd-vps}"
dest="${XMD_BRIEF_DEST:-$HOME/storage/shared/Documents/xmd-briefs}"

code=0
case "${1:-}" in
    run)
        # ServerAlive*: a quiet stretch of the run must not let a mobile network forget the connection, and one
        # that did die is noticed within two minutes, not never
        ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=4 "$host" "$@" || code=$?
        if [[ "$code" -eq 255 ]]; then  # ssh's own failure, not the brief's
            echo "the connection to the VPS failed or was lost. A brief that had started is finished there all" >&2
            echo "the same: tap download-latest-daily-brief in a few minutes (bash deploy/termux/vps.sh pull)" >&2
            exit 255
        fi
        # a brief with failed chunks is still written (exit 1): download whatever there is either way
        pull=(pull 1)
        ;;
    pull)
        pull=("$@")
        ;;
    *)
        echo "usage: vps.sh run 24h|since [PERCENT] | pull [DAYS]" >&2
        exit 2
        ;;
esac

mkdir -p "$dest"
record="$HOME/.xmd-pulled"
touch "$record"
# the archive is unpacked in Termux's own storage first, never straight into the folder a reader or a sync sees
staging="$(mktemp -d "$HOME/.xmd-pull.XXXXXX")"
trap 'rm -rf "$staging"' EXIT
echo "downloading to $dest:"
ssh "$host" "${pull[@]}" | tar -xf - -C "$staging"
shopt -s nullglob
for brief in "$staging"/*; do
    name="${brief##*/}"
    if [[ ! -f "$brief" || -L "$brief" ]]; then  # the VPS sends brief files only: anything else is not kept
        echo "  not a file, left out: $name"
    elif grep -qxF -- "$name" "$record"; then
        echo "  already downloaded: $name"
    elif [[ -e "$dest/$name" ]]; then  # there already (and maybe annotated since): left as it is
        echo "$name" >> "$record"
        echo "  already there: $name"
    else
        mv -n -- "$brief" "$dest/$name"
        echo "$name" >> "$record"
        echo "  new: $name"
    fi
done
exit "$code"
