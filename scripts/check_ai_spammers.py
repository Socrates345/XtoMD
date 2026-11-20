"""Which handles of sources/x.md are on levelsio's "Blocked for AI Reply" list (accounts that spam automated AI
replies, https://blockedforaireply.levels.io). Reads only: downloads the list, prints the matches, changes nothing.

    python scripts\\check_ai_spammers.py                     # sources/x.md
    python scripts\\check_ai_spammers.py --x-md other/x.md

Exit 0: none on the list, 1: some are (remove them from x.md yourself if you agree), 2: the list could not be read.
The list changes all the time (an account can appeal and be removed), so a match is worth a look, not proof.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

from xmd.core.config import _read_md_list  # noqa: E402

LIST_URL = "https://blockedforaireply.levels.io/api/usernames.txt"  # plain text, one username per line


def blocked_names(text: str) -> set[str]:
    """The list's usernames, lowercased (X handles ignore case), `@` and blank lines dropped."""
    return {line.strip().lstrip("@").lower() for line in text.splitlines() if line.strip()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print the handles of x.md that are on the Blocked for AI Reply list.")
    ap.add_argument("--x-md", default="sources/x.md")
    args = ap.parse_args(argv)

    try:
        resp = httpx.get(LIST_URL, timeout=30, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"could not read {LIST_URL}: {exc}", file=sys.stderr)
        return 2
    blocked = blocked_names(resp.text)
    if not blocked:
        print(f"{LIST_URL} came back empty", file=sys.stderr)
        return 2

    handles = [e["handle"] for e in _read_md_list(Path(args.x_md))]
    hits = [h for h in handles if h.lower() in blocked]
    print(f"{len(handles)} handles in {args.x_md}, {len(blocked)} on the list: {len(hits)} on both")
    for handle in hits:
        print(f"  @{handle}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
