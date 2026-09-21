import pytest

from xmd.summary.chunk import BAND
from xmd.summary.prompt import BRIEF_SCHEMA, DEFAULT_PROMPT, bounded_schema, load_prompt

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


def test_the_output_example_shows_several_items_because_a_weak_model_copies_its_example():
    assert load_prompt("regular").count('{"headline"') >= 3
