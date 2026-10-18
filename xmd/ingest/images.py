"""A picture's fingerprint, so a post that shows the same picture again is known as a repeat (see filters.py).

Only the bytes are compared: the same file posted again gives the same bytes, each time under a new url. A picture
changed at all (a date edited in it) is another picture.
"""

from __future__ import annotations

import hashlib
import re

import httpx

TIMEOUT = 10.0  # seconds per picture: a slow one is not worth holding a fetch for

_X_PICTURE = re.compile(r"^https://pbs\.twimg\.com/", re.IGNORECASE)


def small(url: str) -> str:
    """X's small rendition of one of its pictures: the same picture gives the same bytes in it, at about 40% of
    the full size's. Any other url as it is."""
    if not _X_PICTURE.match(url):
        return url
    base, _, query = url.partition("?")
    kept = [p for p in query.split("&") if p and not p.startswith("name=")]
    return f"{base}?{'&'.join([*kept, 'name=small'])}"


def digest(url: str, client: httpx.Client) -> str | None:
    """The fingerprint of the picture at `url`, or None when it can't be had."""
    try:
        response = client.get(small(url), timeout=TIMEOUT, follow_redirects=True)
        response.raise_for_status()
    except Exception:  # a picture never fails a fetch: without it, the post is just not compared
        return None
    return hashlib.sha256(response.content).hexdigest()[:32]
