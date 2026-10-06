# Sourced by brief.sh and vps/ssh-entry.sh: where things are on this machine.

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$repo/.venv/bin/python"

# `digest_dir:` in sources.yaml (digests/ unless you changed it), as given there: a path from the repo root
digests_dir() {
    (cd "$repo" && "$python" -c 'from xmd.core.config import load_config; print(load_config("sources.yaml").digest_dir)')
}
