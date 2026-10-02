import json

from xmd.core.config import Language
from xmd.summary.engine import Completion, EngineError
from xmd.summary.translate import (
    DETECT_SCHEMA, TRANSLATE_SCHEMA, Translation, load_cache, save_cache, translate_texts, worth_checking,
)

EN = Language(("en",), "en")
EN_FR = Language(("en", "fr"), "en")


class FakeModel:
    """Tags a text with the language `langs` gives it and translates by `translations`, as a model would."""

    def __init__(self, langs, translations=None, fail=()):
        self.langs, self.translations, self.fail, self.calls = langs, translations or {}, set(fail), []

    def complete_json(self, system, user, schema, max_tokens=300, temperature=0.2):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if schema is DETECT_SCHEMA:
            if "detect" in self.fail:
                raise EngineError("HTTP 500: boom")
            posts = []
            for line in user.splitlines():
                n, text = line[1:].split("] ", 1)
                posts.append({"n": int(n), "lang": next(v for k, v in self.langs.items() if text.startswith(k[:20]))})
            reply = {"posts": posts}
        else:
            if user in self.fail:
                raise EngineError("the reply hit the token cap in the middle of the JSON")
            reply = {"translation": self.translations.get(user, "")}
        return reply, Completion(json.dumps(reply), "stop", 100, 30, 0, 0.1)


ZH = "今天央行宣布降息"
DE = "Die Zentralbank senkt heute die Zinsen"
FR = "La banque centrale baisse ses taux"
EN_TEXT = "The central bank cuts rates today"
LANGS = {ZH: "zh", DE: "de-DE", FR: "fr", EN_TEXT: "en"}
TRANSLATIONS = {ZH: "The central bank announced a rate cut today", DE: "The central bank cuts rates today"}


def test_only_texts_outside_the_accepted_languages_are_translated():
    model = FakeModel(LANGS, TRANSLATIONS)
    done = translate_texts(model, [ZH, DE, FR, EN_TEXT], EN_FR)
    assert done.translations == {
        ZH: Translation("zh", "The central bank announced a rate cut today"),
        DE: Translation("de", "The central bank cuts rates today"),  # "de-DE" read as "de"
    }
    assert (done.checked, done.foreign) == (4, 2)
    detect, *translations = model.calls
    assert detect["schema"] is DETECT_SCHEMA and detect["user"].startswith(f"[1] {ZH}\n")
    assert [c["user"] for c in translations] == [ZH, DE]  # one call each, the text alone
    assert all("into English" in c["system"] and c["schema"] is TRANSLATE_SCHEMA for c in translations)


def test_texts_are_tagged_in_batches_and_each_text_once(monkeypatch):
    from xmd.summary import translate
    monkeypatch.setattr(translate, "DETECT_BATCH", 2)
    model = FakeModel(LANGS, TRANSLATIONS)
    translate_texts(model, [EN_TEXT, FR, EN_TEXT, ZH], EN_FR)
    tags = [c for c in model.calls if c["schema"] is DETECT_SCHEMA]
    assert [c["user"].count("\n") for c in tags] == [2, 1]  # three distinct texts, two a call


def test_links_handles_and_bare_reactions_are_not_worth_a_call():
    assert not worth_checking("https://t.co/abc @someone #tag $ACME")
    assert not worth_checking("🔥🔥")
    assert worth_checking(ZH) and worth_checking("ok go now")
    model = FakeModel(LANGS)
    assert translate_texts(model, ["https://t.co/abc", ""], EN).translations == {}
    assert model.calls == []


def test_a_failed_or_empty_or_unchanged_translation_leaves_the_post_as_posted():
    model = FakeModel({**LANGS, "Das ist gut": "de"}, {DE: DE}, fail={ZH})
    done = translate_texts(model, [ZH, DE, "Das ist gut"], EN)
    assert done.translations == {}
    assert len(done.failed) == 3 and "token cap" in done.failed[0]


def test_a_failed_tagging_call_translates_nothing_and_says_so():
    done = translate_texts(FakeModel(LANGS, TRANSLATIONS, fail={"detect"}), [ZH], EN)
    assert done.translations == {} and done.failed == ["language tags for 1 posts: HTTP 500: boom"]


def test_a_number_the_original_lacks_is_flagged():
    model = FakeModel(LANGS, {ZH: "The central bank cut rates by 50 points today"})
    assert translate_texts(model, [ZH], EN).translations[ZH].unsupported == ("50",)


def test_a_very_long_post_stays_as_posted(monkeypatch):
    from xmd.summary import translate
    monkeypatch.setattr(translate, "MAX_TRANSLATE_CHARS", 5)
    done = translate_texts(FakeModel(LANGS, TRANSLATIONS), [ZH + ZH], EN)
    assert done.translations == {} and done.too_long == 1


def test_tags_and_translations_are_cached_so_a_second_assembly_costs_no_call(tmp_path):
    cache: dict = {}
    first = translate_texts(FakeModel(LANGS, TRANSLATIONS), [ZH, EN_TEXT], EN, cache)
    save_cache(tmp_path / "t.json", cache)
    model = FakeModel({}, {})
    again = translate_texts(model, [ZH, EN_TEXT], EN, load_cache(tmp_path / "t.json"))
    assert model.calls == [] and again.translations == first.translations


def test_a_missing_or_broken_cache_is_empty(tmp_path):
    assert load_cache(tmp_path / "none.json") == {}
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert load_cache(tmp_path / "bad.json") == {}
