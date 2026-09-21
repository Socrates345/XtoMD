import json

from xmd.summary import sections
from xmd.summary.brief import BriefItem
from xmd.summary.engine import Completion, EngineError
from xmd.summary.prompt import SECTION_SCHEMA, load_section_prompt
from xmd.summary.sections import (
    input_hash, load_cache, save_cache, section_inputs, summarize_sections, summary_problem,
)
from xmd.summary.topics import Topic

MAPPING = {str(n): {"source": f"@s{n}", "url": f"https://x.com/s{n}/status/{n}"} for n in range(1, 9)}


def _item(headline, detail, ids, section="X / business", tier="regular"):
    return BriefItem(tier, section, headline, detail, tuple(ids), False, ())


KEPT = [
    _item("ACME lists on an exchange", "The listing went live", [1, 2, 3]),
    _item("A broker ships an update", "", [4]),
    _item("Fees fall to 12 cents", "Average fee fell", [5, 6]),
    _item("Only two bullets here", "so no summary", [7], section="X / news"),
    _item("Second one", "", [8], section="X / news"),
]
TOPICS = {"X / business": [Topic("ACME", 25, 3, 7.3)]}


class FakeModel:
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def complete_json(self, system, user, schema, max_tokens=300, temperature=0.2):
        self.calls.append({"system": system, "user": user, "schema": schema, "max_tokens": max_tokens})
        reply = self.script.pop(0) if self.script else {"summary": "ACME dominated, posted about by 3 accounts."}
        if isinstance(reply, Exception):
            raise reply
        return reply, Completion(json.dumps(reply), "stop", 100, 30, 0, 0.1)


def test_the_input_names_the_section_the_loud_names_and_the_accounts_behind_each_bullet():
    message = section_inputs(KEPT, MAPPING, TOPICS)["X / business"]
    assert message.splitlines()[:2] == ["Section: X / business", "Louder than usual: ACME (25 posts from 3 accounts)"]
    assert "- ACME lists on an exchange — The listing went live (3 accounts)" in message
    assert "- A broker ships an update (1 account)" in message  # no detail: no dash; one account: singular


def test_a_section_with_too_few_bullets_gets_no_summary_and_no_loud_line_when_there_are_none():
    inputs = section_inputs(KEPT, MAPPING, TOPICS)
    assert list(inputs) == ["X / business"]  # "X / news" has two bullets
    assert "Louder" not in section_inputs(KEPT, MAPPING, {})["X / business"]


def test_a_very_long_section_is_cut_at_its_least_important_end(monkeypatch):
    monkeypatch.setattr(sections, "MAX_BULLETS", 2)
    message = section_inputs(KEPT, MAPPING, {})["X / business"]
    assert "ACME lists" in message and "A broker" in message and "Fees fall" not in message


def test_each_section_is_sent_with_the_prompt_and_schema_and_the_answer_is_returned():
    model = FakeModel()
    inputs = section_inputs(KEPT, MAPPING, TOPICS)
    result = summarize_sections(model, inputs, "the system prompt")
    assert result["X / business"].text == "ACME dominated, posted about by 3 accounts."
    (call,) = model.calls
    assert call["system"] == "the system prompt" and call["user"] == inputs["X / business"]
    assert call["schema"] is SECTION_SCHEMA


def test_an_answer_already_in_the_cache_is_not_asked_for_again():
    model, cache, seen = FakeModel(), {}, []
    inputs = section_inputs(KEPT, MAPPING, TOPICS)
    summarize_sections(model, inputs, "p", cache)
    summarize_sections(model, inputs, "p", cache, on_result=lambda label, s, cached: seen.append(cached))
    assert len(model.calls) == 1 and seen == [True]
    summarize_sections(model, inputs, "another prompt", cache)  # a different prompt is a different question
    assert len(model.calls) == 2


def test_a_failed_call_leaves_the_section_without_a_summary_and_nothing_is_cached():
    cache = {}
    result = summarize_sections(FakeModel(EngineError("server down")), section_inputs(KEPT, MAPPING, {}), "p", cache)
    assert result["X / business"].text is None and result["X / business"].error == "server down" and cache == {}


def test_a_figure_the_input_never_had_makes_the_summary_unusable():
    cache = {}
    bad = {"summary": "ACME dominated, posted about by 9 accounts."}  # the input says 3, 1 and 2
    result = summarize_sections(FakeModel(bad), section_inputs(KEPT, MAPPING, TOPICS), "p", cache)
    assert result["X / business"].text is None and "not in the input: 9" in result["X / business"].error and cache == {}


def test_an_empty_summary_is_unusable_and_a_restated_figure_is_fine():
    assert summarize_sections(FakeModel({"summary": "  "}), section_inputs(KEPT, MAPPING, {}), "p")["X / business"].error == "empty summary"
    message = section_inputs(KEPT, MAPPING, TOPICS)["X / business"]
    assert summary_problem("ACME was loud: 25 posts from 3 accounts, fees fell to 12 cents.", message) is None
    assert summary_problem("Fees fell to 13 cents.", message) == "not in the input: 13"


def test_the_cache_round_trips_and_a_missing_or_broken_file_is_an_empty_cache(tmp_path):
    path = tmp_path / "sections.json"
    assert load_cache(path) == {}
    save_cache(path, {input_hash("p", "m"): "text"})
    assert load_cache(path) == {input_hash("p", "m"): "text"}
    path.write_text("not json", encoding="utf-8")
    assert load_cache(path) == {}


def test_the_section_prompt_has_no_front_matter_and_asks_for_the_schemas_key():
    prompt = load_section_prompt()
    assert not prompt.startswith("---") and "name: xmd-section-summary" not in prompt
    assert '{"summary": "..."}' in prompt and "Louder than usual" in prompt
