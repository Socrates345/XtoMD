"""The X backends. Each module offers `fetch(source, api_key, max_items, ...) -> list[FeedItem]` (see
fetcher.py); what they share about paging lives here."""

import sys

UNLIMITED = sys.maxsize  # `max_items` when no item ceiling applies and only the age cutoff ends a fetch
MAX_PAGES = 25  # pages one handle may cost when nothing else ends its fetch (25 pages are ~500 tweets)


def page_limit(max_items: int, page_size: int) -> int:
    """The most pages one handle's fetch may take: what `max_items` needs, twice over (pages come back part-empty:
    tweets with no URL, say), and never fewer than MAX_PAGES. With no item ceiling it is MAX_PAGES exactly. It is the
    net under the age cutoff, which never fires when a backend changes its date format, and under a cursor that
    never ends: neither may turn a fetch into a walk through a handle's whole history."""
    if max_items >= UNLIMITED:
        return MAX_PAGES
    return max(MAX_PAGES, 2 * -(-max_items // page_size))
