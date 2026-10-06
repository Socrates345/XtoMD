import pytest

from xmd.summary.chunk import BAND
from xmd.summary.prompt import BRIEF_SCHEMA, DEFAULT_PROMPT, SECTION_SCHEMA, bounded_schema, load_prompt
from xmd.summary.translate import DETECT_SCHEMA, TRANSLATE_SCHEMA

TINY = """---
name: t
description: d
---

shared rules

## Band: alpha

about alpha

## Band: beta

about beta
"""


def test_a_prompt_is_the_shared_rules_plus_only_its_band(tmp_path):
    path = tmp_path / "p.md"
    path.write_text(TINY, encoding="utf-8")
    prompt = load_prompt("beta", path)
    assert prompt == "shared rules\n\n## This chunk\n\nBand: beta. about beta\n"
    assert "alpha" not in prompt and "name: t" not in prompt  # no other band, no front matter


def test_an_unknown_band_names_the_ones_there_are(tmp_path):
    path = tmp_path / "p.md"
    path.write_text(TINY, encoding="utf-8")
    with pytest.raises(ValueError, match="alpha, beta"):
        load_prompt("gamma", path)


def test_the_real_prompt_has_a_section_for_every_band_the_export_writes():
    for band in BAND.values():  # regular, retweets, recap
        prompt = load_prompt(band)
        assert f"Band: {band}." in prompt
        assert "## Band:" not in prompt  # the other bands' sections are left out
        assert not prompt.startswith("---")


def test_the_real_prompt_documents_every_key_of_the_schema():
    prompt = load_prompt("regular")
    for key in BRIEF_SCHEMA["properties"]["items"]["items"]["properties"]:
        assert f"`{key}`" in prompt
    assert DEFAULT_PROMPT.name == "brief.md"


def test_the_bounded_schema_limits_the_items_and_leaves_the_original_alone():
    schema = bounded_schema(7, 28)
    assert schema["properties"]["items"]["minItems"] == 7 and schema["properties"]["items"]["maxItems"] == 28
    assert "minItems" not in BRIEF_SCHEMA["properties"]["items"]
    assert schema["properties"]["items"]["items"] == BRIEF_SCHEMA["properties"]["items"]["items"]


def _objects(schema: dict):
    """Every object in a schema, the one inside an array included."""
    if schema.get("type") == "object":
        yield schema
        for child in schema["properties"].values():
            yield from _objects(child)
    elif schema.get("type") == "array":
        yield from _objects(schema["items"])


@pytest.mark.parametrize("schema", [
    SECTION_SCHEMA, BRIEF_SCHEMA, bounded_schema(1, 5), DETECT_SCHEMA, TRANSLATE_SCHEMA,
], ids=["section", "brief", "bounded brief", "detect", "translate"])
def test_every_object_in_a_schema_takes_all_its_keys_and_no_other_as_a_hosted_strict_mode_demands(schema):
    found = list(_objects(schema))
    assert found
    for obj in found:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_the_output_example_shows_several_items_because_a_weak_model_copies_its_example():
    assert load_prompt("regular").count('{"headline"') >= 3


def test_a_language_setting_replaces_the_language_line_of_both_prompts():
    from xmd.core.config import Language
    from xmd.summary.prompt import load_section_prompt
    lang = Language(("en", "fr"), "en")
    for prompt in (load_prompt("regular", language=lang), load_section_prompt(language=lang)):
        assert "the reader reads English, French" in prompt and "write in English" in prompt.replace("write it in", "write in")
        assert "language of the posts." not in prompt and "language of the bullets." not in prompt
    assert "- Language: write in the language of the posts." in load_prompt("regular")  # none set: as before


def test_a_prompt_without_a_language_line_cannot_take_a_language_setting(tmp_path):
    from xmd.core.config import Language
    path = tmp_path / "p.md"
    path.write_text(TINY, encoding="utf-8")
    with pytest.raises(ValueError, match="no `- Language:` line"):
        load_prompt("beta", path, language=Language(("en",), "en"))


def test_the_translate_prompt_names_the_target_language():
    from xmd.summary.prompt import load_detect_prompt, load_translate_prompt
    assert "into German" in load_translate_prompt("German") and "{target}" not in load_translate_prompt("German")
    assert "ISO 639-1" in load_detect_prompt() and "name:" not in load_detect_prompt()
