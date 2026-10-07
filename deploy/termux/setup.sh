#!/data/data/com.termux/files/usr/bin/bash
# One-time setup of the phone, inside Termux (the F-Droid build, with the Termux:Widget add-on), from the repo:
#
#   bash deploy/termux/setup.sh
#
# Installs what the pipeline needs, makes the virtual environment, asks for access to the phone's files, and
# puts three buttons in Termux:Widget's list. Safe to run again, after a `git pull` for instance.
# docs/remote-setup.md has the steps around it (.env, the SSH key, the widget).
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -z "${PREFIX:-}" || ! -d "$PREFIX/bin" ]]; then
    echo "this is the phone's setup: run it inside Termux" >&2
    exit 1
fi

echo "== packages"
pkg install -y python openssh util-linux tar

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

echo "== python environment"
cd "$repo"
if [[ ! -x .venv/bin/python ]]; then
    python -m venv .venv
fi
# tzdata: Android keeps no time-zone database where Python looks for one (`timezone:` in sources.yaml)
.venv/bin/pip install --quiet -e . tzdata

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

if [[ ! -f .env ]]; then
    cp deploy/env.example .env
    chmod 600 .env
    echo
    echo "made .env from deploy/env.example: put your keys in it (nano .env), then"
    echo "  set -a; . ./.env; set +a; .venv/bin/python scripts/smoke_engine.py"
fi
echo
echo "done."
echo "Add the Termux:Widget widget to the home screen to get the buttons."
