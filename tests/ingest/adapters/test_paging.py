import pytest

from xmd.ingest.adapters import MAX_PAGES, UNLIMITED, page_limit


def test_with_no_item_ceiling_the_guard_is_exactly_max_pages():
    """The age cutoff is then the only brake, and it never fires if a backend changes its date format:
    the guard is the net under it, so it must not grow with the (unbounded) ceiling."""
    assert page_limit(UNLIMITED, 20) == MAX_PAGES


@pytest.mark.parametrize("max_items, expected", [(30, MAX_PAGES), (100, MAX_PAGES), (600, 60), (1000, 100)])
def test_an_explicit_ceiling_gets_the_pages_it_needs_twice_over_and_never_fewer_than_max_pages(max_items, expected):
    """A deliberate `max_per_source: 300` (600 items, 30 pages) must not be cut at 25 pages by the guard."""
    assert page_limit(max_items, 20) == expected
