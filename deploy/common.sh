# Sourced by brief.sh and vps/ssh-entry.sh: where things are on this machine.

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$repo/.venv/bin/python"

# `digest_dir:` in sources.yaml (digests/ unless you changed it), as given there: a path from the repo root. Read on
# its own, without the keys: ssh-entry.sh has not read .env, which may hold the X backend's key
digests_dir() {
    (cd "$repo" && "$python" -c '
import sys
from xmd.core.config import read_digest_dir
try:
    print(read_digest_dir("sources.yaml"))
except ValueError as exc:  # as make_brief.py says it: one line, no traceback
    sys.exit(f"config error: {exc}")
')
}
