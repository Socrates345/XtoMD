import traceback

import pytest

from xmd.core.config import Language, load_config, read_group_levels, read_language

X_KEY_YAML = "x:\n  api_key: test-key\n"


def _setup(tmp_path, yaml_text=X_KEY_YAML, x_md="dave\n"):
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "sources" / "x.md").write_text(x_md, encoding="utf-8")
    path = tmp_path / "sources.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


def test_basic_handle_parsing(tmp_path):
    path = _setup(tmp_path, x_md="@dave\n# a comment\n\nerin | Erin Example\n")
    cfg = load_config(path)
    by_name = {s.name: s for s in cfg.sources}

    assert len(cfg.sources) == 2
    assert (by_name["@dave"].type, by_name["@dave"].handle) == ("twitterapi", "dave")
    assert by_name["Erin Example"].handle == "erin"


X_MD_WITH_GROUPS = """\
dave | David   # above any header -> default group

## Finance
focus: interest rates, bond markets
NewsFrank | Frank Wire
gina_alerts

## science
aim: research talks
erin
"""


def test_groups_and_legacy_focus_lines_are_skipped(tmp_path):
    path = _setup(tmp_path, x_md=X_MD_WITH_GROUPS)
    cfg = load_config(path)

    by_handle = {s.handle: s for s in cfg.sources}
    assert by_handle["dave"].group == ""  # pre-header -> default
    assert by_handle["dave"].name == "David"  # inline comment stripped
    assert by_handle["NewsFrank"].group == "finance"  # header lowercased
    assert by_handle["gina_alerts"].group == "finance"
    assert by_handle["erin"].group == "science"
    # legacy focus:/aim: lines (from the old LLM-recap era) must not be
    # misparsed as bogus handles
    assert {s.handle for s in cfg.sources} == {"dave", "NewsFrank", "gina_alerts", "erin"}


def test_priority_field_marks_source_priority(tmp_path):
    path = _setup(
        tmp_path,
        x_md="NewsFrank | Frank Wire | priority\ngina_alerts | | priority\ndave\n",
    )
    cfg = load_config(path)
    by_handle = {s.handle: s for s in cfg.sources}
    assert by_handle["NewsFrank"].priority is True
    assert by_handle["gina_alerts"].priority is True
    assert by_handle["dave"].priority is False


def test_what_a_handle_says_is_written_variable_value_in_any_order(tmp_path):
    x_md = (
        "NewsFrank | name:Frank Wire | priority:high\n"
        "jumper | only:airdrop\n"
        "bob | hide: giveaway, presale | name: Bob\n"
        "carol | reposts:off | Priority : High\n"
        "dave | priority:normal | reposts:on\n"
    )
    by_handle = {s.handle: s for s in load_config(_setup(tmp_path, x_md=x_md)).sources}

    assert (by_handle["NewsFrank"].name, by_handle["NewsFrank"].priority) == ("Frank Wire", True)
    assert (by_handle["jumper"].name, by_handle["jumper"].only) == ("@jumper", ("airdrop",))  # no name needed
    assert (by_handle["bob"].name, by_handle["bob"].hide) == ("Bob", ("giveaway", "presale"))  # the name comes last
    carol = by_handle["carol"]
    assert (carol.reposts, carol.priority) == (False, True)  # spaces and capitals change nothing
    dave = by_handle["dave"]
    assert (dave.priority, dave.reposts, dave.only, dave.hide) == (False, True, (), ())  # the defaults, written out


def test_what_a_group_says_is_written_variable_value_too(tmp_path):
    x_md = (
        "## business | level:high\nfoo\n"
        "## chat | recap:on | level:low\nbar\n"
        "## news | level:low | recap:off\nbaz\n"
        "## rest | level:medium\nqux\n"
    )
    cfg = load_config(_setup(tmp_path, x_md=x_md))
    assert cfg.group_levels == {"business": "high", "news": "low"}  # a recap group has no level, medium is normal
    assert cfg.recap_groups == frozenset({"chat"})


def test_a_list_written_before_variable_value_still_loads(tmp_path):
    """Bare words in the fields, and the name as the first field that is no option: every list until now."""
    x_md = (
        "## Business | high | rp_off\n"
        "NewsFrank | Frank Wire | priority\n"
        "carol | rp_off | Carol | priority\n"
        "## Chatter | recap | low\n"
        "dave | Crypto: The News\n"
    )
    cfg = load_config(_setup(tmp_path, x_md=x_md))
    by_handle = {s.handle: s for s in cfg.sources}

    frank, carol = by_handle["NewsFrank"], by_handle["carol"]
    assert (frank.name, frank.priority, frank.reposts) == ("Frank Wire", True, False)
    assert (carol.name, carol.priority, carol.reposts) == ("Carol", True, False)
    assert by_handle["dave"].name == "Crypto: The News"  # a colon in a name does not make it a variable
    assert (cfg.group_levels, cfg.recap_groups) == ({"business": "high"}, frozenset({"chatter"}))


@pytest.mark.parametrize("quoted", ['"#ad"', "“#ad”"])  # straight quotes, and a phone keyboard's curly ones
def test_a_quoted_keyword_keeps_its_hash_and_its_comma(tmp_path, quoted):
    """Unquoted, the `#` would start a comment and take the rest of the line with it."""
    x_md = f'bob | hide: {quoted}, "buy, sell" | priority:high  # a real comment\n'
    bob = load_config(_setup(tmp_path, x_md=x_md)).sources[0]
    assert bob.hide == ("#ad", "buy, sell")
    assert bob.priority is True  # what follows the quoted keyword is still read


def test_a_quoted_name_keeps_its_hash_and_its_pipe(tmp_path):
    source = load_config(_setup(tmp_path, x_md='csharp | name:"C# Weekly | News" | priority:high\n')).sources[0]
    assert (source.name, source.priority) == ("C# Weekly | News", True)


def test_a_double_slash_starts_a_comment_wherever_a_hash_does(tmp_path):
    """`//` is the comment to write: a line that opens with `#` is a heading to a markdown reader."""
    x_md = (
        '// a whole line, with a "quote" and a # in it\n'
        "dave | name:David  // after a handle\n"
        "## crypto | level:high | reposts:off  // after a group's options\n"
        'bob | hide: "http://", scam  // quoted, the keyword keeps its slashes\n'
        "## news // after a group's name\n"
        "erin\n"
    )
    cfg = load_config(_setup(tmp_path, x_md=x_md))
    by_handle = {s.handle: s for s in cfg.sources}

    assert list(by_handle) == ["dave", "bob", "erin"]  # the comment line is no handle
    assert by_handle["dave"].name == "David"
    assert (by_handle["bob"].group, by_handle["bob"].reposts, by_handle["bob"].hide) == ("crypto", False, ("http://", "scam"))
    assert by_handle["erin"].group == "news"
    assert cfg.group_levels == {"crypto": "high"}


def test_a_list_commented_with_hashes_still_loads_as_it_did(tmp_path):
    x_md = "# a whole line\ndave | David  # after a handle\n## C# devs\nerin\n"
    by_handle = {s.handle: s for s in load_config(_setup(tmp_path, x_md=x_md)).sources}

    assert list(by_handle) == ["dave", "erin"]
    assert by_handle["dave"].name == "David"
    assert by_handle["erin"].group == "c# devs"  # a group's name keeps its #


def test_a_group_s_filters_hold_for_each_of_its_handles(tmp_path):
    x_md = (
        "alice\n"
        "## crypto | level:high | reposts:off | hide:presale  // a comment after the options\n"
        "bob\n"
        "dave | hide:giveaway | only:airdrop\n"
        "frank | reposts:on\n"
        "## news\n"
        "erin | reposts:off\n"
    )
    cfg = load_config(_setup(tmp_path, x_md=x_md))
    by_handle = {s.handle: s for s in cfg.sources}

    assert (by_handle["bob"].reposts, by_handle["bob"].hide) == (False, ("presale",))
    dave = by_handle["dave"]
    assert (dave.reposts, dave.hide, dave.only) == (False, ("presale", "giveaway"), ("airdrop",))  # keywords add up
    assert by_handle["frank"].reposts is True  # a handle that says otherwise wins over its group
    assert (by_handle["alice"].reposts, by_handle["alice"].hide) == (True, ())  # above the header
    assert (by_handle["erin"].reposts, by_handle["erin"].hide) == (False, ())  # the next group starts clean
    assert cfg.group_levels == {"crypto": "high"}  # the level is still read next to them


def test_a_field_that_is_no_variable_is_warned_about_not_silently_ignored(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="xmd"):
        cfg = load_config(_setup(tmp_path, x_md="carol | name:Carol | priorty:high\ndave | name:David | priority:high\n"))
    carol = cfg.sources[0]
    assert (carol.name, carol.priority) == ("Carol", False)
    assert "unknown option 'priorty:high' on @carol is ignored (use name:, priority:, reposts:, only:, hide:)" in caplog.text
    assert "dave" not in caplog.text  # and a correct line says nothing


def test_a_value_a_variable_does_not_take_is_warned_about_and_changes_nothing(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="xmd"):
        cfg = load_config(_setup(tmp_path, x_md="## crypto | level:hgih\ncarol | reposts:no | priority:low\n"))
    carol = cfg.sources[0]
    assert (carol.reposts, carol.priority, cfg.group_levels) == (True, False, {})
    assert "unknown value 'hgih' for `level:` on group 'crypto' is ignored (use high, low, normal, medium, regular)" in caplog.text
    assert "unknown value 'no' for `reposts:` on @carol is ignored (use on, off)" in caplog.text
    assert "unknown value 'low' for `priority:` on @carol is ignored (use high, normal)" in caplog.text


@pytest.mark.parametrize("line", ["jumper | only:", "jumper | only: #airdrop"])  # the second: an unquoted # is a comment
def test_a_filter_that_names_no_keyword_is_warned_about_and_keeps_everything(tmp_path, caplog, line):
    with caplog.at_level("WARNING", logger="xmd"):
        jumper = load_config(_setup(tmp_path, x_md=line + "\n")).sources[0]
    assert jumper.only == ()  # not "keep nothing"
    assert "`only:` on @jumper names no keyword" in caplog.text


def test_x_backend_defaults_to_twitterapi(tmp_path):
    path = _setup(tmp_path)
    assert load_config(path).x_backend == "twitterapi"


def test_x_backend_tweetapi_uses_its_own_key(tmp_path):
    path = _setup(tmp_path, yaml_text="x:\n  backend: tweetapi\n  tweetapi_api_key: tw-secret\n")
    cfg = load_config(path)
    assert cfg.x_backend == "tweetapi"
    assert cfg.tweetapi_api_key == "tw-secret"
    assert cfg.active_x_api_key == "tw-secret"


def test_x_backend_tweetapi_requires_its_own_key_not_twitterapis(tmp_path):
    path = _setup(tmp_path, yaml_text="x:\n  backend: tweetapi\n  api_key: irrelevant\n")
    with pytest.raises(ValueError, match="tweetapi_api_key"):
        load_config(path)


def test_invalid_x_backend_rejected(tmp_path):
    path = _setup(tmp_path, yaml_text="x:\n  backend: bogus\n  api_key: k\n")
    with pytest.raises(ValueError, match="x.backend"):
        load_config(path)


def test_api_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XMD_TWITTERAPI_KEY", "env-secret")
    path = _setup(tmp_path, yaml_text="")
    assert load_config(path).x_api_key == "env-secret"


def test_tweetapi_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XMD_TWEETAPI_KEY", "env-secret")
    path = _setup(tmp_path, yaml_text="x:\n  backend: tweetapi\n")
    assert load_config(path).tweetapi_api_key == "env-secret"


def test_tweetapi_rate_limit_defaults_to_10(tmp_path):
    path = _setup(tmp_path, yaml_text="x:\n  backend: tweetapi\n  tweetapi_api_key: tw-secret\n")
    assert load_config(path).tweetapi_rate_limit_per_minute == 10


def test_tweetapi_rate_limit_from_yaml(tmp_path):
    path = _setup(
        tmp_path,
        yaml_text="x:\n  backend: tweetapi\n  tweetapi_api_key: tw-secret\n"
        "  tweetapi_rate_limit_per_minute: 60\n",
    )
    assert load_config(path).tweetapi_rate_limit_per_minute == 60


def test_timezone_defaults_to_paris(tmp_path):
    assert load_config(_setup(tmp_path)).tz.key == "Europe/Paris"


def test_timezone_from_yaml(tmp_path):
    path = _setup(tmp_path, yaml_text=X_KEY_YAML + "timezone: America/New_York\n")
    assert load_config(path).tz.key == "America/New_York"


def test_unknown_timezone_is_a_config_error(tmp_path):
    path = _setup(tmp_path, yaml_text=X_KEY_YAML + "timezone: Paris\n")
    with pytest.raises(ValueError, match="timezone"):
        load_config(path)


def test_missing_api_key_raises(tmp_path):
    path = _setup(tmp_path, yaml_text="")
    with pytest.raises(ValueError, match="api_key"):
        load_config(path)


def test_missing_config_file():
    with pytest.raises(FileNotFoundError):
        load_config("does-not-exist.yaml")


def test_a_yaml_syntax_error_names_the_line_and_never_quotes_the_file(tmp_path):
    """PyYAML's own message quotes the lines around the mistake, and the key line is often one of them."""
    path = _setup(tmp_path, yaml_text='x:\n  backend: [twitterapi\n  api_key: "sekret-value"\n')
    with pytest.raises(ValueError, match=r"not valid YAML \(line \d+\)") as caught:
        load_config(path)
    everything_a_traceback_would_print = "".join(traceback.format_exception(caught.value))
    assert "sekret-value" not in everything_a_traceback_would_print  # neither the message nor a chained cause


@pytest.mark.parametrize("yaml_text, needle", [
    ("- a\n- b\n", "mapping"),
    ("just a string\n", "mapping"),
    ("x: hello\n", "`x:`"),
    ("x:\n  api_key: k\nfetch: 3\n", "`fetch:`"),
    ("x:\n  api_key: k\nfilters:\n  - a\n", "`filters:`"),
    ("x:\n  api_key: k\ndigest: yes\n", "`digest:`"),
])
def test_a_config_that_is_not_settings_gives_a_clean_error_not_a_traceback(tmp_path, yaml_text, needle):
    path = _setup(tmp_path, yaml_text=yaml_text)
    with pytest.raises(ValueError, match="sources.yaml") as caught:
        load_config(path)
    assert needle in str(caught.value)


def test_an_empty_config_or_an_empty_section_is_still_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("XMD_TWITTERAPI_KEY", "env-secret")
    for yaml_text in ("", "x:\nfetch:\nfilters:\ndigest:\n"):
        assert load_config(_setup(tmp_path, yaml_text=yaml_text)).x_api_key == "env-secret"


def test_a_path_setting_left_empty_means_its_default(tmp_path):
    cfg = load_config(_setup(tmp_path, yaml_text=X_KEY_YAML + "storage:\ndigest_dir: ''\nsources_dir:  # sources\n"))
    assert (str(cfg.storage), str(cfg.digest_dir)) == ("xmd.db", "digests")
    assert cfg.sources[0].handle == "dave"  # read from the default sources/


def test_a_path_setting_that_is_not_text_is_a_config_error(tmp_path):
    with pytest.raises(ValueError, match="`storage:` must be a path, not a list"):
        load_config(_setup(tmp_path, yaml_text=X_KEY_YAML + "storage:\n  - xmd.db\n"))


def test_no_sources_gives_actionable_error(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "x.md").write_text("", encoding="utf-8")
    path = tmp_path / "sources.yaml"
    path.write_text(X_KEY_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match=r"x\.md"):
        load_config(path)


def test_missing_x_md_gives_actionable_error(tmp_path):
    (tmp_path / "sources").mkdir()
    path = tmp_path / "sources.yaml"
    path.write_text(X_KEY_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match=r"x\.md"):
        load_config(path)


def test_duplicate_handles_collapsed(tmp_path):
    path = _setup(tmp_path, x_md="dave\ndave\n")
    assert len(load_config(path).sources) == 1


def test_custom_sources_dir(tmp_path):
    (tmp_path / "mylists").mkdir()
    (tmp_path / "mylists" / "x.md").write_text("dave\n", encoding="utf-8")
    path = tmp_path / "sources.yaml"
    path.write_text("sources_dir: mylists\nx:\n  api_key: test-key\n", encoding="utf-8")
    assert len(load_config(path).sources) == 1


def test_fetch_defaults(tmp_path):
    path = _setup(tmp_path)
    cfg = load_config(path)
    assert cfg.max_per_source == 15
    assert cfg.x_max_age_hours == 48
    assert str(cfg.storage) == "xmd.db"
    assert str(cfg.digest_dir) == "digests"


def test_fetch_settings_from_yaml(tmp_path):
    path = _setup(tmp_path, yaml_text=X_KEY_YAML + "fetch:\n  max_per_source: 5\n  max_age_hours: 12\n")
    cfg = load_config(path)
    assert cfg.max_per_source == 5
    assert cfg.x_max_age_hours == 12


def test_filter_defaults(tmp_path):
    path = _setup(tmp_path)
    cfg = load_config(path)
    assert cfg.drop_retweets is False  # retweets included, per this tool's brief
    assert cfg.drop_replies is True
    assert cfg.block_keywords == []


def test_filter_settings_from_yaml(tmp_path):
    path = _setup(tmp_path, yaml_text=X_KEY_YAML + 'filters:\n  drop_retweets: true\n  block_keywords: ["ad"]\n')
    cfg = load_config(path)
    assert cfg.drop_retweets is True
    assert cfg.block_keywords == ["ad"]


def test_recap_option_on_a_group_header_marks_that_group_only(tmp_path):
    path = _setup(
        tmp_path,
        x_md="dave\n## Chatter | recap\nerin\n## finance\nNewsFrank | Frank | priority\n",
    )
    cfg = load_config(path)
    assert cfg.recap_groups == frozenset({"chatter"})  # lowercased like the group itself
    by_handle = {s.handle: s for s in cfg.sources}
    assert by_handle["erin"].group == "chatter"  # the option doesn't leak into the name
    assert by_handle["NewsFrank"].group == "finance"  # the next header resets it


def test_recap_option_is_case_and_space_tolerant(tmp_path):
    path = _setup(tmp_path, x_md="##  loud but fun  |  RECAP \nerin\n")
    assert load_config(path).recap_groups == frozenset({"loud but fun"})


def test_no_recap_groups_by_default(tmp_path):
    assert load_config(_setup(tmp_path, x_md=X_MD_WITH_GROUPS)).recap_groups == frozenset()


def test_digest_defaults(tmp_path):
    cfg = load_config(_setup(tmp_path))
    assert (cfg.snippet_chars, cfg.retweet_chars, cfg.media_chars) == (140, 100, 500)
    assert cfg.trending_similarity == 0.4


def test_digest_settings_from_yaml(tmp_path):
    yaml_text = X_KEY_YAML + "digest:\n  snippet_chars: 90\n  retweet_chars: 60\n  media_chars: 300\n  trending_similarity: 0.55\n"
    cfg = load_config(_setup(tmp_path, yaml_text=yaml_text))
    assert (cfg.snippet_chars, cfg.retweet_chars, cfg.media_chars) == (90, 60, 300)
    assert cfg.trending_similarity == 0.55


@pytest.mark.parametrize("value", ["0", "1.5", "-0.2"])
def test_trending_similarity_must_be_a_fraction(tmp_path, value):
    path = _setup(tmp_path, yaml_text=X_KEY_YAML + f"digest:\n  trending_similarity: {value}\n")
    with pytest.raises(ValueError, match="trending_similarity"):
        load_config(path)


def test_a_group_header_can_carry_a_level_and_only_high_and_low_are_recorded(tmp_path):
    x_md = "dave\n## Business | high\nerin\n## News | LOW\nbob\n## finance\nalice\n## Chatter | recap\ncarol\n"
    cfg = load_config(_setup(tmp_path, x_md=x_md))
    assert cfg.group_levels == {"business": "high", "news": "low"}  # finance is normal; chatter is recap
    assert cfg.recap_groups == frozenset({"chatter"})


def test_a_recap_group_has_no_level_and_the_first_level_named_wins(tmp_path):
    x_md = "## chat | recap | low\nana\n## both | low | high\nbo\n"  # the header the user actually wrote
    cfg = load_config(_setup(tmp_path, x_md=x_md))
    assert cfg.group_levels == {"both": "low"} and cfg.recap_groups == frozenset({"chat"})


def test_no_levels_by_default(tmp_path):
    assert load_config(_setup(tmp_path, x_md=X_MD_WITH_GROUPS)).group_levels == {}


def test_read_group_levels_needs_only_the_x_md_and_no_api_key(tmp_path):
    x_md = tmp_path / "x.md"
    x_md.write_text("## Business | high\nfoo | Foo | priority\n## Loud but fun | recap\nbar\n## news | low\nbaz\n", encoding="utf-8")
    assert read_group_levels(x_md) == {"business": "high", "news": "low"}  # the recap group names no level


def test_the_middle_level_can_be_written_normal_medium_or_regular_and_is_the_default(tmp_path):
    x_md = "## a | normal\nx\n## b | Medium\ny\n## c | regular\nz\n## d | high\nw\n"
    cfg = load_config(_setup(tmp_path, x_md=x_md))
    assert cfg.group_levels == {"d": "high"}  # a, b and c are normal, so nothing to record


def test_the_first_level_named_wins_even_when_it_is_the_middle_one(tmp_path):
    cfg = load_config(_setup(tmp_path, x_md="## a | medium | high\nx\n## b | high | medium\ny\n"))
    assert cfg.group_levels == {"b": "high"}


def test_an_unknown_option_on_a_group_header_is_warned_about_not_silently_ignored(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="xmd"):
        cfg = load_config(_setup(tmp_path, x_md="## business | hgih\nfoo\n## news | low\nbar\n"))
    assert cfg.group_levels == {"news": "low"}  # the typo did not become a level
    assert "unknown option 'hgih' on group 'business'" in caplog.text
    assert "news" not in caplog.text  # and a correct header says nothing


def test_no_language_block_leaves_every_post_in_its_own_language(tmp_path):
    assert load_config(_setup(tmp_path)).language is None


def test_language_takes_codes_or_english_names(tmp_path):
    path = _setup(tmp_path, X_KEY_YAML + "language:\n  accepted: [English, FR, de]\n  translate_to: en\n")
    lang = load_config(path).language
    assert (lang.accepted, lang.translate_to) == (("en", "fr", "de"), "en")
    assert lang.accepted_names == "English, French, German"
    assert read_language(path) == lang  # the model step reads it without the api key


def test_language_with_one_half_missing_fills_it_from_the_other(tmp_path):
    only_target = _setup(tmp_path, X_KEY_YAML + "language:\n  translate_to: english\n")
    assert load_config(only_target).language == Language(("en",), "en")
    only_accepted = _setup(tmp_path, X_KEY_YAML + "language:\n  accepted: [fr, en]\n")
    assert load_config(only_accepted).language == Language(("fr", "en"), "fr")


@pytest.mark.parametrize("block, message", [
    ("  accepted: [en]\n  translate_to: de\n", "must be one of language.accepted"),
    ("  accepted: [klingon]\n", "unknown language 'klingon'"),
    ("  accepted: en\n  translate_to: xx\n", "unknown language 'xx'"),
    ("  accepted: {en: 1}\n", "must be a list"),
])
def test_a_wrong_language_setting_is_named(tmp_path, block, message):
    with pytest.raises(ValueError, match=message):
        load_config(_setup(tmp_path, X_KEY_YAML + "language:\n" + block))


def test_read_language_without_a_settings_file_is_none(tmp_path):
    assert read_language(tmp_path / "missing.yaml") is None
