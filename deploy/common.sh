# Sourced by brief.sh and vps/ssh-entry.sh: where things are on this machine.

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$repo/.venv/bin/python"

# `digest_dir:` in sources.yaml (digests/ unless you changed it), as given there: a path from the repo root
digests_dir() {
    (cd "$repo" && "$python" -c '
import sys
from xmd.core.config import load_config
try:
    print(load_config("sources.yaml").digest_dir)
except (FileNotFoundError, ValueError) as exc:  # as make_brief.py says it: one line, no traceback
    sys.exit(f"config error: {exc}")
')
}
