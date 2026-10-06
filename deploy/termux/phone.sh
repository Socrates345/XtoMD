#!/data/data/com.termux/files/usr/bin/bash
# On the phone (Termux): make the brief on the phone itself, no VPS involved. Only the model is elsewhere
# (.env at the repo root names it). The home-screen shortcuts setup.sh installs call this; so can you:
#
#   bash deploy/termux/phone.sh 24h         everything published in the last 24 hours
#   bash deploy/termux/phone.sh 24h 10      ... fitted to a 10-minute read
#   bash deploy/termux/phone.sh since       only what is new since the phone's own last digest
#
# The brief is copied to XMD_BRIEF_DEST (.env), or to Documents/xmd-briefs when .env names no folder: without
# a copy it would stay in Termux's own storage, where no other app can open it. docs/remote.md.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export XMD_BRIEF_DEST="${XMD_BRIEF_DEST:-$HOME/storage/shared/Documents/xmd-briefs}"  # .env's, read by brief.sh, wins

# Android suspends an app whose screen is off: hold the CPU for the few minutes the run takes
termux-wake-lock 2> /dev/null || true
trap 'termux-wake-unlock 2> /dev/null || true' EXIT

bash "$repo/deploy/brief.sh" "$@"
