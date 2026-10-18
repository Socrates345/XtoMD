#!/data/data/com.termux/files/usr/bin/bash
# One-time setup of the phone, inside Termux (the F-Droid build, with the Termux:Widget add-on), from the repo:
#
#   bash deploy/termux/setup.sh          the buttons: the phone asks the VPS for briefs and downloads them
#   bash deploy/termux/setup.sh alone    ... and what a brief made on the phone itself needs (phone.sh)
#
# Installs what the buttons need (SSH), asks for access to the phone's files, puts the buttons in Termux:Widget's
# list and makes the phone's .env. With `alone`, also Python, the virtual environment and the model's settings in
# .env. Safe to run again, after a `git pull` for instance: an existing .env or virtual environment is kept.
# docs/remote-setup.md has the steps around it (the SSH key, the widget).
set -euo pipefail

case "${1:-}" in
    "") alone=0 ;;
    alone) alone=1 ;;
    *)
        echo "usage: setup.sh [alone]" >&2
        exit 2
        ;;
esac

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -z "${PREFIX:-}" || ! -d "$PREFIX/bin" ]]; then
    echo "this is the phone's setup: run it inside Termux" >&2
    exit 1
fi

echo "== packages"
packages=(openssh tar)
if [[ "$alone" -eq 1 ]]; then
    packages+=(python util-linux)  # util-linux: flock, which brief.sh takes
fi
pkg install -y "${packages[@]}"

echo "== access to the phone's files (where the briefs go)"
if [[ -d "$HOME/storage/shared" ]]; then
    echo "already allowed: Android does not ask again"
else
    echo "Android asks whether Termux may reach your files: allow it."
    termux-setup-storage
    for _ in $(seq 60); do
        [[ -d "$HOME/storage/shared" ]] && break
        sleep 1
    done
fi
# the folder the briefs go to unless .env names another (phone.sh, vps.sh): making it proves the access is real
briefs="$HOME/storage/shared/Documents/xmd-briefs"
if ! mkdir -p "$briefs" 2> /dev/null || ! touch "$briefs/.write-test" 2> /dev/null; then
    echo "Termux cannot write to the phone's files yet: allow it (Settings > Apps > Termux > Permissions)," >&2
    echo "then run this script again" >&2
    exit 1
fi
rm -f "$briefs/.write-test"
echo "ok: briefs go to Documents/xmd-briefs"

cd "$repo"
if [[ "$alone" -eq 1 ]]; then
    echo "== python environment"
    if [[ ! -x .venv/bin/python ]]; then
        python -m venv .venv
    fi
    # tzdata: Android keeps no time-zone database where Python looks for one (`timezone:` in sources.yaml)
    .venv/bin/pip install --quiet -e . tzdata
fi

echo "== home-screen shortcuts"
shortcuts="$HOME/.shortcuts"
mkdir -p "$shortcuts"
chmod 700 "$shortcuts"

shortcut() {  # shortcut NAME SCRIPT [ARGUMENT...]: one button in Termux:Widget's list
    local name="$1"
    shift
    printf '#!%s/bin/bash\nexec bash%s\n' "$PREFIX" "$(printf ' %q' "$@")" > "$shortcuts/$name"
    chmod 700 "$shortcuts/$name"
    echo "  $name"
}

# the buttons an earlier version of this script made, under the names they had then: the list would keep them
for old in brief-vps-24h brief-vps-10min brief-vps-latest brief-phone-24h brief-phone-10min; do
    rm -f "$shortcuts/$old"
done

shortcut download-latest-daily-brief "$repo/deploy/termux/vps.sh" pull
shortcut generate-last-24h-brief "$repo/deploy/termux/vps.sh" run 24h
shortcut generate-last-24h-brief-10min-read "$repo/deploy/termux/vps.sh" run 24h 10
shortcut generate-since-last-brief "$repo/deploy/termux/vps.sh" run since

echo "== settings"
if [[ ! -f .env ]]; then
    cp deploy/termux/env.example .env
    chmod 600 .env
    echo "made .env, the phone's settings (deploy/termux/env.example): the VPS's .env is a separate file"
else
    echo "kept the .env already there"
fi
if [[ "$alone" -eq 1 ]] && ! grep -q '^XMD_LLM_BASE_URL=' .env; then
    # the model's settings, without the template's opening comment (up to its first blank line)
    { echo; sed '1,/^$/d' deploy/env.example; } >> .env
    echo "added the model's settings to .env: put your keys in it (nano .env), then"
    echo "  set -a; . ./.env; set +a; .venv/bin/python scripts/smoke_engine.py"
fi

echo
echo "done."
echo "Add the Termux:Widget widget to the home screen to get the buttons."
if [[ "$alone" -eq 0 ]]; then
    echo "For briefs made on the phone itself: bash deploy/termux/setup.sh alone (docs/remote-setup.md, step 4)."
fi
