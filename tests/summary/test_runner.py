import json

from xmd.summary.chunk import Chunk, Post, make_chunks
from xmd.summary.engine import Completion, EngineError
from xmd.summary.prompt import BRIEF_SCHEMA, bounded_schema
from xmd.summary.runner import (
    ChunkResult, check_reply, compression_label, reply_budget, run_chunks, summarize, suspect_truncation, write_run,
)
from xmd.core.tiers import RECAP, REGULAR, RETWEET

RATIOS = {REGULAR: 0.5, RETWEET: 0.5, RECAP: 0.5}
GOOD = {"items": [{"headline": "Rates rise", "detail": "The bank raised rates by a quarter point", "ids": [1, 2]}]}
GOOD_WORDS = 2 + 8  # headline + detail


def _completion(text="{}", prompt_tokens=1000, completion_tokens=200, finish="stop"):
    return Completion(text, finish, prompt_tokens, completion_tokens, 0, 0.5)


class FakeEngine:
    """Answers each call from a script: a dict is a reply, an exception is raised. Then GOOD forever."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def complete_json(self, system, user, schema, max_tokens=1500, temperature=0.2):
        self.calls.append({"system": system, "user": user, "schema": schema, "max_tokens": max_tokens,
                           "temperature": temperature})
        reply = self.script.pop(0) if self.script else GOOD
        if isinstance(reply, Exception):
            raise reply
        return reply, _completion(json.dumps(reply))


def _chunks():
    posts = [
        Post(1, "X", REGULAR, "@a one two three", 4), Post(2, "X", REGULAR, "@b four five", 3),
        Post(3, "X", RETWEET, "@c RT@d six", 2), Post(4, "X / chat", RECAP, "@e seven", 2),
    ]
    return make_chunks(posts, [], RATIOS)  # regular-01, retweets-01, recap-01


def _system(band):
    return f"prompt for {band}"


def test_each_chunk_goes_out_with_its_bands_prompt_its_posts_and_a_reply_budget():
    engine, chunks = FakeEngine(), _chunks()
    results = run_chunks(engine, chunks, _system, temperature=0.1)
    assert [r.chunk.id for r in results] == ["regular-01", "retweets-01", "recap-01"] and all(r.ok for r in results)
    for call, chunk in zip(engine.calls, chunks):
        assert call["system"] == f"prompt for {chunk.band}" and call["user"] == chunk.render()
        assert call["schema"] is BRIEF_SCHEMA and call["temperature"] == 0.1
        assert call["max_tokens"] == reply_budget(chunk)


def test_the_reply_budget_is_counted_in_items_and_bounded():
    many = tuple(Post(n, "X", REGULAR, "w", 1) for n in range(1, 101))
    assert reply_budget(Chunk("r", REGULAR, many, (), 830)) == 3100  # 28 items x 100 + 300
    assert reply_budget(Chunk("t", RETWEET, many[:30], (), 393)) == 1100  # retweet themes, 8 at most: 8 x 100 + 300
    assert reply_budget(Chunk("r", REGULAR, many, (), 3000)) == 3200  # capped, so it fits an 8K context
    assert reply_budget(Chunk("r", REGULAR, many[:1], (), 20)) == 500  # and never below a floor


def test_a_failed_attempt_is_retried_and_the_attempts_counted():
    results = run_chunks(FakeEngine(EngineError("boom")), _chunks()[:1], _system, retries=1)
    assert results[0].ok and results[0].attempts == 2 and results[0].error is None


def test_a_chunk_that_keeps_failing_is_recorded_and_the_run_goes_on():
    engine = FakeEngine(EngineError("first", _completion(finish="length")), EngineError("second"))
    failed, *rest = run_chunks(engine, _chunks(), _system, retries=1)
    assert not failed.ok and failed.attempts == 2 and failed.error == "second"
    assert failed.completion.finish == "length"  # what came back on the way is kept
    assert len(rest) == 2 and all(r.ok for r in rest)


def test_a_reply_of_the_wrong_shape_counts_as_a_failure_and_no_retries_means_one_attempt():
    bad = {"items": [{"headline": "h", "detail": "d", "ids": ["x"]}]}
    (result,) = run_chunks(FakeEngine(bad), _chunks()[:1], _system, retries=0)
    assert not result.ok and result.attempts == 1 and "ids must be a list of integers" in result.error


def _big_chunk(tier=REGULAR, target=600):
    return Chunk("regular-01", tier, tuple(Post(n, "X", tier, "w", 5) for n in range(1, 41)), (), target)  # 20 items, min 5


def test_a_reply_with_far_too_few_items_is_a_failure_and_is_retried():
    thin = {"items": [{"headline": "everything", "detail": "in one line", "ids": [1]}]}
    fine = {"items": [{"headline": f"story {k}", "detail": "d", "ids": [k]} for k in range(1, 9)]}
    (retried,) = run_chunks(FakeEngine(thin, fine), [_big_chunk()], _system, retries=1)
    assert retried.ok and retried.attempts == 2 and len(retried.items) == 8
    (stuck,) = run_chunks(FakeEngine(thin, thin), [_big_chunk()], _system, retries=1)
    assert not stuck.ok and "too thin: 1 item(s) for 40 posts, at least 5 expected" in stuck.error


def test_a_schema_function_gives_each_chunk_its_own_schema():
    engine, chunks = FakeEngine(), _chunks()
    run_chunks(engine, chunks, _system, schema=lambda c: bounded_schema(c.min_items, c.max_items))
    for call, chunk in zip(engine.calls, chunks):
        limits = call["schema"]["properties"]["items"]
        assert (limits["minItems"], limits["maxItems"]) == (chunk.min_items, chunk.max_items)


def test_check_reply_names_what_is_wrong():
    assert check_reply(GOOD) is None and check_reply({"items": []}) is None
    assert check_reply({}) == "no `items` list"
    assert "not an object" in check_reply({"items": ["x"]})
    assert "strings" in check_reply({"items": [{"headline": 1, "detail": "d", "ids": []}]})
    assert "integers" in check_reply({"items": [{"headline": "h", "detail": "d", "ids": [True]}]})


def test_results_report_their_reply_words_and_the_run_is_summed_up():
    results = run_chunks(FakeEngine(), _chunks(), _system)
    assert results[0].reply_words == GOOD_WORDS and len(results[0].items) == 1
    total = summarize(results)
    assert total["chunks"] == 3 and total["ok"] == 3 and total["failed"] == 0
    assert total["reply_words"] == 3 * GOOD_WORDS and total["prompt_tokens"] == 3000 and total["completion_tokens"] == 600
    assert total["target_words"] == sum(r.chunk.target_words for r in results)
    assert total["chars_per_token"] == round(sum(r.prompt_chars for r in results) / 3000, 2)


def test_on_result_is_called_once_per_chunk_in_order():
    seen = []
    run_chunks(FakeEngine(), _chunks(), _system, on_result=lambda r: seen.append(r.chunk.id))
    assert seen == ["regular-01", "retweets-01", "recap-01"]


def _with_ratio(chunk_id, chars, tokens):
    chunk = _chunks()[0]
    renamed = Chunk(chunk_id, chunk.tier, chunk.posts, chunk.repeated, chunk.target_words)
    return ChunkResult(renamed, 500, chars, completion=_completion(prompt_tokens=tokens), reply=GOOD)


def test_a_prompt_that_arrives_with_far_fewer_tokens_than_its_text_deserves_looks_truncated():
    runs = [_with_ratio("a", 14000, 4000), _with_ratio("b", 14200, 4050), _with_ratio("c", 13800, 3900),
            _with_ratio("d", 14100, 2300)]  # 6.1 characters per token against ~3.5
    assert suspect_truncation(runs) == ["d"]
    assert suspect_truncation(runs[:2]) == []  # too few chunks to tell what is typical


def test_a_tiny_chunk_is_never_flagged_because_it_cannot_overflow_and_is_mostly_system_prompt():
    runs = [_with_ratio("a", 14000, 4000), _with_ratio("b", 14200, 4050), _with_ratio("c", 13800, 3900),
            _with_ratio("tiny", 3000, 500)]  # 6 characters per token, but only 3K characters
    assert suspect_truncation(runs) == []
    assert suspect_truncation([]) == []


def test_write_run_saves_a_file_per_chunk_and_a_manifest_with_totals_and_a_table(tmp_path):
    engine = FakeEngine(EngineError("no"), EngineError("no again"))
    results = run_chunks(engine, _chunks(), _system, retries=1)
    write_run(tmp_path / "run", {"label": "t", "engine": {"model": "m"}}, results)

    files = sorted(p.name for p in (tmp_path / "run").iterdir())
    assert files == ["recap-01.json", "regular-01.json", "retweets-01.json", "run.json"]
    good = json.loads((tmp_path / "run" / "retweets-01.json").read_text(encoding="utf-8"))
    assert good["ok"] and good["reply"] == GOOD and good["ids"] == [3] and json.loads(good["raw_text"]) == GOOD
    assert good["completion"]["prompt_tokens"] == 1000 and good["max_tokens"] == reply_budget(results[1].chunk)
    assert good["max_items"] == results[1].chunk.max_items
    bad = json.loads((tmp_path / "run" / "regular-01.json").read_text(encoding="utf-8"))
    assert not bad["ok"] and bad["reply"] is None and bad["error"] == "no again" and bad["attempts"] == 2
    manifest = json.loads((tmp_path / "run" / "run.json").read_text(encoding="utf-8"))
    assert manifest["label"] == "t" and manifest["summary"]["failed"] == 1 and manifest["summary"]["ok"] == 2
    assert [row["chunk"] for row in manifest["chunks"]] == ["regular-01", "retweets-01", "recap-01"]
    assert manifest["chunks"][0]["ok"] is False and manifest["chunks"][1]["reply_words"] == GOOD_WORDS


def test_a_runs_compression_is_named_from_its_manifest_and_a_run_saved_as_a_scenario_still_reads():
    assert compression_label({"compression_ratio": 0.3, "ratios": {}}) == "compression 0.30"
    assert compression_label({"compression_ratio": 0.1}) == "compression 0.10"
    assert compression_label({"scenario": {"name": "A", "ratios": {}}}) == "scenario A"  # saved before --compression
