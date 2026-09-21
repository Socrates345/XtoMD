import pytest

from xmd.verify import chunk_stats

REPLY = {"items": [
    {"headline": "Rates rise", "detail": "The bank raised rates by a quarter point today", "ids": [1, 2]},
    {"headline": "Tariff cut", "detail": "New import rules", "ids": [3]},
]}


def _record(reply=None, raw_text=None, ids=(1, 2, 3, 4)):
    return {"chunk": "regular-01", "ids": list(ids), "reply": reply, "raw_text": raw_text}


def test_a_parsed_reply_is_measured_item_by_item():
    stats = chunk_stats(_record(REPLY))
    assert stats.items == 2 and stats.ids_per_item == 1.5 and stats.max_ids == 2
    assert stats.cited == 3 and stats.in_chunk == 1.0
    assert stats.covered == 0.75  # posts 1, 2 and 3 of 4
    assert stats.duplicate_headlines == 0
    assert stats.detail_words == pytest.approx((9 + 3) / 2)


def test_citing_a_post_that_is_not_in_the_chunk_lowers_in_chunk():
    reply = {"items": [{"headline": "h", "detail": "d", "ids": [1, 99]}, {"headline": "g", "detail": "d", "ids": [98, 2]}]}
    stats = chunk_stats(_record(reply))
    assert stats.in_chunk == 0.5 and stats.cited == 4
    assert stats.covered == 0.5  # invented numbers do not count as covering anything


def test_a_repeated_headline_is_counted_ignoring_case_and_spacing():
    reply = {"items": [{"headline": "Big news", "detail": "a", "ids": [1]}, {"headline": " big NEWS ", "detail": "b", "ids": [2]}]}
    assert chunk_stats(_record(reply)).duplicate_headlines == 1


def test_a_reply_cut_off_mid_json_is_read_as_far_as_it_goes():
    cut = ('{"items": [{"headline": "Rates rise", "detail": "The bank raised rates", "ids": [1, 2]},'
           '{"headline": "Tariff cut", "detail": "New import rules", "ids": [3, 4]},'
           '{"headline": "Third", "detail": "cut off here", "ids": [4')
    stats = chunk_stats(_record(reply=None, raw_text=cut))
    assert stats.items == 3  # the third headline was written
    assert stats.max_ids == 2 and stats.cited == 4 and stats.in_chunk == 1.0 and stats.covered == 1.0  # its unfinished ids are ignored


def test_an_empty_reply_and_a_missing_raw_text_give_zeros_not_errors():
    stats = chunk_stats(_record({"items": []}))
    assert stats.items == 0 and stats.ids_per_item == 0.0 and stats.in_chunk == 1.0 and stats.covered == 0.0
    assert chunk_stats(_record(reply=None, raw_text=None)).items == 0


from xmd.verify import support  # noqa: E402

POSTS = {
    1: "@alice Acme said revenue hit $4.2 billion, up 35% from 1,200 last year. $ACME jumped",
    2: "@b The SEC opened a probe into Globex after the hack",
    3: "@c unrelated chatter about lunch",
}


def _item(headline, detail, ids):
    return {"headline": headline, "detail": detail, "ids": ids}


def test_numbers_handles_and_tickers_found_in_a_cited_post_are_supported():
    result = support([_item("Acme revenue jumps 35%", "It hit $4.2 billion, from 1200 a year earlier; $ACME rose, says @alice", [1])], POSTS)
    assert result.facts == 5  # 35, 4.2, 1200, $ACME, @alice
    assert result.unsupported_facts == 0 and result.items_with_unsupported_fact == 0


def test_an_invented_number_is_caught_and_a_number_inside_another_does_not_pass():
    result = support([_item("Revenue rises", "Up 37% to $4.2 billion, 3 analysts agree", [1])], POSTS)
    assert result.unsupported_facts == 2  # 37 and 3 (3 is only the first digit of 35, not a number of its own)
    assert result.items_with_unsupported_fact == 1
    assert result.flagged == ((0, ("37", "3")),)  # which item, and which figures


def test_support_comes_only_from_the_cited_posts_but_the_union_of_them_counts():
    assert support([_item("SEC probe", "into Globex", [1])], POSTS).unsupported_names > 0  # post 1 says neither
    assert support([_item("SEC probe", "into Globex", [1, 2])], POSTS).unsupported_names == 0


def test_names_ignore_sentence_starts_and_match_by_stem():
    result = support([_item("Globex hack", "Regulators opened a probe. Then Globex said the SEC is involved", [2])], POSTS)
    # SEC and Globex are supported; "Regulators" starts the detail and "Then" starts a sentence: both skipped
    assert result.names == 2 and result.unsupported_names == 0  # SEC and the second Globex
    assert support([_item("Hack", "The Hooli office was hit", [2])], POSTS).unsupported_names == 1  # Hooli


def test_an_item_citing_nothing_real_is_counted_as_uncited_and_its_facts_are_unsupported():
    result = support([_item("Revenue", "Up 35%", [99]), _item("Lunch", "chatter", [3])], POSTS)
    assert result.uncited_items == 1 and result.unsupported_facts == 1 and result.items == 2
